"""Linux wallpaper setters.

The rendered composition is supplied by the caller.  This module never edits
the pixels or keeps a second copy of the image.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
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


def run_backend_command(command: list[str]) -> str:
    """Run a desktop command and return its standard output."""
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
    return result.stdout


def current_desktops() -> set[str]:
    """Lower-case names from ``XDG_CURRENT_DESKTOP`` (for example ``{"ubuntu", "gnome"}``)."""
    return {name.strip().lower() for name in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if name.strip()}


class WallpaperBackend(ABC):
    executable: str = ""
    # XDG_CURRENT_DESKTOP names whose own background this backend controls.
    desktops: tuple[str, ...] = ()

    def __init__(self, output: str | None = None) -> None:
        self.output = output

    @classmethod
    def available(cls) -> bool:
        return shutil.which(cls.executable) is not None

    @abstractmethod
    def set(self, path: str | Path) -> None:
        """Show an existing rendered composition."""


class DmsBackend(WallpaperBackend):
    """DankMaterialShell's built-in wallpaper (Niri, Hyprland, and other compositors)."""

    executable = "dms"

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


class SwayBackend(WallpaperBackend):
    """Sway's built-in background, which starts swaybg for each output."""

    executable = "swaymsg"
    desktops = ("sway",)

    def set(self, path: str | Path) -> None:
        run_backend_command(["swaymsg", "output", self.output or "*", "bg", str(image_path(path)), "fill"])


class GnomeBackend(WallpaperBackend):
    """GNOME, Budgie, and Unity; GNOME's lock screen follows this background."""

    executable = "gsettings"
    desktops: tuple[str, ...] = ("gnome", "budgie", "unity")
    schema = "org.gnome.desktop.background"
    keys: tuple[str, ...] = ("picture-uri", "picture-uri-dark")

    def set(self, path: str | Path) -> None:
        uri = image_path(path).as_uri()
        supported = set(run_backend_command(["gsettings", "list-keys", self.schema]).split())
        keys = [key for key in self.keys if key in supported] or [self.keys[0]]
        for key in keys:
            run_backend_command(["gsettings", "set", self.schema, key, uri])


class CinnamonBackend(GnomeBackend):
    desktops = ("x-cinnamon", "cinnamon")
    schema = "org.cinnamon.desktop.background"
    keys = ("picture-uri",)


class MateBackend(WallpaperBackend):
    executable = "gsettings"
    desktops = ("mate",)

    def set(self, path: str | Path) -> None:
        run_backend_command(["gsettings", "set", "org.mate.background", "picture-filename", str(image_path(path))])


class KdeBackend(WallpaperBackend):
    """KDE Plasma 5.24 and later; applies to every desktop containment."""

    executable = "plasma-apply-wallpaperimage"
    desktops = ("kde",)

    def set(self, path: str | Path) -> None:
        run_backend_command(["plasma-apply-wallpaperimage", str(image_path(path))])


class XfceBackend(WallpaperBackend):
    """Xfce sets each monitor and workspace that already has a backdrop image."""

    executable = "xfconf-query"
    desktops = ("xfce",)

    def set(self, path: str | Path) -> None:
        target = str(image_path(path))
        properties = run_backend_command(["xfconf-query", "-c", "xfce4-desktop", "-l"]).split()
        properties = [name for name in properties if name.endswith("/last-image")
                      and (not self.output or f"/monitor{self.output}/" in name)]
        if not properties:
            raise DesktopBackendError("No Xfce backdrop found; set a wallpaper once in Desktop settings")
        for name in properties:
            run_backend_command(["xfconf-query", "-c", "xfce4-desktop", "-p", name, "-s", target])


class FehBackend(WallpaperBackend):
    """X11 window managers such as i3, bspwm, and Openbox; ``~/.fehbg`` restores it at login."""

    executable = "feh"

    @classmethod
    def available(cls) -> bool:
        return super().available() and bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY")

    def set(self, path: str | Path) -> None:
        run_backend_command(["feh", "--bg-fill", str(image_path(path))])


_BACKENDS: dict[str, type[WallpaperBackend]] = {
    "dms": DmsBackend, "awww": AwwwBackend, "swww": SwwwBackend, "sway": SwayBackend,
    "gnome": GnomeBackend, "cinnamon": CinnamonBackend, "mate": MateBackend, "kde": KdeBackend,
    "xfce": XfceBackend, "feh": FehBackend,
}
# For "auto" outside a recognized desktop environment. DMS comes first because it
# draws its own wallpaper layer, which would otherwise cover a swww image.
_STANDALONE = ("dms", "awww", "swww", "feh")


def wallpaper_backend_name() -> str:
    """The backend "auto" selects: the running desktop's own, else an installed tool."""
    desktops = current_desktops()
    for name, backend in _BACKENDS.items():
        if desktops.intersection(backend.desktops) and backend.available():
            return name
    return next((name for name in _STANDALONE if _BACKENDS[name].available()), "")


def wallpaper_backend(name: str = "auto", *, output: str | None = None) -> WallpaperBackend:
    """Select a setter by name, or the one for this session with "auto"."""
    if name == "auto":
        name = wallpaper_backend_name()
        if not name:
            raise DesktopBackendError(
                "No wallpaper backend found; set [wallpaper].backend (see README for the supported list)")
    if name not in _BACKENDS:
        raise DesktopBackendError(f"Unknown wallpaper backend: {name} (choose from {', '.join(_BACKENDS)})")
    return _BACKENDS[name](output)
