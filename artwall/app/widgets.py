"""Small widget helpers shared by the window and its dialogs."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import logging
from pathlib import Path
from typing import Any, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

LOG = logging.getLogger(__name__)
T = TypeVar("T")


class Background:
    """Run blocking work (SQLite, desktop commands, image decoding) off the GTK thread."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="artwall")

    def run(self, work: Callable[[], T], done: Callable[[T], object],
            failed: Callable[[Exception], object]) -> None:
        def deliver(future: Future[T]) -> None:
            def on_main_thread() -> bool:
                try:
                    result = future.result()
                except Exception as exc:  # Reported to the user; the window stays usable.
                    LOG.warning("background task failed: %s", exc)
                    failed(exc)
                else:
                    done(result)
                return False

            GLib.idle_add(on_main_thread)

        try:
            self.pool.submit(work).add_done_callback(deliver)
        except RuntimeError:  # The pool has shut down because the app is quitting.
            pass

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def label(text: str, *classes: str, wrap: bool = True, selectable: bool = False, lines: int = 0) -> Any:
    # Breaking inside long words (URLs, JSON) keeps one token from widening the whole page.
    widget = Gtk.Label(label=text, xalign=0, wrap=wrap, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=selectable,
                       css_classes=list(classes))
    if not wrap:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    if lines:
        widget.set_lines(lines)
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    return widget


def scroll_to(scroller: Any, child: Any) -> None:
    """Show a page in a scrolled window at its natural height.

    GTK's default viewport allocates a page's minimum height, and a picture that can
    shrink has none, so the wallpaper preview only got whatever the text left over.
    """
    scroller.set_child(Gtk.Viewport(child=child, vscroll_policy=Gtk.ScrollablePolicy.NATURAL))


def text_view(text: str, *classes: str, max_height: int = 360) -> Any:
    """Long read-only text in its own scrolling box, so it neither widens nor floods the page."""
    view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10,
                        bottom_margin=10, left_margin=12, right_margin=12, css_classes=list(classes))
    view.get_buffer().set_text(text)
    return Gtk.ScrolledWindow(child=view, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                              max_content_height=max_height, css_classes=["card"])


def vbox(spacing: int = 12, margin: int = 0) -> Any:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(margin)
    return box


def picture(background: Background, load: Callable[[], Path], alt: str, *classes: str,
            cover: bool = False) -> Any:
    """A picture whose image is decoded in the background, then shown."""
    widget = Gtk.Picture(alternative_text=alt, can_shrink=True, css_classes=list(classes))
    widget.set_content_fit(Gtk.ContentFit.COVER if cover else Gtk.ContentFit.CONTAIN)
    background.run(lambda: Gdk.Texture.new_from_filename(str(load())), widget.set_paintable,
                   lambda exc: widget.set_alternative_text(f"{alt} (image unavailable)"))
    return widget


def web_link(url: str) -> str | None:
    """Only museum pages over HTTP(S) open in the browser."""
    return url if url.startswith(("https://", "http://")) else None
