"""The small interface implemented by museum adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from artwall.models import Artwork


class SourceError(RuntimeError):
    """A museum API or image request failed."""


class ArtworkSource(Protocol):
    def search(self, query: str, *, page: int = 1, limit: int = 30) -> list[Artwork]: ...

    def verify_cached_image(self, artwork: Artwork, *, min_dimension: int = 1400) -> bool: ...

    def download_image(
        self, artwork: Artwork, cache_dir: str | Path, *, width: int = 4096,
        force: bool = False,
    ) -> Path: ...
