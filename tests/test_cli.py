"""End-to-end command scenarios in a temporary home with fake desktop commands."""

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from artwall.models import CuratorDecision, DecisionArtwork
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

    def test_curate_repairs_truncated_images_and_skips_bad_replacements(self):
        for index, replacement_valid in enumerate((True, False)):
            with self.subTest(replacement_valid=replacement_valid):
                damaged = self.artwork(str(500 + index), (1600, 1450),
                                       iiif_url="https://www.artic.edu/iiif/2/test-image")
                path = Path(damaged.cached_image_path)
                complete = path.read_bytes()
                truncated = complete[:-100]
                path.write_bytes(truncated)
                # This is the failure the old header-only check missed.
                with Image.open(path) as image:
                    image.verify()
                healthy = self.artwork(str(600 + index), (1600, 1450))
                with self.database() as db:
                    db.upsert_artwork(damaged)
                    db.upsert_artwork(healthy)
                response = BytesIO(complete if replacement_valid else truncated)
                response.headers = {"Content-Type": "image/jpeg"}
                with patch("artwall.sources.artic.ArticSource._open", return_value=response) as download, \
                        self.assertLogs("artwall", level="WARNING"):
                    self.assertIn("Queued", self.cli("curate", "--offline", "--count", "1"))
                download.assert_called_once()
                with self.database() as db:
                    if replacement_valid:
                        repaired = db.get_artwork(damaged.id)
                        self.assertNotEqual(repaired.cached_image_path, str(path))
                        with Image.open(repaired.cached_image_path) as image:
                            image.load()
                    else:
                        self.assertFalse(any(damaged.id in c.artwork_ids for c in db.list_compositions()))
                self.assertEqual(self.commands(), [])

    def test_curator_fits_related_whole_works_without_arbitrary_details(self):
        cases = (
            ("pair", 2, ((1600, 1450), (1600, 1450)), "near_single", 2),
            ("trio", 3, ((1500, 2000),) * 3, "near_single", 3),
            ("wide", 3, ((3200, 1400), (1600, 1450)), "single", 1),
            ("overfull", 2, ((3200, 1400),) * 2, "diptych_50", 1),
            ("unrelated", 3, ((1600, 1450),), "near_single", 1),
        )
        for name, limit, sizes, layout, expected in cases:
            with self.subTest(name=name):
                self.configure(f"[curation]\nmax_artworks = {limit}\n")
                works = [self.artwork(f"{name}-{i}", size, artist=f"Artist {name}")
                         for i, size in enumerate(sizes)]
                with self.database() as db:
                    for work in works:
                        db.upsert_artwork(work)
                draft = CuratorDecision(layout, works[0].id,
                                        tuple(DecisionArtwork(w.id) for w in works[:2 if layout == "diptych_50" else 1]),
                                        use_gradient=False)
                with patch("artwall.curation.OllamaCurator._chat", return_value=draft.to_dict()), \
                     patch("artwall.curation.OllamaCurator.describe_final", side_effect=lambda decision, pool: decision):
                    self.cli("curate", "--count", "1")
                with self.database() as db:
                    composition = next(c for c in db.list_compositions() if c.decision.primary_artwork_id == works[0].id)
                self.assertEqual(len(composition.artwork_ids), expected)
                self.assertNotEqual(composition.decision.layout, "artwork_detail")
                self.assertEqual(set(composition.artwork_ids), {w.id for w in works[:expected]})
                self.assertTrue(all(crop is None for crop in composition.render["crops"].values()))
                if expected > 1:
                    x0, y0, x1, y1 = composition.render["artwork_rect"]
                    self.assertGreater((x1 - x0) * (y1 - y0) / (860 * 360), 0.85)
                    panel_width = (x1 - x0 - 8 * (expected - 1)) / expected
                    with Image.open(composition.output_path) as rendered, Image.open(works[0].cached_image_path) as source:
                        # A neighbor's frame or shadow must not darken the first original.
                        self.assertEqual(rendered.getpixel((round(x0 + panel_width) - 2, y0 + 30)),
                                         source.getpixel((0, 0)))

    def test_render_preserves_the_original_with_a_diffuse_surround_and_clear_edge(self):
        self.config = "[display]\nwidth = 1720\nheight = 720\n"
        self.configure()
        art = self.artwork("color-study", (1600, 1450), title="Color Study")
        original = Image.new("RGB", (1600, 1450), (180, 80, 45))
        draw = ImageDraw.Draw(original)
        draw.rectangle((0, 0, 800, 725), fill=(50, 105, 150))
        draw.rectangle((800, 725, 1600, 1450), fill=(155, 150, 90))
        original.save(art.cached_image_path)
        with self.database() as db:
            self.queue(db, "color-study", art)
            db.mark_shown("color-study")
        output = self.root / "diffuse.png"
        self.cli("render", "--output", str(output))
        with Image.open(output) as rendered:
            self.assertEqual(rendered.size, (1720, 720))
            # At this display size the caption fits entirely in the left surround.
            region = rendered.crop((600, 80, 1120, 640))
            with Image.open(art.cached_image_path) as source:
                inset = round(720 * 0.006)
                intact = ImageOps.contain(source.convert("RGB"), (1720 - 2 * inset, 720 - 2 * inset), Image.Resampling.LANCZOS)
                left = (1720 - intact.width) // 2
                top = (720 - intact.height) // 2
                expected = intact.crop((600 - left, 80 - top, 1120 - left, 640 - top))
            self.assertIsNone(ImageChops.difference(region, expected).getbbox())
            surround = rendered.crop((20, 20, 300, 300))
            self.assertGreater(max(ImageStat.Stat(surround).stddev), 1)
            self.assertNotEqual(rendered.getpixel((left - 1, 360)), rendered.getpixel((left - 3, 360)))

    def test_captions_wrap_complete_fields_and_stay_inside_the_display(self):
        title = "View of the River Dee 3 Miles Short of Bala"
        companion_titles = ("There They Go Plucked (i.e. fleeced), plate 20 from Los Caprichos",
                            "A Mountain Landscape with Travellers Crossing a Bridge above a Rocky Stream")
        medium = "Aquatint on cream laid paper with etched lines and delicate washes of pale blue and green"
        description = ("A broad river winds between wooded hills beneath a pale sky. "
                       "Small figures gather near the bank while distant mountains fade into the light. "
                       "The companion prints contrast dense etched shadows with open areas of paper, "
                       "drawing attention to the different textures of trees and stone.")
        cases = ((3440, 1440, 2, "bottom_left", 620),
                 (3840, 2160, 3, "top_right", 320),
                 (1720, 720, 1, "bottom_right", 620),
                 (860, 360, 3, "bottom_left", 320),
                 (320, 240, 3, "top_left", 320))
        draw_text = ImageDraw.ImageDraw.text
        for index, (width, height, count, anchor, caption_width) in enumerate(cases):
            with self.subTest(display=(width, height), works=count, anchor=anchor):
                self.config = f"[display]\nwidth = {width}\nheight = {height}\n"
                self.configure("[caption]\nshow_medium = true\n")
                works = [self.artwork(f"caption-{index}-primary", (1600, 1450), title=title,
                                      artist="Paul Sandby", date_display="1777", movement="Romanticism",
                                      medium=medium)]
                works += [self.artwork(f"caption-{index}-{number}", (1500, 2000), title=value)
                          for number, value in enumerate(companion_titles[:count - 1])]
                with self.database() as db:
                    composition = self.queue(db, f"caption-{index}", *works, description=description,
                                             caption_anchor=anchor, caption_max_width_px=caption_width)
                    db.mark_shown(composition.id)
                painted = []

                def capture(draw, xy, text, *args, **kwargs):
                    bounds = draw.textbbox(xy, text, font=kwargs.get("font"), anchor=kwargs.get("anchor"))
                    painted.append((text, bounds))
                    return draw_text(draw, xy, text, *args, **kwargs)

                output = self.root / f"caption-{index}.png"
                with patch.object(ImageDraw.ImageDraw, "text", new=capture):
                    self.cli("render", "--output", str(output))
                # Each line has a shadow followed by the actual type. Check
                # the painted output, including shadows, rather than a line counter.
                foreground = painted[1::2]
                rendered_text = " ".join(text for text, _ in foreground)
                self.assertIn(title, rendered_text)
                self.assertIn(medium, rendered_text)
                for companion in companion_titles[:count - 1]:
                    self.assertIn("With: " + companion, rendered_text)
                self.assertTrue(rendered_text.endswith(description), rendered_text)
                self.assertNotIn("…", rendered_text)
                for _, (left, top, right, bottom) in painted:
                    self.assertGreaterEqual(min(left, top), 0)
                    self.assertLessEqual(right, width)
                    self.assertLessEqual(bottom, height)
                for (_, previous), (_, following) in zip(foreground, foreground[1:]):
                    self.assertGreaterEqual(following[1], previous[3], "Caption lines overlap")
                with Image.open(output) as rendered:
                    self.assertEqual(rendered.size, (width, height))

    def test_failed_update_and_long_shutdown_keep_the_next_work_queued(self):
        with self.database() as db:
            for identifier in ("first", "second", "third"):
                self.queue(db, identifier, self.artwork(identifier, title=identifier.title()))
        self.cli("install-timers")
        units = self.root / "config" / "systemd" / "user"
        login = (units / "artwall-login.service").read_text()
        timer = (units / "artwall-rotate.timer").read_text()
        self.assertIn(f"ExecStart={(self.root / 'bin' / 'artwall').resolve()} next --if-due", login)
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

        self.cli("next", "--if-due")
        self.desktop_down.touch()
        with self.assertLogs("artwall", level="ERROR"):
            self.cli("next", status=1)
        with self.database() as db:
            self.assertEqual(db.get_current().id, "first")
            shown_at = db.get_current().shown_at
            self.assertEqual([item.id for item in db.list_compositions(only_unshown=True)], ["second", "third"])
        current = self.root / "cache" / "artwall" / "current.json"
        self.assertEqual(json.loads(current.read_text())["id"], "first")
        self.desktop_down.unlink()
        commands = self.commands()
        for elapsed in (120 * 60 - 1, 120 * 60):
            with patch("artwall.rotation.time.time", return_value=shown_at + elapsed):
                self.assertIn("wallpaper unchanged", self.cli("next", "--if-due"))
            self.assertEqual(self.current_id(), "first")
            self.assertEqual(self.commands(), commands)
            self.assertEqual(json.loads(current.read_text())["id"], "first")
        # A week away results in exactly one change, even after the earlier failed update.
        with patch("artwall.rotation.time.time", return_value=shown_at + 7 * 86400):
            self.cli("next", "--if-due")
        self.assertEqual(self.current_id(), "second")
        self.assertEqual(json.loads(current.read_text())["id"], "second")
        with self.database() as db:
            self.assertEqual([item.id for item in db.list_compositions(only_unshown=True)], ["third"])
        self.cli("next", "--no-apply")
        self.assertEqual(self.current_id(), "third")
