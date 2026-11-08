"""Source-neutral records. Missing museum facts stay missing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import math
import time
from typing import Any, Literal, Mapping, Sequence, assert_never, cast, get_args

Layout = Literal["single", "diptych", "triptych", "artwork_detail"]
Anchor = Literal["bottom_left", "bottom_right", "top_left", "top_right"]
LAYOUT_COUNTS: dict[Layout, int] = {
    "single": 1, "diptych": 2, "triptych": 3, "artwork_detail": 1,
}
# Earlier releases split pairs into fixed 50/50 and 60/40 slots, and named narrow
# single works near_single. Stored decisions with those names still load.
_LEGACY_LAYOUTS = {"diptych_50": "diptych", "diptych_60": "diptych", "near_single": "single"}
ANCHORS: tuple[Anchor, ...] = get_args(Anchor)


def ratio_fit(ratio: float, target: float) -> float:
    """How closely one aspect ratio matches another, from 0 to 1.

    This is also the fraction of an image kept when cropping it to the target.
    """
    return min(ratio / target, target / ratio)


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
class Whole:
    """One original: full bleed when wide enough, otherwise against a diffuse surround."""

    work: DecisionArtwork


@dataclass(frozen=True)
class Row:
    """Two or three whole originals, left to right at a shared height."""

    works: tuple[DecisionArtwork, DecisionArtwork] | tuple[DecisionArtwork, DecisionArtwork, DecisionArtwork]
    primary: int = 0  # Index of the work the caption is about.

    def __post_init__(self) -> None:
        ids = [work.id for work in self.works]
        if len(set(ids)) != len(ids):
            raise DecisionError("A work cannot appear twice in one composition")
        if not 0 <= self.primary < len(ids):
            raise DecisionError("Primary artwork must be selected")

    @classmethod
    def of(cls, works: Sequence[DecisionArtwork], primary: int = 0) -> Row:
        if len(works) == 2:
            return cls((works[0], works[1]), primary)
        if len(works) == 3:
            return cls((works[0], works[1], works[2]), primary)
        raise DecisionError("A row holds two or three works")


@dataclass(frozen=True)
class Detail:
    """One original beside an enlarged region of itself."""

    work: DecisionArtwork
    region: Crop | None = None  # None lets the renderer choose a detailed region.


Arrangement = Whole | Row | Detail


def arrangement_from_layout(layout: object, primary_id: object, works: Sequence[DecisionArtwork],
                            detail_crop: Crop | None = None) -> Arrangement:
    """Parse the flat wire form (a layout name, primary ID, and works) used by models and storage."""
    name = _LEGACY_LAYOUTS.get(layout, layout) if isinstance(layout, str) else layout
    if name not in LAYOUT_COUNTS:
        raise DecisionError(f"Unsupported layout: {layout}")
    if len(works) != LAYOUT_COUNTS[name]:
        raise DecisionError(f"{name} requires {LAYOUT_COUNTS[name]} artwork(s)")
    ids = [work.id for work in works]
    if primary_id not in ids:
        raise DecisionError("Primary artwork must be selected")
    if detail_crop is not None and name != "artwork_detail":
        raise DecisionError("Detail crop only belongs in artwork_detail layout")
    if name == "artwork_detail":
        return Detail(works[0], detail_crop)
    if name == "single":
        return Whole(works[0])
    return Row.of(works, ids.index(primary_id))


@dataclass(frozen=True)
class CuratorDecision:
    """A structurally valid composition; curation.validate_decision checks it against museum records."""

    arrangement: Arrangement
    caption_anchor: Anchor = "bottom_left"
    caption_max_width_px: int = 620
    use_gradient: bool = True
    relationship: str = ""
    description: str = ""
    curator_reason: str = ""

    def __post_init__(self) -> None:
        if self.caption_anchor not in ANCHORS:
            raise DecisionError(f"Unsupported caption anchor: {self.caption_anchor}")
        if not 320 <= self.caption_max_width_px <= 900:
            raise DecisionError("Caption width must be between 320 and 900 pixels")

    @property
    def artworks(self) -> tuple[DecisionArtwork, ...]:
        match self.arrangement:
            case Row(works=works):
                return works
            case Whole(work=work) | Detail(work=work):
                return (work,)
        assert_never(self.arrangement)

    @property
    def artwork_ids(self) -> list[str]:
        return [entry.id for entry in self.artworks]

    @property
    def primary_artwork_id(self) -> str:
        match self.arrangement:
            case Row(works=works, primary=primary):
                return works[primary].id
            case Whole(work=work) | Detail(work=work):
                return work.id
        assert_never(self.arrangement)

    @property
    def layout(self) -> Layout:
        """The wire name of the arrangement."""
        match self.arrangement:
            case Whole():
                return "single"
            case Row(works=works):
                return "diptych" if len(works) == 2 else "triptych"
            case Detail():
                return "artwork_detail"
        assert_never(self.arrangement)

    def to_dict(self) -> dict[str, Any]:
        """The flat wire form models answer in and storage keeps."""
        region = self.arrangement.region if isinstance(self.arrangement, Detail) else None
        return {"layout": self.layout, "primary_artwork_id": self.primary_artwork_id,
                "artworks": [asdict(entry) for entry in self.artworks],
                "caption_anchor": self.caption_anchor, "caption_max_width_px": self.caption_max_width_px,
                "use_gradient": self.use_gradient, "detail_crop": asdict(region) if region else None,
                "relationship": self.relationship, "description": self.description,
                "curator_reason": self.curator_reason}

    @classmethod
    def from_dict(cls, raw: object) -> CuratorDecision:
        """Parse untrusted JSON into a decision, rejecting unexpected fields and types."""
        if not isinstance(raw, Mapping):
            raise DecisionError("Decision must be a JSON object")
        if set(raw) - _WIRE_FIELDS:
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
        arrangement = arrangement_from_layout(raw.get("layout"), raw.get("primary_artwork_id"), artworks,
                                              Crop.parse(raw.get("detail_crop")))
        return cls(arrangement, caption_anchor=raw.get("caption_anchor", "bottom_left"),
                   caption_max_width_px=width, use_gradient=raw.get("use_gradient", True), **text)


_WIRE_FIELDS = {"layout", "primary_artwork_id", "artworks", "caption_anchor", "caption_max_width_px",
                "use_gradient", "detail_crop", "relationship", "description", "curator_reason"}


@dataclass(frozen=True)
class Note:
    """A point of interest the curator pins to the rendered wallpaper.

    ``box`` is normalized to the whole wallpaper, so the overlay can place it on
    any screen. ``source`` is the museum sentence a factual claim rests on.
    """

    artwork_id: str
    title: str
    body: str
    box: Crop
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"artwork_id": self.artwork_id, "title": self.title, "body": self.body,
                "box": asdict(self.box), "source": self.source}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Note:
        box = Crop.parse(value.get("box"))
        if box is None:
            raise DecisionError("A note needs a box")
        return cls(str(value["artwork_id"]), str(value["title"]), str(value["body"]), box, str(value.get("source", "")))


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
    notes: tuple[Note, ...] = ()

    @property
    def artwork_ids(self) -> list[str]:
        return self.decision.artwork_ids

    def to_dict(self) -> dict[str, Any]:
        """The stored JSON payload; shown_at and show_count live in their own columns."""
        return {"id": self.id, "created_at": self.created_at, "artwork_ids": self.artwork_ids,
                "decision": self.decision.to_dict(), "render": self.render, "output_path": self.output_path,
                "notes": [note.to_dict() for note in self.notes]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, shown_at: float | None = None,
                  show_count: int = 0) -> Composition:
        notes = []
        for raw in value.get("notes") or []:
            try:
                notes.append(Note.from_dict(raw))
            except (DecisionError, KeyError, TypeError, AttributeError):
                continue  # A damaged note never hides the wallpaper itself.
        return cls(id=str(value["id"]), decision=CuratorDecision.from_dict(value.get("decision")),
                   output_path=str(value["output_path"]), created_at=float(value["created_at"]),
                   render=dict(value.get("render") or {}), shown_at=shown_at, show_count=show_count,
                   notes=tuple(notes))
