"""Generate user timers from the active artwall configuration."""

from __future__ import annotations

from pathlib import Path
import subprocess
import json
from typing import Any, Callable

from artwall.config import Settings, config_home


def user_unit_dir() -> Path:
    return config_home() / "systemd" / "user"


def unit_contents(settings: Settings, executable: str) -> dict[str, str]:
    if not executable.startswith("/") or any(ch in executable for ch in "\n\r\x00"):
        raise ValueError("Systemd needs an absolute artwall executable path")
    if " " in executable:
        raise ValueError("Systemd executable path may not contain spaces")
    return {
        "artwall-login.service": f"""[Unit]
Description=Advance Artwall at login when rotation is due
PartOf=graphical-session.target
After=graphical-session.target
StartLimitIntervalSec=2min
StartLimitBurst=12

[Service]
Type=oneshot
ExecStart={executable} next --if-due
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=graphical-session.target
""",
        "artwall-rotate.service": f"""[Unit]
Description=Rotate to the next cached museum wallpaper
After=graphical-session.target

[Service]
Type=oneshot
ExecStart={executable} next
""",
        "artwall-rotate.timer": f"""[Unit]
Description=Rotate museum wallpaper {_schedule(settings)[0]}
PartOf=graphical-session.target
After=graphical-session.target

[Timer]
{_schedule(settings)[1]}
Unit=artwall-rotate.service

[Install]
WantedBy=graphical-session.target
""",
        "artwall-curate.service": f"""[Unit]
Description=Keep museum wallpapers prepared ahead of rotation
After=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=2h
ExecStart=-{executable} refresh --if-stale
ExecStart={executable} curate
""",
        "artwall-curate.timer": """[Unit]
Description=Top up prepared museum wallpapers hourly

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=5min
Unit=artwall-curate.service

[Install]
WantedBy=timers.target
""",
    }


def _schedule(settings: Settings) -> tuple[str, str]:
    """The timer's description and trigger lines: a daily time, or an interval.

    A daily timer is not Persistent: at login, artwall-login.service makes the
    one catch-up change, so a missed time does not change the wallpaper twice.
    """
    if settings.rotate_at:
        return f"every day at {settings.rotate_at}", f"OnCalendar=*-*-* {settings.rotate_at}:00\nAccuracySec=1min"
    minutes = settings.interval_minutes
    return f"every {minutes} minutes", f"OnActiveSec={minutes}min\nOnUnitActiveSec={minutes}min"


def write_units(settings: Settings, directory: Path, executable: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in unit_contents(settings, executable).items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


def update_rotate_timer(
    settings: Settings,
    executable: str,
    *,
    directory: Path | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> bool:
    """Update an installed rotation timer after a GUI interval change.

    Return False when timers have not been installed or user systemd is
    unavailable. The caller's saved configuration remains valid either way.
    """
    directory = directory or user_unit_dir()
    service = directory / "artwall-rotate.service"
    timer = directory / "artwall-rotate.timer"
    if not service.is_file() or not timer.is_file():
        return False
    content = unit_contents(settings, executable)["artwall-rotate.timer"]
    timer.write_text(content, encoding="utf-8")
    try:
        runner(["systemctl", "--user", "daemon-reload"], check=True)
        runner(["systemctl", "--user", "try-restart", "artwall-rotate.timer"], check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def next_rotation(runner: Callable[..., Any] = subprocess.run) -> float | None:
    """When the installed rotation timer fires next (Unix time), or None when no timer is active."""
    try:
        result = runner(["systemctl", "--user", "list-timers", "artwall-rotate.timer", "--all", "--output=json"],
                        capture_output=True, text=True, timeout=5)
        timers = json.loads(result.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for timer in timers if isinstance(timers, list) else []:
        if isinstance(timer, dict) and timer.get("unit") == "artwall-rotate.timer":
            when = timer.get("next")
            if isinstance(when, int) and when > 0:
                return when / 1_000_000
    return None


def restart_rotation(runner: Callable[..., Any] = subprocess.run) -> None:
    """Give a wallpaper chosen by hand a full interval before the timer rotates again."""
    try:
        runner(["systemctl", "--user", "try-restart", "artwall-rotate.timer"], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


TRAY_UNIT = "artwall-tray.service"


def tray_unit(executable: str) -> str:
    if not executable.startswith("/") or any(ch in executable for ch in " \n\r\x00"):
        raise ValueError("Systemd needs an absolute artwall executable path without spaces")
    # KillMode=process: the app windows the tray opens outlive a tray restart.
    return f"""[Unit]
Description=Artwall in the system tray
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={executable} tray
Restart=on-failure
RestartSec=5s
KillMode=process

[Install]
WantedBy=graphical-session.target
"""


def set_tray(enabled: bool, executable: str, *, directory: Path | None = None,
             runner: Callable[..., Any] = subprocess.run) -> None:
    """Start the tray icon now and at every login, or stop and remove it."""
    path = (directory or user_unit_dir()) / TRAY_UNIT
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tray_unit(executable), encoding="utf-8")
        runner(["systemctl", "--user", "daemon-reload"], check=True)
        runner(["systemctl", "--user", "enable", "--now", TRAY_UNIT], check=True)
        runner(["systemctl", "--user", "restart", TRAY_UNIT], check=True)  # Picks up a new executable.
    elif path.is_file():
        runner(["systemctl", "--user", "disable", "--now", TRAY_UNIT], check=True)
        path.unlink()
        runner(["systemctl", "--user", "daemon-reload"], check=True)


def tray_enabled(directory: Path | None = None) -> bool:
    return ((directory or user_unit_dir()) / TRAY_UNIT).is_file()
