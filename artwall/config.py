"""Small, explicit TOML configuration, XDG paths, and atomic file writes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
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
    width: int = 3440
    height: int = 1440
    interval_minutes: int = 120
    wallpaper_backend: str = "auto"
    wallpaper_output: str | None = None
    lockscreen_backend: str = "auto"
    public_lock_screen_safe: bool = False
    public_domain_only: bool = True
    source_artic: bool = True
    model: str = "qwen3.6:27b"
    ollama_url: str = "http://127.0.0.1:11434"
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
            (0 <= self.explore_fraction and 0 <= self.wildcard_fraction
             and self.explore_fraction + self.wildcard_fraction < 1,
             "Exploration fractions must be non-negative and sum to less than 1"),
        )
        for valid, message in checks:
            if not valid:
                raise ValueError(message)


# Each Settings field's TOML table, key, and type. Unknown keys are refused when
# editing, instead of writing a value that appears to change behavior but is ignored.
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
    "ollama_url": ("llm", "base_url", str),
}
_KEY_TYPES = {(section, key): kind for section, key, kind in _OPTIONS.values()}


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


_TABLE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9_-]*)\]\s*(?:#.*)?$")
_ANY_TABLE = re.compile(r"^\s*\[{1,2}[^\]]+\]{1,2}\s*(?:#.*)?$")
_KEY = re.compile(r"^(\s*[A-Za-z_][A-Za-z0-9_-]*\s*=\s*)(.*)$")


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def _comment_suffix(rhs: str) -> str:
    """Keep an existing inline comment while ignoring # inside TOML strings."""
    quote = ""
    escaped = False
    for index, char in enumerate(rhs):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            start = index
            while start and rhs[start - 1] in " \t":
                start -= 1
            return rhs[start:]
    return ""


def _replace_value(text: str, section: str, key: str, literal: str) -> str:
    lines = text.splitlines(keepends=True)
    begin = end = None
    for index, line in enumerate(lines):
        body = line.strip("\r\n")
        match = _TABLE.match(body)
        if _ANY_TABLE.match(body):
            if begin is not None:
                end = index
                break
            if match and match.group(1) == section:
                begin = index
    if begin is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        blank = "" if not text else "\n"
        return text + separator + blank + f"[{section}]\n{key} = {literal}\n"
    if end is None:
        end = len(lines)
    for index in range(begin + 1, end):
        body = lines[index].rstrip("\r\n")
        match = _KEY.match(body)
        if match and match.group(1).split("=", 1)[0].strip() == key:
            newline = lines[index][len(body):]
            lines[index] = match.group(1) + literal + _comment_suffix(match.group(2)) + newline
            return "".join(lines)
    lines.insert(end, f"{key} = {literal}\n")
    return "".join(lines)


def update_config_values(
    updates: Mapping[str, Mapping[str, object]], path: Path | None = None
) -> Settings:
    """Atomically update selected TOML keys, preserving other lines and comments.

    Validation happens before replacing the real file. A failed edit leaves the
    previous configuration intact. The returned Settings reflect the new file.
    """
    path = path or config_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    contents = original
    for section, keys in updates.items():
        for key, value in keys.items():
            kind = _KEY_TYPES.get((section, key))
            if kind is None or kind is tuple:
                raise ValueError(f"Unknown configuration key: {section}.{key}")
            literal = _toml_literal(_typed(value, kind, f"{section}.{key}"))
            contents = _replace_value(contents, section, key, literal)
    settings = _settings(tomllib.loads(contents))
    if contents != original:
        write_atomic(path, contents)
    return settings
