"""A small, local gallery and control panel for Artwall.

The server has no account, cookies, remote assets, or non-loopback listener.  All
mutations require a per-process token and a matching same-origin request.
"""

from __future__ import annotations

import errno
import json
import logging
import re
import secrets
import shutil
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from pathlib import Path
from threading import Lock
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import urlopen

from PIL import Image, ImageOps

from artwall import rotation
from artwall.config import Settings, config_path, data_dir, database_path, load_config, update_config_values
from artwall.models import Artwork, Composition
from artwall.storage import Database
from artwall.systemd import update_rotate_timer, user_unit_dir


LOG = logging.getLogger("artwall.gui")
ASSETS = Path(__file__).parent / "gui_assets"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_BODY = 16_384


def _artwork_summary(art: Artwork) -> dict[str, str]:
    return {"id": art.id, "title": art.title or "Untitled", "artist": art.artist or "Artist unknown",
            "date": art.date_display or "", "url": art.canonical_url or "", "source": art.source}


def _record_summary(db: Database, composition: Composition) -> dict[str, Any]:
    decision = composition.decision
    artworks = [_artwork_summary(art) for artwork_id in composition.artwork_ids
                if (art := db.get_artwork(artwork_id))]
    primary = next((art for art in artworks if art["id"] == decision.primary_artwork_id),
                   artworks[0] if artworks else None)
    flags = db.get_feedback(composition.id)
    return {"id": composition.id, "artworks": artworks,
            "primary_artwork_id": primary["id"] if primary else None,
            "title": primary["title"] if primary else "Untitled composition",
            "artist": primary["artist"] if primary else "Artist unknown",
            "date": primary["date"] if primary else "",
            "description": decision.description, "relationship": decision.relationship,
            "layout": decision.layout, "created_at": composition.created_at, "shown_at": composition.shown_at,
            "image": f"/image/{composition.id}", "thumbnail": f"/thumb/{composition.id}",
            "feedback": {kind: flags.get(kind, False) for kind in ("like", "dislike", "favorite")},
            "comment": db.get_comment(composition.id)}


def gallery_state(db: Database, settings: Settings) -> dict[str, Any]:
    """A compact, factual view of the active queue and viewed compositions."""
    current = db.get_current()
    # History lists every showing, newest first; the gallery shows each composition once.
    history = {composition.id: composition for composition in db.list_history(limit=80)}.values()
    works = sorted(db.list_artworks(limit=5000),
                   key=lambda art: (art.title.casefold(), (art.artist or "").casefold()))
    return {
        "settings": {"interval_minutes": settings.interval_minutes, "model": settings.model,
                     "max_artworks": settings.max_artworks},
        "current": _record_summary(db, current) if current else None,
        "queued": [_record_summary(db, composition) for composition in islice(rotation.upcoming(db, settings), 60)],
        "history": [_record_summary(db, composition) for composition in history],
        "blacklisted_artists": db.list_blacklisted_artists(),
        "blacklisted_artworks": [_artwork_summary(art) for art in db.list_blacklisted_artworks()],
        "artworks": [_artwork_summary(art) for art in works],
    }


def vote_from_gallery(db: Database, config_file: Path, composition_id: str, kind: rotation.Vote,
                      expected_current: str | None) -> str:
    """Toggle a vote; thumbs down on the current wallpaper also rotates to the next one."""
    composition = db.get_composition(composition_id)
    if composition is None:
        raise ValueError("Unknown composition")
    current = db.get_current()
    is_current = current is not None and current.id == composition_id
    if kind == "dislike" and expected_current == composition_id and not is_current:
        return "The wallpaper has already changed."
    was_set = db.get_feedback(composition_id).get(kind, False)
    # A rejected work can remain on screen when no alternative is cached or
    # the desktop backend fails. Pressing thumbs down again retries the change.
    enabled = (kind == "dislike" and is_current) or not was_set
    rotation.vote(db, composition, kind, enabled=enabled)
    if kind == "dislike" and is_current:
        try:
            shown = rotation.advance(db, load_config(config_file))
        except rotation.NoAlternativeError:
            return "Thumbs down saved. No other eligible artwork is ready; curate more to continue."
        except (OSError, RuntimeError) as exc:
            LOG.warning("Could not advance wallpaper after thumbs down: %s", exc)
            return "Thumbs down saved, but the wallpaper could not change. Check the desktop backend."
        return f"Thumbs down saved. Now showing {_record_summary(db, shown)['title']}."
    if kind == "dislike" and enabled and not was_set and composition.shown_at is None:
        return "Thumbs down saved. Removed from upcoming rotation."
    return "Feedback saved"


