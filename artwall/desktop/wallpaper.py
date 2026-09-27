"""Wayland wallpaper setters.

The rendered composition is supplied by the caller.  This module never edits
the pixels or keeps a second copy of the image.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
import shutil
import subprocess


class DesktopBackendError(RuntimeError):
    """A desktop integration command could not apply a composition."""


def image_path(path: str | Path) -> Path:
    """Return an absolute existing image path, following ``current.png`` links.

    Passing the resolved, immutable composition path also makes shells that
    cache an image by filename notice every rotation.
    """

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise DesktopBackendError(f"Wallpaper image does not exist: {resolved}")
    return resolved


def run_backend_command(command: list[str]) -> None:
    if shutil.which(command[0]) is None:
        raise DesktopBackendError(f"Desktop command is not installed: {command[0]}")
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()
        raise DesktopBackendError(
            f"Desktop command failed: {command[0]}" + (f": {detail}" if detail else "")
        ) from exc
    # Some IPC clients print an error while returning status zero.
    if result.stdout.lstrip().startswith("ERROR:"):
        raise DesktopBackendError(f"Desktop command failed: {result.stdout.strip()}")


class WallpaperBackend(ABC):
    def __init__(self, output: str | None = None) -> None:
        self.output = output

    @abstractmethod
    def set(self, path: str | Path) -> None:
        """Show an existing rendered composition."""


class DmsBackend(WallpaperBackend):
    """DankMaterialShell's built-in wallpaper, used by this Niri setup."""

    def set(self, path: str | Path) -> None:
        target = str(image_path(path))
        if self.output:
            run_backend_command(["dms", "ipc", "call", "wallpaper", "setFor", self.output, target])
        else:
            run_backend_command(["dms", "ipc", "call", "wallpaper", "set", target])


class SwwwBackend(WallpaperBackend):
    """The swww Wayland daemon; its daemon must already be running."""

    executable = "swww"

    def set(self, path: str | Path) -> None:
        outputs = ["--outputs", self.output] if self.output else []
        run_backend_command([self.executable, "img", *outputs, str(image_path(path))])


class AwwwBackend(SwwwBackend):
    """The awww Wayland daemon, which shares swww's command line."""

    executable = "awww"


# In order of preference for "auto": DMS avoids two wallpaper layers.
_BACKENDS: dict[str, type[WallpaperBackend]] = {"dms": DmsBackend, "awww": AwwwBackend, "swww": SwwwBackend}


def wallpaper_backend(name: str = "auto", *, output: str | None = None) -> WallpaperBackend:
    """Select a setter by name, or the first installed one for "auto"."""
    if name == "auto":
        name = next((candidate for candidate in _BACKENDS if shutil.which(candidate)), "")
        if not name:
            raise DesktopBackendError("No wallpaper backend found (install DMS, awww, or swww)")
    if name not in _BACKENDS:
        raise DesktopBackendError(f"Unknown wallpaper backend: {name}")
    return _BACKENDS[name](output)
