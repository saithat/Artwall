"""Desktop backends against real tools, inside the scripts/check-desktops container.

Each test shows a prepared wallpaper through the CLI with ``auto`` backends, then
reads the setting back with the desktop's own tool. Run via ``scripts/check-desktops``.
"""

from __future__ import annotations

import os
from pathlib import Path
import json
import subprocess
import sys
import time
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

from artwall.desktop import lock_screen_backend
from tests.support import HomeTest

_REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")


def _sun_height(screenshot: Path) -> int:
    """How tall the test wallpaper's sun appears in a screenshot, in pixels."""
    with Image.open(screenshot) as shot:
        bands = shot.convert("RGB").split()
    masks = [band.point(lambda v, want=want: 255 if abs(v - want) <= 8 else 0)
             for band, want in zip(bands, (235, 205, 135))]
    box = ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2]).getbbox()
    return 0 if box is None else box[3] - box[1]


def _read(*command: str) -> str:
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()


def _gsettings(schema: str, key: str) -> str:
    # The D-Bus-activated dconf service writes under the session's own config home,
    # not this test's temporary XDG_CONFIG_HOME, so read back from the same place.
    environment = {name: value for name, value in os.environ.items() if name != "XDG_CONFIG_HOME"}
    return subprocess.run(["gsettings", "get", schema, key], check=True, text=True, capture_output=True,
                          env=environment).stdout.strip()


