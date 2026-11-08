"""Best-effort detection of the display's pixel size for ``artwall init``.

Rendering never calls this: the configured size stays authoritative, so a
timer running outside the graphical session renders the same wallpaper.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable, Iterable

# (name, width, height, preferred) for each enabled output.
Output = tuple[str, int, int, bool]


def _niri(text: str) -> Iterable[Output]:
    for name, output in json.loads(text).items():
        index = output.get("current_mode")
        if index is not None:
            mode = output["modes"][index]
            yield name, mode["width"], mode["height"], False


def _hyprland(text: str) -> Iterable[Output]:
    for output in json.loads(text):
        if not output.get("disabled"):
            yield output["name"], output["width"], output["height"], bool(output.get("focused"))


def _sway(text: str) -> Iterable[Output]:
    for output in json.loads(text):
        mode = output.get("current_mode")
        if output.get("active") and mode:
            yield output["name"], mode["width"], mode["height"], bool(output.get("focused"))


def _wlr_randr(text: str) -> Iterable[Output]:
    for output in json.loads(text):
        mode = next((mode for mode in output.get("modes", []) if mode.get("current")), None)
        if output.get("enabled") and mode:
            yield output["name"], mode["width"], mode["height"], False


def _kscreen(text: str) -> Iterable[Output]:
    for output in json.loads(text).get("outputs", []):
        mode = next((mode for mode in output.get("modes", []) if mode.get("id") == output.get("currentModeId")), None)
        if output.get("enabled") and mode:
            yield output["name"], mode["size"]["width"], mode["size"]["height"], bool(output.get("priority") == 1)


_XRANDR = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+", re.M)


def _xrandr(text: str) -> Iterable[Output]:
    for name, primary, width, height in _XRANDR.findall(text):
        yield name, int(width), int(height), bool(primary)


_PROBES: tuple[tuple[list[str], Callable[[str], Iterable[Output]]], ...] = (
    (["niri", "msg", "--json", "outputs"], _niri),
    (["hyprctl", "-j", "monitors"], _hyprland),
    (["swaymsg", "-t", "get_outputs", "--raw"], _sway),
    (["kscreen-doctor", "-j"], _kscreen),
    (["wlr-randr", "--json"], _wlr_randr),
    (["xrandr", "--query"], _xrandr),
)


def _choose(outputs: list[Output], preferred_name: str | None) -> tuple[int, int] | None:
    if not outputs:
        return None
    if preferred_name:
        named = [output for output in outputs if output[0] == preferred_name]
        if named:
            return named[0][1], named[0][2]
    # The focused or primary output, else the largest by area.
    chosen = max(outputs, key=lambda output: (output[3], output[1] * output[2]))
    return chosen[1], chosen[2]


def detect_display_size(output: str | None = None) -> tuple[int, int] | None:
    """Return the pixel size of ``output`` (or the main display), or None if unknown."""
    for command, parse in _PROBES:
        if shutil.which(command[0]) is None:
            continue
        try:
            result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=5)
            found: list[Any] = list(parse(result.stdout))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            continue
        size = _choose([item for item in found if item[1] > 0 and item[2] > 0], output)
        if size:
            return size
    return None
