"""The gallery's behavior, independent of any toolkit.

The native app (``artwall.app``) is a thin view over this module, so votes,
settings, exclusions, and preparation are tested without a graphical session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
import json
import logging
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from threading import Lock, Thread
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageOps

from artwall import rotation
from artwall.config import Settings, config_path, data_dir, database_path, load_config, model_api_key
from artwall.models import Artwork, Composition
from artwall.storage import Database
from artwall.systemd import update_rotate_timer, user_unit_dir

LOG = logging.getLogger(__name__)
BATCH = 3  # Wallpapers made by one "Prepare new wallpapers" request.
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


PreparationStatus = Literal["idle", "running", "ready", "error"]


@dataclass(frozen=True)
class Preparation:
    status: PreparationStatus
    prepared: int
    message: str


@dataclass
class Gallery:
    database_path: Path = field(default_factory=database_path)
    config_file: Path = field(default_factory=config_path)
    unit_dir: Path = field(default_factory=user_unit_dir)
    data_root: Path = field(default_factory=data_dir)
    _vote_lock: Lock = field(default_factory=Lock, repr=False)  # A vote and its rotation are one step.
    _lock: Lock = field(default_factory=Lock, repr=False)  # Guards the preparation status.
    _preparation: Preparation = Preparation("idle", 0, "Uses your saved settings and feedback. New wallpapers go first.")

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
                      rotation.Reaction.stored(db.get_feedback(composition.id)), db.get_comment(composition.id))

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
                return f"Thumbs down saved. Now showing {self._record(db, shown).title}."
            if kind == "dislike" and enabled and not was_set and composition.shown_at is None:
                return "Thumbs down saved. Removed from upcoming rotation."
            return "Feedback saved"

    def save_settings(self, *, interval_minutes: int, model: str, max_artworks: int) -> str:
        """Validate and save the gallery's settings, updating an installed rotation timer."""
        model = model.strip()
        configured = load_config(self.config_file)
        # An agent may use its own default model; model servers need a name.
        if ((not model and configured.model_provider != "agent") or len(model) > 120
                or any(ord(ch) < 32 for ch in model)):
            raise ValueError("Enter a valid model name")
        if not 15 <= interval_minutes <= 1440:
            raise ValueError("Rotation interval must be 15–1440 minutes")
        with self._database() as db:
            old = db.settings(configured)
            settings = db.save_gallery_settings(configured, {
                "interval_minutes": interval_minutes, "max_artworks": max_artworks, "model": model})
        if interval_minutes != old.interval_minutes:
            executable = shutil.which("artwall")
            if not executable or not update_rotate_timer(settings, str(Path(executable).resolve()),
                                                         directory=self.unit_dir):
                return "Settings saved. Run artwall install-timers to apply the new rotation interval."
        return "Settings saved. Prepare new wallpapers to create layouts with these settings."

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

    def models(self) -> list[str]:
        """Model names the configured endpoint offers, or [] when it cannot be asked quickly."""
        return available_models(self.settings())

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

    def start_preparation(self) -> str:
        with self._lock:
            if self._preparation.status == "running":
                return "Already preparing new wallpapers."
            # -P keeps the working directory off sys.path, so an unrelated
            # artwall/ folder there can never replace the installed package.
            process = subprocess.Popen(
                [sys.executable, "-P", "-m", "artwall.cli", "curate", "--count", str(BATCH), "--next"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            self._preparation = Preparation(
                "running", 0, f"Preparing {BATCH} wallpapers with your saved settings. This may take a few minutes.")
            Thread(target=self._watch_preparation, args=(process,), daemon=True).start()
        return "Preparing new wallpapers. You can keep using the gallery."

    def _watch_preparation(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        count = 0
        with process.stdout:
            for line in process.stdout:
                if line.startswith("Queued "):
                    count += 1
                    with self._lock:
                        self._preparation = Preparation(
                            "running", count, f"{count} of {BATCH} wallpapers ready. Still preparing…")
                else:
                    LOG.warning("curator: %s", line.rstrip())
        result = process.wait()
        if result == 0:
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


def available_models(settings: Settings) -> list[str]:
    """Model names the configured endpoint offers, or [] when it cannot be asked quickly."""
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
