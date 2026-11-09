"""The description, reactions, works, and feedback shown for one composition."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from artwall.app.widgets import label, text_view, vbox, web_link  # noqa: E402
from artwall.gallery import Record, State  # noqa: E402
from artwall.rotation import Vote  # noqa: E402

if TYPE_CHECKING:
    from artwall.app.window import ArtwallWindow

_REACTIONS: tuple[tuple[Vote, str, str | None, str], ...] = (
    ("like", "Like", None, "Show more like this"),
    ("dislike", "Not for Me", None, "Show less like this; on your wall now, it moves to the next work"),
    ("favorite", "Favorite", "starred-symbolic", "Keep this among your favorites"),
)


def when(record: Record) -> str:
    if record.shown_at is None:
        return "COMING UP"
    return "SHOWN " + datetime.fromtimestamp(record.shown_at).strftime("%b %-d, %Y").upper()


def details(window: ArtwallWindow, record: Record, state: State) -> Any:
    box = vbox(spacing=24)
    heading = vbox(spacing=6)
    heading.append(label(record.title, "title-1", "art-title", selectable=True))
    if record.byline:
        heading.append(label(record.byline, "title-4", "dim-label", selectable=True))
    if record.companions:
        heading.append(label("With " + " · ".join(work.title for work in record.companions), "dim-label"))
    box.append(heading)
    box.append(_reactions(window, record))
    if record.description:
        box.append(label(record.description, "body", "art-description", selectable=True))
    if record.relationship:
        box.append(label(record.relationship, "dim-label", selectable=True))
    if record.notes:
        box.append(_notes(record))
    box.append(_works(window, record, state))
    box.append(_feedback(window, record))
    if record.transcript:
        box.append(_conversation(record))
    return box


def _conversation(record: Record) -> Any:
    group = Adw.PreferencesGroup(
        title="Curator Conversation",
        description="The exact requests the model received for this wallpaper and its raw answers, before "
                    "Artwall checked them. Images were sent alongside and are not shown.")
    for purpose, prompt, answer in record.transcript:
        row = Adw.ExpanderRow(title=purpose or "Request", use_markup=False,
                              subtitle=f"{len(prompt):,} characters sent")
        for heading, text in (("Prompt", prompt), ("Answer", answer)):
            body = vbox(spacing=6, margin=12)
            body.append(label(heading, "heading"))
            body.append(text_view(text, "monospace", "caption"))
            row.add_row(body)
        group.add(row)
    return group


def _reactions(window: ArtwallWindow, record: Record) -> Any:
    row = Gtk.Box(spacing=8)
    if record.shown_at is None:  # Upcoming: it can still be replaced before it is shown.
        again = Gtk.Button(tooltip_text="Replace this upcoming wallpaper with a new one using different works")
        again.set_child(Adw.ButtonContent(icon_name="view-refresh-symbolic", label="Regenerate"))
        again.connect("clicked", lambda _button: window.regenerate(record))
        row.append(again)
        row.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
    for kind, text, icon, tooltip in _REACTIONS:
        button = Gtk.ToggleButton(active=record.reaction.includes(kind), tooltip_text=tooltip,
                                  css_classes=[f"reaction-{kind}"])
        button.set_child(Adw.ButtonContent(icon_name=icon, label=text) if icon else Gtk.Label(label=text))
        button.connect("clicked", lambda _button, chosen=kind: window.vote(record, chosen))
        row.append(button)
    return row


def _notes(record: Record) -> Any:
    group = Adw.PreferencesGroup(title="What to Look For",
                                 description="The curator's notes. Explain (Ctrl+E) shows them on the wallpaper.")
    for number, note in enumerate(record.notes, start=1):
        row = Adw.ExpanderRow(title=f"{number}. {note.title}", subtitle=note.body, use_markup=False,
                              subtitle_lines=3)
        if note.source:
            row.add_row(Adw.ActionRow(title="From the museum description", subtitle=f"“{note.source}”",
                                      use_markup=False, subtitle_selectable=True))
        group.add(row)
    return group


def _works(window: ArtwallWindow, record: Record, state: State) -> Any:
    group = Adw.PreferencesGroup(title="The Work" if len(record.works) == 1 else "Works in This Composition")
    blocked = {work.id for work in state.blocked_works}
    for work in record.works:
        row = Adw.ActionRow(title=work.title, subtitle=" · ".join(filter(None, (work.artist, work.date))),
                            use_markup=False, title_lines=2)
        if url := web_link(work.url):
            museum = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open the museum page",
                                valign=Gtk.Align.CENTER, css_classes=["flat"])
            museum.connect("clicked", lambda _button, link=url: Gtk.UriLauncher(uri=link).launch(window, None, None))
            row.add_suffix(museum)
        excluded = work.id in blocked
        toggle = Gtk.Button(label="Allow" if excluded else "Exclude", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Allow this work again" if excluded else "Leave this work out of future rotation")
        toggle.connect("clicked", lambda _button, chosen=work.id, block=not excluded:
                       window.mutate(lambda: window.gallery.set_artwork_blocked(chosen, block)))
        row.add_suffix(toggle)
        group.add(row)
    return group


def _feedback(window: ArtwallWindow, record: Record) -> Any:
    group = Adw.PreferencesGroup(
        title="Feedback for the Curator",
        description="Too much empty space? An awkward pairing? Saved on this device and used when "
                    "preparing future wallpapers.")
    text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=10, bottom_margin=10,
                        left_margin=12, right_margin=12)
    text.set_size_request(-1, 96)
    buffer = text.get_buffer()
    buffer.set_text(window.drafts.get(record.id, record.comment))
    buffer.connect("changed", lambda changed: window.drafts.__setitem__(
        record.id, changed.get_text(changed.get_start_iter(), changed.get_end_iter(), False)))
    frame = Gtk.Frame(child=text)
    save = Gtk.Button(label="Save Feedback", halign=Gtk.Align.END, css_classes=["suggested-action"])
    save.connect("clicked", lambda _button: window.save_comment(
        record, buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)))
    box = vbox(spacing=12)
    box.append(frame)
    box.append(save)
    group.add(box)
    return group
