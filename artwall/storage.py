"""SQLite records for artworks, queued compositions, history, and feedback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Literal, Mapping, NamedTuple, get_args

from artwall.config import Settings, with_gallery_settings
from artwall.models import Artwork, Composition


LOG = logging.getLogger(__name__)
_SCHEMA_VERSION = 3
FeedbackKind = Literal["like", "dislike", "favorite", "skip", "blacklist"]
TargetType = Literal["composition", "artwork"]
_PUT_STATE = ("INSERT INTO state(key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value")


class ArtistPreference(NamedTuple):
    score: int = 0
    blacklisted: bool = False


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            raise RuntimeError(f"Database version {version} is newer than this artwall release")
        if version == 0:
            with self.connection:
                self.connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS artworks (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        cached_image_path TEXT,
                        updated_at REAL NOT NULL,
                        UNIQUE(source, source_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_artworks_source ON artworks(source);

                    CREATE TABLE IF NOT EXISTS compositions (
                        id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        shown_at REAL,
                        show_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_compositions_queue
                        ON compositions(shown_at, created_at);
                    CREATE TABLE IF NOT EXISTS composition_artworks (
                        composition_id TEXT NOT NULL REFERENCES compositions(id) ON DELETE CASCADE,
                        artwork_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        PRIMARY KEY(composition_id, artwork_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_composition_artwork_id
                        ON composition_artworks(artwork_id);

                    CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        composition_id TEXT NOT NULL REFERENCES compositions(id),
                        shown_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_history_shown ON history(shown_at DESC);

                    CREATE TABLE IF NOT EXISTS feedback (
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        enabled INTEGER NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(target_type, target_id, kind)
                    );
                    CREATE TABLE IF NOT EXISTS artist_preferences (
                        artist TEXT PRIMARY KEY,
                        score INTEGER NOT NULL DEFAULT 0,
                        blacklisted INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS api_cache (
                        key TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    PRAGMA user_version = 1;
                    """
                )
            version = 1
        if version == 1:
            with self.connection:
                self.connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS composition_comments (
                        composition_id TEXT PRIMARY KEY
                            REFERENCES compositions(id) ON DELETE CASCADE,
                        comment TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    PRAGMA user_version = 2;
                    """
                )
            version = 2
        if version == 2:
            # ALTER TABLE is not idempotent. Take the write lock first and
            # re-check, so concurrent openers (a login rotation and a timer)
            # or an interrupted earlier attempt cannot fail every later open.
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(compositions)")}
                if self.connection.execute("PRAGMA user_version").fetchone()[0] == 2:
                    if "queue_priority" not in columns:
                        self.connection.execute(
                            "ALTER TABLE compositions ADD COLUMN queue_priority REAL NOT NULL DEFAULT 0")
                    self.connection.execute("DROP INDEX IF EXISTS idx_compositions_queue")
                    self.connection.execute(
                        "CREATE INDEX idx_compositions_queue "
                        "ON compositions(shown_at, queue_priority DESC, created_at)")
                    self.connection.execute("PRAGMA user_version = 3")
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert_artwork(self, artwork: Artwork) -> None:
        if not artwork.cached_image_path:
            old = self.get_artwork(artwork.id)
            if old and old.cached_image_path:
                artwork.cached_image_path = old.cached_image_path
        payload = json.dumps(artwork.to_dict(), ensure_ascii=False)
        with self.connection:
            self.connection.execute(
                """INSERT INTO artworks(id, source, source_id, payload, cached_image_path, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                       cached_image_path=excluded.cached_image_path,
                       updated_at=excluded.updated_at""",
                (artwork.id, artwork.source, artwork.source_id, payload, artwork.cached_image_path, time.time()),
            )

    def get_artwork(self, artwork_id: str) -> Artwork | None:
        row = self.connection.execute(
            "SELECT payload FROM artworks WHERE id = ?", (artwork_id,)
        ).fetchone()
        return Artwork.from_dict(json.loads(row["payload"])) if row else None

    def list_artworks(self, *, limit: int = 1000) -> list[Artwork]:
        rows = self.connection.execute(
            "SELECT payload FROM artworks ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Artwork.from_dict(json.loads(row["payload"])) for row in rows]

    def count_artworks(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM artworks").fetchone()[0])

    def add_composition(self, composition: Composition, *, queue_priority: float = 0) -> None:
        """Insert a composition; artwork IDs are indexed for repeat avoidance."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO compositions(id, payload, created_at, queue_priority) VALUES (?, ?, ?, ?)",
                (composition.id, json.dumps(composition.to_dict(), ensure_ascii=False),
                 composition.created_at, queue_priority),
            )
            self.connection.executemany(
                "INSERT INTO composition_artworks(composition_id, artwork_id, position) VALUES (?, ?, ?)",
                [(composition.id, artwork_id, index) for index, artwork_id in enumerate(composition.artwork_ids)],
            )

    @staticmethod
    def _compositions(rows: Iterable[sqlite3.Row]) -> list[Composition]:
        result = []
        for row in rows:
            try:
                result.append(Composition.from_dict(json.loads(row["payload"]), shown_at=row["shown_at"],
                                                    show_count=row["show_count"]))
            except (ValueError, KeyError, TypeError) as exc:
                # One damaged record must not stop rotation through the others.
                LOG.warning("Ignoring unreadable composition %s: %s", row["id"], exc)
        return result

    def get_composition(self, composition_id: str) -> Composition | None:
        rows = self.connection.execute("SELECT * FROM compositions WHERE id = ?", (composition_id,))
        return next(iter(self._compositions(rows)), None)

    def list_compositions(self, *, only_unshown: bool = False, limit: int = 1000) -> list[Composition]:
        if only_unshown:
            query = ("SELECT * FROM compositions WHERE shown_at IS NULL "
                     "ORDER BY queue_priority DESC, created_at, id LIMIT ?")
        else:
            query = "SELECT * FROM compositions ORDER BY created_at DESC, id LIMIT ?"
        return self._compositions(self.connection.execute(query, (limit,)))

    def get_state(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(_PUT_STATE, (key, value))

    def gallery_settings(self) -> dict[str, object]:
        """Values saved from the gallery, which take precedence over config.toml."""
        saved = json.loads(self.get_state("gallery_settings") or "{}")
        return saved if isinstance(saved, dict) else {}

    def settings(self, configured: Settings) -> Settings:
        """The configuration with gallery-saved values applied."""
        return with_gallery_settings(configured, self.gallery_settings())

    def save_gallery_settings(self, configured: Settings, values: Mapping[str, object]) -> Settings:
        """Validate and save gallery values, returning the settings they produce."""
        saved = {**self.gallery_settings(), **values}
        settings = with_gallery_settings(configured, saved)
        self.set_state("gallery_settings", json.dumps(saved, sort_keys=True))
        return settings

    def get_current(self) -> Composition | None:
        identifier = self.get_state("current_composition_id")
        return self.get_composition(identifier) if identifier else None

    def mark_shown(self, composition_id: str) -> None:
        now = time.time()
        with self.connection:
            updated = self.connection.execute(
                "UPDATE compositions SET shown_at=?, show_count=show_count+1 WHERE id=?",
                (now, composition_id),
            )
            if not updated.rowcount:
                raise KeyError(f"Unknown composition: {composition_id}")
            self.connection.execute(
                "INSERT INTO history(composition_id, shown_at) VALUES (?, ?)", (composition_id, now)
            )
            self.connection.execute(_PUT_STATE, ("current_composition_id", composition_id))

    def list_history(self, *, limit: int = 30) -> list[Composition]:
        return self._compositions(self.connection.execute(
            """SELECT c.* FROM history h JOIN compositions c ON c.id=h.composition_id
               ORDER BY h.id DESC LIMIT ?""",
            (limit,),
        ))

    def recent_artwork_ids(self, *, limit: int = 100) -> list[str]:
        rows = self.connection.execute(
            """SELECT ca.artwork_id, MAX(h.id) AS latest FROM history h
               JOIN composition_artworks ca ON ca.composition_id=h.composition_id
               GROUP BY ca.artwork_id ORDER BY latest DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [row["artwork_id"] for row in rows]

    def set_feedback(
        self, target_id: str, kind: FeedbackKind, enabled: bool = True, *,
        target_type: TargetType = "composition",
    ) -> None:
        if kind not in get_args(FeedbackKind):
            raise ValueError(f"Unknown feedback kind: {kind}")
        if target_type not in get_args(TargetType):
            raise ValueError(f"Unknown feedback target type: {target_type}")
        with self.connection:
            self.connection.execute(
                """INSERT INTO feedback(target_type, target_id, kind, enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(target_type, target_id, kind) DO UPDATE SET
                       enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (target_type, target_id, kind, int(enabled), time.time()),
            )

    def get_feedback(self, target_id: str, *, target_type: TargetType = "composition") -> dict[str, bool]:
        rows = self.connection.execute(
            "SELECT kind, enabled FROM feedback WHERE target_type=? AND target_id=?",
            (target_type, target_id),
        ).fetchall()
        return {row["kind"]: bool(row["enabled"]) for row in rows}

    def flagged_ids(self, *kinds: FeedbackKind, target_type: TargetType = "composition") -> set[str]:
        """IDs with any of these feedback kinds currently enabled."""
        rows = self.connection.execute(
            f"""SELECT target_id FROM feedback WHERE target_type=? AND enabled=1
                AND kind IN ({",".join("?" * len(kinds))})""",
            (target_type, *kinds),
        ).fetchall()
        return {str(row["target_id"]) for row in rows}

    def set_comment(self, composition_id: str, text: str) -> None:
        """Save a private note about a composition; an empty note clears it."""
        if self.get_composition(composition_id) is None:
            raise KeyError(f"Unknown composition: {composition_id}")
        with self.connection:
            if text.strip():
                self.connection.execute(
                    """INSERT INTO composition_comments(composition_id, comment, updated_at)
                       VALUES (?, ?, ?) ON CONFLICT(composition_id) DO UPDATE SET
                       comment=excluded.comment, updated_at=excluded.updated_at""",
                    (composition_id, text, time.time()),
                )
            else:
                self.connection.execute(
                    "DELETE FROM composition_comments WHERE composition_id=?", (composition_id,)
                )

    def get_comment(self, composition_id: str) -> str:
        row = self.connection.execute(
            "SELECT comment FROM composition_comments WHERE composition_id=?", (composition_id,)
        ).fetchone()
        return str(row["comment"]) if row else ""

    def curation_feedback(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """Recent private impressions, with enough context to guide the curator."""
        rows = self.connection.execute(
            """SELECT c.*, n.comment FROM composition_comments n
               JOIN compositions c ON c.id=n.composition_id
               ORDER BY n.updated_at DESC, c.id LIMIT ?""", (limit,),
        )
        result = []
        for row in rows:
            compositions = self._compositions([row])
            if not compositions:
                continue
            composition = compositions[0]
            works = [work for i in composition.artwork_ids if (work := self.get_artwork(i))]
            result.append({"layout": composition.decision.layout,
                           "works": [{"title": work.title[:140], "artist": (work.artist or "")[:100]}
                                     for work in works],
                           "comment": str(row["comment"])[:800]})
        return result

    def set_artwork_blacklisted(self, artwork_id: str, enabled: bool = True) -> None:
        if self.get_artwork(artwork_id) is None:
            raise KeyError(f"Unknown artwork: {artwork_id}")
        self.set_feedback(artwork_id, "blacklist", enabled, target_type="artwork")

    def list_blacklisted_artworks(self) -> list[Artwork]:
        rows = self.connection.execute(
            """SELECT a.payload FROM artworks a JOIN feedback f
               ON f.target_type='artwork' AND f.target_id=a.id
               WHERE f.kind='blacklist' AND f.enabled=1
               ORDER BY a.id COLLATE NOCASE"""
        ).fetchall()
        return [Artwork.from_dict(json.loads(row["payload"])) for row in rows]

    def list_favorites(self, *, limit: int = 100) -> list[Composition]:
        return self._compositions(self.connection.execute(
            """SELECT c.* FROM compositions c JOIN feedback f
               ON f.target_type='composition' AND f.target_id=c.id
               WHERE f.kind='favorite' AND f.enabled=1
               ORDER BY f.updated_at DESC LIMIT ?""",
            (limit,),
        ))

    def _save_artist(self, artist: str, preference: ArtistPreference) -> None:
        if not artist.strip():
            raise ValueError("Artist must be nonempty")
        with self.connection:
            self.connection.execute(
                """INSERT INTO artist_preferences(artist, score, blacklisted, updated_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(artist) DO UPDATE SET
                   score=excluded.score, blacklisted=excluded.blacklisted,
                   updated_at=excluded.updated_at""",
                (artist, preference.score, int(preference.blacklisted), time.time()),
            )

    def get_artist_preference(self, artist: str) -> ArtistPreference:
        row = self.connection.execute(
            "SELECT score, blacklisted FROM artist_preferences WHERE artist=?", (artist,)
        ).fetchone()
        return ArtistPreference(row["score"], bool(row["blacklisted"])) if row else ArtistPreference()

    def list_artist_preferences(self) -> dict[str, ArtistPreference]:
        rows = self.connection.execute(
            "SELECT artist, score, blacklisted FROM artist_preferences"
        ).fetchall()
        return {row["artist"]: ArtistPreference(row["score"], bool(row["blacklisted"])) for row in rows}

    def adjust_artist_score(self, artist: str, delta: int) -> int:
        """Move an artist's taste score, kept within -10 to 10; return the new score."""
        preference = self.get_artist_preference(artist)
        score = max(-10, min(10, preference.score + delta))
        self._save_artist(artist, preference._replace(score=score))
        return score

    def set_artist_blacklisted(self, artist: str, enabled: bool = True) -> None:
        self._save_artist(artist, self.get_artist_preference(artist)._replace(blacklisted=enabled))

    def list_blacklisted_artists(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT artist FROM artist_preferences WHERE blacklisted=1 ORDER BY artist COLLATE NOCASE"
        ).fetchall()
        return [str(row["artist"]) for row in rows]

    def get_api_cache(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM api_cache WHERE key=? AND expires_at>?", (key, time.time())
        ).fetchone()
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(row["payload"])
        return payload

    def put_api_cache(self, key: str, value: Mapping[str, Any], *, ttl_hours: float = 24) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO api_cache(key, payload, expires_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,
                   expires_at=excluded.expires_at""",
                (key, json.dumps(value, ensure_ascii=False), time.time() + ttl_hours * 3600),
            )
