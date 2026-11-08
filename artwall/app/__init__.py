"""The native GTK 4 and libadwaita gallery.

PyGObject, GTK 4, and libadwaita come from the system (for example Fedora's
python3-gobject, gtk4, and libadwaita), so artwall must run in a Python
environment that can see system packages. Everything the window does goes
through ``artwall.gallery``, which is tested without a graphical session.
"""

from __future__ import annotations

from collections.abc import Sequence

INSTALL_HINT = (
    "The Artwall app needs PyGObject with GTK 4 and libadwaita 1.5 or newer. Install them from your "
    "distribution (Fedora: python3-gobject gtk4 libadwaita; Debian/Ubuntu: python3-gi gir1.2-gtk-4.0 "
    "gir1.2-adw-1; Arch: python-gobject gtk4 libadwaita), and run artwall from a Python environment with "
    "access to system packages (see README: Install)."
)


def run(argv: Sequence[str]) -> int:
    """Start the app, or reach the running one: ``--explain`` opens the explain overlay."""
    try:
        from artwall.app.window import ArtwallApplication
    except (ImportError, ValueError) as exc:  # gi missing, or GTK/Adw typelibs unavailable
        raise RuntimeError(f"{INSTALL_HINT} ({exc})") from exc
    return int(ArtwallApplication().run(list(argv)))
