"""Offline rotation through prepared compositions, navigation history, and votes.

Nothing here calls a museum or model: rotation only shows what is already rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import logging
from pathlib import Path
import time
from typing import Iterable, Iterator, Literal, get_args
from uuid import uuid4

from PIL import Image

from artwall.config import Settings, cache_dir, write_atomic
from artwall.curation import lock_screen_safe
from artwall.desktop import lock_screen_backend, wallpaper_backend
from artwall.models import Artwork, Composition, Detail
from artwall.storage import Database


LOG = logging.getLogger(__name__)
Vote = Literal["like", "dislike", "favorite"]


class Reaction(Enum):
    """A composition's single reaction. Its value is the reaction's artist score.

    One state rules out contradictions such as a liked and disliked work. A
    favorite includes a like.
    """

    DISLIKED = -1
    NONE = 0
    LIKED = 1
    FAVORITE = 2

    @classmethod
    def stored(cls, flags: dict[str, bool]) -> Reaction:
        """Read the reaction from a composition's stored feedback flags."""
        if flags.get("favorite"):
            return cls.FAVORITE
        if flags.get("like"):
            return cls.LIKED
        return cls.DISLIKED if flags.get("dislike") else cls.NONE

    def includes(self, kind: Vote) -> bool:
        match kind:
            case "like":
                return self in (Reaction.LIKED, Reaction.FAVORITE)
            case "favorite":
                return self is Reaction.FAVORITE
            case "dislike":
                return self is Reaction.DISLIKED

    def flags(self) -> dict[Vote, bool]:
        return {kind: self.includes(kind) for kind in get_args(Vote)}

    def voted(self, kind: Vote, enabled: bool) -> Reaction:
        """Setting a vote replaces any other; clearing one leaves no reaction."""
        if enabled == self.includes(kind):
            return self
        if not enabled:
            return Reaction.NONE
        return {"like": Reaction.LIKED, "favorite": Reaction.FAVORITE, "dislike": Reaction.DISLIKED}[kind]


class NoAlternativeError(RuntimeError):
    """No other eligible composition is cached."""


@dataclass(frozen=True)
class Exclusions:
    """Works and artists the user has removed from rotation and curation."""

    artwork_ids: frozenset[str]
    artists: frozenset[str]
    lock_screen_safe_only: bool

    @classmethod
    def load(cls, db: Database, settings: Settings) -> Exclusions:
        return cls(frozenset(db.flagged_ids("blacklist", "dislike", target_type="artwork")),
                   frozenset(name.casefold() for name in db.list_blacklisted_artists()),
                   settings.public_lock_screen_safe)

    def allows(self, artwork: Artwork) -> bool:
        # Artist names compare without case, as curation's taste scoring does.
        return (artwork.id not in self.artwork_ids
                and (not artwork.artist or artwork.artist.casefold() not in self.artists)
                and (not self.lock_screen_safe_only or lock_screen_safe(artwork)))


def _valid_image(composition: Composition, settings: Settings) -> bool:
    path = Path(composition.output_path)
    if not path.is_file():
        return False
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != (settings.width, settings.height):
                return False
            image.verify()
    except (OSError, ValueError) as exc:
        LOG.warning("queued_composition_excluded id=%s reason=bad_image error=%s", composition.id, exc)
        return False
    return True


def eligible(db: Database, settings: Settings, compositions: Iterable[Composition]) -> Iterator[Composition]:
    """Yield the compositions that current settings and feedback still allow."""
    exclusions = Exclusions.load(db, settings)
    disliked = db.flagged_ids("dislike")
    for composition in compositions:
        ids = composition.artwork_ids
        if (composition.id in disliked or len(ids) > settings.max_artworks
                or (settings.max_artworks == 1 and isinstance(composition.decision.arrangement, Detail))
                or not _valid_image(composition, settings)):
            continue
        works = [db.get_artwork(artwork_id) for artwork_id in ids]
        if all(work is not None and work.public_domain and work.open_access
               and exclusions.allows(work) for work in works):
            yield composition


