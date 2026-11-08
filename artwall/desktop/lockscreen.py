"""Lock-screen image integration kept separate from wallpaper setting."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
import re
import shutil
from typing import Callable

from artwall.config import cache_dir, config_home, write_atomic

from .wallpaper import DesktopBackendError, current_desktops, image_path, run_backend_command


class LockScreenBackend(ABC):
    @abstractmethod
    def configure(self, path: str | Path) -> None:
        """Make the next lock screen use this existing composition."""


class DmsLockScreenBackend(LockScreenBackend):
    """Set the image used by the DMS locker via its documented settings IPC."""

    def configure(self, path: str | Path) -> None:
        run_backend_command(
            [
                "dms",
                "ipc",
                "call",
                "settings",
                "set",
                "lockScreenWallpaperPath",
                str(image_path(path)),
            ]
        )


class HyprlockBackend(LockScreenBackend):
    """Point Hyprlock at the stable ``current.png`` link once.

    The caller may pass an immutable composition to ``configure``. Hyprlock
    reads the canonical cache link when it starts, so subsequent rotations do
    not require a config rewrite or a signal to a running locker.
    """

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        current_path: str | Path | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path is not None else config_home() / "hypr" / "hyprlock.conf"
        self.current_path = Path(current_path) if current_path is not None else cache_dir() / "current.png"

    def configure(self, path: str | Path) -> None:
        image_path(path)
        if shutil.which("hyprlock") is None:
            raise DesktopBackendError("Desktop command is not installed: hyprlock")
        config = self.config_path.expanduser().absolute()
        if config.is_symlink():
            raise DesktopBackendError(f"Hyprlock config is a symlink; set its background path manually: {config}")
        stable_path = str(self.current_path.expanduser().absolute())
        background = (
            "background {\n"
            "    monitor =\n"
            f"    path = {stable_path}\n"
            "    blur_passes = 0\n"
            "}\n"
        )
        if config.exists():
            original = config.read_text(encoding="utf-8")
            pattern = re.compile(r"(?ms)^([ \t]*background[ \t]*\{)(.*?)(^[ \t]*\})")
            matches = list(pattern.finditer(original))
            if matches:
                def update_block(match: re.Match[str]) -> str:
                    body = match.group(2)
                    path_line = re.compile(r"(?m)^([ \t]*path[ \t]*=)[^\n]*$")
                    if path_line.search(body):
                        body = path_line.sub(lambda found: f"{found.group(1)} {stable_path}", body)
                    else:
                        body = f"\n    path = {stable_path}" + body
                    return match.group(1) + body + match.group(3)

                updated = pattern.sub(update_block, original)
            elif re.search(r"(?m)^[ \t]*source[ \t]*=", original):
                raise DesktopBackendError(
                    f"Hyprlock config sources other files; add a background path to {stable_path} manually: {config}"
                )
            else:
                updated = original.rstrip() + "\n\n" + background
        else:
            updated = (
                "# Managed by artwall. Hyprlock reads current.png when it starts.\n"
                + background
                + "\ninput-field {\n"
                + "    monitor =\n"
                + "    size = 300, 48\n"
                + "    position = 0, 0\n"
                + "    halign = center\n"
                + "    valign = center\n"
                + "    inner_color = rgba(29, 32, 33, 0.75)\n"
                + "    outer_color = rgb(215, 153, 33)\n"
                + "    font_color = rgb(235, 219, 178)\n"
                + "}\n"
            )
            original = ""
        if updated != original:
            write_atomic(config, updated)


class SwaylockBackend(LockScreenBackend):
    """Point swaylock's config at the stable ``current.png`` link once."""

    def __init__(self, *, config_path: str | Path | None = None, current_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path is not None else config_home() / "swaylock" / "config"
        self.current_path = Path(current_path) if current_path is not None else cache_dir() / "current.png"

    def configure(self, path: str | Path) -> None:
        image_path(path)
        if shutil.which("swaylock") is None:
            raise DesktopBackendError("Desktop command is not installed: swaylock")
        config = self.config_path.expanduser().absolute()
        if config.is_symlink():
            raise DesktopBackendError(f"swaylock config is a symlink; set its image path manually: {config}")
        line = f"image={self.current_path.expanduser().absolute()}"
        original = config.read_text(encoding="utf-8") if config.exists() else ""
        pattern = re.compile(r"(?m)^[ \t]*image[ \t]*=[^\n]*$")
        if pattern.search(original):
            updated = pattern.sub(lambda _: line, original)
        else:
            updated = (original.rstrip() + "\n" if original.strip() else "") + line + "\n"
        if updated != original:
            write_atomic(config, updated)


class KdeLockScreenBackend(LockScreenBackend):
    """Set the Plasma lock-screen image through ``kscreenlockerrc``."""

    def configure(self, path: str | Path) -> None:
        tool = next((name for name in ("kwriteconfig6", "kwriteconfig5") if shutil.which(name)), "kwriteconfig6")
        groups = [arg for group in ("Greeter", "Wallpaper", "org.kde.image", "General") for arg in ("--group", group)]
        run_backend_command([tool, "--file", "kscreenlockerrc", *groups, "--key", "Image", image_path(path).as_uri()])


class NoLockScreenBackend(LockScreenBackend):
    """For lock screens that follow the wallpaper (GNOME, Cinnamon) or keep their own image."""

    def configure(self, path: str | Path) -> None:
        return None


_LOCKERS: dict[str, Callable[[], LockScreenBackend]] = {
    "dms": DmsLockScreenBackend, "hyprlock": HyprlockBackend, "swaylock": SwaylockBackend,
    "kde": KdeLockScreenBackend, "none": NoLockScreenBackend, "off": NoLockScreenBackend,
}


def lock_screen_backend_name() -> str:
    """The locker "auto" selects; "none" when the lock screen needs no separate image."""
    if "kde" in current_desktops() and (shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")):
        return "kde"
    return next((name for name in ("dms", "hyprlock", "swaylock") if shutil.which(name)), "none")


def lock_screen_backend(name: str = "auto") -> LockScreenBackend:
    if name == "auto":
        name = lock_screen_backend_name()
    if name not in _LOCKERS:
        raise DesktopBackendError(f"Unknown lock-screen backend: {name} (choose from {', '.join(_LOCKERS)})")
    return _LOCKERS[name]()
