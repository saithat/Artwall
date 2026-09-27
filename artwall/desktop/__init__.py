"""Desktop and lock-screen adapters for rendered artwall compositions."""

from .lockscreen import DmsLockScreenBackend, HyprlockBackend, LockScreenBackend, NoLockScreenBackend, lock_screen_backend
from .wallpaper import (
    AwwwBackend,
    DesktopBackendError,
    DmsBackend,
    SwwwBackend,
    WallpaperBackend,
    wallpaper_backend,
)

__all__ = [
    "AwwwBackend",
    "DesktopBackendError",
    "DmsBackend",
    "DmsLockScreenBackend",
    "HyprlockBackend",
    "LockScreenBackend",
    "NoLockScreenBackend",
    "SwwwBackend",
    "WallpaperBackend",
    "lock_screen_backend",
    "wallpaper_backend",
]
