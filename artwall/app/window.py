"""Artwall's main window: the work on your wall, what comes next, and what you have seen."""

from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
import logging
import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from artwall.app.details import details, when  # noqa: E402
from artwall.app.explain import CSS as EXPLAIN_CSS, open_explain  # noqa: E402
from artwall.app.preferences import Preferences  # noqa: E402
from artwall.app.widgets import Background, label, picture, vbox  # noqa: E402
from artwall.gallery import Gallery, Record, State  # noqa: E402
from artwall.launcher import APP_ID, DATA  # noqa: E402
from artwall.rotation import Vote  # noqa: E402

LOG = logging.getLogger(__name__)

_CSS = """
.artwork { border-radius: 12px; }
.art-card picture { border-radius: 12px 12px 0 0; }
.art-frame { margin: 0; }
.art-title { font-family: "Noto Serif", Georgia, serif; }
.art-description { font-size: 1.08em; line-height: 1.45; }
/* Reactions use the theme's success and error colors: tinted when off, solid when chosen. */
button.reaction-like { color: @success_color; }
button.reaction-like:checked { background-color: @success_bg_color; color: @success_fg_color; }
button.reaction-dislike { color: @error_color; }
button.reaction-dislike:checked { background-color: @error_bg_color; color: @error_fg_color; }
"""


