"""The gallery's behavior, independent of any toolkit.

The native app (``artwall.app``) is a thin view over this module, so votes,
settings, exclusions, and preparation are tested without a graphical session.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import islice
import json
import logging
import os
from pathlib import Path
import re
import signal
import secrets
import shutil
import subprocess
import sys
from threading import Lock, Thread
from typing import Any, Literal, Sequence
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageOps

from artwall import rotation
from artwall.config import (
    Settings, config_path, curation_fingerprint, data_dir, database_path, load_config, model_api_key,
)
from artwall.models import Artwork, Composition, Note
from artwall.prompts import EDITABLE, PromptPart, resolve, validate_overrides
from artwall.storage import Database
from artwall.systemd import next_rotation, restart_rotation, update_rotate_timer, user_unit_dir

LOG = logging.getLogger(__name__)
BATCH = 3  # Wallpapers made by one "Prepare new wallpapers" request.
WHY_SLOW = ("Each one downloads new full-size museum images (about one per second, as the museum asks) "
            "and asks the curator model 2–4 times, so allow a minute or more per wallpaper.")
MAX_COMMENT = 5000


@dataclass(frozen=True)
class Work:
    id: str
    title: str
    artist: str
    date: str
    url: str
    source: str

    @classmethod
    def of(cls, art: Artwork) -> Work:
        return cls(art.id, art.title or "Untitled", art.artist or "Artist unknown", art.date_display or "",
                   art.canonical_url or "", art.source)


@dataclass(frozen=True)
class Record:
    """One composition as the gallery shows it."""

    id: str
    works: tuple[Work, ...]
    primary: Work | None
    description: str
    relationship: str
    layout: str
    image_path: Path
    created_at: float
    shown_at: float | None
    reaction: rotation.Reaction
    comment: str
    notes: tuple[Note, ...] = ()
    # The curator's requests and raw answers for this wallpaper: (purpose, prompt, answer).
    transcript: tuple[tuple[str, str, str], ...] = ()

    @property
    def title(self) -> str:
        return self.primary.title if self.primary else "Untitled composition"

    @property
    def byline(self) -> str:
        return " · ".join(part for part in (self.primary.artist, self.primary.date) if part) if self.primary else ""

    @property
    def companions(self) -> tuple[Work, ...]:
        return tuple(work for work in self.works if work is not self.primary)


@dataclass(frozen=True)
class State:
    settings: Settings
    current: Record | None
    queued: tuple[Record, ...]
    history: tuple[Record, ...]
    blocked_artists: tuple[str, ...]
    blocked_works: tuple[Work, ...]
    artists: tuple[str, ...]  # Known artist names, for exclusion suggestions.
    works: tuple[Work, ...]

    def find(self, composition_id: str) -> Record | None:
        records = (self.current, *self.queued, *self.history)
        return next((record for record in records if record and record.id == composition_id), None)


PreparationStatus = Literal["idle", "running", "ready", "error", "cancelled"]


@dataclass(frozen=True)
class Preparation:
    status: PreparationStatus
    prepared: int
    message: str
    total: int = 0


@dataclass
class Gallery:
    database_path: Path = field(default_factory=database_path)
    config_file: Path = field(default_factory=config_path)
    unit_dir: Path = field(default_factory=user_unit_dir)
    data_root: Path = field(default_factory=data_dir)
    _vote_lock: Lock = field(default_factory=Lock, repr=False)  # A vote and its rotation are one step.
    _lock: Lock = field(default_factory=Lock, repr=False)  # Guards the preparation status.
    _preparation: Preparation = Preparation("idle", 0, "Uses your saved settings and feedback. New wallpapers go first.")
    _process: subprocess.Popen[str] | None = field(default=None, repr=False)
    _cancelled: bool = False

    def _database(self) -> Database:
        return Database(self.database_path)

    def settings(self) -> Settings:
        with self._database() as db:
            return db.settings(load_config(self.config_file))

    def _record(self, db: Database, composition: Composition) -> Record:
        works = tuple(Work.of(art) for artwork_id in composition.artwork_ids if (art := db.get_artwork(artwork_id)))
        primary = next((work for work in works if work.id == composition.decision.primary_artwork_id),
                       works[0] if works else None)
        return Record(composition.id, works, primary, composition.decision.description,
                      composition.decision.relationship, composition.decision.layout,
                      Path(composition.output_path), composition.created_at, composition.shown_at,
                      rotation.Reaction.stored(db.get_feedback(composition.id)), db.get_comment(composition.id),
                      composition.notes, _transcript(composition.render.get("transcript")))

    def state(self) -> State:
        """A factual view of the current wallpaper, the queue, and history."""
        with self._database() as db:
            settings = db.settings(load_config(self.config_file))
            current = db.get_current()
            # History lists every showing, newest first; the gallery shows each composition once.
            history = {composition.id: composition for composition in db.list_history(limit=80)}.values()
            works = sorted(db.list_artworks(limit=5000),
                           key=lambda art: (art.title.casefold(), (art.artist or "").casefold()))
            return State(
                settings=settings,
                current=self._record(db, current) if current else None,
                queued=tuple(self._record(db, item) for item in islice(rotation.upcoming(db, settings), 60)),
                history=tuple(self._record(db, item) for item in history),
                blocked_artists=tuple(db.list_blacklisted_artists()),
                blocked_works=tuple(Work.of(art) for art in db.list_blacklisted_artworks()),
                artists=tuple(sorted({art.artist for art in works if art.artist}, key=str.casefold)),
                works=tuple(Work.of(art) for art in works),
            )

    def vote(self, composition_id: str, kind: rotation.Vote, expected_current: str | None = None) -> str:
        """Toggle a reaction; thumbs down on the current wallpaper also shows the next one."""
        with self._vote_lock, self._database() as db:
            composition = db.get_composition(composition_id)
            if composition is None:
                raise ValueError("Unknown composition")
            current = db.get_current()
            is_current = current is not None and current.id == composition_id
            if kind == "dislike" and expected_current == composition_id and not is_current:
                return "The wallpaper has already changed."
            was_set = rotation.Reaction.stored(db.get_feedback(composition_id)).includes(kind)
            # A rejected work can remain on screen when no alternative is cached or
            # the desktop backend fails. Pressing thumbs down again retries the change.
            enabled = (kind == "dislike" and is_current) or not was_set
            rotation.vote(db, composition, kind, enabled=enabled)
            if kind == "dislike" and is_current:
                try:
                    shown = rotation.advance(db, db.settings(load_config(self.config_file)))
                except rotation.NoAlternativeError:
                    return "Thumbs down saved. No other prepared wallpaper is available yet."
                except Exception as exc:
                    LOG.warning("Could not advance wallpaper after thumbs down: %s", exc)
                    return "Thumbs down saved, but the wallpaper could not change. Check the desktop backend."
                restart_rotation()
                return f"Thumbs down saved. Now showing {self._record(db, shown).title}."
            if kind == "dislike" and enabled and not was_set and composition.shown_at is None:
                return "Thumbs down saved. Removed from upcoming rotation."
            return "Feedback saved"

    def show_next(self) -> str:
        """Show the next prepared wallpaper now, giving it a full rotation interval."""
        with self._vote_lock, self._database() as db:
            try:
                shown = rotation.advance(db, db.settings(load_config(self.config_file)))
            except rotation.NoAlternativeError as exc:
                raise ValueError("No other prepared wallpaper is available yet.") from exc
            except Exception as exc:
                LOG.warning("Could not show the next wallpaper: %s", exc)
                raise ValueError("The wallpaper could not change. Check the desktop backend.") from exc
            title = self._record(db, shown).title
        restart_rotation()
        return f"Now showing {title}."

    def next_change(self) -> float | None:
        """When the rotation timer shows the next wallpaper (Unix time), if timers are installed."""
        return next_rotation()

    def save_settings(self, *, interval_minutes: int, model: str, max_artworks: int, **model_settings: str) -> str:
        """Validate and save the app's settings, updating the rotation timer and the upcoming queue.

        ``model_settings`` may set model_provider, model_url, agent, api_key_env, and api_key_file.
        The API key itself is never saved; only where to read it.
        """
        unknown = set(model_settings) - _MODEL_SETTINGS
        if unknown:
            raise ValueError(f"Unknown setting: {sorted(unknown)[0]}")
        model = model.strip()
        configured = load_config(self.config_file)
        with self._database() as db:
            old = db.settings(configured)
        provider = model_settings.get("model_provider", old.model_provider)
        # An agent may use its own default model; model servers need a name.
        if (not model and provider != "agent") or len(model) > 120 or any(ord(ch) < 32 for ch in model):
            raise ValueError("Enter a valid model name")
        if not 15 <= interval_minutes <= 1440:
            raise ValueError("Rotation interval must be 15–1440 minutes")
        variable = model_settings.get("api_key_env")
        if variable is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", variable):
            raise ValueError("Enter an environment variable name such as OPENAI_API_KEY")
        values = {"interval_minutes": interval_minutes, "max_artworks": max_artworks, "model": model,
                  **{key: value.strip() for key, value in model_settings.items()}}
        with self._database() as db:
            settings = db.save_gallery_settings(configured, values)
            settings = db.settings(configured)
        return self._apply("Settings saved.", old, settings)

    def prompt_parts(self) -> list[tuple[PromptPart, str]]:
        """Each editable instruction with the text the curator uses now."""
        with self._database() as db:
            current = resolve(db.prompt_overrides())
        return [(part, current[part.key]) for part in EDITABLE]

    def save_prompts(self, texts: dict[str, str | None]) -> str:
        """Save edited instructions; text equal to the default, or empty, restores the default."""
        configured = load_config(self.config_file)
        with self._database() as db:
            old = db.settings(configured)
            overrides = {**db.prompt_overrides(), **{key: "" for key in texts}}
            overrides.update(validate_overrides({k: v for k, v in texts.items()}))
            db.save_prompt_overrides({key: value for key, value in overrides.items() if value})
            settings = db.settings(configured)
        return self._apply("Curator instructions saved.", old, settings)

    def _apply(self, notice: str, old: Settings, settings: Settings) -> str:
        """Follow a settings change through: the rotation timer, then the upcoming queue."""
        if settings.interval_minutes != old.interval_minutes:
            executable = shutil.which("artwall")
            if not executable or not update_rotate_timer(settings, str(Path(executable).resolve()),
                                                         directory=self.unit_dir):
                notice += " Run artwall install-timers to apply the new rotation interval."
        if curation_fingerprint(settings) != curation_fingerprint(old):
            # Upcoming wallpapers follow the new settings; old ones stay until replaced.
            self.start_preparation(None)
            notice += " Upcoming wallpapers are being prepared again with them."
        return notice

    def set_comment(self, composition_id: str, comment: str) -> str:
        if len(comment) > MAX_COMMENT:
            raise ValueError(f"Feedback must be at most {MAX_COMMENT} characters")
        with self._database() as db:
            if db.get_composition(composition_id) is None:
                raise ValueError("Unknown composition")
            db.set_comment(composition_id, comment.strip())
        return "Feedback saved. It will guide newly prepared wallpapers." if comment.strip() else "Feedback cleared"

    def set_artist_blocked(self, artist: str, blocked: bool) -> str:
        artist = artist.strip()
        if not artist or len(artist) > 250:
            raise ValueError("Enter an artist name")
        with self._database() as db:
            db.set_artist_blacklisted(artist, blocked)
        return f"{artist} {'excluded' if blocked else 'allowed again'}"

    def set_artwork_blocked(self, artwork_id: str, blocked: bool) -> str:
        with self._database() as db:
            art = db.get_artwork(artwork_id)
            if art is None:
                raise ValueError("Unknown artwork")
            db.set_artwork_blacklisted(artwork_id, blocked)
        return f"{art.title or 'Artwork'} {'excluded' if blocked else 'allowed again'}"

    def models(self, **changes: str) -> list[str]:
        """Model names to choose from, for the saved settings or unsaved ``changes`` to them.

        Model servers are asked for their list; Claude Code offers its aliases. An
        empty list means the endpoint could not be asked quickly.
        """
        unknown = set(changes) - _MODEL_SETTINGS
        if unknown:
            raise ValueError(f"Unknown setting: {sorted(unknown)[0]}")
        settings = self.settings()
        values: dict[str, Any] = {key: value.strip() for key, value in changes.items()}
        if "api_key_file" in values:
            values["api_key_file"] = values["api_key_file"] or None
        try:
            settings = replace(settings, **values)
        except ValueError:
            return []  # For example an endpoint that is not a URL yet.
        if settings.model_provider == "agent":
            return list(_AGENT_MODELS.get(settings.agent, ()))
        return available_models(settings, strict=True)

    def thumbnail(self, record: Record, size: tuple[int, int] = (800, 400)) -> Path:
        """A cached JPEG preview of a composition for card grids."""
        source = record.image_path
        directory = self.data_root / "cache" / "gallery-thumbnails"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{record.id}-{size[0]}x{size[1]}.jpg"
        if destination.is_file() and destination.stat().st_mtime >= source.stat().st_mtime:
            return destination
        temporary = directory / f".{record.id}-{secrets.token_hex(6)}.jpg"
        try:
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail(size, Image.Resampling.LANCZOS)
                image.save(temporary, "JPEG", quality=86, optimize=True)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    # Preparation runs the normal curator in a subprocess so the window stays responsive.

    def preparation(self) -> Preparation:
        with self._lock:
            return self._preparation

    def regenerate(self, composition_ids: Sequence[str] | None = None) -> str:
        """Replace upcoming wallpapers (all of them by default) with newly curated ones.

        The replaced works are avoided this time, so regenerating brings different art.
        """
        with self._lock:
            if self._preparation.status == "running":
                return "Already preparing new wallpapers."
        with self._database() as db:
            settings = db.settings(load_config(self.config_file))
            ids = list(composition_ids) if composition_ids is not None else [
                composition.id for composition in rotation.upcoming(db, settings)]
            removed = db.discard_upcoming(ids)
        count = len(removed) if composition_ids is not None else settings.queue_size
        if count == 0:
            raise ValueError("Nothing to regenerate")
        avoid = sorted({artwork_id for composition in removed for artwork_id in composition.artwork_ids})
        return self.start_preparation(count, avoid=avoid)

    def start_preparation(self, count: int | None = BATCH, *, avoid: Sequence[str] = ()) -> str:
        """Curate ``count`` wallpapers ahead of the queue, or with None top the queue up to its size."""
        with self._lock:
            if self._preparation.status == "running":
                return "Already preparing new wallpapers."
            # -P keeps the working directory off sys.path, so an unrelated
            # artwall/ folder there can never replace the installed package.
            command = [sys.executable, "-P", "-m", "artwall.cli", "curate"]
            if count is not None:
                command += ["--count", str(count), "--next", "--avoid", ",".join(avoid)]
            # Its own process group, so cancelling also stops an agent CLI it started.
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       start_new_session=True)
            self._process, self._cancelled = process, False
            total = count if count is not None else self.settings().queue_size
            self._preparation = Preparation(
                "running", 0, f"Preparing up to {total} wallpaper{'s' if total != 1 else ''}. {WHY_SLOW}", total)
            Thread(target=self._watch_preparation, args=(process, total), daemon=True).start()
        return "Preparing new wallpapers. You can keep using Artwall."

    def cancel_preparation(self) -> str:
        """Stop preparing wallpapers; those already finished stay in the queue."""
        with self._lock:
            process = self._process
            if process is None or self._preparation.status != "running":
                return "Nothing is being prepared."
            self._cancelled = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # It finished just now.
        return "Cancelling…"

    def _watch_preparation(self, process: subprocess.Popen[str], total: int) -> None:
        assert process.stdout is not None
        count = 0
        with process.stdout:
            for line in process.stdout:
                if line.startswith(("Replaced ", "already prepared")) or "already prepared" in line:
                    continue
                if line.startswith("Queued "):
                    count += 1
                    with self._lock:
                        self._preparation = Preparation(
                            "running", count, f"{count} of {total} wallpapers ready. {WHY_SLOW}", total)
                else:
                    LOG.warning("curator: %s", line.rstrip())
        result = process.wait()
        with self._lock:
            cancelled, self._process = self._cancelled, None
        if cancelled:
            final = Preparation("cancelled", count, f"Cancelled. {_kept(count)} The hourly curation fills the rest "
                                                    "of the queue.")
        elif result == 0:
            final = Preparation("ready", count,
                                f"{count} new wallpaper{'s' if count != 1 else ''} ready at the front of Coming next.")
        elif count:
            final = Preparation("error", count,
                                f"{count} wallpapers ready, but preparation stopped early. Try again to prepare more.")
        else:
            final = Preparation("error", 0,
                                "No new wallpapers could be prepared. Check the museum connection and try again.")
        with self._lock:
            self._preparation = final


def _kept(count: int) -> str:
    return f"{count} new wallpaper{'s were' if count != 1 else ' was'} kept." if count else "No new wallpapers were kept."


_MODEL_SETTINGS = {"model_provider", "model_url", "agent", "api_key_env", "api_key_file"}
# Claude Code accepts these aliases for its latest models; other agents name their own.
_AGENT_MODELS = {"claude": ("fable", "opus", "sonnet", "haiku")}


def _transcript(raw: object) -> tuple[tuple[str, str, str], ...]:
    entries = raw if isinstance(raw, list) else []
    return tuple((str(entry.get("purpose", "")), str(entry.get("prompt", "")), str(entry.get("answer", "")))
                 for entry in entries if isinstance(entry, dict))


def available_models(settings: Settings, *, strict: bool = False) -> list[str]:
    """Model names the configured endpoint offers, or [] when it cannot be asked quickly.

    With ``strict``, an endpoint that cannot be reached raises ValueError saying so,
    so the app can tell that apart from a server with no models.
    """
    if settings.model_provider == "agent":
        return []  # Agent CLIs choose among their own models; leave the name free-form.
    base_url = settings.model_url.rstrip("/")
    headers = {}
    if settings.model_provider == "openai":
        url, key, name = base_url + "/models", "data", "id"
        try:
            api_key = model_api_key(settings)
        except OSError:
            return []
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        base = urlsplit(base_url)
        if base.scheme != "http" or base.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return []
        url, key, name = base_url + "/api/tags", "models", "name"
    try:
        with urlopen(Request(url, headers=headers), timeout=2) as response:
            value = json.load(response)
        return sorted({item[name] for item in value.get(key, [])
                       if isinstance(item, dict) and isinstance(item.get(name), str)})
    except OSError as exc:
        if strict:
            server = "Ollama" if settings.model_provider == "ollama" else "The endpoint"
            raise ValueError(f"{server} is not reachable at {base_url}") from exc
        return []
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
