"""Source-neutral records. Missing museum facts stay missing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import math
import time
from typing import Any, Literal, Mapping, cast, get_args

Layout = Literal["single", "near_single", "diptych_50", "diptych_60", "triptych", "artwork_detail"]
Anchor = Literal["bottom_left", "bottom_right", "top_left", "top_right"]
LAYOUT_COUNTS: dict[Layout, int] = {
    "single": 1, "near_single": 1, "diptych_50": 2, "diptych_60": 2, "triptych": 3, "artwork_detail": 1,
}
ANCHORS: tuple[Anchor, ...] = get_args(Anchor)


class DecisionError(ValueError):
    """A proposed composition cannot safely be rendered."""


@dataclass
class Artwork:
    source: str
    source_id: str
    title: str
    artist: str | None = None
    date_display: str | None = None
    date_start: int | None = None
    date_end: int | None = None
    medium: str | None = None
    dimensions: str | None = None
    culture: str | None = None
    geographic_origin: str | None = None
    period: str | None = None
    style: str | None = None
    movement: str | None = None
    subject_tags: list[str] | None = None
    museum_description: str | None = None
    canonical_url: str | None = None
    image_url: str | None = None
    iiif_url: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    rights_statement: str | None = None
    public_domain: bool = False
    open_access: bool = False
    source_metadata: dict[str, Any] | None = None
    cached_image_path: str | None = None

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def aspect_ratio(self) -> float | None:
        if self.image_width and self.image_height and self.image_height > 0:
            return self.image_width / self.image_height
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Artwork:
        known = {item.name for item in fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in known})


@dataclass(frozen=True)
class Crop:
    """A normalized region of an image; it always lies inside the unit square."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if min(self.x, self.y) < 0 or self.width <= 0 or self.height <= 0:
            raise DecisionError("Crop must have positive dimensions inside image")
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise DecisionError("Crop extends beyond image")

    @property
    def area(self) -> float:
        return self.width * self.height

    def box(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The pixel box of this region in an image of the given size."""
        return (round(self.x * width), round(self.y * height),
                round((self.x + self.width) * width), round((self.y + self.height) * height))

    @classmethod
    def parse(cls, raw: object) -> Crop | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or set(raw) != {"x", "y", "width", "height"}:
            raise DecisionError("Crop needs x, y, width, and height")
        values = [raw[key] for key in ("x", "y", "width", "height")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values):
            raise DecisionError("Crop values must be finite numbers")
        return cls(*(float(value) for value in values))


@dataclass(frozen=True)
class DecisionArtwork:
    id: str
    crop: Crop | None = None


@dataclass(frozen=True)
class CuratorDecision:
    """A structurally valid layout; curation.validate_decision checks it against museum records."""

    layout: Layout
    primary_artwork_id: str
    artworks: tuple[DecisionArtwork, ...]
    caption_anchor: Anchor = "bottom_left"
    caption_max_width_px: int = 620
    use_gradient: bool = True
    detail_crop: Crop | None = None
    relationship: str = ""
    description: str = ""
    curator_reason: str = ""

    def __post_init__(self) -> None:
        ids = self.artwork_ids
        if self.layout not in LAYOUT_COUNTS:
            raise DecisionError(f"Unsupported layout: {self.layout}")
        if len(ids) != LAYOUT_COUNTS[self.layout]:
            raise DecisionError(f"{self.layout} requires {LAYOUT_COUNTS[self.layout]} artwork(s)")
        if len(set(ids)) != len(ids):
            raise DecisionError("A work cannot appear twice in one composition")
        if self.primary_artwork_id not in ids:
            raise DecisionError("Primary artwork must be selected")
        if self.caption_anchor not in ANCHORS:
            raise DecisionError(f"Unsupported caption anchor: {self.caption_anchor}")
        if not 320 <= self.caption_max_width_px <= 900:
            raise DecisionError("Caption width must be between 320 and 900 pixels")
        if self.detail_crop is not None and self.layout != "artwork_detail":
            raise DecisionError("Detail crop only belongs in artwork_detail layout")

    @property
    def artwork_ids(self) -> list[str]:
        return [entry.id for entry in self.artworks]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["artworks"] = list(value["artworks"])
        return value

    @classmethod
    def from_dict(cls, raw: object) -> CuratorDecision:
        """Parse untrusted JSON into a decision, rejecting unexpected fields and types."""
        if not isinstance(raw, Mapping):
            raise DecisionError("Decision must be a JSON object")
        if set(raw) - {item.name for item in fields(cls)}:
            raise DecisionError("Decision contains unsupported fields")
        entries = raw.get("artworks")
        if not isinstance(entries, list):
            raise DecisionError("Decision artworks must be a list")
        artworks = []
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) - {"id", "crop"} or not isinstance(entry.get("id"), str):
                raise DecisionError("Artwork entry must be an object with an id and optional crop")
            artworks.append(DecisionArtwork(entry["id"], Crop.parse(entry.get("crop"))))
        width = raw.get("caption_max_width_px", 620)
        if isinstance(width, bool) or not isinstance(width, int):
            raise DecisionError("Caption width must be an integer")
        if not isinstance(raw.get("use_gradient", True), bool):
            raise DecisionError("use_gradient must be boolean")
        text = {key: raw.get(key) or "" for key in ("relationship", "description", "curator_reason")}
        if not all(isinstance(value, str) for value in text.values()):
            raise DecisionError("Decision text fields must be strings")
        # __post_init__ checks the layout, anchor, and primary ID values.
        return cls(layout=cast(Layout, raw.get("layout")),
                   primary_artwork_id=cast(str, raw.get("primary_artwork_id")),
                   artworks=tuple(artworks), caption_anchor=raw.get("caption_anchor", "bottom_left"),
                   caption_max_width_px=width, use_gradient=raw.get("use_gradient", True),
                   detail_crop=Crop.parse(raw.get("detail_crop")), **text)


@dataclass(frozen=True)
class Composition:
    """A rendered wallpaper in the queue or history, with the decision that produced it."""

    id: str
    decision: CuratorDecision
    output_path: str
    created_at: float = field(default_factory=time.time)
    render: dict[str, Any] = field(default_factory=dict)
    shown_at: float | None = None
    show_count: int = 0

    @property
    def artwork_ids(self) -> list[str]:
        return self.decision.artwork_ids

    def to_dict(self) -> dict[str, Any]:
        """The stored JSON payload; shown_at and show_count live in their own columns."""
        return {"id": self.id, "created_at": self.created_at, "artwork_ids": self.artwork_ids,
                "decision": self.decision.to_dict(), "render": self.render, "output_path": self.output_path}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, shown_at: float | None = None,
                  show_count: int = 0) -> Composition:
        return cls(id=str(value["id"]), decision=CuratorDecision.from_dict(value.get("decision")),
                   output_path=str(value["output_path"]), created_at=float(value["created_at"]),
                   render=dict(value.get("render") or {}), shown_at=shown_at, show_count=show_count)
