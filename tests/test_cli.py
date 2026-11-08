"""End-to-end command scenarios in a temporary home with fake desktop commands."""

import json
from io import BytesIO
from pathlib import Path
import threading
import time
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from artwall import cli
from artwall.config import database_path
from artwall.models import CuratorDecision, DecisionArtwork, Whole, arrangement_from_layout
from artwall.storage import Database
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

    def test_auto_backends_follow_the_running_desktop_environment(self):
        (self.bin_dir / "dms").unlink()  # A desktop environment's own setter, with no shell installed.
        background = "org.gnome.desktop.background"
        xfce_image = "/backdrop/screen0/monitorDP-1/workspace0/last-image"
        desktops = {
            "ubuntu:GNOME": ({"gsettings": "picture-uri\npicture-uri-dark\npicture-options\n"},
                             lambda image: [f"gsettings list-keys {background}",
                                            f"gsettings set {background} picture-uri {image.as_uri()}",
                                            f"gsettings set {background} picture-uri-dark {image.as_uri()}"]),
            "KDE": ({"plasma-apply-wallpaperimage": "", "kwriteconfig6": ""},
                    lambda image: [f"plasma-apply-wallpaperimage {image}",
                                   "kwriteconfig6 --file kscreenlockerrc --group Greeter --group Wallpaper "
                                   f"--group org.kde.image --group General --key Image {image.as_uri()}"]),
            "XFCE": ({"xfconf-query": f"{xfce_image}\n/backdrop/screen0/monitorDP-1/workspace0/image-style\n"},
                     lambda image: ["xfconf-query -c xfce4-desktop -l",
                                    f"xfconf-query -c xfce4-desktop -p {xfce_image} -s {image}"]),
        }
        with self.database() as db:
            for name in desktops:
                self.queue(db, name.lower(), self.artwork(name, (1600, 1200)))
        for desktop, (commands, expected) in desktops.items():
            with self.subTest(desktop=desktop), patch.dict("os.environ", {"XDG_CURRENT_DESKTOP": desktop}):
                for command, output in commands.items():
                    self.fake_command(command, output)
                self.commands_log.unlink(missing_ok=True)
                self.cli("next")
                with self.database() as db:
                    image = Path(db.get_current().output_path).resolve()
                self.assertEqual(self.commands(), expected(image))
                for command in commands:
                    (self.bin_dir / command).unlink()

    def test_init_detects_the_display_size(self):
        niri = {"DP-1": {"current_mode": 1, "modes": [{"width": 1920, "height": 1080},
                                                      {"width": 3440, "height": 1440}]}}
        hyprland = [{"name": "HDMI-A-1", "width": 3840, "height": 2160, "focused": False},
                    {"name": "eDP-1", "width": 2560, "height": 1600, "focused": True}]
        xrandr = ("Screen 0: minimum 8 x 8, current 4480 x 1440\n"
                  "HDMI-1 connected 2560x1440+1920+0 (normal) 597mm x 336mm\n"
                  "DP-2 connected primary 1920x1080+0+0 (normal) 527mm x 296mm\nDP-3 disconnected\n")
        cases = (("niri", json.dumps(niri), (3440, 1440)), ("hyprctl", json.dumps(hyprland), (2560, 1600)),
                 ("xrandr", xrandr, (1920, 1080)))
        for command, output, (width, height) in cases:
            with self.subTest(command=command):
                self.config_file.unlink()
                self.fake_command(command, output)
                self.assertIn(f"Display: {width} × {height} (detected)", self.cli("init"))
                self.assertIn(f"Display: {width} × {height}\nBackend: auto (dms)", self.cli("status"))
                (self.bin_dir / command).unlink()
        self.config_file.unlink()
        self.assertIn("Display: 1920 × 1080 (not detected", self.cli("init"))

    def test_signed_in_agent_cli_curates_without_an_api_key(self):
        art = self.artwork("agent", (1800, 1400), title="Harbor at Dusk", artist="An Etcher",
                           museum_description="Boats rest in a quiet harbor beneath a low evening sky while "
                                              "lamps glow along the pier and gulls circle above the dark water.")
        with self.database() as db:
            db.upsert_artwork(art)
        answer = CuratorDecision(Whole(DecisionArtwork(art.id)), description=art.museum_description,
                                 curator_reason="Calm harbor.").to_dict()
        events = [{"type": "system", "subtype": "init", "tools": []},
                  {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(answer)}]
        self.fake_command("claude", "\n".join(json.dumps(event) for event in events) + "\n", capture_stdin=True)
        self.configure('[llm]\nprovider = "agent"\nagent = "claude"\nmodel = ""\n')
        self.cli("curate", "--count", "1")
        with self.database() as db:
            (composition,) = db.list_compositions(only_unshown=True)
        self.assertNotIn("unavailable or invalid", composition.decision.curator_reason)
        (call, *_) = [line for line in self.commands() if line.startswith("claude ")]
        # No tools, MCP servers, user settings, or saved session; no --model when left empty.
        self.assertIn('--tools  --strict-mcp-config --setting-sources  --no-session-persistence', call)
        self.assertNotIn("--model", call)
        message = json.loads((self.bin_dir / "claude.stdin").read_text().splitlines()[0])
        kinds = [block["type"] for block in message["message"]["content"]]
        self.assertEqual(kinds, ["text", "image"])

        # Any other CLI: the prompt on stdin, {image} replaced, and an answer wrapped in prose.
        other = self.artwork("agent-2", (1800, 1400), title="Quiet Bay", museum_description=art.museum_description)
        with self.database() as db:
            db.upsert_artwork(other)
        answer = CuratorDecision(Whole(DecisionArtwork(other.id)), description=art.museum_description).to_dict()
        self.fake_command("my-agent", "Here is my choice:\n" + json.dumps(answer) + "\nEnjoy!")
        self.configure('[llm]\nprovider = "agent"\nagent = "command"\nmodel = ""\n'
                       'agent_command = ["my-agent", "--attach", "{image}"]\n')
        self.cli("curate", "--count", "1")
        with self.database() as db:
            queued = [c for c in db.list_compositions(only_unshown=True) if other.id in c.artwork_ids]
        self.assertEqual(len(queued), 1)
        self.assertNotIn("unavailable or invalid", queued[0].decision.curator_reason)
        self.assertTrue(any(line.startswith("my-agent --attach /") and line.endswith("contact-sheet.jpg")
                            for line in self.commands()))

    def test_curator_notes_teach_with_grounded_text_pinned_to_the_wallpaper(self):
        museum = ("Jean Duvet depicted unicorns in several of his print series as representations of purity. "
                  "Duvet densely engraved his prints.")
        art = self.artwork("notes", (1800, 1400), title="Triumph of the Unicorn", artist="Jean Duvet",
                           date_display="1540–50", museum_description=museum)
        with self.database() as db:
            db.upsert_artwork(art)
        selection = CuratorDecision(Whole(DecisionArtwork(art.id)), description=museum.split(". ")[0] + ".").to_dict()
        visible = "Dense parallel lines build the shadows under the procession, so the white paper reads as light."
        notes = {"notes": [
            {"work": 1, "title": "Engraved shadows", "body": visible, "box": [100, 200, 400, 600], "source": ""},
            {"work": 1, "title": "A symbol of purity", "source": museum.split(". ")[0] + ".",
             "body": "Duvet used the unicorn as a representation of purity in several print series.",
             "box": [420, 150, 700, 500]},
            # Rejected: a quote the museum never wrote, an invented name, a missing box, and an unknown work.
            {"work": 1, "title": "Royal patron", "source": "King Henri II commissioned the series.",
             "body": "King Henri II commissioned this print for his court at Fontainebleau.", "box": [0, 0, 500, 500]},
            {"work": 1, "title": "The rider", "source": "",
             "body": "The rider resembles Diane de Poitiers, who appears at the front of the procession.",
             "box": [0, 0, 300, 300]},
            {"work": 1, "title": "No place", "body": visible, "source": ""},
            {"work": 2, "title": "Elsewhere", "body": visible, "box": [0, 0, 500, 500], "source": ""},
        ]}

        def answer(prompt: str, **_: object) -> object:
            return notes if prompt.startswith("You are a museum educator") else selection

        with patch("artwall.curation.ModelCurator._chat", side_effect=answer):
            self.cli("curate", "--count", "1")
        self.cli("next", "--no-apply")
        with self.database() as db:
            (composition,) = db.list_compositions()
        self.assertEqual([note.title for note in composition.notes], ["Engraved shadows", "A symbol of purity"])
        first = composition.notes[0]
        self.assertEqual((first.box.x, first.box.y, first.box.width, first.box.height), (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(composition.notes[1].source, museum.split(". ")[0] + ".")
        info = self.cli("info")
        self.assertIn("What to look for:", info)
        self.assertIn("- Engraved shadows: " + visible, info)
        # Notes survive the database round trip and older compositions without notes still load.
        self.assertEqual(len(json.loads(self.cli("info", "--json"))["notes"]), 2)

    def test_curation_keeps_the_queue_topped_up_to_its_size(self):
        self.configure("[curation]\nqueue_size = 3\n")
        with self.database() as db:
            self.queue(db, "waiting", self.artwork("waiting"))
            for index in range(10):
                db.upsert_artwork(self.artwork(f"top-{index}", (1600, 1200), title=f"Study {index}"))
        # The waiting wallpaper predates these settings, so it is replaced once three new ones exist.
        output = self.cli("curate", "--offline")
        self.assertEqual(output.count("Queued"), 3)
        self.assertIn("Replaced 1 wallpaper prepared with earlier settings", output)
        with self.database() as db:
            self.assertIsNone(db.get_composition("waiting"))
        self.assertIn("3 wallpapers are already prepared", self.cli("curate", "--offline"))
        self.configure("[curation]\nqueue_size = 3\nmax_artworks = 2\n")  # A setting that shapes wallpapers.
        self.assertIn("Replaced 3 wallpapers", self.cli("curate", "--offline"))
        # Without a reachable model, new wallpapers fill gaps but never replace model-era ones.
        self.configure('[curation]\nqueue_size = 4\nmax_artworks = 1\n[llm]\nbase_url = "http://127.0.0.1:9"\n')
        output = self.cli("curate")
        self.assertEqual(output.count("Queued"), 4)
        self.assertNotIn("Replaced", output)
        self.assertEqual(self.cli("curate", "--offline", "--count", "1").count("Queued"), 1)  # Explicit counts still add.

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
            ("pair", 2, ((1600, 1450), (1600, 1450)), "single", 2),
            ("trio", 3, ((1500, 2000),) * 3, "single", 3),
            ("wide", 3, ((3200, 1400), (1600, 1450)), "single", 1),
            ("overfull", 2, ((3200, 1400),) * 2, "diptych", 1),
            ("unrelated", 3, ((1600, 1450),), "single", 1),
        )
        for name, limit, sizes, layout, expected in cases:
            with self.subTest(name=name):
                self.configure(f"[curation]\nmax_artworks = {limit}\n")
                works = [self.artwork(f"{name}-{i}", size, artist=f"Artist {name}")
                         for i, size in enumerate(sizes)]
                with self.database() as db:
                    for work in works:
                        db.upsert_artwork(work)
                chosen = [DecisionArtwork(w.id) for w in works[:2 if layout == "diptych" else 1]]
                draft = CuratorDecision(arrangement_from_layout(layout, works[0].id, chosen), use_gradient=False)
                with patch("artwall.curation.ModelCurator._chat", return_value=draft.to_dict()), \
                     patch("artwall.curation.ModelCurator.describe_final", side_effect=lambda decision, pool: decision):
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
                self.configure("[caption]\non_wallpaper = true\nshow_medium = true\n")
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

    def test_upgrade_survives_concurrent_and_interrupted_migrations(self):
        self.configure("[curation]\nmax_artworks = 2\n")
        with self.database() as db:
            self.queue(db, "legacy", self.artwork("1"), self.artwork("2"))
            self.queue(db, "legacy-single", self.artwork("3"))
            # Earlier releases stored pairs as diptych_50 or diptych_60, and narrow works as near_single.
            db.connection.execute("UPDATE compositions SET payload=replace(payload, '\"diptych\"', '\"diptych_60\"')")
            db.connection.execute("UPDATE compositions SET payload=replace(payload, '\"single\"', '\"near_single\"')")
            db.connection.execute("DROP INDEX idx_compositions_queue")
            db.connection.execute("ALTER TABLE compositions DROP COLUMN queue_priority")
            db.connection.execute("PRAGMA user_version = 2")
            db.connection.commit()
        # A login rotation and a timer can open an older database at the same moment.
        start, errors = threading.Barrier(4), []

        def open_database() -> None:
            start.wait()
            try:
                Database(database_path()).close()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=open_database) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        # A process that stopped after adding the column but before recording the version.
        with self.database() as db:
            db.connection.execute("PRAGMA user_version = 2")
            db.connection.commit()
        self.assertIn("Queued: 2", self.cli("status"))
        for layout in ("diptych", "single"):
            self.cli("next", "--no-apply")
            with self.database() as db:
                self.assertEqual(db.get_current().decision.layout, layout)
        with self.database() as db:
            self.assertEqual(db.connection.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_low_resolution_works_are_downloaded_once(self):
        # ArtIC serves some public-domain works only at 843 pixels wide,
        # although the catalog lists a much larger original.
        small = self.artwork("700", (843, 600), image_width=3000, image_height=2135,
                             iiif_url="https://www.artic.edu/iiif/2/small-image")
        served = BytesIO()
        Image.new("RGB", (843, 600)).save(served, format="JPEG")

        def respond(url: str) -> BytesIO:
            response = BytesIO(served.getvalue())
            response.headers = {"Content-Type": "image/jpeg"}
            return response

        with self.database() as db:
            db.upsert_artwork(small)
            for index in range(2):
                db.upsert_artwork(self.artwork(str(710 + index), (1600, 1450)))
        with patch("artwall.sources.artic.ArticSource._open", side_effect=respond) as download:
            for _ in range(2):
                self.assertIn("Queued", self.cli("curate", "--offline", "--count", "1"))
        download.assert_called_once()
        with self.database() as db:
            self.assertFalse(any(small.id in c.artwork_ids for c in db.list_compositions()))

    def test_curation_skips_works_queued_concurrently_by_another_run(self):
        with self.database() as db:
            for index in range(4):
                db.upsert_artwork(self.artwork(str(800 + index), (1600, 1450)))
        real_choose = cli.choose_composition
        raced = []

        def choose_while_another_run_queues(pool, taste, **options):
            decision = real_choose(pool, taste, **options)
            if not raced:  # Another curator queues the same work first.
                self.clock = time.time()
                with self.database() as other:
                    raced.append(self.queue(other, "other-run", other.get_artwork(decision.primary_artwork_id)))
            return decision

        with patch("artwall.cli.choose_composition", side_effect=choose_while_another_run_queues), \
                self.assertLogs("artwall", level="INFO") as logs:
            self.assertEqual(self.cli("curate", "--offline", "--count", "2").count("Queued"), 1)
        self.assertTrue(any("queued_concurrently" in line for line in logs.output))
        with self.database() as db:
            queued = [i for c in db.list_compositions(only_unshown=True) for i in c.artwork_ids]
        self.assertEqual(len(queued), len(set(queued)), queued)
