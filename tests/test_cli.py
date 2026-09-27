"""End-to-end command scenarios in a temporary home with fake desktop commands and model server."""

import json
from pathlib import Path

from PIL import Image

from artwall.config import data_dir
from tests.support import HomeTest

REPAIRED = ("The dense etched lines surround a large arch, while a small figure near the lower edge gives "
            "the structure a striking sense of scale.")
SHARED = ("The left print holds a broad pale arch against dense etched lines, while the right print stacks "
          "smaller arches in a tighter view. Together they make the architecture feel both expansive and enclosed.")
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

    def test_model_choice_gains_a_related_companion_and_a_new_shared_caption(self):
        first = self.artwork("1", (2100, 1500), title="First, from Views of Rome", artist="Piranesi")
        second = self.artwork("2", (1500, 1500), title="Second, from Views of Rome", artist="Piranesi")
        requests = self.ollama({"layout": "near_single", "primary_artwork_id": first.id, "artworks": [{"id": first.id}]},
                               {"description": SHARED}, extra="[curation]\nmax_artworks = 3\n")
        with self.database() as db:
            db.upsert_artwork(first)
            db.upsert_artwork(second)
        self.cli("curate", "--count", "1")
        with self.database() as db:
            (composition,) = db.list_compositions()
        self.assertEqual((composition.decision.layout, composition.artwork_ids, composition.decision.description),
                         ("diptych_50", [first.id, second.id], SHARED))
        self.assertEqual(composition.decision.relationship, "Two plates from Views of Rome, by Piranesi.")
        self.assertIn("ALL the numbered works", requests[1]["messages"][0]["content"])
        self.assertTrue(requests[1]["messages"][0]["images"])

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

    def test_votes_are_idempotent_and_a_dislike_waits_for_a_replacement(self):
        with self.database() as db:
            self.queue(db, "one", self.artwork("1", title="Arcade", artist="An Etcher"))
        self.cli("next", "--no-apply")
        self.assertIn("Marked current composition: like", self.cli("like"))
        self.assertIn("Already marked: like", self.cli("like"))
        self.assertIn("wallpaper unchanged", self.cli("dislike", "--no-apply"))
        with self.database() as db:
            self.assertEqual(self.current_id(), "one")
            self.assertEqual(db.get_feedback("one"), {"like": False, "dislike": True})
            self.assertEqual(db.get_artist_preference("An Etcher").score, -1)
            self.queue(db, "two", self.artwork("2", title="Second"))
        self.cli("dislike", "--no-apply")
        self.assertEqual(self.current_id(), "two")
        with self.database() as db:
            self.assertEqual(db.get_artist_preference("An Etcher").score, -1)

    def test_database_from_an_earlier_release_upgrades_and_keeps_rotating(self):
        image = data_dir() / "compositions" / "legacy.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (860, 360)).save(image)
        legacy = {
            "id": "legacy", "created_at": 1.0, "artwork_ids": ["artic:1"], "output_path": str(image),
            "decision": {"layout": "near_single", "primary_artwork_id": "artic:1",
                         "artworks": [{"id": "artic:1", "crop": None}], "caption_anchor": "bottom_left",
                         "caption_max_width_px": 620, "use_gradient": True, "detail_crop": None,
                         "relationship": "", "description": "", "curator_reason": "Deterministic selection."},
            "render": {"caption_anchor": "top_left", "actual_layout": "near_single", "crops": {"artic:1": None}},
        }
        with self.database() as db:
            db.upsert_artwork(self.artwork("1", title="Arcade"))
            db.connection.executescript("DROP TABLE composition_comments; PRAGMA user_version = 1;")
            with db.connection:
                db.connection.executemany("INSERT INTO compositions(id, payload, created_at) VALUES (?, ?, ?)",
                                          [("unreadable", '{"id": "unreadable"}', 0), ("legacy", json.dumps(legacy), 1)])
        self.assertIn("Arcade", self.cli("next", "--no-apply"))
        self.assertIn("Layout: near_single", self.cli("info"))
        with self.database() as db:
            self.assertEqual(db.connection.execute("PRAGMA user_version").fetchone()[0], 2)
            db.set_comment("legacy", "Still here.")
            self.assertEqual(db.get_comment("legacy"), "Still here.")

    def test_init_and_timers_follow_the_config_and_mistyped_values_are_refused(self):
        self.config_file.unlink()
        self.cli("init")
        self.assertIn("[display]", self.config_file.read_text())
        self.configure("[rotation]\ninterval_minutes = 90\n")
        self.cli("install-timers")
        units = self.root / "config" / "systemd" / "user"
        self.assertIn("OnUnitActiveSec=90min", (units / "artwall-rotate.timer").read_text())
        self.assertIn(f"ExecStart={(self.root / 'bin' / 'artwall').resolve()} next",
                      (units / "artwall-rotate.service").read_text())
        self.assertEqual(self.commands(), ["systemctl --user daemon-reload",
                                           "systemctl --user enable --now artwall-curate.timer artwall-rotate.timer"])
        self.configure('[lockscreen]\npublic_lock_screen_safe = "false"\n')
        self.cli("status", status=1)