def _installed_models(settings: Settings) -> list[str]:
    base = urlsplit(settings.ollama_url)
    if base.scheme != "http" or base.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return []
    try:
        with urlopen(settings.ollama_url.rstrip("/") + "/api/tags", timeout=2) as response:
            value = json.load(response)
        return sorted({item["name"] for item in value.get("models", [])
                       if isinstance(item, dict) and isinstance(item.get("name"), str)})
    except (OSError, ValueError, KeyError, TypeError):
        return []


@dataclass
class GuiContext:
    database_path: Path
    data_root: Path
    config_file: Path
    unit_dir: Path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    feedback_lock: Lock = field(default_factory=Lock, repr=False)

    def image_path(self, composition_id: str) -> Path:
        if not _ID.fullmatch(composition_id):
            raise FileNotFoundError("Invalid composition ID")
        with Database(self.database_path) as db:
            record = db.get_composition(composition_id)
        if not record:
            raise FileNotFoundError("Unknown composition")
        output = Path(record.output_path).resolve()
        compositions = (self.data_root / "compositions").resolve()
        if not output.is_relative_to(compositions) or output.suffix.lower() != ".png" or not output.is_file():
            raise FileNotFoundError("Composition image unavailable")
        return output

    def thumbnail_path(self, composition_id: str) -> Path:
        source = self.image_path(composition_id)
        directory = self.data_root / "cache" / "gui-thumbnails"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{composition_id}.jpg"
        if destination.is_file() and destination.stat().st_mtime >= source.stat().st_mtime:
            return destination
        temporary = directory / f".{composition_id}-{secrets.token_hex(6)}.jpg"
        try:
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((800, 400), Image.Resampling.LANCZOS)
                image.save(temporary, "JPEG", quality=86, optimize=True)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination


