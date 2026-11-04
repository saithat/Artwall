"""Scriptable command line interface for local wallpaper curation."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from typing import Callable
from uuid import uuid4
import webbrowser

from artwall import rotation
from artwall.config import Settings, cache_dir, config_path, data_dir, database_path, load_config, write_atomic
from artwall.curation import OllamaCurator, Taste, choose_composition, score_candidate
from artwall.models import Artwork, Composition, CuratorDecision
from artwall.render import RenderResult, RenderSettings, render_wallpaper
from artwall.sources.artic import ArticSource
from artwall.sources.base import ArtworkSource
from artwall.storage import Database
from artwall.systemd import user_unit_dir, write_units


LOG = logging.getLogger("artwall")
Handler = Callable[[argparse.Namespace, Settings], None]
_MIN_DIMENSION = 1400  # longest side, in pixels, a wallpaper source must have


def _database() -> Database:
    return Database(database_path())


def _current(db: Database) -> Composition:
    current = db.get_current()
    if current is None:
        raise RuntimeError("No current composition")
    return current


def _primary(db: Database, composition: Composition) -> Artwork | None:
    return db.get_artwork(composition.decision.primary_artwork_id)


def _label(db: Database, composition: Composition) -> str:
    primary = _primary(db, composition)
    if primary is None:
        return composition.id
    return f"{primary.title} — {primary.artist or 'Artist unknown'} ({primary.date_display or 'date unknown'})"


def _announce(db: Database, composition: Composition, *, applied: bool) -> None:
    print(_label(db, composition))
    print(f"Image: {composition.output_path}")
    if not applied:
        print("Desktop application skipped (--no-apply).")


def _taste(db: Database, reserved: set[str]) -> Taste:
    preferences = db.list_artist_preferences()
    shown = [primary for composition in db.list_history(limit=8) if (primary := _primary(db, composition))]
    reserved_works = [art for artwork_id in reserved if (art := db.get_artwork(artwork_id))]
    return Taste(
        recent_ids=set(db.recent_artwork_ids(limit=35)) | reserved,
        recent_artists=[art.artist for art in shown + reserved_works if art.artist],
        liked_artists=[name for name, value in preferences.items() if value.score > 0 and not value.blacklisted],
        disliked_artists=[name for name, value in preferences.items() if value.score < -2 or value.blacklisted],
    )


def _known_low_resolution(art: Artwork) -> bool:
    """A cached download at the largest size the museum serves is still too small."""
    return bool(art.cached_image_path) and max(art.image_width or 0, art.image_height or 0) < _MIN_DIMENSION


def _select_pool(db: Database, settings: Settings, taste: Taste, reserved: set[str]) -> list[Artwork]:
    """Mostly the best-scoring works, plus seeded exploration and wildcards."""
    exclusions = rotation.Exclusions.load(db, settings)
    scored = [(score, art) for art in db.list_artworks(limit=3000)
              if art.public_domain and art.open_access and exclusions.allows(art)
              and not _known_low_resolution(art)
              and math.isfinite(score := score_candidate(art, taste))]
    ranked = [art for _, art in sorted(scored, key=lambda pair: pair[0], reverse=True)]
    count = settings.candidate_pool_size
    exploit = max(1, round(count * (1 - settings.explore_fraction - settings.wildcard_fraction)))
    randomizer = random.Random(time.strftime("%Y-%m-%d") + str(len(reserved)))
    selected = ranked[:exploit]
    adjacent = ranked[exploit:180]
    selected += randomizer.sample(adjacent, min(round(count * settings.explore_fraction), len(adjacent)))
    chosen = {art.id for art in selected}
    wildcard = [art for art in ranked if art.id not in chosen]
    selected += randomizer.sample(wildcard, max(0, min(count - len(selected), len(wildcard))))
    return selected[:count]


def _prepare_images(db: Database, source: ArtworkSource, candidates: list[Artwork]) -> list[Artwork]:
    available = []
    for art in candidates:
        try:
            if not source.verify_cached_image(art, min_dimension=_MIN_DIMENSION):
                source.download_image(art, data_dir() / "cache" / "images", width=4096, force=True)
                if _known_low_resolution(art):
                    LOG.info("candidate_excluded reason=low_resolution id=%s size=%sx%s",
                             art.id, art.image_width, art.image_height)
                    db.upsert_artwork(art)  # Remember the result instead of downloading it every round.
                    continue
            db.upsert_artwork(art)
            available.append(art)
        except Exception as exc:
            LOG.warning("candidate_excluded reason=image_failure id=%s error=%s", art.id, exc)
    return available


def _queued_since(db: Database, since: float) -> set[str]:
    """Works queued by any curation run, including a concurrent one, since a time."""
    return {artwork_id for composition in db.list_compositions(only_unshown=True)
            if composition.created_at >= since for artwork_id in composition.artwork_ids}


def _render(db: Database, decision: CuratorDecision, settings: Settings, output: Path) -> RenderResult:
    artworks = {artwork_id: art for artwork_id in decision.artwork_ids if (art := db.get_artwork(artwork_id))}
    if len(artworks) != len(decision.artworks):
        raise ValueError("Composition references an artwork missing from the database")
    return render_wallpaper(decision, artworks, output, RenderSettings(
        width=settings.width, height=settings.height, show_medium=settings.show_medium))


def _queue(db: Database, decision: CuratorDecision, settings: Settings, *, queue_priority: float = 0) -> Composition:
    """Render a decision and store it, with a JSON sidecar, before it is ever shown."""
    identifier = uuid4().hex
    output = data_dir() / "compositions" / f"{identifier}.png"
    rendered = _render(db, decision, settings, output)
    details = {key: value for key, value in asdict(rendered).items() if key != "path"}
    composition = Composition(identifier, decision, str(rendered.path),
                              render={**details, "width": settings.width, "height": settings.height})
    write_atomic(output.with_suffix(".json"), json.dumps(composition.to_dict(), ensure_ascii=False, indent=2) + "\n")
    db.add_composition(composition, queue_priority=queue_priority)
    return composition


def cmd_init(_: argparse.Namespace, settings: Settings) -> None:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).resolve().parent / "config.example.toml", path)
    for directory in (data_dir(), cache_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    with _database():
        pass
    print(f"Config: {path}\nData: {data_dir()}\nCache: {cache_dir()}")


def cmd_refresh(args: argparse.Namespace, settings: Settings) -> None:
    if not settings.source_artic:
        raise RuntimeError("ArtIC source is disabled in config")
    if args.page is not None and args.page < 1:
        raise ValueError("Page must be at least 1")
    added = 0
    with _database() as db:
        source = ArticSource(db)
        terms = (args.query,) if args.query else settings.search_terms
        try:
            saved_page = max(1, int(db.get_state("next_search_page") or "1"))
        except ValueError:
            saved_page = 1
        page = args.page or (1 if args.query else saved_page)
        for term in terms:
            try:
                found = source.search(term, page=page, limit=args.limit)
                for art in found:
                    db.upsert_artwork(art)
                added += len(found)
                LOG.info("source_search source=artic query=%r page=%d count=%d", term, page, len(found))
            except Exception as exc:
                LOG.warning("source_search_failed source=artic query=%r page=%d error=%s", term, page, exc)
        if not args.query and args.page is None:
            db.set_state("next_search_page", str(page + 1 if added else 1))
        cached = db.count_artworks()
    if not added and not cached:
        raise RuntimeError("No museum metadata available; ArtIC search failed and the cache is empty")
    print(f"Refreshed {added} ArtIC records from page {page}. Metadata remains cached locally.")


def cmd_curate(args: argparse.Namespace, settings: Settings) -> None:
    count = args.count or settings.curation_batch_size
    if not 1 <= count <= 30:
        raise ValueError("Count must be between 1 and 30")
    curator = (OllamaCurator(settings.model, settings.ollama_url, description_words=settings.caption_words)
               if settings.use_local_model and not args.offline else None)
    created = 0
    started = time.time()
    queue_priority = started if args.next else 0
    with _database() as db:
        source = ArticSource(db)
        reserved = {artwork_id for composition in rotation.upcoming(db, settings)
                    for artwork_id in composition.artwork_ids}
        for _ in range(count):
            reserved |= _queued_since(db, started)
            taste = _taste(db, reserved)
            pool = _prepare_images(db, source, _select_pool(db, settings, taste, reserved))
            if not pool:
                break
            decision = choose_composition(pool, taste, max_artworks=settings.max_artworks,
                                          screen_ratio=settings.width / settings.height, curator=curator,
                                          feedback=db.curation_feedback())
            # The gallery, a timer, or the command line may be curating at the
            # same time. Re-check just before rendering so no work is queued twice.
            reserved |= _queued_since(db, started)
            if reserved.intersection(decision.artwork_ids):
                LOG.info("composition_skipped reason=queued_concurrently artworks=%s", decision.artwork_ids)
                continue
            composition = _queue(db, decision, settings, queue_priority=queue_priority)
            reserved.update(composition.artwork_ids)
            created += 1
            print(f"Queued {composition.id[:8]}: {_label(db, composition)}", flush=True)
    if not created:
        raise RuntimeError("No new composition could be made. Run 'artwall refresh' or inspect cached image failures.")


def cmd_next(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        composition = (rotation.advance_if_due(db, settings, apply=not args.no_apply)
                       if args.if_due else rotation.advance(db, settings, apply=not args.no_apply))
        if composition:
            _announce(db, composition, applied=not args.no_apply)
        else:
            print("Rotation interval has not elapsed; wallpaper unchanged.")


def cmd_previous(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        _announce(db, rotation.go_back(db, settings, apply=not args.no_apply), applied=not args.no_apply)


def cmd_feedback(args: argparse.Namespace, settings: Settings) -> None:
    kind = args.command
    with _database() as db:
        current = _current(db)
        if kind == "skip":
            db.set_feedback(current.id, "skip")
        elif kind == "blacklist-current":
            primary = _primary(db, current)
            if primary:
                db.set_artwork_blacklisted(primary.id)
                print(f"Blacklisted {primary.title}")
        else:
            changed = rotation.vote(db, current, kind)
            print(f"Marked current composition: {kind}" if changed else f"Already marked: {kind}")
        if kind in ("skip", "dislike"):
            try:
                _announce(db, rotation.advance(db, settings, apply=not args.no_apply), applied=not args.no_apply)
            except RuntimeError as exc:
                if kind == "skip":
                    raise
                print(f"Thumbs down saved; wallpaper unchanged: {exc}")


def cmd_artist(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        artist = args.artist
        if not artist and (current := db.get_current()) and (primary := _primary(db, current)):
            artist = primary.artist
        if not artist:
            raise RuntimeError("No artist supplied or present in current composition")
        if args.command == "blacklist-artist":
            db.set_artist_blacklisted(artist)
            print(f"Blacklisted artist: {artist}")
        else:
            score = db.adjust_artist_score(artist, 2 if args.command == "more-from-artist" else -2)
            print(f"Artist preference: {artist} = {score}")


def cmd_info(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        composition = _current(db)
        if args.json:
            print(json.dumps(composition.to_dict(), ensure_ascii=False, indent=2))
            return
        decision = composition.decision
        print(f"Layout: {decision.layout}")
        for index, artwork_id in enumerate(composition.artwork_ids, 1):
            art = db.get_artwork(artwork_id)
            if art is None:
                continue
            print(f"\n{index}. {art.title}")
            context = art.movement or art.period
            if context and "culture or style" in context.casefold():
                context = None
            print(" · ".join(x for x in (art.artist, art.date_display, context) if x))
            for line in (art.medium, art.canonical_url, art.rights_statement and f"Rights: {art.rights_statement}"):
                if line:
                    print(line)
        for label, text in (("", decision.description), ("Pairing: ", decision.relationship),
                            ("Curator: ", decision.curator_reason)):
            if text:
                print(f"\n{label}{text}")


def cmd_debug(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        composition = _current(db)
        artworks = [art.to_dict() for artwork_id in composition.artwork_ids if (art := db.get_artwork(artwork_id))]
        print(json.dumps({"composition": composition.to_dict(), "artworks": artworks}, ensure_ascii=False, indent=2))


def cmd_open(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        primary = _primary(db, _current(db))
    if primary is None or not primary.canonical_url:
        raise RuntimeError("Current artwork has no canonical museum page")
    print(primary.canonical_url)
    if not args.print_only:
        webbrowser.open(primary.canonical_url)


def cmd_list(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        records = db.list_favorites(limit=args.limit) if args.command == "favorites" else db.list_history(limit=args.limit)
        for composition in records:
            print(f"{composition.id[:8]}  {_label(db, composition)}")


def cmd_status(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        current = db.get_current()
        print(f"Artworks: {db.count_artworks()}\nQueued: {sum(1 for _ in rotation.upcoming(db, settings))}\n"
              f"Current: {_label(db, current) if current else 'none'}")
    print(f"Database: {database_path()}\nWallpaper: {cache_dir() / 'current.png'}")
    print(f"Backend: {settings.wallpaper_backend} · Lock screen: {settings.lockscreen_backend}")


def cmd_render(args: argparse.Namespace, settings: Settings) -> None:
    with _database() as db:
        composition = _current(db)
        output = Path(args.output).expanduser() if args.output else Path(composition.output_path)
        print(_render(db, composition.decision, settings, output).path)


def cmd_install_timers(args: argparse.Namespace, settings: Settings) -> None:
    executable = shutil.which("artwall")
    if not executable:
        raise RuntimeError("Install the artwall CLI first (for example: uv tool install --editable .)")
    for path in write_units(settings, user_unit_dir(), str(Path(executable).resolve())):
        print(path)
    if not args.no_enable:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        # Reenable removes the old timers.target link when upgrading to the
        # graphical-session timer, so a lingering user manager cannot rotate early.
        subprocess.run(["systemctl", "--user", "reenable", "artwall-rotate.timer"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "artwall-curate.timer"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "artwall-rotate.timer"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "artwall-login.service"], check=True)
        print("User timers and login rotation enabled.")


def cmd_gui(args: argparse.Namespace, settings: Settings) -> None:
    from artwall.gui import run_gui

    run_gui(args.port, open_browser=not args.no_open)


_COMMANDS: dict[str, tuple[Handler, str]] = {
    "init": (cmd_init, "create user configuration"),
    "status": (cmd_status, "show local status"),
    "info": (cmd_info, "inspect the current works"),
    "debug-current": (cmd_debug, "show source data and curator decision"),
    "render": (cmd_render, "re-render the current composition"),
    "open": (cmd_open, "open the museum page"),
    "next": (cmd_next, "show the next cached wallpaper"),
    "previous": (cmd_previous, "show the previous wallpaper"),
    "like": (cmd_feedback, "like current work"),
    "dislike": (cmd_feedback, "dislike current work"),
    "favorite": (cmd_feedback, "favorite current work"),
    "skip": (cmd_feedback, "skip current work"),
    "blacklist-current": (cmd_feedback, "blacklist current primary work"),
    "more-from-artist": (cmd_artist, "increase an artist preference"),
    "less-from-artist": (cmd_artist, "decrease an artist preference"),
    "blacklist-artist": (cmd_artist, "blacklist an artist"),
    "history": (cmd_list, "list viewed compositions"),
    "favorites": (cmd_list, "list favorites"),
    "refresh": (cmd_refresh, "refresh museum metadata"),
    "curate": (cmd_curate, "prepare future wallpapers"),
    "install-timers": (cmd_install_timers, "install and enable user timers"),
    "gui": (cmd_gui, "open the local gallery and settings"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artwall", description="A personal museum curator for your wallpaper")
    parser.add_argument("--verbose", action="store_true", help="show detailed logs")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (handler, help_text) in _COMMANDS.items():
        command = sub.add_parser(name, help=help_text)
        command.set_defaults(handler=handler)
        if name in ("next", "previous", "skip", "dislike"):
            command.add_argument("--no-apply", action="store_true", help="update local state without desktop IPC")
        if name == "next":
            command.add_argument("--if-due", action="store_true", help="advance only after the rotation interval has elapsed")
        if name == "info":
            command.add_argument("--json", action="store_true")
        if name == "open":
            command.add_argument("--print-only", action="store_true")
        if name == "render":
            command.add_argument("--output")
        if name in ("history", "favorites"):
            command.add_argument("--limit", type=int, default=20)
        if name == "refresh":
            command.add_argument("--query", help="search one ArtIC topic")
            command.add_argument("--limit", type=int, default=30)
            command.add_argument("--page", type=int, help="search a specific museum results page")
        if name == "curate":
            command.add_argument("--count", type=int)
            command.add_argument("--offline", action="store_true", help="use local deterministic curator")
            command.add_argument("--next", action="store_true", help="place new wallpapers ahead of the existing queue")
        if name in ("more-from-artist", "less-from-artist", "blacklist-artist"):
            command.add_argument("artist", nargs="?", help="defaults to the current primary artist")
        if name == "install-timers":
            command.add_argument("--no-enable", action="store_true", help="write units without enabling timers")
        if name == "gui":
            command.add_argument("--port", type=int, default=8765, help="localhost port (default: 8765)")
            command.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        args.handler(args, load_config())
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
