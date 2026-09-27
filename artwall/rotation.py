"""Offline rotation through prepared compositions, navigation history, and votes.

Nothing here calls a museum or model: rotation only shows what is already rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Iterable, Iterator, Literal, get_args
from uuid import uuid4

from PIL import Image

from artwall.config import Settings, cache_dir, write_atomic
from artwall.curation import lock_screen_safe
from artwall.desktop import lock_screen_backend, wallpaper_backend
from artwall.models import Artwork, Composition
from artwall.storage import Database


LOG = logging.getLogger(__name__)
Vote = Literal["like", "dislike", "favorite"]


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
                   frozenset(db.list_blacklisted_artists()), settings.public_lock_screen_safe)

    def allows(self, artwork: Artwork) -> bool:
        return (artwork.id not in self.artwork_ids
                and (not artwork.artist or artwork.artist not in self.artists)
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
                or (settings.max_artworks == 1 and composition.decision.layout == "artwork_detail")
                or not _valid_image(composition, settings)):
            continue
        works = [db.get_artwork(artwork_id) for artwork_id in ids]
        if all(work is not None and exclusions.allows(work) for work in works):
            yield composition


def upcoming(db: Database, settings: Settings) -> Iterator[Composition]:
    """Unshown compositions that can still be shown, oldest first."""
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

    Like and favorite clear a dislike, and a dislike clears both. The primary
    work carries the like or dislike into future curation.
    """
    old = db.get_feedback(composition.id)
    before: dict[Vote, bool] = {flag: old.get(flag, False) for flag in get_args(Vote)}
    after = dict(before)
    if kind == "like":
        after["like"], after["dislike"], after["favorite"] = enabled, False, before["favorite"] and enabled
    elif kind == "dislike":
        after["like"], after["dislike"], after["favorite"] = False, enabled, False
    else:
        after["like"], after["dislike"], after["favorite"] = True, False, enabled
    for flag, value in after.items():
        if value != before[flag]:
            db.set_feedback(composition.id, flag, value)

    def score(flags: dict[Vote, bool]) -> int:
        return int(flags["like"]) + int(flags["favorite"]) - int(flags["dislike"])

    delta = score(after) - score(before)
    art = db.get_artwork(composition.decision.primary_artwork_id)
    if art:
        db.set_feedback(art.id, "like", after["like"], target_type="artwork")
        db.set_feedback(art.id, "dislike", after["dislike"], target_type="artwork")
        if art.artist and delta:
            db.adjust_artist_score(art.artist, delta)
    return delta != 0
