"""End-to-end gallery scenarios over real loopback HTTP."""

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hypothesis import example, given, settings, strategies as st

from artwall.config import config_path, data_dir, database_path, load_config
from artwall.gui import GuiContext, create_server
from artwall.systemd import user_unit_dir
from tests.support import HomeTest

DISLIKE_CURRENT = {"id": "current", "kind": "dislike", "current_id": "current"}


class GalleryTests(HomeTest):
    config = ('[display]\nwidth = 860\nheight = 360\n[rotation]\ninterval_minutes = 120 # Keep this note\n'
              '[llm]\nmodel = "qwen3.6:27b" # Local model\n')

    def setUp(self):
        super().setUp()
        self.art = self.artwork("10", title="The Lighthouse", artist="A Painter", date_display="c. 1820")
        with self.database() as db:
            for identifier in ("current", "queued"):
                self.queue(db, identifier, self.art, description="A lighthouse rises above the sea.")
            db.mark_shown("current")
        context = GuiContext(database_path(), data_dir(), config_path(), user_unit_dir())
        self.base = self.serve(create_server(context, 0))
        self.token = ""
        self.token = self.state()["csrf_token"]

    def call(self, path: str, payload: Any = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        request = Request(self.base + path, headers={"X-Artwall-Token": self.token, **(headers or {})})
        if payload is not None:
            request.data = json.dumps(payload).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def state(self) -> dict[str, Any]:
        status, body = self.call("/api/state")
        self.assertEqual(status, 200)
        return json.loads(body)

    def post(self, path: str, payload: dict[str, Any]) -> str:
        status, body = self.call(path, payload)
        self.assertEqual(status, 200, body)
        return json.loads(body)["notice"]

    def score(self) -> int:
        with self.database() as db:
            return db.get_artist_preference("A Painter").score

    @settings(max_examples=12, database=None, deadline=None, derandomize=True)
    @given(interval=st.integers(min_value=0, max_value=1500),
           model=st.text(alphabet='abcXYZ0123:#"\\ ', min_size=1, max_size=20).filter(str.strip))
    @example(interval=1, model="local")
    @example(interval=180, model="llava:latest")
    def test_settings_edits_preserve_comments_and_update_the_timer(self, interval: int, model: str):
        timer = user_unit_dir() / "artwall-rotate.timer"
        if not timer.exists():
            self.cli("install-timers", "--no-enable")
        before = self.config_file.read_text()
        status, _ = self.call("/api/settings", {"interval_minutes": interval, "model": model, "max_artworks": 2})
        if not 15 <= interval <= 1440:
            self.assertEqual((status, self.config_file.read_text()), (400, before))
            return
        self.assertEqual(status, 200)
        saved = self.config_file.read_text()
        self.assertIn(f"interval_minutes = {interval} # Keep this note", saved)
        self.assertIn(f"model = {json.dumps(model.strip())} # Local model", saved)
        configured = load_config()
        self.assertEqual((configured.interval_minutes, configured.model, configured.max_artworks),
                         (interval, model.strip(), 2))
        self.assertIn(f"OnUnitActiveSec={interval}min", timer.read_text())

    def test_artwork_and_artist_blacklists_filter_independently(self):
        with self.database() as db:
            self.queue(db, "same-artist", self.artwork("11", artist="A Painter"))
            self.queue(db, "other-artist", self.artwork("12", artist="Another Painter"))
        queued = lambda: [item["id"] for item in self.state()["queued"]]
        self.assertEqual(queued(), ["queued", "same-artist", "other-artist"])
        self.post("/api/blacklist/artwork", {"id": self.art.id, "blocked": True})
        self.assertEqual(queued(), ["same-artist", "other-artist"])
        self.post("/api/blacklist/artwork", {"id": self.art.id, "blocked": False})
        self.post("/api/blacklist/artist", {"artist": "A Painter", "blocked": True})
        self.assertEqual(queued(), ["other-artist"])
        self.post("/api/blacklist/artist", {"artist": "A Painter", "blocked": False})
        self.assertEqual(queued(), ["queued", "same-artist", "other-artist"])

    def test_disliking_current_retries_a_failed_update_then_moves_one_work(self):
        with self.database() as db:
            alternate = self.queue(db, "alternate", self.artwork("99", title="A Different Work"))
            self.queue(db, "later", self.artwork("100", title="Another Work"))
        self.desktop_down.touch()
        with self.assertLogs("artwall", level="WARNING"):
            notice = self.post("/api/feedback", DISLIKE_CURRENT)
        self.assertIn("could not change", notice)
        self.assertEqual(self.state()["current"]["id"], "current")
        self.assertEqual([item["id"] for item in self.state()["queued"]], ["alternate", "later"])
        self.desktop_down.unlink()
        self.assertIn("Now showing A Different Work", self.post("/api/feedback", DISLIKE_CURRENT))
        self.assertIn("already changed", self.post("/api/feedback", DISLIKE_CURRENT))
        self.assertEqual(self.state()["current"]["id"], "alternate")
        self.assertEqual([item["id"] for item in self.state()["queued"]], ["later"])
        self.assertEqual([line.split()[-1] for line in self.commands()], [alternate.output_path] * 2)
        self.assertEqual(self.score(), -1)

    def test_rejects_cross_origin_requests_missing_tokens_and_path_traversal(self):
        comment = {"id": "current", "comment": "evil"}
        self.assertEqual(self.call("/api/comment", comment, {"X-Artwall-Token": ""})[0], 403)
        self.assertEqual(self.call("/api/comment", comment, {"Origin": "https://elsewhere.example"})[0], 403)
        self.assertEqual(self.call("/image/not-a-composition")[0], 404)
        self.assertEqual(self.call("/image/..%2F..%2Fetc%2Fpasswd")[0], 404)
        self.assertEqual(self.state()["current"]["comment"], "")
