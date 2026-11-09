"""Curator settings, prompts, and exclusions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from artwall.config import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES  # noqa: E402
from artwall.gallery import State, Work  # noqa: E402
from artwall.prompts import FIXED  # noqa: E402

if TYPE_CHECKING:
    from artwall.app.window import ArtwallWindow

# Rotation interval units: name, minutes in one, smallest amount, spin step.
_UNITS = (("minutes", 1, MIN_INTERVAL_MINUTES, 5), ("hours", 60, 1, 1), ("days", 24 * 60, 1, 1))


def split_interval(minutes: int) -> tuple[int, int]:
    """The largest unit that shows a saved interval exactly: (amount, unit index)."""
    for index in (2, 1, 0):
        if minutes % _UNITS[index][1] == 0:
            return minutes // _UNITS[index][1], index
    return minutes, 0


_PROVIDERS = (("ollama", "Ollama (local)"), ("openai", "OpenAI-Compatible API"), ("agent", "Agent CLI (subscription)"))
_AGENTS = (("claude", "Claude Code"), ("codex", "Codex"), ("command", "Custom command (config.toml)"))
_MATCHES = 6


def _monospace_view(text: str, *, editable: bool) -> Any:
    view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True, editable=editable, cursor_visible=editable,
                        top_margin=10, bottom_margin=10, left_margin=12, right_margin=12)
    view.get_buffer().set_text(text)
    return view


def _text(view: Any) -> str:
    buffer = view.get_buffer()
    return str(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))


class Preferences(Adw.PreferencesDialog):  # type: ignore[misc]
    def __init__(self, window: ArtwallWindow, state: State) -> None:
        super().__init__(search_enabled=False)
        self.window = window
        self.add(self._curator_page(state))
        self.prompts = Prompts(window)
        self.add(self.prompts)
        self.exclusions = Exclusions(window, state)
        self.add(self.exclusions)
        self.saved = self._values()
        self.connect("closed", lambda _dialog: self._save())

    def _curator_page(self, state: State) -> Any:
        settings = state.settings
        page = Adw.PreferencesPage(title="Curator", icon_name="preferences-system-symbolic")
        rotation = Adw.PreferencesGroup(title="Rotation")
        amount, unit = split_interval(settings.interval_minutes)
        self.interval = Gtk.SpinButton(valign=Gtk.Align.CENTER, numeric=True)
        self.interval.update_property([Gtk.AccessibleProperty.LABEL], ["Change wallpaper every"])
        self.unit = Gtk.DropDown(model=Gtk.StringList.new([name for name, *_ in _UNITS]), valign=Gtk.Align.CENTER)
        self.unit.update_property([Gtk.AccessibleProperty.LABEL], ["Interval unit"])
        self.unit.set_selected(unit)
        self._set_interval_range(amount)
        self.unit.connect("notify::selected", lambda *_: self._unit_changed())
        self.unit_shown = unit
        every = Adw.ActionRow(title="Change Wallpaper Every",
                              subtitle="From 15 minutes to 7 days. Give each work enough time to be seen.")
        every.add_suffix(self.interval)
        every.add_suffix(self.unit)
        self.count = Adw.ComboRow(title="Works on One Wallpaper",
                                  subtitle="Related works can share the wall when they fit together well.",
                                  model=Gtk.StringList.new(["One", "Up to two", "Up to three"]))
        self.count.set_selected(settings.max_artworks - 1)
        rotation.add(every)
        rotation.add(self.count)
        tray = Adw.SwitchRow(title="Show in System Tray", active=self.window.gallery.tray_enabled(),
                             subtitle="The current work and quick actions, from login on.")
        tray.connect("notify::active", lambda row, _param: self.window.mutate(
            lambda: self.window.gallery.set_tray(row.get_active())))
        rotation.add(tray)
        page.add(rotation)

        curator = Adw.PreferencesGroup(
            title="Curator Model",
            description="Changes here prepare upcoming wallpapers again. API keys are never stored by Artwall; "
                        "it reads them from a file or an environment variable.")
        self.provider = Adw.ComboRow(title="Provider", model=Gtk.StringList.new([name for _, name in _PROVIDERS]))
        self.provider.set_selected(next((i for i, (key, _) in enumerate(_PROVIDERS)
                                         if key == settings.model_provider), 0))
        self.endpoint = Adw.EntryRow(title="Endpoint", text=settings.model_url, show_apply_button=True)
        self.agent = Adw.ComboRow(title="Agent", subtitle="Uses the account the agent CLI is signed in with.",
                                  model=Gtk.StringList.new([name for _, name in _AGENTS]))
        self.agent.set_selected(next((i for i, (key, _) in enumerate(_AGENTS) if key == settings.agent), 0))
        self.model = Adw.ComboRow(title="Vision Model", enable_search=True,
                                  expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.model_values: list[str] = []
        self.fetches = 0  # Only the latest model list is shown.
        self.saved_model = (settings.model_provider, settings.agent, settings.model)
        self.key_file = Adw.EntryRow(title="API Key File (optional)", text=settings.api_key_file or "",
                                     show_apply_button=True)
        self.key_env = Adw.EntryRow(title="API Key Environment Variable", text=settings.api_key_env,
                                    show_apply_button=True)
        for row in (self.provider, self.endpoint, self.agent, self.model, self.key_file, self.key_env):
            curator.add(row)
        self._show_models([], settings.model, loading=True)
        self.provider.connect("notify::selected", self._provider_changed)
        self.agent.connect("notify::selected", lambda *_: self._fetch_models(switched=True))
        for row in (self.endpoint, self.key_file, self.key_env):
            row.connect("apply", lambda *_: self._fetch_models())
        self._show_provider_rows()
        self._fetch_models()
        page.add(curator)
        return page

    def _set_interval_range(self, amount: float) -> None:
        _name, size, lowest, step = _UNITS[self.unit.get_selected()]
        self.interval.set_range(lowest, MAX_INTERVAL_MINUTES // size)
        self.interval.set_increments(step, step * (6 if size == 1 else 1))
        self.interval.set_value(amount)

    def _unit_changed(self) -> None:
        """Keep about the same interval in the new unit: 120 minutes become 2 hours."""
        minutes = self.interval.get_value() * _UNITS[self.unit_shown][1]
        self.unit_shown = self.unit.get_selected()
        self._set_interval_range(max(1, round(minutes / _UNITS[self.unit_shown][1])))

    def _interval_minutes(self) -> int:
        return int(self.interval.get_value()) * _UNITS[self.unit.get_selected()][1]

    def _provider_changed(self, *_: Any) -> None:
        self._show_provider_rows()
        self._fetch_models(switched=True)

    def _model_settings(self) -> dict[str, str]:
        return {"model_provider": _PROVIDERS[self.provider.get_selected()][0],
                "model_url": self.endpoint.get_text().strip(), "agent": _AGENTS[self.agent.get_selected()][0],
                "api_key_file": self.key_file.get_text().strip(), "api_key_env": self.key_env.get_text().strip()}

    def _selected_model(self) -> str:
        index = self.model.get_selected()
        return self.model_values[index] if 0 <= index < len(self.model_values) else ""

    def _fetch_models(self, *, switched: bool = False) -> None:
        """Ask the provider in the form, saved or not, which models it offers.

        A model belongs to its provider and agent: after switching either, the saved
        model is offered only when they match what was saved; otherwise the choice resets.
        """
        self.fetches += 1
        fetch, keep, changes = self.fetches, self._selected_model(), self._model_settings()
        if switched:
            provider, agent, model = self.saved_model
            same = changes["model_provider"] == provider and (provider != "agent" or changes["agent"] == agent)
            keep = model if same else ""
        self._show_models([], keep, loading=True)
        self.window.background.run(lambda: self.window.gallery.models(**changes),
                                   lambda names: self._fetched(fetch, names, keep),
                                   lambda exc: self._fetched(fetch, [], keep, str(exc)))

    def _fetched(self, fetch: int, names: list[str], keep: str, error: str = "") -> None:
        if fetch == self.fetches:
            self._show_models(names, keep, error=error)

    def _show_models(self, names: list[str], keep: str, *, loading: bool = False, error: str = "") -> None:
        agent = _PROVIDERS[self.provider.get_selected()][0] == "agent"
        values = ([""] if agent else []) + names  # "" is the agent's default model.
        if keep and keep not in values:
            # Keep a saved model the list does not show (an unreachable endpoint, or a full model name).
            values.insert(1 if agent else 0, keep)
        if not values:
            values = [keep]
        keep = keep if keep in values else values[0]
        self.model_values = values
        self.model.set_model(Gtk.StringList.new([value or ("Default" if agent else "No model listed")
                                                 for value in values]))
        self.model.set_selected(values.index(keep) if keep in values else 0)
        if loading:
            subtitle = "Loading models…"
        elif error:
            start = " Start it (ollama serve)," if error.startswith("Ollama") else " Check it,"
            subtitle = f"{error}.{start} then press ✓ beside the endpoint to ask again."
        elif agent:
            subtitle = "Default uses the agent's own choice."
        elif names:
            subtitle = f"{len(names)} model{'s' if len(names) != 1 else ''} at this endpoint. Choose one with vision support."
        elif keep:
            subtitle = "The endpoint did not list models; showing the saved model."
        else:
            subtitle = "The endpoint did not list models. Check it, then press ✓ to ask again."
        self.model.set_subtitle(subtitle)

    def _show_provider_rows(self) -> None:
        provider = _PROVIDERS[self.provider.get_selected()][0]
        self.endpoint.set_visible(provider != "agent")
        self.agent.set_visible(provider == "agent")
        self.key_file.set_visible(provider == "openai")
        self.key_env.set_visible(provider == "openai")
        self.model.set_title("Model" if provider == "agent" else "Vision Model")

    def _values(self) -> dict[str, Any]:
        return {"interval_minutes": self._interval_minutes(), "max_artworks": self.count.get_selected() + 1,
                "model": self._selected_model(), **self._model_settings()}

    def _save(self) -> None:
        values = self._values()
        prompts = self.prompts.changes()
        if values == self.saved and not prompts:
            return
        gallery = self.window.gallery

        def save() -> str:
            notices = []
            if values != self.saved:
                notices.append(gallery.save_settings(**values))
            if prompts:
                notices.append(gallery.save_prompts(prompts))
            return " ".join(notices)

        self.window.mutate(save)


class Prompts(Adw.PreferencesPage):  # type: ignore[misc]
    """The curator's instructions: editable parts, and the fixed rules shown for transparency."""

    def __init__(self, window: ArtwallWindow) -> None:
        super().__init__(title="Prompts", icon_name="document-edit-symbolic")
        intro = Adw.PreferencesGroup(
            description="These instructions shape how the curator chooses, arranges, and explains art. Edits "
                        "apply to newly prepared wallpapers, and upcoming ones are prepared again. Facts are "
                        "still checked against museum records whatever the prompts say. Each wallpaper's "
                        "details show the exact requests the model received.")
        self.add(intro)
        self.views: dict[str, tuple[Any, str]] = {}  # key -> (text view, text when opened)
        for part, text in window.gallery.prompt_parts():
            group = Adw.PreferencesGroup(title=part.title, description=part.description)
            view = _monospace_view(text, editable=True)
            view.set_size_request(-1, 140)
            reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text="Restore the default instructions")
            reset.connect("clicked", lambda _button, v=view, default=part.default: v.get_buffer().set_text(default))
            group.set_header_suffix(reset)
            group.add(Gtk.Frame(child=view))
            self.add(group)
            self.views[part.key] = (view, text)
        fixed = Adw.PreferencesGroup(title="Always Included",
                                     description="The answer format and factual limits. These are not editable.")
        for title, text in FIXED.items():
            row = Adw.ExpanderRow(title=title, use_markup=False)
            row.add_row(_monospace_view(text, editable=False))
            fixed.add(row)
        self.add(fixed)

    def changes(self) -> dict[str, str | None]:
        return {key: _text(view) for key, (view, opened) in self.views.items() if _text(view) != opened}


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