class LiveDesktopTests(HomeTest):
    def setUp(self) -> None:
        super().setUp()
        # Real tools only: no fake dms, and no compositor unless a test provides one.
        session = patch.dict(os.environ, {"PATH": _REAL_PATH, "XDG_CURRENT_DESKTOP": "", "DISPLAY": "",
                                          "WAYLAND_DISPLAY": "", "SWAYSOCK": ""})
        session.start()
        self.addCleanup(session.stop)
        self.shown = 0

    def show(self, **environment: str) -> Path:
        """Queue one composition, run `artwall next` in that session, and return its image."""
        self.shown += 1
        with self.database() as db:
            self.queue(db, f"live-{self.shown}", self.artwork(f"live-{self.shown}", (1600, 1200)))
        with patch.dict(os.environ, environment):
            self.cli("next")
            with self.database() as db:
                return Path(db.get_current().output_path).resolve()

    def status(self, **environment: str) -> str:
        with patch.dict(os.environ, environment):
            return self.cli("status")

    def test_gnome_sets_light_and_dark_backgrounds(self):
        image = self.show(XDG_CURRENT_DESKTOP="ubuntu:GNOME")
        for key in ("picture-uri", "picture-uri-dark"):
            self.assertEqual(_gsettings("org.gnome.desktop.background", key), f"'{image.as_uri()}'")
        self.assertIn("Backend: auto (gnome) · Lock screen: auto (none)", self.status(XDG_CURRENT_DESKTOP="GNOME"))

    def test_cinnamon_and_mate_set_their_own_schemas(self):
        image = self.show(XDG_CURRENT_DESKTOP="X-Cinnamon")
        self.assertEqual(_gsettings("org.cinnamon.desktop.background", "picture-uri"),
                         f"'{image.as_uri()}'")
        image = self.show(XDG_CURRENT_DESKTOP="MATE")
        self.assertEqual(_gsettings("org.mate.background", "picture-filename"), f"'{image}'")

    def test_xfce_sets_each_existing_backdrop(self):
        properties = [f"/backdrop/screen0/monitor{name}/workspace0/last-image" for name in ("DP-1", "HDMI-1")]
        for name in properties:
            _read("xfconf-query", "-c", "xfce4-desktop", "-p", name, "-n", "-t", "string", "-s", "/old.png")
        image = self.show(XDG_CURRENT_DESKTOP="XFCE")
        for name in properties:
            self.assertEqual(_read("xfconf-query", "-c", "xfce4-desktop", "-p", name), str(image))

    def test_kde_lock_screen_is_written_to_kscreenlockerrc(self):
        # Applying a Plasma wallpaper needs a running plasmashell, so check the lock screen directly.
        with self.database() as db:
            image = Path(self.queue(db, "kde", self.artwork("kde", (1600, 1200))).output_path).resolve()
        lock_screen_backend("kde").configure(image)
        value = _read("kreadconfig6", "--file", "kscreenlockerrc", "--group", "Greeter", "--group", "Wallpaper",
                      "--group", "org.kde.image", "--group", "General", "--key", "Image")
        self.assertEqual(value, image.as_uri())

    def test_sway_starts_swaybg_and_init_detects_its_output(self):
        sway = {"XDG_CURRENT_DESKTOP": "sway", "WAYLAND_DISPLAY": "wayland-1",
                "SWAYSOCK": os.environ["ARTWALL_TEST_SWAYSOCK"]}
        image = self.show(**sway)
        commands = []
        for process in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                commands.append(process.read_bytes().split(b"\0"))
            except OSError:
                continue
        self.assertTrue(any(command[0].endswith(b"swaybg") and str(image).encode() in command
                            for command in commands))
        self.config_file.unlink()
        with patch.dict(os.environ, sway):
            self.assertIn("Display: 2560 × 1440 (detected)", self.cli("init"))

    def test_x11_window_manager_uses_feh_and_xrandr(self):
        x11 = {"DISPLAY": os.environ["ARTWALL_TEST_X11_DISPLAY"], "HOME": str(self.root)}
        image = self.show(**x11)
        self.assertIn(str(image), (self.root / ".fehbg").read_text())
        self.config_file.unlink()
        with patch.dict(os.environ, x11):
            self.assertIn("Display: 1600 × 900 (detected)", self.cli("init"))

    def test_tray_registers_with_the_host_and_its_menu_drives_rotation(self):
        """A fake StatusNotifierWatcher stands in for the desktop's tray host."""
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        with self.database() as db:
            for identifier, title in (("harbor", "Harbor at Dusk"), ("clouds", "Study of Clouds")):
                self.queue(db, identifier, self.artwork(identifier, title=title, artist="Jane Etcher"))
        self.cli("next", "--no-apply")
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        registered: list[str] = []
        watcher = Gio.DBusNodeInfo.new_for_xml(
            '<node><interface name="org.kde.StatusNotifierWatcher"><method name="RegisterStatusNotifierItem">'
            '<arg name="service" type="s" direction="in"/></method></interface></node>').interfaces[0]

        def register(_bus, _sender, _path, _interface, _method, parameters, invocation):
            registered.append(parameters.unpack()[0])
            invocation.return_value(None)

        context = GLib.MainContext.new()
        context.push_thread_default()
        self.addCleanup(context.pop_thread_default)
        bus.register_object("/StatusNotifierWatcher", watcher, register, None, None)
        Gio.bus_own_name_on_connection(bus, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE, None, None)
        tray = subprocess.Popen([sys.executable, "-P", "-m", "artwall.cli", "tray"], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                                     "XDG_CURRENT_DESKTOP": "sway", "SWAYSOCK": os.environ["ARTWALL_TEST_SWAYSOCK"],
                                     "WAYLAND_DISPLAY": os.environ["ARTWALL_TEST_WAYLAND_DISPLAY"]})
        self.addCleanup(tray.kill)
        deadline = time.monotonic() + 15
        while not registered and time.monotonic() < deadline and tray.poll() is None:
            context.iteration(False)
            time.sleep(0.05)
        if not registered:
            tray.kill()
            self.fail("The tray did not register:\n" + tray.communicate()[0])
        self.assertEqual(registered, [f"org.kde.StatusNotifierItem-{tray.pid}-1"])

        def call(path, interface, method, signature, *args):
            return bus.call_sync(registered[0], path, interface, method, GLib.Variant(signature, args) if signature
                                 else None, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()

        def menu():
            _revision, (_root, _props, children) = call("/MenuBar", "com.canonical.dbusmenu", "GetLayout",
                                                        "(iias)", 0, -1, [])
            return {props.get("label", "-"): number for number, props, _children in children}

        def tooltip():
            return call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "Get", "(ss)",
                        "org.kde.StatusNotifierItem", "ToolTip")[0][3]

        self.assertEqual(call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "Get", "(ss)",
                              "org.kde.StatusNotifierItem", "Menu")[0], "/MenuBar")
        items = menu()
        self.assertIn("Harbor at Dusk", items)
        self.assertIn("Harbor at Dusk", tooltip())
        call("/MenuBar", "com.canonical.dbusmenu", "Event", "(isvu)", items["Like"], "clicked",
             GLib.Variant("s", ""), 0)
        call("/MenuBar", "com.canonical.dbusmenu", "Event", "(isvu)", items["Next Wallpaper"], "clicked",
             GLib.Variant("s", ""), 0)
        deadline = time.monotonic() + 10
        while self.current_id() != "clouds" and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.current_id() != "clouds":
            tray.kill()
            self.fail("Next Wallpaper did not rotate:\n" + tray.communicate()[0])
        with self.database() as db:
            self.assertTrue(db.get_feedback("harbor").get("like"))
        call("/MenuBar", "com.canonical.dbusmenu", "AboutToShow", "(i)", 0)  # Opening the menu reads it again.
        self.assertIn("Study of Clouds", menu())
        self.assertIn("Study of Clouds", tooltip())
        call("/MenuBar", "com.canonical.dbusmenu", "Event", "(isvu)", menu()["Quit Tray"], "clicked",
             GLib.Variant("s", ""), 0)
        self.assertEqual(tray.wait(timeout=5), 0)

    def current_id(self) -> str:
        with self.database() as db:
            return db.get_current().id

    def test_native_app_opens_each_page_and_dialog(self):
        """Start the GTK app under headless Sway; with ARTWALL_SCREENSHOTS set, save each view."""
        self.config = "[display]\nwidth = 1920\nheight = 1080\n[rotation]\nat = \"07:30\"\n[llm]\nenabled = false\n"
        self.configure()
        with self.database() as db:
            for index, (size, title) in enumerate((((1800, 1200), "Harbor at Dusk"), ((1200, 1500), "Study of Clouds"),
                                                   ((1600, 1100), "Mountain Path"))):
                art = self.artwork(f"app-{index}", size, title=title, artist="Jane Etcher", date_display="1821",
                                   canonical_url=f"https://www.artic.edu/artworks/{index}",
                                   museum_description="A pale sun hangs over dark water while two small boats rest "
                                                      "near the far bank and clouds drift across a wide sky.")
                with Image.open(art.cached_image_path) as source:
                    image = source.copy()
                draw = ImageDraw.Draw(image)
                draw.rectangle((0, size[1] * 2 // 3, size[0], size[1]), fill=(40, 70, 95 + 30 * index))
                draw.ellipse((size[0] // 2 - 120, 120, size[0] // 2 + 120, 360), fill=(235, 205, 135))
                image.save(art.cached_image_path)
                db.upsert_artwork(art)
        self.cli("curate", "--offline", "--count", "3")
        self.cli("next", "--no-apply")
        self.cli("next", "--no-apply")
        self.cli("like")  # Shows a chosen reaction in the screenshots.
        with self.database() as db:  # Curator notes, as a model would pin them to the current wallpaper.
            current = db.get_current()
            payload = current.to_dict()
            payload["notes"] = [
                {"artwork_id": current.artwork_ids[0], "title": "A low, pale sun",
                 "body": "The pale disk sits high and small, so the dark water below carries most of the weight "
                         "and the eye travels up from the boats to the light.",
                 "box": {"x": 0.42, "y": 0.08, "width": 0.16, "height": 0.26}, "source": ""},
                {"artwork_id": current.artwork_ids[0], "title": "Dark water",
                 "body": "A broad band of blue fills the lower third, giving the quiet harbor its calm weight.",
                 "box": {"x": 0.2, "y": 0.68, "width": 0.6, "height": 0.28},
                 "source": "A pale sun hangs over dark water while two small boats rest near the far bank and "
                           "clouds drift across a wide sky."}]
            payload["notes"] += [  # As many notes as a curator writes, so the page is taller than the window.
                {"artwork_id": current.artwork_ids[0], "title": f"Detail {number}",
                 "body": "A further passage worth a slower look, described at the length a curator's note "
                         "usually takes, which is two or three full sentences on the page.",
                 "box": {"x": 0.1 * number, "y": 0.4, "width": 0.1, "height": 0.1}, "source": ""}
                for number in (3, 4, 5)]
            # A conversation with long unbroken tokens, as real prompts and answers have.
            token = "https://www.artic.edu/iiif/2/" + "a1b2c3d4" * 40 + "/full/843,/0/default.jpg"
            payload.setdefault("render", {})["transcript"] = [
                {"purpose": purpose, "prompt": "Candidates: " + token + " " + "word " * 3000,
                 "answer": '{"layout":"single","artworks":[{"id":"' + "x" * 300 + '"}]}'}
                for purpose in ("Choosing the art", "Describing the final layout", "Writing teaching notes")]
            db.connection.execute("UPDATE compositions SET payload = ? WHERE id = ?",
                                  (json.dumps(payload), current.id))
            db.connection.commit()
        session = {"WAYLAND_DISPLAY": os.environ["ARTWALL_TEST_WAYLAND_DISPLAY"],
                   "SWAYSOCK": os.environ["ARTWALL_TEST_SWAYSOCK"], "XDG_CURRENT_DESKTOP": "sway"}
        environment = {**os.environ, **session, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        app = subprocess.Popen([sys.executable, "-P", "-m", "artwall.cli", "gui"], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(app.kill)
        deadline = time.monotonic() + 20
        while "io.github.saithat.Artwall" not in subprocess.run(
                ["swaymsg", "-t", "get_tree"], env=environment, capture_output=True, text=True).stdout:
            if app.poll() is not None or time.monotonic() > deadline:
                app.kill()
                self.fail("The app window did not appear:\n" + (app.communicate()[0] or ""))
            time.sleep(0.2)
        window = ["gdbus", "call", "--session", "--dest", "io.github.saithat.Artwall",
                  "--object-path", "/io/github/saithat/Artwall/window/1", "--method", "org.gtk.Actions.Activate"]
        shots = os.environ.get("ARTWALL_SCREENSHOTS")
        views = (("now", None), ("next", "'next'"), ("seen", "'seen'"), ("preferences", None))
        for name, page in views:
            if name == "preferences":
                subprocess.run([*window, "preferences", "[]", "{}"], check=True, env=environment, capture_output=True)
            elif page:
                subprocess.run([*window, "show-page", f"[<{page}>]", "{}"], check=True, env=environment,
                               capture_output=True)
            time.sleep(1.5)
            self.assertIsNone(app.poll(), "The app exited while switching views")
            if shots:
                subprocess.run(["grim", f"{shots}/{name}.png"], check=True, env=environment)
            if name == "now":  # The preview keeps its size however long the page below it is.
                shot = Path(shots or self.root) / "now.png"
                if not shots:
                    subprocess.run(["grim", str(shot)], check=True, env=environment)
                self.assertGreater(_sun_height(shot), 100, "The wallpaper preview is missing or squeezed")
        # The explain overlay: full screen, then pointing at the first note's marker.
        subprocess.run(["gdbus", "call", "--session", "--dest", "io.github.saithat.Artwall", "--object-path",
                        "/io/github/saithat/Artwall", "--method", "org.gtk.Actions.Activate", "explain", "[]", "{}"],
                       check=True, env=environment, capture_output=True)
        time.sleep(2)
        self.assertIsNone(app.poll(), "The app exited while opening the explain overlay")
        tree = subprocess.run(["swaymsg", "-t", "get_tree"], env=environment, capture_output=True, text=True).stdout
        if "Explain: Harbor at Dusk" not in tree:
            app.terminate()
            self.fail("The explain overlay did not open:\n" + app.communicate(timeout=10)[0])
        if shots:
            subprocess.run(["grim", f"{shots}/explain.png"], check=True, env=environment)
        # Headless Sway has no pointer, so open the first note as its number key does.
        subprocess.run(["gdbus", "call", "--session", "--dest", "io.github.saithat.Artwall", "--object-path",
                        "/io/github/saithat/Artwall/window/2", "--method", "org.gtk.Actions.Activate",
                        "toggle-note", "[<1>]", "{}"], check=True, env=environment, capture_output=True)
        time.sleep(1.5)
        if shots:
            subprocess.run(["grim", f"{shots}/explain-note.png"], check=True, env=environment)
        app.terminate()
        output = app.communicate(timeout=10)[0]
        self.assertNotIn("Traceback", output, output)
