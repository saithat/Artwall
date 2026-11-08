"""Small, explicit TOML configuration, XDG paths, and atomic file writes."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any, Mapping


def _xdg(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name, str(Path.home() / fallback))).expanduser()


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    return config_home() / "artwall" / "config.toml"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "artwall"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "artwall"


def database_path() -> Path:
    return data_dir() / "database.sqlite"


def write_atomic(path: Path, text: str) -> None:
    """Replace a text file in one step so readers never see a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Settings:
    width: int = 1920
    height: int = 1080
    interval_minutes: int = 120
    wallpaper_backend: str = "auto"
    wallpaper_output: str | None = None
    lockscreen_backend: str = "auto"
    public_lock_screen_safe: bool = False
    public_domain_only: bool = True
    source_artic: bool = True
    model: str = "qwen3.6:27b"
    model_provider: str = "ollama"
    model_url: str = "http://127.0.0.1:11434"
    api_key_env: str = "OPENAI_API_KEY"
    api_key_file: str | None = None
    use_local_model: bool = True
    curation_batch_size: int = 14
    candidate_pool_size: int = 30
    max_artworks: int = 1
    caption_words: int = 40
    show_medium: bool = False
    explore_fraction: float = 0.25
    wildcard_fraction: float = 0.15
    search_terms: tuple[str, ...] = (
        "Romanticism", "Symbolism", "Japanese print", "engraving", "etching",
        "scientific illustration", "botanical", "astronomy", "manuscript",
        "Art Nouveau", "mythology", "storm", "ruins", "mountains", "ocean",
        "natural history", "woodblock", "architectural drawing",
    )

    def __post_init__(self) -> None:
        checks = (
            (self.width >= 320 and self.height >= 240, "Display size must be at least 320 × 240"),
            (self.interval_minutes >= 15, "Rotation interval must be at least 15 minutes"),
            (1 <= self.max_artworks <= 3, "max_artworks must be from 1 to 3"),
            (1 <= self.curation_batch_size <= 30, "batch_size must be from 1 to 30"),
            (1 <= self.candidate_pool_size <= 100, "candidate_pool_size must be from 1 to 100"),
            (self.public_domain_only, "This release only accepts public-domain museum images"),
            (20 <= self.caption_words <= 50, "description_words must be from 20 to 50"),
            (not self.use_local_model or bool(self.model.strip()),
             "An enabled local model needs a nonempty model name"),
            (self.model_provider in {"ollama", "openai"}, 'llm.provider must be "ollama" or "openai"'),
            (0 <= self.explore_fraction and 0 <= self.wildcard_fraction
             and self.explore_fraction + self.wildcard_fraction < 1,
             "Exploration fractions must be non-negative and sum to less than 1"),
        )
        for valid, message in checks:
            if not valid:
                raise ValueError(message)


# Each Settings field's TOML table, key, and type.
_OPTIONS: dict[str, tuple[str, str, type]] = {
    "width": ("display", "width", int),
    "height": ("display", "height", int),
    "interval_minutes": ("rotation", "interval_minutes", int),
    "wallpaper_backend": ("wallpaper", "backend", str),
    "wallpaper_output": ("wallpaper", "output", str),
    "lockscreen_backend": ("lockscreen", "backend", str),
    "public_lock_screen_safe": ("lockscreen", "public_lock_screen_safe", bool),
    "source_artic": ("sources", "artic", bool),
    "public_domain_only": ("sources", "public_domain_only", bool),
    "curation_batch_size": ("curation", "batch_size", int),
    "candidate_pool_size": ("curation", "candidate_pool_size", int),
    "max_artworks": ("curation", "max_artworks", int),
    "explore_fraction": ("curation", "explore_fraction", float),
    "wildcard_fraction": ("curation", "wildcard_fraction", float),
    "search_terms": ("curation", "search_terms", tuple),
    "caption_words": ("caption", "description_words", int),
    "show_medium": ("caption", "show_medium", bool),
    "use_local_model": ("llm", "enabled", bool),
    "model": ("llm", "model", str),
    "model_provider": ("llm", "provider", str),
    "model_url": ("llm", "base_url", str),
    "api_key_env": ("llm", "api_key_env", str),
    "api_key_file": ("llm", "api_key_file", str),
}


def model_api_key(settings: Settings) -> str | None:
    """The API key for an OpenAI-compatible endpoint, from a file or the environment.

    The key itself is never stored in config.toml. Local servers need none.
    """
    if settings.api_key_file:
        return Path(settings.api_key_file).expanduser().read_text(encoding="utf-8").strip() or None
    return os.environ.get(settings.api_key_env) or None


def _typed(value: object, kind: type, name: str) -> object:
    """Check a TOML value's type; integers are accepted where a float is expected."""
    if kind is float and type(value) is int:
        value = float(value)
    if kind is tuple and isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    if type(value) is not kind or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError(f"{name} must be a {'list of strings' if kind is tuple else kind.__name__}")
    return value


def _settings(document: Mapping[str, object]) -> Settings:
    values: dict[str, Any] = {}
    for name, (section, key, kind) in _OPTIONS.items():
        table = document.get(section, {})
        if not isinstance(table, dict):
            raise ValueError(f"[{section}] must be a TOML table")
        if key in table:
            values[name] = _typed(table[key], kind, f"{section}.{key}")
    return Settings(**values)


def load_config(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.is_file():
        return Settings()
    with path.open("rb") as handle:
        return _settings(tomllib.load(handle))


# Settings the gallery edits. They are saved in the database and take
# precedence over config.toml, which artwall never rewrites.
GALLERY_SETTINGS: dict[str, type] = {"interval_minutes": int, "model": str, "max_artworks": int}


def with_gallery_settings(settings: Settings, saved: Mapping[str, object]) -> Settings:
    """Apply gallery-saved values, checking their types; Settings validates their ranges."""
    values: dict[str, Any] = {name: _typed(saved[name], kind, name)
                               for name, kind in GALLERY_SETTINGS.items() if name in saved}
    unknown = set(saved) - set(GALLERY_SETTINGS)
    if unknown:
        raise ValueError(f"Unknown gallery setting: {sorted(unknown)[0]}")
    return replace(settings, **values)
