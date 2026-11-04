"""End-to-end gallery scenarios over real loopback HTTP."""

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from hypothesis import example, given, settings, strategies as st

from artwall.config import config_path, data_dir, database_path, load_config
from artwall.gui import GuiContext, create_server
from artwall.models import CuratorDecision, DecisionArtwork
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

    def wait_for_preparation(self) -> dict[str, Any]:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, body = self.call("/api/preparation")
            self.assertEqual(status, 200)
            preparation = json.loads(body)
            if preparation["status"] != "running":
                return preparation
            time.sleep(0.05)
        self.fail("Gallery preparation did not finish")

    def test_prepare_new_layouts_goes_ahead_of_old_queue_and_preserves_history(self):
        self.config_file.write_text(self.config.replace("[llm]\n", "[llm]\nenabled = false\n")
                                    + "\n[curation]\nmax_artworks = 2\n")
        with self.database() as db:
            for index in range(6):
                db.upsert_artwork(self.artwork(str(100 + index), (1600, 1450),
                                              artist="An Etcher", subject_tags=["mountains"]))
            db.set_comment("current", "Keep the original paper margins.")
            db.set_feedback("current", "favorite")
            # Upgrade an older queue without losing its order or preferences.
            db.connection.execute("DROP INDEX idx_compositions_queue")
            db.connection.execute("ALTER TABLE compositions DROP COLUMN queue_priority")
            db.connection.execute("PRAGMA user_version = 2")
            db.connection.commit()
        self.assertIn("Preparing", self.post("/api/curate", {}))
        preparation = self.wait_for_preparation()
        self.assertEqual((preparation["status"], preparation["prepared"]), ("ready", 3), preparation)
        state = self.state()
        self.assertEqual(state["current"]["id"], "current")
        self.assertEqual(state["current"]["comment"], "Keep the original paper margins.")
        self.assertTrue(state["current"]["feedback"]["favorite"])
        self.assertEqual(len(state["queued"]), 4)
        self.assertEqual(state["queued"][-1]["id"], "queued")
        with self.database() as db:
            new = db.list_compositions(only_unshown=True)[:3]
            self.assertTrue(all(len(c.artwork_ids) == 2 for c in new))
        self.assertEqual(self.commands(), [])  # Preparation never changes the desktop.
        for composition in new:
            self.cli("next", "--no-apply")
            self.assertEqual(self.state()["current"]["id"], composition.id)
        self.cli("next", "--no-apply")
        self.assertEqual(self.state()["current"]["id"], "queued")

    def test_failed_preparation_keeps_the_existing_queue_available(self):
        self.config_file.write_text(self.config.replace("[llm]\n", "[llm]\nenabled = false\n"))
        self.post("/api/curate", {})
        preparation = self.wait_for_preparation()
        self.assertEqual((preparation["status"], preparation["prepared"]), ("error", 0), preparation)
        self.assertEqual([c["id"] for c in self.state()["queued"]], ["queued"])
        self.assertEqual(self.commands(), [])

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
        # Names match regardless of case, as they do during curation.
        self.post("/api/blacklist/artist", {"artist": "a painter", "blocked": True})
        self.assertEqual(queued(), ["other-artist"])
        self.post("/api/blacklist/artist", {"artist": "a painter", "blocked": False})
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
        self.assertEqual(self.call("/api/curate", {}, {"X-Artwall-Token": ""})[0], 403)
        self.assertEqual(self.call("/api/curate", {}, {"Origin": "https://elsewhere.example"})[0], 403)
        self.assertEqual(self.call("/image/not-a-composition")[0], 404)
        self.assertEqual(self.call("/image/..%2F..%2Fetc%2Fpasswd")[0], 404)
        self.assertEqual(self.state()["current"]["comment"], "")

    def test_written_feedback_is_saved_with_context_and_sent_to_future_curation(self):
        comment = "Less empty space, and pair prints with a similar tonal range."
        notice = self.post("/api/comment", {"id": "current", "comment": "  " + comment + "\n"})
        self.assertIn("newly prepared wallpapers", notice)
        self.assertEqual(self.state()["current"]["comment"], comment)
        self.assertEqual(self.state()["current"]["id"], "current")
        art = self.artwork("future", (1800, 1400), title="A Museum Title", artist="An Etcher",
                           museum_description="A dark hillside slopes down to the water.")
        with self.database() as db:
            db.upsert_artwork(art)
        draft = CuratorDecision("near_single", art.id, (DecisionArtwork(art.id),),
                                description=art.museum_description)
        with patch("artwall.curation.OllamaCurator._chat", return_value=draft.to_dict()) as chat:
            self.cli("curate", "--count", "1")
        prompt = chat.call_args.args[0]
        feedback = json.loads(prompt.split("\nViewer feedback: ", 1)[1].split("\nCandidates: ", 1)[0])
        self.assertEqual(feedback, [{"layout": "near_single",
                                     "works": [{"title": "The Lighthouse", "artist": "A Painter"}],
                                     "comment": comment}])
        self.assertIn("cannot change museum facts", prompt)
        self.assertIn("Composition review:", prompt)
        self.assertIn("2.389:1", prompt)
        with self.database() as db:
            self.assertEqual(db.get_artwork(art.id).title, "A Museum Title")
        updated = "Keep the artwork large."
        self.post("/api/comment", {"id": "current", "comment": updated})
        self.assertEqual(self.state()["current"]["comment"], updated)
        self.assertEqual(self.call("/api/comment", {"id": "current", "comment": "x" * 5001})[0], 400)
        self.assertEqual(self.state()["current"]["comment"], updated)
        self.post("/api/comment", {"id": "current", "comment": " \n "})
        with self.database() as db:
            self.assertEqual(db.curation_feedback(), [])

    def test_removing_a_favorite_also_removes_its_like(self):
        favorite = {"id": "queued", "kind": "favorite"}
        self.post("/api/feedback", favorite)
        self.assertEqual(self.state()["queued"][0]["feedback"], {"like": True, "dislike": False, "favorite": True})
        self.assertEqual(self.score(), 2)
        self.post("/api/feedback", favorite)
        self.assertEqual(self.state()["queued"][0]["feedback"], {"like": False, "dislike": False, "favorite": False})
        self.assertEqual(self.score(), 0)

    def test_long_feedback_fits_a_prompt_budget_newest_first(self):
        with self.database() as db:
            for index in range(8):
                self.queue(db, f"noted-{index}", self.artwork(f"n{index}"))
                db.set_comment(f"noted-{index}", f"Note {index}: " + "x" * 790)
            art = self.artwork("future", (1800, 1400), title="A Museum Title")
            db.upsert_artwork(art)
        draft = CuratorDecision("near_single", art.id, (DecisionArtwork(art.id),))
        with patch("artwall.curation.OllamaCurator._chat", return_value=draft.to_dict()) as chat:
            self.cli("curate", "--count", "1")
        selection = chat.call_args_list[0]
        block = selection.args[0].split("\nViewer feedback: ", 1)[1].split("\nCandidates: ", 1)[0]
        self.assertLessEqual(len(block), 4000)
        self.assertTrue(json.loads(block)[0]["comment"].startswith("Note 7:"))
        self.assertEqual(selection.kwargs["num_ctx"], 16384)