def make_handler(context: GuiContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Artwall/1"

        def _origin(self) -> str:
            return f"http://127.0.0.1:{cast(tuple[str, int], self.server.server_address)[1]}"

        def _trusted_request(self) -> bool:
            if self.headers.get("Host") != self._origin().removeprefix("http://"):
                self._error(403, "Invalid host")
                return False
            origin = self.headers.get("Origin")
            if origin and origin != self._origin():
                self._error(403, "Invalid origin")
                return False
            return True

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self._headers(status, content_type, len(body))
            self.wfile.write(body)

        def _json(self, status: int, value: dict[str, Any]) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _file(self, path: Path, content_type: str) -> None:
            self._headers(200, content_type, path.stat().st_size)
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile)

        def do_GET(self) -> None:
            if not self._trusted_request():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    self._file(ASSETS / "index.html", "text/html; charset=utf-8")
                elif path == "/app.css":
                    self._file(ASSETS / "app.css", "text/css; charset=utf-8")
                elif path == "/app.js":
                    self._file(ASSETS / "app.js", "text/javascript; charset=utf-8")
                elif path == "/api/state":
                    with Database(context.database_path) as db:
                        state = gallery_state(db, load_config(context.config_file))
                    state["csrf_token"] = context.token
                    self._json(200, state)
                elif path == "/api/models":
                    self._json(200, {"models": _installed_models(load_config(context.config_file))})
                elif path.startswith("/image/"):
                    self._file(context.image_path(path.removeprefix("/image/")), "image/png")
                elif path.startswith("/thumb/"):
                    self._file(context.thumbnail_path(path.removeprefix("/thumb/")), "image/jpeg")
                else:
                    self._error(404, "Not found")
            except FileNotFoundError:
                self._error(404, "Image unavailable")
            except (ValueError, OSError) as exc:
                LOG.exception("GUI GET failed: %s", exc)
                self._error(500, "Unable to load gallery")

        def do_POST(self) -> None:
            if not self._trusted_request():
                return
            if self.headers.get("X-Artwall-Token") != context.token:
                self._error(403, "Invalid request token")
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self._error(415, "Expected JSON")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > _MAX_BODY:
                    raise ValueError("Request too large or empty")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                notice = self._mutate(urlsplit(self.path).path, body)
                self._json(200, {"ok": True, "notice": notice})
            except (ValueError, KeyError, TypeError) as exc:
                self._error(400, str(exc))
            except (OSError, RuntimeError) as exc:
                LOG.exception("GUI POST failed: %s", exc)
                self._error(500, "Could not save this change")

        def _mutate(self, path: str, body: dict[str, Any]) -> str:
            if path == "/api/settings":
                interval = body.get("interval_minutes")
                count = body.get("max_artworks")
                model = body.get("model")
                if type(interval) is not int or not 15 <= interval <= 1440:
                    raise ValueError("Rotation interval must be 15–1440 minutes")
                if type(count) is not int or count not in (1, 2, 3):
                    raise ValueError("Artwork count must be 1, 2, or 3")
                if not isinstance(model, str) or not model.strip() or len(model) > 120 or any(ord(ch) < 32 for ch in model):
                    raise ValueError("Enter a valid local Ollama model name")
                old = load_config(context.config_file)
                settings = update_config_values({"rotation": {"interval_minutes": interval},
                                                 "curation": {"max_artworks": count},
                                                 "llm": {"model": model.strip()}}, path=context.config_file)
                if interval != old.interval_minutes:
                    executable = shutil.which("artwall")
                    if not executable or not update_rotate_timer(
                            settings, str(Path(executable).resolve()), directory=context.unit_dir):
                        return "Settings saved. Run artwall install-timers to apply the new rotation interval."
                return "Settings saved. New artwork count applies to upcoming compositions."
            if path == "/api/feedback":
                composition_id = body.get("id")
                kind = body.get("kind")
                expected_current = body.get("current_id")
                if not isinstance(composition_id, str):
                    raise ValueError("Choose a composition")
                if kind not in ("like", "dislike", "favorite"):
                    raise ValueError("Feedback must be like, dislike, or favorite")
                if expected_current is not None and not isinstance(expected_current, str):
                    raise ValueError("Invalid current composition")
                # ThreadingHTTPServer can handle requests from multiple gallery
                # tabs at once. Keep feedback and its rotation in one operation.
                with context.feedback_lock, Database(context.database_path) as db:
                    return vote_from_gallery(db, context.config_file, composition_id, kind, expected_current)
            if path == "/api/comment":
                composition_id = body.get("id")
                comment = body.get("comment")
                if not isinstance(composition_id, str) or not isinstance(comment, str) or len(comment) > 5000:
                    raise ValueError("Comment must be at most 5000 characters")
                with Database(context.database_path) as db:
                    db.set_comment(composition_id, comment.strip())
                return "Note saved"
            if path == "/api/blacklist/artist":
                artist = body.get("artist")
                blocked = body.get("blocked")
                if not isinstance(artist, str) or not artist.strip() or len(artist) > 250 or type(blocked) is not bool:
                    raise ValueError("Enter an artist name")
                with Database(context.database_path) as db:
                    db.set_artist_blacklisted(artist.strip(), blocked)
                return "Artist blacklist updated"
            if path == "/api/blacklist/artwork":
                identifier = body.get("id")
                blocked = body.get("blocked")
                if not isinstance(identifier, str) or type(blocked) is not bool:
                    raise ValueError("Choose an artwork")
                with Database(context.database_path) as db:
                    db.set_artwork_blacklisted(identifier, blocked)
                return "Artwork blacklist updated"
            raise ValueError("Unknown action")

        def log_message(self, format: str, *args: object) -> None:
            LOG.info("%s %s", self.address_string(), format % args)

    return Handler


def create_server(context: GuiContext, port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(context))
    server.daemon_threads = True
    return server


def run_gui(port: int, *, open_browser: bool = True) -> None:
    address = f"http://127.0.0.1:{port}/"
    context = GuiContext(database_path(), data_dir(), config_path(), user_unit_dir())
    try:
        server = create_server(context, port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        try:
            with urlopen(address + "api/state", timeout=2) as response:
                if not response.headers.get("Server", "").startswith("Artwall/"):
                    raise RuntimeError("Another application is using the GUI port")
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Port {port} is in use; choose another with --port") from error
        print(f"Artwall gallery already running: {address}", flush=True)
        if open_browser:
            webbrowser.open(address)
        return
    with server:
        address = f"http://127.0.0.1:{server.server_address[1]}/"
        print(f"Artwall gallery: {address}", flush=True)
        if open_browser:
            webbrowser.open(address)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\nArtwall gallery stopped.")
