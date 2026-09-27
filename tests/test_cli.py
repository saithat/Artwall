"""End-to-end command scenarios in a temporary home with fake desktop commands and model server."""

import json
from pathlib import Path

from PIL import Image

from tests.support import HomeTest

REPAIRED = ("The dense etched lines surround a large arch, while a small figure near the lower edge gives "
            "the structure a striking sense of scale.")
COAST = ("The museum describes an illuminated horizon over a vast coastal landscape with a tiny figure "
         "standing at the edge of a rocky foreground.")


class CliTests(HomeTest):
    def current_id(self) -> str | None:
        with self.database() as db:
            current = db.get_current()
        return current.id if current else None

    def test_offline_curation_prepares_a_queue_that_rotates_onto_the_desktop(self):
        with self.database() as db:
            for index in range(3):
                db.upsert_artwork(self.artwork(
                    str(index), (1600, 1200), title=f"Arcade {index}", artist="An Etcher", date_display="1800",
                    movement="Romanticism", canonical_url=f"https://www.artic.edu/artworks/{index}",
                    museum_description=f"Arcade {index} opens onto a bright courtyard beyond dense etched shadows.",
                ))
        self.assertEqual(self.cli("curate", "--offline", "--count", "2").count("Queued"), 2)
        self.assertIn("Queued: 2", self.cli("status"))
        self.cli("next")
        with self.database() as db:
            current = db.get_current()
            primary = db.get_artwork(current.decision.primary_artwork_id)
        image = Path(current.output_path).resolve()
        self.assertEqual(self.commands(), [f"dms ipc call wallpaper set {image}",
                                           f"dms ipc call settings set lockScreenWallpaperPath {image}"])
        cache = self.root / "cache" / "artwall"
        self.assertEqual((cache / "current.png").resolve(), image)
        self.assertEqual(json.loads((cache / "current.json").read_text())["id"], current.id)
        info = self.cli("info")
        self.assertIn(primary.museum_description, info)
        self.assertIn(primary.canonical_url, info)
        self.assertIn("Marked current composition: like", self.cli("like"))
        self.assertIn("Queued: 1", self.cli("status"))
        self.assertIn(primary.title, self.cli("history"))
        self.cli("render", "--output", str(self.root / "again.png"))
        for path in (image, self.root / "again.png"):
            with Image.open(path) as rendered:
                self.assertEqual(rendered.size, (860, 360))

    def test_local_model_caption_is_repaired_or_replaced_by_museum_text(self):
        arch = self.artwork("1", (2200, 1000), title="The Gothic Arch", artist="Giovanni Battista Piranesi")
        requests = self.ollama(
            {"layout": "single", "primary_artwork_id": arch.id, "artworks": [{"id": arch.id, "crop": None}],
             "description": "This arch was commissioned for wealthy travelers and later entered a royal collection.",
             "curator_reason": "strong diagonal composition"},
            {"description": REPAIRED},
        )
        with self.database() as db:
            db.upsert_artwork(arch)
        self.cli("curate", "--count", "1")
        self.assertTrue(requests[0]["messages"][0]["images"])
        self.assertNotIn("images", requests[1]["messages"][0])
        self.assertIn("Rejection reason", requests[1]["messages"][0]["content"])

        # With the model unreachable, curation still works from museum metadata.
        self.configure('[llm]\nbase_url = "http://127.0.0.1:1"\n')
        with self.database() as db:
            db.upsert_artwork(self.artwork("2", (2200, 1000), title="Coast", museum_description=COAST))
        self.cli("curate", "--count", "1")
        with self.database() as db:
            first, second = db.list_compositions(only_unshown=True)
        self.assertEqual((first.artwork_ids, first.decision.description), ([arch.id], REPAIRED))
        self.assertEqual(second.decision.description, COAST)
        self.assertIn("Local model unavailable", second.decision.curator_reason)

    def test_rotation_skips_unusable_entries_walks_back_and_reuses_the_queue_offline(self):
        arcade, harbor, rejected = (self.artwork(str(i), title=title) for i, title in
                                    enumerate(("Arcade", "Harbor", "Rejected")))
        with self.database() as db:
            Path(self.queue(db, "bad-image", arcade).output_path).write_bytes(b"not a PNG")
            self.queue(db, "too-many-works", arcade, harbor)
            self.queue(db, "good-0", arcade)
            self.queue(db, "disliked-work", rejected)
            self.queue(db, "good-1", harbor)
            self.queue(db, "good-2", arcade)
            db.set_feedback(rejected.id, "dislike", target_type="artwork")
        for expected in ("good-0", "good-1", "good-2"):
            self.cli("next", "--no-apply")
            self.assertEqual(self.current_id(), expected)
        for expected in ("good-1", "good-0"):
            self.cli("previous", "--no-apply")
            self.assertEqual(self.current_id(), expected)
        self.cli("previous", "--no-apply", status=1)
        # With the queue used up, rotation returns to the least recently shown work.
        self.cli("next", "--no-apply")
        self.assertEqual(self.current_id(), "good-2")
        self.assertEqual(self.commands(), [])
