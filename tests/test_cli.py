"""End-to-end command scenarios in a temporary home with fake desktop commands."""

import json
from pathlib import Path
import time
from unittest.mock import patch

from PIL import Image

from tests.support import HomeTest

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

    def test_content_filters_apply_to_curation_and_prepared_wallpapers(self):
        self.configure("[lockscreen]\npublic_lock_screen_safe = true\n")
        safe = self.artwork("safe", (1600, 1200), title="Landscape")
        unsafe_title = self.artwork("unsafe-title", (1600, 1200), title="Nude Study")
        unsafe_tag = self.artwork("unsafe-tag", (1600, 1200), subject_tags=["execution"])
        no_rights = self.artwork("no-rights", (1600, 1200), public_domain=False)
        no_access = self.artwork("no-access", (1600, 1200), open_access=False)
        with self.database() as db:
            db.upsert_artwork(safe)
            for art in (unsafe_title, unsafe_tag, no_rights, no_access):
                self.queue(db, art.source_id, art)
        self.cli("curate", "--offline", "--count", "1")
        self.assertIn("Queued: 1", self.cli("status"))
        self.cli("next", "--no-apply")
        with self.database() as db:
            self.assertEqual(db.get_current().artwork_ids, [safe.id])

        self.configure()  # Disabling the lock-screen filter admits the two rights-eligible works.
        self.assertIn("Queued: 2", self.cli("status"))
        for expected in (unsafe_title.id, unsafe_tag.id):
            self.cli("next", "--no-apply")
            with self.database() as db:
                self.assertEqual(db.get_current().artwork_ids, [expected])
        self.assertIn("Queued: 0", self.cli("status"))

    def test_failed_update_and_long_shutdown_keep_the_next_work_queued(self):
        with self.database() as db:
            for identifier in ("first", "second", "third"):
                self.queue(db, identifier, self.artwork(identifier, title=identifier.title()))
        self.cli("install-timers")
        units = self.root / "config" / "systemd" / "user"
        login = (units / "artwall-login.service").read_text()
        timer = (units / "artwall-rotate.timer").read_text()
        self.assertIn(f"ExecStart={(self.root / 'bin' / 'artwall').resolve()} next", login)
        self.assertIn("WantedBy=graphical-session.target", login)
        self.assertIn("Restart=on-failure", login)
        self.assertIn("OnActiveSec=120min", timer)
        self.assertIn("OnUnitActiveSec=120min", timer)
        self.assertIn("PartOf=graphical-session.target", timer)
        self.assertIn("WantedBy=graphical-session.target", timer)
        self.assertNotIn("OnCalendar=", timer)
        self.assertNotIn("Persistent=", timer)
        self.assertEqual(self.commands(), ["systemctl --user daemon-reload",
                                           "systemctl --user reenable artwall-rotate.timer",
                                           "systemctl --user enable --now artwall-curate.timer",
                                           "systemctl --user restart artwall-rotate.timer",
                                           "systemctl --user enable artwall-login.service"])

        self.cli("next")
        self.desktop_down.touch()
        with self.assertLogs("artwall", level="ERROR"):
            self.cli("next", status=1)
        with self.database() as db:
            self.assertEqual(db.get_current().id, "first")
            self.assertEqual([item.id for item in db.list_compositions(only_unshown=True)], ["second", "third"])
        current = self.root / "cache" / "artwall" / "current.json"
        self.assertEqual(json.loads(current.read_text())["id"], "first")
        self.desktop_down.unlink()
        # The login service invokes `next` once after a long gap and a new database connection.
        with patch("artwall.storage.time.time", return_value=time.time() + 7 * 86400):
            self.cli("next")
        self.assertEqual(self.current_id(), "second")
        with self.database() as db:
            self.assertEqual([item.id for item in db.list_compositions(only_unshown=True)], ["third"])
        self.cli("next", "--no-apply")
        self.assertEqual(self.current_id(), "third")
