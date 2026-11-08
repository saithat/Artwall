"""`artwall explain`: the wallpaper full screen, with the curator's notes on it.

The window shows the current composition image itself rather than a transparent
layer over the desktop, so it works on every desktop and over open windows.
Markers sit on each note's region; pointing at one outlines the detail and
opens its note, and a click or the note's number key keeps it open.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from artwall.app.widgets import Background, label, vbox  # noqa: E402
from artwall.gallery import Record  # noqa: E402
from artwall.models import Note  # noqa: E402

_HIGHLIGHT = Gdk.RGBA()
_HIGHLIGHT.parse("rgba(255, 244, 214, 0.95)")
_SHADE = Gdk.RGBA()
_SHADE.parse("rgba(0, 0, 0, 0.35)")


class NoteLayer(Gtk.Widget):  # type: ignore[misc]
    """The wallpaper, scaled to fit, with a marker at each note and one note card.

    The card is drawn inside this widget rather than as a popover, so opening it
    never moves the pointer onto another surface and closes it again.
    """

    _CARD_WIDTH = 380
    _GAP = 16

    def __init__(self, record: Record, background: Background) -> None:
        super().__init__(hexpand=True, vexpand=True, focusable=False)
        self.record = record
        self.texture: Any = None
        self.active: Note | None = None
        self.pinned: Note | None = None
        self.markers: list[tuple[Note, Any]] = []
        for number, note in enumerate(record.notes, start=1):
            self._add_marker(number, note)
        self.card = vbox(spacing=8)
        self.card.add_css_class("explain-card")
        self.card_title = label("", "heading")
        self.card_body = label("", "body")
        self.card_source = label("", "caption", "dim-label")
        for child in (self.card_title, self.card_body, self.card_source):
            self.card.append(child)
        self.card.set_parent(self)
        self.card.set_visible(False)
        background.run(lambda: Gdk.Texture.new_from_filename(str(record.image_path)), self._loaded, lambda exc: None)

    def _loaded(self, texture: Any) -> None:
        self.texture = texture
        self.queue_allocate()
        self.queue_draw()

    def _add_marker(self, number: int, note: Note) -> None:
        marker = Gtk.Button(label=str(number), css_classes=["explain-marker", "circular"], tooltip_text=note.title)
        marker.update_property([Gtk.AccessibleProperty.LABEL], [f"Note {number}: {note.title}"])
        marker.set_parent(self)
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: self.show_note(note))
        hover.connect("leave", lambda *_: self.show_note(self.pinned))
        marker.add_controller(hover)
        marker.connect("clicked", lambda _button: self.pin(note))  # Also Enter or Space after Tab.
        self.markers.append((note, marker))

    def show_note(self, note: Note | None) -> None:
        """Outline a note's detail and show its card; None hides the card."""
        self.active = note
        if note is not None:
            self.card_title.set_label(note.title)
            self.card_body.set_label(note.body)
            self.card_source.set_label(f"“{note.source}” (museum description)" if note.source else "")
            self.card_source.set_visible(bool(note.source))
        self.card.set_visible(note is not None)
        self.queue_allocate()
        self.queue_draw()

    def pin(self, note: Note) -> None:
        self.pinned = None if self.pinned is note else note
        self.show_note(self.pinned)

    def _image_rect(self) -> tuple[float, float, float, float]:
        """Where the wallpaper is drawn: scaled to fit, centered (x, y, width, height)."""
        width, height = self.get_width(), self.get_height()
        if self.texture is None or not width or not height:
            return 0, 0, width, height
        scale = min(width / self.texture.get_width(), height / self.texture.get_height())
        shown_w, shown_h = self.texture.get_width() * scale, self.texture.get_height() * scale
        return (width - shown_w) / 2, (height - shown_h) / 2, shown_w, shown_h

    def _note_rect(self, note: Note) -> tuple[float, float, float, float]:
        x, y, width, height = self._image_rect()
        box = note.box
        return x + box.x * width, y + box.y * height, box.width * width, box.height * height

    def do_measure(self, orientation: Any, for_size: int) -> tuple[int, int, int, int]:
        return 0, 0, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        for note, marker in self.markers:
            x, y, w, h = self._note_rect(note)
            size = max(marker.measure(Gtk.Orientation.HORIZONTAL, -1)[1], 36)
            marker.size_allocate(_rect(min(max(x + w / 2 - size / 2, 8), width - size - 8),
                                       min(max(y + h / 2 - size / 2, 8), height - size - 8), size, size), -1)
        if self.active is not None and self.card.get_visible():
            # Beside the detail, on whichever side has room, kept on screen.
            card_w = min(self._CARD_WIDTH, width - 2 * self._GAP)
            card_h = self.card.measure(Gtk.Orientation.VERTICAL, card_w)[1]
            x, y, w, h = self._note_rect(self.active)
            left = x + w + self._GAP if x + w + self._GAP + card_w <= width - self._GAP else x - self._GAP - card_w
            left = min(max(left, self._GAP), width - card_w - self._GAP)
            top = min(max(y + h / 2 - card_h / 2, self._GAP), height - card_h - self._GAP)
            self.card.size_allocate(_rect(left, top, card_w, card_h), -1)

    def do_snapshot(self, snapshot: Any) -> None:
        if self.texture is not None:
            x, y, width, height = self._image_rect()
            snapshot.append_texture(self.texture, Graphene.Rect().init(x, y, width, height))
        if self.active is not None:
            x, y, width, height = self._note_rect(self.active)
            outline = Gsk.RoundedRect()
            outline.init_from_rect(Graphene.Rect().init(x, y, width, height), 10)
            snapshot.append_outset_shadow(outline, _SHADE, 0, 0, 4000, 0)  # Dim everything else.
            snapshot.append_border(outline, [3, 3, 3, 3], [_HIGHLIGHT] * 4)
        for _note, marker in self.markers:
            self.snapshot_child(marker, snapshot)
        if self.card.get_visible():
            self.snapshot_child(self.card, snapshot)

    def do_dispose(self) -> None:
        for _note, marker in self.markers:
            marker.unparent()
        self.markers = []
        self.card.unparent()
        Gtk.Widget.do_dispose(self)


