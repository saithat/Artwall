"""Install the native app's launcher entry and icon for the current user."""

from __future__ import annotations

from pathlib import Path

from artwall.config import data_home, write_atomic

APP_ID = "io.github.saithat.Artwall"
DATA = Path(__file__).parent / "app" / "data"
_ICON = Path("icons") / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"


def install_launcher(executable: str) -> list[Path]:
    """Write the desktop entry, pointing at this artwall executable, and its icon."""
    if not executable.startswith("/") or any(ch in executable for ch in " \n\r\x00"):
        raise ValueError("The launcher needs an absolute artwall path without spaces")
    entry = (DATA / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    entry = entry.replace("Exec=artwall gui\n", f"Exec={executable} gui\n", 1)
    desktop = data_home() / "applications" / f"{APP_ID}.desktop"
    icon = data_home() / _ICON
    write_atomic(desktop, entry)
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes((DATA / _ICON).read_bytes())
    return [desktop, icon]
