"""Curator settings and exclusions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from artwall.gallery import State, Work  # noqa: E402

if TYPE_CHECKING:
    from artwall.app.window import ArtwallWindow

_PROVIDERS = {"ollama": "Ollama", "openai": "An OpenAI-compatible API"}
_MATCHES = 6


class Preferences(Adw.PreferencesDialog):  # type: ignore[misc]
    def __init__(self, window: ArtwallWindow, state: State) -> None:
        super().__init__(search_enabled=False)
        self.window = window
        self.saved = (state.settings.interval_minutes, state.settings.model, state.settings.max_artworks)
        self.add(self._curator_page(state))
        self.exclusions = Exclusions(window, state)
        self.add(self.exclusions)
        self.connect("closed", lambda _dialog: self._save())

    def _curator_page(self, state: State) -> Any:
        settings = state.settings
        page = Adw.PreferencesPage(title="Curator", icon_name="preferences-system-symbolic")
        rotation = Adw.PreferencesGroup(title="Rotation")
        self.interval = Adw.SpinRow.new_with_range(15, 1440, 15)
        self.interval.set_title("Change Wallpaper Every")
        self.interval.set_subtitle("Minutes. Give each work enough time to be seen.")
        self.interval.set_value(settings.interval_minutes)
        rotation.add(self.interval)
        page.add(rotation)

        if settings.model_provider == "agent":
            source = (f"Your signed-in {settings.agent} agent. Leave the model empty for its default. "
                      "The agent is chosen in config.toml.")
        else:
            provider = _PROVIDERS.get(settings.model_provider, settings.model_provider)
            source = f"{provider} at {settings.model_url}. The endpoint and any API key are set in config.toml."
        curator = Adw.PreferencesGroup(title="Curator Model", description=source)
        self.model = Adw.EntryRow(title="Vision Model", text=settings.model)
        self.models = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        popover = Gtk.Popover(child=Gtk.ScrolledWindow(child=self.models, max_content_height=320,
                                                       propagate_natural_height=True))
        choose = Gtk.MenuButton(icon_name="view-list-symbolic", popover=popover, valign=Gtk.Align.CENTER,
                                tooltip_text="Models offered by the endpoint", sensitive=False,
                                css_classes=["flat"])
        self.model.add_suffix(choose)
        self.window.background.run(self.window.gallery.models,
                                   lambda names: self._offer(names, choose, popover), lambda exc: None)
        self.count = Adw.ComboRow(title="Works on One Wallpaper",
                                  subtitle="Related works can share the wall when they fit together well.",
                                  model=Gtk.StringList.new(["One", "Up to two", "Up to three"]))
        self.count.set_selected(settings.max_artworks - 1)
        curator.add(self.model)
        curator.add(self.count)
        page.add(curator)
        return page

    def _offer(self, names: list[str], button: Any, popover: Any) -> None:
        for name in names:
            row = Gtk.Button(label=name, css_classes=["flat"])
            row.connect("clicked", lambda _row, chosen=name: (self.model.set_text(chosen), popover.popdown()))
            self.models.append(row)
        button.set_sensitive(bool(names))

    def _save(self) -> None:
        values = (int(self.interval.get_value()), self.model.get_text().strip(), self.count.get_selected() + 1)
        if values == self.saved:
            return
        interval, model, count = values
        self.window.mutate(lambda: self.window.gallery.save_settings(
            interval_minutes=interval, model=model, max_artworks=count))


class Exclusions(Adw.PreferencesPage):  # type: ignore[misc]
    """Artists and works left out of curation and rotation."""

    def __init__(self, window: ArtwallWindow, state: State) -> None:
        super().__init__(title="Exclusions", icon_name="action-unavailable-symbolic")
        self.window = window
        self.state = state
        self.rows: list[tuple[Any, Any]] = []  # (group, row) pairs rebuilt on each update

        self.artists = Adw.PreferencesGroup(
            title="Artists", description="Names match regardless of capitalization.")
        add_artist = Adw.EntryRow(title="Exclude an Artist", show_apply_button=True)
        add_artist.connect("apply", self._exclude_artist)
        self.artists.add(add_artist)
        self.add(self.artists)

        self.works = Adw.PreferencesGroup(title="Artworks")
        self.search = Adw.EntryRow(title="Find an Artwork to Exclude")
        self.search.connect("changed", lambda _row: self.update(self.state))
        self.works.add(self.search)
        self.add(self.works)
        self.update(state)

    def _exclude_artist(self, row: Any) -> None:
        artist = row.get_text().strip()
        if artist:
            row.set_text("")
            self.window.mutate(lambda: self.window.gallery.set_artist_blocked(artist, True))

    def _row(self, group: Any, title: str, subtitle: str, action: str, apply: Any) -> None:
        row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
        button = Gtk.Button(label=action, valign=Gtk.Align.CENTER, css_classes=["flat"])
        button.connect("clicked", lambda _button: self.window.mutate(apply))
        row.add_suffix(button)
        group.add(row)
        self.rows.append((group, row))

    def update(self, state: State) -> None:
        self.state = state
        for group, row in self.rows:
            group.remove(row)
        self.rows.clear()
        gallery = self.window.gallery
        for artist in state.blocked_artists:
            self._row(self.artists, artist, "Excluded", "Allow",
                      lambda name=artist: gallery.set_artist_blocked(name, False))
        query = self.search.get_text().strip().casefold()
        blocked = {work.id for work in state.blocked_works}
        if query:
            matches = [work for work in state.works if work.id not in blocked
                       and (query in work.title.casefold() or query in work.artist.casefold())][:_MATCHES]
            for work in matches:
                self._row(self.works, work.title, _byline(work), "Exclude",
                          lambda chosen=work.id: gallery.set_artwork_blocked(chosen, True))
        for work in state.blocked_works:
            self._row(self.works, work.title, "Excluded · " + _byline(work), "Allow",
                      lambda chosen=work.id: gallery.set_artwork_blocked(chosen, False))


def _byline(work: Work) -> str:
    return " · ".join(filter(None, (work.artist, work.date)))
