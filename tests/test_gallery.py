"""Gallery scenarios through the toolkit-independent service the native app uses."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import patch

from hypothesis import example, given, settings, strategies as st

from artwall.gallery import Gallery, Preparation, Record
from artwall.models import CuratorDecision, DecisionArtwork, Whole
from artwall.systemd import user_unit_dir
from tests.support import HomeTest


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
        self.gallery = Gallery()

    def current(self) -> Record:
        current = self.gallery.state().current
        assert current is not None
        return current

    def queued(self) -> list[str]:
        return [record.id for record in self.gallery.state().queued]

    def score(self) -> int:
        with self.database() as db:
            return db.get_artist_preference("A Painter").score

    def wait_for_preparation(self) -> Preparation:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            preparation = self.gallery.preparation()
            if preparation.status != "running":
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
        self.assertIn("Preparing", self.gallery.start_preparation())
        self.assertIn("Already preparing", self.gallery.start_preparation())
        preparation = self.wait_for_preparation()
        self.assertEqual((preparation.status, preparation.prepared), ("ready", 3), preparation)
        current = self.current()
        self.assertEqual((current.id, current.comment), ("current", "Keep the original paper margins."))
        self.assertTrue(current.reaction.includes("favorite"))
        self.assertEqual(len(self.queued()), 4)
        self.assertEqual(self.queued()[-1], "queued")
        with self.database() as db:
            new = db.list_compositions(only_unshown=True)[:3]
            self.assertTrue(all(len(c.artwork_ids) == 2 for c in new))
        self.assertEqual(self.commands(), [])  # Preparation never changes the desktop.
        for composition in new:
            self.cli("next", "--no-apply")
            self.assertEqual(self.current().id, composition.id)
        self.cli("next", "--no-apply")
        self.assertEqual(self.current().id, "queued")

    def test_regenerating_replaces_upcoming_wallpapers_with_different_works(self):
        self.config_file.write_text(self.config.replace("[llm]\n", "[llm]\nenabled = false\n")
                                    + "\n[curation]\nqueue_size = 2\n")
        with self.database() as db:
            for index in range(4):
                db.upsert_artwork(self.artwork(f"fresh-{index}", (1600, 1200), title=f"Fresh {index}"))
            # The queued work is cached too, so only the avoid list keeps it out.
            db.upsert_artwork(self.artwork("10", (1600, 1200), title="The Lighthouse", artist="A Painter"))
            old_image = Path(db.get_composition("queued").output_path)
        with self.assertRaises(ValueError):
            self.gallery.regenerate(["current"])  # Shown compositions stay in history.
        self.assertIn("Preparing", self.gallery.regenerate(["queued"]))
        self.assertEqual(self.wait_for_preparation().status, "ready")
        self.assertFalse(old_image.exists())
        (replacement,) = self.gallery.state().queued
        self.assertNotIn(self.art.id, [work.id for work in replacement.works])
        self.assertTrue(replacement.image_path.is_file())
        # Regenerating everything refills the queue to its size.
        self.gallery.regenerate()
        self.assertEqual(self.wait_for_preparation().prepared, 2)
        self.assertEqual(len(self.queued()), 2)
        self.assertNotIn(replacement.id, self.queued())
        self.assertEqual(self.current().id, "current")

    def test_failed_preparation_keeps_the_existing_queue_available(self):
        self.config_file.write_text(self.config.replace("[llm]\n", "[llm]\nenabled = false\n"))
        self.gallery.start_preparation()
        preparation = self.wait_for_preparation()
        self.assertEqual((preparation.status, preparation.prepared), ("error", 0), preparation)
        self.assertEqual(self.queued(), ["queued"])
        self.assertEqual(self.commands(), [])

    @settings(max_examples=12, database=None, deadline=None, derandomize=True)
    @given(interval=st.integers(min_value=0, max_value=1500),
           model=st.text(alphabet='abcXYZ0123:#"\\ ', min_size=1, max_size=20).filter(str.strip))
    @example(interval=1, model="local")
    @example(interval=180, model="llava:latest")
    def test_settings_are_saved_in_the_database_and_update_the_timer(self, interval: int, model: str):
        timer = user_unit_dir() / "artwall-rotate.timer"
        if not timer.exists():
            self.cli("install-timers", "--no-enable")
        config = self.config_file.read_text()
        before = self.gallery.settings()
        try:
            self.gallery.save_settings(interval_minutes=interval, model=model, max_artworks=2)
        except ValueError:
            self.assertFalse(15 <= interval <= 1440)
            self.assertEqual(self.gallery.settings(), before)
            return
        self.assertEqual(self.config_file.read_text(), config)  # The gallery never rewrites config.toml.
        saved = self.gallery.settings()
        self.assertEqual((saved.interval_minutes, saved.model, saved.max_artworks), (interval, model.strip(), 2))
        self.assertIn(f"OnUnitActiveSec={interval}min", timer.read_text())
        # Commands see the saved values too, over config.toml.
        self.assertIn(f"interval_minutes = {interval}", self.cli("status"))

    def test_artwork_and_artist_exclusions_filter_independently(self):
        with self.database() as db:
            self.queue(db, "same-artist", self.artwork("11", artist="A Painter"))
            self.queue(db, "other-artist", self.artwork("12", artist="Another Painter"))
        self.assertEqual(self.queued(), ["queued", "same-artist", "other-artist"])
        self.gallery.set_artwork_blocked(self.art.id, True)
        self.assertEqual(self.queued(), ["same-artist", "other-artist"])
        self.assertEqual([work.id for work in self.gallery.state().blocked_works], [self.art.id])
        self.gallery.set_artwork_blocked(self.art.id, False)
        # Names match regardless of case, as they do during curation.
        self.gallery.set_artist_blocked("a painter", True)
        self.assertEqual(self.queued(), ["other-artist"])
        self.gallery.set_artist_blocked("a painter", False)
        self.assertEqual(self.queued(), ["queued", "same-artist", "other-artist"])
        for change in (lambda: self.gallery.set_artist_blocked("  ", True),
                       lambda: self.gallery.set_artwork_blocked("artic:missing", True),
                       lambda: self.gallery.vote("missing", "like"),
                       lambda: self.gallery.set_comment("missing", "note")):
            with self.assertRaises(ValueError):
                change()

    def test_disliking_current_retries_a_failed_update_then_moves_one_work(self):
        with self.database() as db:
            alternate = self.queue(db, "alternate", self.artwork("99", title="A Different Work"))
            self.queue(db, "later", self.artwork("100", title="Another Work"))
        self.desktop_down.touch()
        with self.assertLogs("artwall", level="WARNING"):
            notice = self.gallery.vote("current", "dislike", "current")
        self.assertIn("could not change", notice)
        self.assertEqual(self.current().id, "current")
        self.assertEqual(self.queued(), ["alternate", "later"])
        self.desktop_down.unlink()
        self.assertIn("Now showing A Different Work", self.gallery.vote("current", "dislike", "current"))
        self.assertIn("already changed", self.gallery.vote("current", "dislike", "current"))
        self.assertEqual(self.current().id, "alternate")
        self.assertEqual(self.queued(), ["later"])
        self.assertEqual([line.split()[-1] for line in self.commands() if line.startswith("dms ")],
                         [alternate.output_path] * 2)
        self.assertIn("systemctl --user try-restart artwall-rotate.timer", self.commands())
        self.assertEqual(self.score(), -1)

    def test_written_feedback_is_saved_with_context_and_sent_to_future_curation(self):
        comment = "Less empty space, and pair prints with a similar tonal range."
        notice = self.gallery.set_comment("current", "  " + comment + "\n")
        self.assertIn("newly prepared wallpapers", notice)
        self.assertEqual((self.current().comment, self.current().id), (comment, "current"))
        art = self.artwork("future", (1800, 1400), title="A Museum Title", artist="An Etcher",
                           museum_description="A dark hillside slopes down to the water.")
        with self.database() as db:
            db.upsert_artwork(art)
        draft = CuratorDecision(Whole(DecisionArtwork(art.id)),
                                description=art.museum_description)
        with patch("artwall.curation.ModelCurator._chat", return_value=draft.to_dict()) as chat:
            self.cli("curate", "--count", "1")
        prompt = chat.call_args_list[0].args[0]  # The selection request; notes come after rendering.
        feedback = json.loads(prompt.split("\nViewer feedback: ", 1)[1].split("\nCandidates: ", 1)[0])
        self.assertEqual(feedback, [{"layout": "single",
                                     "works": [{"title": "The Lighthouse", "artist": "A Painter"}],
                                     "comment": comment}])
        self.assertIn("cannot change museum facts", prompt)
        self.assertIn("Composition review:", prompt)
        self.assertIn("2.389:1", prompt)
        with self.database() as db:
            self.assertEqual(db.get_artwork(art.id).title, "A Museum Title")
        updated = "Keep the artwork large."
        self.gallery.set_comment("current", updated)
        self.assertEqual(self.current().comment, updated)
        with self.assertRaises(ValueError):
            self.gallery.set_comment("current", "x" * 5001)
        self.assertEqual(self.current().comment, updated)
        self.assertEqual(self.gallery.set_comment("current", " \n "), "Feedback cleared")
        with self.database() as db:
            self.assertEqual(db.curation_feedback(), [])

    def test_openai_compatible_endpoint_curates_and_lists_models(self):
        art = self.artwork("api", (1800, 1400), title="Harbor at Dusk", artist="An Etcher",
                           museum_description="Boats rest in a quiet harbor beneath a low evening sky.")
        with self.database() as db:
            db.upsert_artwork(art)
        answer = CuratorDecision(Whole(DecisionArtwork(art.id)),
                                 description=art.museum_description, curator_reason="Calm harbor.").to_dict()
        requests: list[tuple[str, str | None, dict[str, Any]]] = []

        class FakeApi(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def reply(self, status: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self.reply(200, {"data": [{"id": "vision-b"}, {"id": "vision-a"}]})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), body))
                if "response_format" in body:  # A server without JSON mode, such as some local runners.
                    return self.reply(400, {"error": "response_format is not supported"})
                self.reply(200, {"choices": [{"message": {
                    "content": "<think>Weighing the harbor.</think>" + json.dumps(answer)}}]})

        base = self.serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeApi))
        key_file = self.root / "api_key"
        key_file.write_text("secret-key\n")
        self.config_file.write_text(f'[display]\nwidth = 860\nheight = 360\n[llm]\nprovider = "openai"\n'
                                    f'base_url = "{base}/v1"\nmodel = "vision-a"\napi_key_file = "{key_file}"\n')
        self.cli("curate", "--count", "1")
        path, auth, retried = requests[-1]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer secret-key"))
        self.assertEqual(set(retried), {"model", "messages"})
        selection = requests[0][2]["messages"][0]["content"]
        self.assertEqual(selection[1]["type"], "image_url")
        self.assertTrue(selection[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        with self.database() as db:
            (composition,) = [item for item in db.list_compositions(only_unshown=True) if art.id in item.artwork_ids]
        self.assertNotIn("unavailable or invalid", composition.decision.curator_reason)
        self.assertEqual(self.gallery.models(), ["vision-a", "vision-b"])

    def test_reactions_are_exclusive_and_a_favorite_includes_a_like(self):
        # Each press toggles one vote; a thumbs down also drops the work from the queue.
        steps = [("favorite", "like favorite", 2), ("like", "", 0), ("dislike", "dislike", -1),
                 ("like", "like", 1), ("favorite", "like favorite", 2), ("favorite", "", 0)]
        for kind, shown, score in steps:
            with self.subTest(kind=kind, shown=shown):
                self.gallery.vote("queued", kind)
                with self.database() as db:
                    feedback = db.get_feedback("queued")
                self.assertEqual({flag for flag, on in feedback.items() if on}, set(shown.split()))
                self.assertEqual(self.score(), score)

    def test_long_feedback_fits_a_prompt_budget_newest_first(self):
        with self.database() as db:
            for index in range(8):
                self.queue(db, f"noted-{index}", self.artwork(f"n{index}"))
                db.set_comment(f"noted-{index}", f"Note {index}: " + "x" * 790)
            art = self.artwork("future", (1800, 1400), title="A Museum Title")
            db.upsert_artwork(art)
        draft = CuratorDecision(Whole(DecisionArtwork(art.id)))
        with patch("artwall.curation.ModelCurator._chat", return_value=draft.to_dict()) as chat:
            self.cli("curate", "--count", "1")
        selection = chat.call_args_list[0]
        block = selection.args[0].split("\nViewer feedback: ", 1)[1].split("\nCandidates: ", 1)[0]
        self.assertLessEqual(len(block), 4000)
        self.assertTrue(json.loads(block)[0]["comment"].startswith("Note 7:"))
        self.assertEqual(selection.kwargs["num_ctx"], 16384)
