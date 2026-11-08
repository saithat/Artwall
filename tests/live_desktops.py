"""Desktop backends against real tools, inside the scripts/check-desktops container.

Each test shows a prepared wallpaper through the CLI with ``auto`` backends, then
reads the setting back with the desktop's own tool. Run via ``scripts/check-desktops``.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from artwall.desktop import lock_screen_backend
from tests.support import HomeTest

_REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")


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