def _rect(x: float, y: float, width: float, height: float) -> Any:
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), int(width), int(height)
    return rect


class ExplainWindow(Adw.ApplicationWindow):  # type: ignore[misc]
    def __init__(self, app: Any, record: Record, background: Background) -> None:
        super().__init__(application=app, title=f"Explain: {record.title}", css_classes=["explain"])
        self.layer = NoteLayer(record, background)
        overlay = Gtk.Overlay()
        overlay.set_child(self.layer)
        overlay.add_overlay(self._about(record))
        self.set_content(overlay)
        # Number keys toggle notes, so they are reachable without a pointer.
        toggle = Gio.SimpleAction.new("toggle-note", GLib.VariantType.new("i"))
        toggle.connect("activate", lambda _action, number: self.toggle_note(number.get_int32()))
        self.add_action(toggle)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.fullscreen()

    def toggle_note(self, number: int) -> None:
        if 1 <= number <= len(self.layer.markers):
            self.layer.pin(self.layer.markers[number - 1][0])

    def _key(self, _controller: Any, keyval: int, _code: int, _state: Any) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            self.toggle_note(keyval - Gdk.KEY_0)
            return True
        return False

    def _about(self, record: Record) -> Any:
        card = vbox(spacing=8)
        card.add_css_class("explain-card")
        card.set_halign(Gtk.Align.START)
        card.set_valign(Gtk.Align.END)
        card.set_margin_start(32)
        card.set_margin_bottom(32)
        card.set_size_request(380, -1)
        heading = Gtk.Box(spacing=12)
        title = label(record.title, "title-2", "art-title")
        title.set_hexpand(True)
        heading.append(title)
        card.append(heading)
        if record.byline:
            card.append(label(record.byline, "dim-label"))
        if record.companions:
            card.append(label("With " + " · ".join(work.title for work in record.companions), "dim-label"))
        details = vbox(spacing=8)
        if record.description:
            details.append(label(record.description, "body"))
        if record.relationship:
            details.append(label(record.relationship, "caption", "dim-label"))
        if record.notes:
            hint = (f"{len(record.notes)} things to look for. Point at a marker, or press its number to keep the "
                    "note open. Esc closes.")
        else:
            hint = "No curator notes for this wallpaper. Regenerate upcoming wallpapers to get notes. Esc closes."
        details.append(label(hint, "caption", "dim-label"))
        revealer = Gtk.Revealer(child=details, reveal_child=True,
                                transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        toggle = Gtk.Button(icon_name="pan-down-symbolic", valign=Gtk.Align.START, css_classes=["flat", "circular"],
                            tooltip_text="Hide or show the description")
        toggle.connect("clicked", lambda button: (revealer.set_reveal_child(not revealer.get_reveal_child()),
                                                  button.set_icon_name("pan-down-symbolic" if revealer.get_reveal_child()
                                                                       else "pan-up-symbolic")))
        heading.append(toggle)
        card.append(revealer)
        return card


CSS = """
window.explain { background-color: black; }
.explain-marker { min-width: 36px; min-height: 36px; padding: 0; font-weight: 800;
                  background-color: rgba(255, 244, 214, 0.92); color: #1d2021;
                  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.55); }
.explain-marker:hover, .explain-marker:focus-visible { background-color: #ffffff; }
.explain-card { background-color: rgba(20, 20, 20, 0.82); color: white; border-radius: 16px; padding: 18px 20px; }
"""


def open_explain(app: Any, record: Record | None, background: Background) -> None:
    if record is None:
        dialog = Adw.AlertDialog(heading="Nothing on Your Wall Yet",
                                 body="Run artwall next to show a prepared wallpaper first.")
        dialog.add_response("ok", "OK")
        dialog.present(app.get_active_window())
        return
    window = ExplainWindow(app, record, background)
    window.present()
    GLib.idle_add(lambda: window.grab_focus() and False)
