"""Desktop and lock-screen adapters for rendered artwall compositions."""

from .display import detect_display_size
from .lockscreen import LockScreenBackend, lock_screen_backend, lock_screen_backend_name
from .wallpaper import DesktopBackendError, WallpaperBackend, wallpaper_backend, wallpaper_backend_name

__all__ = [
    "DesktopBackendError",
    "LockScreenBackend",
    "WallpaperBackend",
    "detect_display_size",
    "lock_screen_backend",
    "lock_screen_backend_name",
    "wallpaper_backend",
    "wallpaper_backend_name",
]
