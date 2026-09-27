"""End-to-end test home with temporary XDG directories and fake desktop commands.

Commands on PATH are small shell scripts that log their arguments, so the real
subprocess and HTTP code runs without touching this machine's desktop or systemd.
"""

from __future__ import annotations

import contextlib
from http.server import HTTPServer
import io
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import patch

from PIL import Image

from artwall.cli import main
from artwall.config import data_dir, database_path
from artwall.models import Artwork, Composition, CuratorDecision, DecisionArtwork
from artwall.storage import Database

_FAKE_COMMAND = """#!/bin/sh
if [ -e "{down}" ]; then echo "ERROR: no DMS session"; exit 0; fi
printf '%s\\n' "{name} $*" >> "{log}"
"""


class HomeTest(unittest.TestCase):
    """A temporary home with an 860 × 360 display and fake `dms`, `systemctl`, and `artwall` commands."""

    config = "[display]\nwidth = 860\nheight = 360\n"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.commands_log = self.root / "commands.log"
        self.desktop_down = self.root / "desktop-down"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name in ("dms", "systemctl", "artwall"):
            script = bin_dir / name
            script.write_text(_FAKE_COMMAND.format(name=name, down=self.desktop_down, log=self.commands_log))
            script.chmod(0o755)
        environment = {f"XDG_{kind}_HOME": str(self.root / kind.lower()) for kind in ("CONFIG", "DATA", "CACHE")}
        # Only the fake commands are on PATH, and loopback HTTP never goes through a proxy.
        patcher = patch.dict(os.environ, {**environment, "PATH": str(bin_dir), "no_proxy": "*", "NO_PROXY": "*"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_file = self.root / "config" / "artwall" / "config.toml"
        self.config_file.parent.mkdir(parents=True)
        self.configure()
        self.clock = 0.0

    def configure(self, extra: str = "") -> None:
        self.config_file.write_text(self.config + extra)

    def commands(self) -> list[str]:
        return self.commands_log.read_text().splitlines() if self.commands_log.exists() else []

    def database(self) -> Database:
        return Database(database_path())

    def cli(self, *argv: str, status: int = 0) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(list(argv)), status, output.getvalue())
        return output.getvalue()

    def serve(self, server: HTTPServer) -> str:
        """Run a loopback server for this test and return its base URL."""
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def artwork(self, source_id: str, size: tuple[int, int] | None = None, **fields: Any) -> Artwork:
        """A public-domain museum record, with a cached JPEG when a size is given."""
        if size:
            path = self.root / "images" / f"{source_id}.jpg"
            path.parent.mkdir(exist_ok=True)
            Image.new("RGB", size, (60, 60, 70)).save(path, format="JPEG")
            fields = {"image_width": size[0], "image_height": size[1], "cached_image_path": str(path), **fields}
        return Artwork(**{"source": "artic", "source_id": source_id, "title": "Untitled", "artist": "Unknown",
                          "public_domain": True, "open_access": True, **fields})

    def queue(self, db: Database, identifier: str, *works: Artwork, **fields: Any) -> Composition:
        """Store the works and a prepared single or multi-work wallpaper of them at the end of the queue."""
        for art in works:
            db.upsert_artwork(art)
        self.clock += 1
        image = data_dir() / "compositions" / f"{identifier}.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (860, 360), "#282828").save(image)
        layout = fields.pop("layout", ("near_single", "diptych_50", "triptych")[len(works) - 1])
        chosen = CuratorDecision(layout, works[0].id, tuple(DecisionArtwork(art.id) for art in works), **fields)
        composition = Composition(identifier, chosen, str(image), created_at=self.clock)
        db.add_composition(composition)
        return composition
