"""Art Institute of Chicago public-domain artwork adapter.

API and IIIF conventions: https://api.artic.edu/docs/
The museum asks clients that cache images to download serially and throttle requests.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import TYPE_CHECKING, Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

from artwall.models import Artwork
from artwall.sources.base import SourceError

if TYPE_CHECKING:
    from artwall.storage import Database

log = logging.getLogger(__name__)

_API = "https://api.artic.edu/api/v1/artworks"
_FIELDS = (
    "id,title,artist_title,artist_display,date_display,date_start,date_end,"
    "medium_display,dimensions,place_of_origin,style_title,classification_titles,"
    "subject_titles,term_titles,description,short_description,is_public_domain,"
    "is_zoomable,image_id,thumbnail,artwork_type_title,copyright_notice,credit_line"
)
_MAX_IMAGE_BYTES = 100 * 1024 * 1024
_USER_AGENT = "artwall/0.1 (personal open-access museum wallpaper)"


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "li"}:
            self.parts.append(" ")


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _PlainText()
    parser.feed(value)
    result = " ".join(html.unescape("".join(parser.parts)).split())
    return result or None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trusted_iiif_base(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == "artic.edu" or parsed.hostname.endswith(".artic.edu")
    ):
        return None
    if parsed.path.rstrip("/") != "/iiif/2":
        return None
    return value.rstrip("/")


def _jpeg_size(path: Path) -> tuple[int, int]:
    """Return the size of a complete JPEG file, or raise SourceError."""
    try:
        with Image.open(path) as image:
            if image.format != "JPEG":
                raise SourceError(f"{path.name} is not a JPEG")
            size = image.size
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise SourceError(f"{path.name} is unreadable: {exc}") from exc
    return size


def _use_image(artwork: Artwork, path: Path, url: str, size: tuple[int, int]) -> Path:
    artwork.cached_image_path = str(path)
    artwork.image_url = url
    artwork.image_width, artwork.image_height = size
    return path


class ArticSource:
    name = "artic"

    def __init__(
        self,
        database: Database | None = None,
        *,
        timeout: float = 25.0,
        min_request_interval: float = 1.0,
        max_retries: int = 3,
        cache_ttl_hours: int = 24,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.database = database
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self.max_retries = max_retries
        self.cache_ttl_hours = cache_ttl_hours
        self.opener = opener
        self._last_request = 0.0

    def _throttle(self) -> None:
        delay = self.min_request_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    def _open(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": _USER_AGENT, "AIC-User-Agent": "artwall/0.1"})
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                return self.opener(request, timeout=self.timeout)
            except HTTPError as exc:
                exc.close()
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.max_retries:
                    raise SourceError(f"ArtIC HTTP {exc.code}: {url}") from exc
                last_error = exc
                retry_after = exc.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 2**attempt
                log.warning("ArtIC HTTP %s; retrying in %.1f s", exc.code, delay)
                time.sleep(min(delay, 30.0))
            except (URLError, TimeoutError) as exc:
                if attempt == self.max_retries:
                    raise SourceError(f"ArtIC request failed: {url}") from exc
                last_error = exc
                time.sleep(min(2**attempt, 30.0))
        raise SourceError(f"ArtIC request failed: {url}") from last_error

    def _request_json(self, url: str) -> dict[str, Any]:
        if self.database is not None:
            cached = self.database.get_api_cache(url)
            if cached is not None:
                return cached
        try:
            with self._open(url) as response:
                payload = response.read(12 * 1024 * 1024 + 1)
            if len(payload) > 12 * 1024 * 1024:
                raise SourceError("ArtIC API response is unexpectedly large")
            result = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise SourceError("ArtIC returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise SourceError("ArtIC returned a non-object response")
        if self.database is not None:
            self.database.put_api_cache(url, result, ttl_hours=self.cache_ttl_hours)
        return result

    def search(self, query: str, *, page: int = 1, limit: int = 30) -> list[Artwork]:
        if page < 1 or not 1 <= limit <= 100:
            raise ValueError("page must be >= 1 and limit must be between 1 and 100")
        params = {
            "q": query,
            "query[term][is_public_domain]": "true",
            "page": page,
            "limit": limit,
            "fields": _FIELDS,
        }
        url = f"{_API}/search?{urlencode(params)}"
        response = self._request_json(url)
        rows = response.get("data")
        if not isinstance(rows, list):
            raise SourceError("ArtIC search response has no artwork list")
        artworks: list[Artwork] = []
        for row in rows:
            artwork = self._normalize(row, response.get("config"))
            if artwork is not None:
                artworks.append(artwork)
            else:
                log.debug("Excluded ArtIC result %s: rights, image, or metadata", row.get("id") if isinstance(row, dict) else "?")
        return artworks

    @staticmethod
    def _normalize(row: object, config: object) -> Artwork | None:
        if not isinstance(row, dict) or row.get("is_public_domain") is not True:
            return None
        identifier = row.get("id")
        image_id = row.get("image_id")
        base = _trusted_iiif_base(config.get("iiif_url") if isinstance(config, dict) else None)
        title = _text(row.get("title"))
        if identifier is None or not str(identifier).isdigit() or not image_id or not base or not title:
            return None
        image_id = str(image_id)
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", image_id):
            return None
        iiif_url = f"{base}/{quote(image_id, safe='')}"
        thumbnail = row.get("thumbnail")
        if isinstance(thumbnail, list):
            thumbnail = next((item for item in thumbnail if isinstance(item, dict)), None)
        thumbnail = thumbnail if isinstance(thumbnail, dict) else {}
        tags: list[str] = []
        for field in ("subject_titles", "term_titles", "classification_titles"):
            values = row.get(field)
            if isinstance(values, list):
                tags.extend(item.strip() for item in values if isinstance(item, str) and item.strip())
        tags = list(dict.fromkeys(tags))
        style = _text(row.get("style_title"))
        description = _clean_description(row.get("short_description")) or _clean_description(row.get("description"))
        native_width = _number(thumbnail.get("width"))
        preferred_width = min(4096, native_width) if native_width and native_width > 0 else 4096
        return Artwork(
            source="artic",
            source_id=str(identifier),
            title=title,
            artist=_text(row.get("artist_title")),
            date_display=_text(row.get("date_display")),
            date_start=_number(row.get("date_start")),
            date_end=_number(row.get("date_end")),
            medium=_text(row.get("medium_display")),
            dimensions=_text(row.get("dimensions")),
            geographic_origin=_text(row.get("place_of_origin")),
            style=style,
            movement=style,
            subject_tags=tags,
            museum_description=description,
            canonical_url=f"https://www.artic.edu/artworks/{identifier}",
            image_url=f"{iiif_url}/full/{preferred_width},/0/default.jpg",
            iiif_url=iiif_url,
            image_width=native_width,
            image_height=_number(thumbnail.get("height")),
            rights_statement=_text(row.get("copyright_notice")),
            public_domain=True,
            open_access=True,
            source_metadata=row,
        )

    def verify_cached_image(self, artwork: Artwork, *, min_dimension: int = 1400) -> bool:
        """Check cached JPEG integrity and actual size; clear unusable cache pointers."""
        if not artwork.cached_image_path:
            return False
        path = Path(artwork.cached_image_path).expanduser()
        try:
            size = _jpeg_size(path)
        except SourceError as exc:
            log.warning("Ignoring invalid ArtIC image cache %s: %s", path, exc)
            artwork.cached_image_path = None
            artwork.image_width = artwork.image_height = None
            return False
        artwork.image_width, artwork.image_height = size
        if max(size) < min_dimension:
            log.info("Ignoring undersized ArtIC image cache %s (%sx%s)", path, *size)
            artwork.cached_image_path = None
            return False
        return True

    def download_image(
        self, artwork: Artwork, cache_dir: str | Path, *, width: int = 4096,
        force: bool = False,
    ) -> Path:
        """Cache an IIIF image, capped by source width, with size-rejection fallback."""
        if artwork.source != "artic" or not artwork.public_domain or not artwork.iiif_url:
            raise ValueError("Only public-domain ArtIC artworks can be downloaded")
        if not 843 <= width <= 8192:
            raise ValueError("IIIF image width must be between 843 and 8192")
        if not _trusted_iiif_base(artwork.iiif_url.rsplit("/", 1)[0]):
            raise ValueError("Untrusted ArtIC IIIF URL")
        if not artwork.source_id.isascii() or not artwork.source_id.isdigit():
            raise ValueError("Invalid ArtIC artwork ID")
        directory = Path(cache_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        if not force and self.verify_cached_image(artwork, min_dimension=0):
            return Path(str(artwork.cached_image_path))
        thumbnail = (artwork.source_metadata or {}).get("thumbnail")
        native_width = _number(thumbnail.get("width")) if isinstance(thumbnail, dict) else None
        if (not native_width or native_width <= 0) and not force:
            native_width = artwork.image_width if artwork.image_width and artwork.image_width > 0 else None
        effective_width = min(width, native_width) if native_width else width
        sizes = [effective_width] + [size for size in (1686, 843) if size < effective_width]
        for index, size in enumerate(sizes):
            destination = directory / f"artic-{artwork.source_id}-{size}.jpg"
            url = f"{artwork.iiif_url}/full/{size},/0/default.jpg"
            if not force and destination.is_file():
                try:
                    return _use_image(artwork, destination, url, _jpeg_size(destination))
                except SourceError:
                    log.warning("Discarding corrupt ArtIC image cache file: %s", destination)
                    destination.unlink(missing_ok=True)
            try:
                return _use_image(artwork, destination, url, self._fetch_jpeg(url, destination))
            except SourceError as exc:
                rejected_size = isinstance(exc.__cause__, HTTPError) and exc.__cause__.code in {400, 403}
                if not rejected_size or index == len(sizes) - 1:
                    raise
                log.warning("ArtIC rejected %spx image; trying smaller IIIF size", size)
        raise SourceError("ArtIC image download failed")

    def _fetch_jpeg(self, url: str, destination: Path) -> tuple[int, int]:
        """Download to a temporary file and move it into place only when it is a whole JPEG."""
        descriptor, name = tempfile.mkstemp(prefix=".artic-", suffix=".jpg", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, self._open(url) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type and content_type not in {"image/jpeg", "image/jpg", "application/octet-stream"}:
                    raise SourceError(f"ArtIC returned {content_type}, not a JPEG")
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        raise SourceError("ArtIC image exceeds 100 MiB limit")
                    output.write(chunk)
            size = _jpeg_size(temporary)
            os.replace(temporary, destination)
            return size
        finally:
            temporary.unlink(missing_ok=True)