class ArtwallApplication(Adw.Application):  # type: ignore[misc]
    def __init__(self) -> None:
        # Command lines reach the running instance, so `artwall explain` opens there.
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.gallery = Gallery()
        self.background = Background()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            # The icon also resolves before `artwall install-launcher` has run.
            Gtk.IconTheme.get_for_display(display).add_search_path(str(DATA / "icons"))
            css = Gtk.CssProvider()
            css.load_from_string(_CSS + EXPLAIN_CSS)
            Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_name(APP_ID)
        for name, callback, accels in (("quit", lambda *_: self.quit(), ["<Control>q"]),
                                       ("about", lambda *_: self._about(), []),
                                       ("explain", lambda *_: self.explain(), ["<Control>e"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.refresh", ["<Control>r", "F5"])
        self.set_accels_for_action("win.preferences", ["<Control>comma"])
        for index, page in enumerate(("now", "next", "seen"), start=1):
            self.set_accels_for_action(f"win.show-page::{page}", [f"<Alt>{index}"])

    def do_activate(self) -> None:
        window = next((w for w in self.get_windows() if isinstance(w, ArtwallWindow)), None) or ArtwallWindow(self)
        window.present()

    def do_command_line(self, command_line: Any) -> int:
        if "--explain" in command_line.get_arguments()[1:]:
            self.explain()
        else:
            self.activate()
        return 0

    def explain(self) -> None:
        """Show the current wallpaper full screen with the curator's notes."""
        self.hold()  # Keep a command-line-only instance alive while the state loads.

        def show(record: Record | None) -> None:
            open_explain(self, record, self.background)
            self.release()

        def failed(exc: Exception) -> None:
            LOG.warning("explain failed: %s", exc)
            self.release()

        self.background.run(lambda: self.gallery.state().current, show, failed)

    def do_shutdown(self) -> None:
        self.background.shutdown()
        Adw.Application.do_shutdown(self)

    def _about(self) -> None:
        try:
            version = metadata.version("artwall")
        except metadata.PackageNotFoundError:
            version = ""
        about = Adw.AboutDialog(
            application_name="Artwall", application_icon=APP_ID, version=version,
            comments="Real public-domain art on your wall, chosen and explained by a vision-model curator.",
            license_type=Gtk.License.MIT_X11, developer_name="Artwall contributors")
        about.add_credit_section("Artwork, images, and museum records", ["Art Institute of Chicago https://www.artic.edu"])
        about.present(self.get_active_window())


class ArtwallWindow(Adw.ApplicationWindow):  # type: ignore[misc]
    def __init__(self, app: ArtwallApplication) -> None:
        super().__init__(application=app, title="Artwall", default_width=1180, default_height=860)
        self.gallery: Gallery = app.gallery
        self.background: Background = app.background
        self.state: State | None = None
        self.drafts: dict[str, str] = {}  # Unsaved curator feedback, kept across refreshes.
        self.busy = False
        self.dialog: Any = None
        self.dialog_record: str | None = None
        self.preferences: Preferences | None = None
        self.prepared = 0  # Wallpapers the running preparation has finished, as last shown.
        self.polling = False
        self.next_time: float | None = None  # When the rotation timer fires next.
        self.countdown: Any = None
        GLib.timeout_add_seconds(1, self._tick)
        GLib.timeout_add_seconds(60, self._refresh_next_time)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        menu.append("Refresh", "win.refresh")
        menu.append("Preferences", "win.preferences")
        menu.append("About Artwall", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)

        self.now = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.coming = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.seen = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_titled_with_icon(self.now, "now", "On Your Wall", "image-x-generic-symbolic")
        self.coming_page = self.stack.add_titled_with_icon(self._coming_shell(), "next", "Coming Next",
                                                           "view-grid-symbolic")
        self.stack.add_titled_with_icon(self.seen, "seen", "Recently Seen", "document-open-recent-symbolic")

        for name, callback in (("refresh", lambda *_: self.reload()),
                               ("preferences", lambda *_: self.show_preferences())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        show_page = Gio.SimpleAction.new("show-page", GLib.VariantType.new("s"))
        show_page.connect("activate", lambda _action, page: self.stack.set_visible_child_name(page.get_string()))
        self.add_action(show_page)
        self.reload()

    # Loading and actions

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, use_markup=False, timeout=4))

    def reload(self) -> None:
        self.background.run(self.gallery.state, self._show, lambda exc: self.toast(f"Could not load the gallery: {exc}"))
        self._refresh_next_time()

    def _refresh_next_time(self) -> bool:
        self.background.run(self.gallery.next_change, self._set_next_time, lambda exc: None)
        return True

    def _set_next_time(self, when: float | None) -> None:
        self.next_time = when
        self._tick()

    def _tick(self) -> bool:
        """Update the countdown to the next automatic wallpaper change."""
        if self.countdown is None:
            return True
        if self.next_time is None:
            self.countdown.set_label("Automatic rotation is off. Run artwall install-timers to rotate on a schedule.")
            return True
        left = int(self.next_time - time.time())
        if left <= 0:
            self.countdown.set_label("Changing now…")
            if left < -5:  # The timer has fired; show the new wallpaper and its next time.
                self.next_time = None
                self.reload()
            return True
        hours, rest = divmod(left, 3600)
        minutes, seconds = divmod(rest, 60)
        span = f"{hours} h {minutes:02d} min" if hours else f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"
        self.countdown.set_label(f"Next wallpaper in {span}")
        return True

    def show_next(self) -> None:
        self.mutate(self.gallery.show_next)

    def mutate(self, change: Callable[[], str]) -> None:
        """Apply one change in the background, report it, and refresh the window."""
        if self.busy:
            return
        self.busy = True

        def done(notice: str) -> None:
            self.busy = False
            self.toast(notice)
            self.reload()

        def failed(exc: Exception) -> None:
            self.busy = False
            self.toast(str(exc))
            self.reload()

        self.background.run(change, done, failed)

    def vote(self, record: Record, kind: Vote) -> None:
        current = self.state.current.id if self.state and self.state.current else None
        self.mutate(lambda: self.gallery.vote(record.id, kind, current))

    def save_comment(self, record: Record, text: str) -> None:
        def save() -> str:
            notice = self.gallery.set_comment(record.id, text)
            self.drafts.pop(record.id, None)
            return notice

        self.mutate(save)

    def regenerate(self, record: Record | None = None) -> None:
        """Replace one upcoming wallpaper, or after confirmation the whole queue, with new curation."""
        if record is not None:
            self._start_regenerating([record.id])
            return
        confirm = Adw.AlertDialog(heading="Regenerate All Upcoming Wallpapers?",
                                  body="The prepared queue is replaced with newly curated wallpapers using different "
                                       "works. This uses the curator model and may take a few minutes.")
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("regenerate", "Regenerate")
        confirm.set_response_appearance("regenerate", Adw.ResponseAppearance.SUGGESTED)
        confirm.connect("response", self._confirmed_regenerate)
        confirm.present(self)

    def _confirmed_regenerate(self, _dialog: Any, response: str) -> None:
        if response == "regenerate":
            self._start_regenerating(None)

    def _start_regenerating(self, ids: list[str] | None) -> None:
        self.prepare_button.set_sensitive(False)
        self.background.run(lambda: self.gallery.regenerate(ids), self._preparing, self._regenerate_failed)

    def _regenerate_failed(self, exc: Exception) -> None:
        self.prepare_button.set_sensitive(True)
        self.toast(str(exc))

    def _preparing(self, notice: str) -> None:
        self.toast(notice)
        self.prepared = 0
        self._watch_preparation()

    def _watch_preparation(self) -> None:
        if self.polling:
            return
        self.polling = True
        self._poll_preparation()
        GLib.timeout_add_seconds(2, self._poll_preparation)

    def _poll_preparation(self) -> bool:
        progress = self.gallery.preparation()
        running = progress.status == "running"
        self.prepare_button.set_sensitive(not running)
        self.prepare_button.set_label("Preparing…" if running else "Regenerate All")
        self.prepare_status.set_label(progress.message)
        if progress.prepared != self.prepared or not running:
            self.prepared = progress.prepared
            self.reload()
        if not running:
            self.polling = False
            self.toast(progress.message)
        return running

    # Pages

    def _show(self, state: State) -> None:
        self.state = state
        if self.gallery.preparation().status == "running":
            self._watch_preparation()  # For example after settings changed upcoming wallpapers.
        self.now.set_child(self._now_page(state))
        self.coming_grid.set_child(self._grid(
            state.queued, "Nothing Prepared Yet", "Regenerate to prepare the queue now, or wait for the hourly "
            "curation.", "view-grid-symbolic"))
        self.coming_page.set_badge_number(len(state.queued))
        self.seen.set_child(self._grid(state.history, "Nothing Seen Yet", "Works you have lived with appear here.",
                                       "document-open-recent-symbolic"))
        if self.dialog is not None and self.dialog_record is not None:
            record = state.find(self.dialog_record)
            if record is None:
                self.dialog.close()
            else:
                self._fill_dialog(record)
        if self.preferences is not None:
            self.preferences.exclusions.update(state)

    def _now_page(self, state: State) -> Any:
        record = state.current
        if record is None:
            return Adw.StatusPage(icon_name=APP_ID, title="Nothing on Your Wall Yet",
                                  description="Prepare wallpapers under Coming Next, then run artwall next.")
        box = vbox(spacing=18, margin=24)
        bar = Gtk.Box(spacing=8)
        self.countdown = label("", "dim-label", "numeric")
        self.countdown.set_hexpand(True)
        bar.append(self.countdown)
        explain = Gtk.Button(tooltip_text="Show this wallpaper full screen with the curator's notes (Ctrl+E)",
                             action_name="app.explain")
        explain.set_child(Adw.ButtonContent(icon_name="view-reveal-symbolic", label="Explain"))
        forward = Gtk.Button(tooltip_text="Show the next prepared wallpaper now", css_classes=["suggested-action"])
        forward.set_child(Adw.ButtonContent(icon_name="go-next-symbolic", label="Next Wallpaper"))
        forward.connect("clicked", lambda _button: self.show_next())
        bar.append(explain)
        bar.append(forward)
        box.append(bar)
        box.append(picture(self.background, lambda: record.image_path, record.title, "artwork"))
        box.append(details(self, record, state))
        self._tick()
        return Adw.Clamp(maximum_size=1200, child=box)

    def _coming_shell(self) -> Any:
        box = vbox(spacing=0)
        bar = Gtk.Box(spacing=18, margin_top=18, margin_bottom=6, margin_start=24, margin_end=24)
        self.prepare_status = label("Up to six wallpapers stay prepared. Regenerating replaces them with new "
                                    "curation that avoids the replaced works.", "dim-label")
        self.prepare_status.set_hexpand(True)
        self.prepare_button = Gtk.Button(label="Regenerate All", css_classes=["suggested-action", "pill"],
                                         valign=Gtk.Align.CENTER)
        self.prepare_button.connect("clicked", lambda _button: self.regenerate())
        bar.append(self.prepare_status)
        bar.append(self.prepare_button)
        box.append(bar)
        self.coming_grid = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        box.append(self.coming_grid)
        return box

    def _grid(self, records: tuple[Record, ...], title: str, description: str, icon: str) -> Any:
        if not records:
            return Adw.StatusPage(title=title, description=description, icon_name=icon)
        # At least three columns, so a short queue does not stretch one card across the window.
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=3,
                           max_children_per_line=5, column_spacing=18, row_spacing=18, valign=Gtk.Align.START,
                           margin_top=18, margin_bottom=24, margin_start=24, margin_end=24)
        ratio = self.state.settings.width / self.state.settings.height if self.state else 16 / 9
        for record in records:
            flow.append(self._card(record, ratio))
        return Adw.Clamp(maximum_size=1500, child=flow)

    def _card(self, record: Record, ratio: float) -> Any:
        card = vbox(spacing=0)
        art = picture(self.background, lambda: self.gallery.thumbnail(record), record.title, cover=True)
        art.set_size_request(180, -1)
        # Thumbnails keep the display's proportions whatever the card width.
        card.append(Gtk.AspectFrame(ratio=ratio, obey_child=False, child=art, css_classes=["art-frame"]))
        text = vbox(spacing=4, margin=12)
        text.append(label(record.title, "heading", "art-title", wrap=False))
        text.append(label(record.byline or " ", "caption", "dim-label", wrap=False))
        card.append(text)
        button = Gtk.Button(child=card, css_classes=["card", "art-card"], tooltip_text=record.title)
        button.connect("clicked", lambda _button: self.open_details(record))
        return button

    # Dialogs

    def open_details(self, record: Record) -> None:
        self.dialog = Adw.Dialog(title=record.title, content_width=920, content_height=820)
        self.dialog_record = record.id
        self.dialog_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        view = Adw.ToolbarView(content=self.dialog_scroll)
        view.add_top_bar(Adw.HeaderBar())
        self.dialog.set_child(view)
        self.dialog.connect("closed", self._dialog_closed)
        self._fill_dialog(record)
        self.dialog.present(self)

    def _dialog_closed(self, _dialog: Any) -> None:
        self.dialog = None
        self.dialog_record = None

    def _fill_dialog(self, record: Record) -> None:
        if self.state is None:
            return
        box = vbox(spacing=24, margin=24)
        box.append(label(when(record), "caption-heading", "accent"))
        box.append(picture(self.background, lambda: record.image_path, record.title, "artwork"))
        box.append(details(self, record, self.state))
        self.dialog_scroll.set_child(Adw.Clamp(maximum_size=900, child=box))

    def show_preferences(self) -> None:
        if self.state is None:
            return
        self.preferences = Preferences(self, self.state)
        self.preferences.connect("closed", lambda _dialog: setattr(self, "preferences", None))
        self.preferences.present(self)