def upcoming(db: Database, settings: Settings) -> Iterator[Composition]:
    """Unshown compositions, requested batches first and otherwise oldest first."""
    return eligible(db, settings, db.list_compositions(only_unshown=True))


def _publish_current(composition: Composition) -> None:
    """Point the stable cache files at the composition being shown."""
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    link = cache / f".current-{uuid4().hex}.png"
    link.symlink_to(Path(composition.output_path).resolve())
    link.replace(cache / "current.png")
    write_atomic(cache / "current.json", json.dumps(composition.to_dict(), ensure_ascii=False, indent=2) + "\n")


def show(db: Database, composition: Composition, settings: Settings, *, apply: bool = True) -> None:
    """Show a prepared composition on the desktop and lock screen, and record it."""
    target = Path(composition.output_path)
    if not target.is_file():
        raise FileNotFoundError(f"Queued composition is missing: {target}")
    if apply:
        previous = db.get_current()
        wallpaper = wallpaper_backend(settings.wallpaper_backend, output=settings.wallpaper_output)
        locker = lock_screen_backend(settings.lockscreen_backend)
        try:
            wallpaper.set(target)
            locker.configure(target)
        except Exception:
            if previous and Path(previous.output_path).is_file():
                try:
                    wallpaper.set(previous.output_path)
                    locker.configure(previous.output_path)
                except Exception:
                    LOG.exception("desktop_rollback_failed")
            raise
    _publish_current(composition)
    db.mark_shown(composition.id)


def _back_stack(db: Database) -> list[str]:
    return [str(item) for item in json.loads(db.get_state("back_stack") or "[]")]


def advance(db: Database, settings: Settings, *, apply: bool = True) -> Composition:
    """Show the next eligible cached work without network or model calls."""
    previous = db.get_current()
    composition = next(upcoming(db, settings), None)
    if composition is None:
        # Offline rotation remains possible after the queue has been used.
        seen = [item for item in eligible(db, settings, db.list_compositions())
                if not previous or item.id != previous.id]
        if not seen:
            raise NoAlternativeError("No alternative cached composition. Run 'artwall curate' to prepare more art.")
        composition = min(seen, key=lambda item: (item.shown_at or 0, item.id))
    show(db, composition, settings, apply=apply)
    if previous and previous.id != composition.id:
        db.set_state("back_stack", json.dumps([*_back_stack(db), previous.id][-200:]))
    return composition


def advance_if_due(db: Database, settings: Settings, *, apply: bool = True) -> Composition | None:
    """At login, advance once only after a full interval since the last successful show."""
    current = db.get_current()
    if (current and current.shown_at is not None
            and time.time() - current.shown_at <= settings.interval_minutes * 60):
        return None
    return advance(db, settings, apply=apply)


def go_back(db: Database, settings: Settings, *, apply: bool = True) -> Composition:
    """Show the most recent earlier composition that is still eligible."""
    stack = _back_stack(db)
    while stack:
        candidate = db.get_composition(stack.pop())
        if candidate and next(eligible(db, settings, [candidate]), None):
            show(db, candidate, settings, apply=apply)
            db.set_state("back_stack", json.dumps(stack))
            return candidate
    raise RuntimeError("No previous composition in navigation history")


def vote(db: Database, composition: Composition, kind: Vote, *, enabled: bool = True) -> bool:
    """Set or clear a vote; return whether it changed the primary artist's score.

    The primary work carries the like or dislike into future curation.
    """
    stored = db.get_feedback(composition.id)
    before = Reaction.stored(stored)
    after = before.voted(kind, enabled)
    for flag, value in after.flags().items():
        if value != stored.get(flag, False):
            db.set_feedback(composition.id, flag, value)
    delta = after.value - before.value
    art = db.get_artwork(composition.decision.primary_artwork_id)
    if art:
        db.set_feedback(art.id, "like", after.includes("like"), target_type="artwork")
        db.set_feedback(art.id, "dislike", after.includes("dislike"), target_type="artwork")
        if art.artist and delta:
            db.adjust_artist_score(art.artist, delta)
    return delta != 0
