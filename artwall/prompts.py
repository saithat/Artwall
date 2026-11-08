"""The curator's instructions: editable parts with defaults, and the fixed parts.

Editable parts shape taste and teaching. Fixed parts define the answer format and
the factual limits, which local validation enforces whatever the prompt says.
Overrides are saved from the app and applied through ``Settings.prompt_overrides``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MAX_PROMPT_CHARS = 4000


@dataclass(frozen=True)
class PromptPart:
    key: str
    title: str
    description: str
    default: str


# Adapted for wallpaper display from Getty's Principles of Design and the
# National Gallery of Art's Elements of Art: Color. Sources are in README.md.
EDITABLE: tuple[PromptPart, ...] = (
    PromptPart(
        "composition", "Composition Guidance",
        "How the curator judges balance, color, space, and grouping when it chooses works.",
        "Composition review: balance visual weight across the whole screen; keep one clear focus. "
        "Judge proportion at the final display ratio, not in the contact-sheet cells. "
        "For multiple works, seek compatible color temperature, tonal range, and visual density, "
        "with a visible relationship in line, shape, or subject. Give the originals aligned edges "
        "and consistent spacing. Preserve their internal compositions and paper margins. "
        "Negative space should support the art, rather than leave large unused side fields. Prefer an "
        "intact related pair or trio when it substantially improves coverage; keep a well-fitting single "
        "work alone. Do not add an unrelated work or an arbitrary enlarged detail merely to fill space.",
    ),
    PromptPart(
        "selection", "Selection Rules",
        "What the curator prefers when it picks works and a layout from the candidates.",
        "Narrow works are shown intact on a subdued, diffuse backdrop drawn from the artwork's colors, "
        "with a crisp boundary around the original. Artwork should dominate the screen. Keep normal "
        "artwork crops null so the renderer can preserve the originals. Use artwork_detail only for a "
        "meaningful enlarged detail of the same work.",
    ),
    PromptPart(
        "description", "How to Describe the Art",
        "The overview written for each wallpaper, shown in the app and the explain overlay.",
        "When museum_description is empty, describe only visible form: line, light, color, scale, figures, "
        "and placement. Do not enumerate a long museum title; mention at most one named structure and focus "
        "on one or two things to notice. For a multi-art layout, write ONE shared description of every "
        "selected work, with one visible detail from each. Identify companions by a short title or clear "
        "panel location, and explain the visual relationship or contrast. Never describe a companion's title "
        "as the primary work's subject. Avoid generic praise or style clichés without a concrete visible "
        "detail.",
    ),
    PromptPart(
        "notes", "Teaching Notes",
        "How the curator teaches with the notes pinned to details in the explain overlay.",
        "You are a museum educator. Write 2 to 5 short notes that teach the viewer how to look at the art. "
        "Each note points at one specific visible detail (composition, technique, light, line, color, "
        "texture, figures, or symbols) and explains what it does or why it matters.",
    ),
)
DEFAULTS: dict[str, str] = {part.key: part.default for part in EDITABLE}

# Shown in the app for transparency; not editable.
FIXED: dict[str, str] = {
    "Facts": "Name only people, places, styles, and dates explicitly present in the metadata. Never assert "
             "commission, ownership, publication, influence, purpose, symbolism, or provenance without museum "
             "support. A note may state history or meaning only by quoting the supporting museum sentence "
             "exactly in its source field. These limits are also checked locally.",
    "Choice output": "Use exact candidate id strings. Multi-art decisions require a specific, factual "
                     "relationship. Never call multiple selected works part of one named series unless every "
                     "selected museum title names that exact series. The caption_max_width_px must be an "
                     "integer between 320 and 900. Choose the best art and layout independently. For "
                     "artwork_detail, detail_crop is a normalized x/y/width/height object. Choose a caption "
                     "corner away from faces, focal objects, and the center where a lock screen may draw. "
                     "JSON only, no Markdown.",
    "Notes output": "Each note has: work (the number), title (at most 6 words), body (1 to 3 sentences, 15 to "
                    "60 words), box ([x0, y0, x1, y1] drawn tightly around that detail, in 0-1000 coordinates "
                    "of the whole image), and source (an exact museum sentence, or an empty string). "
                    "Return JSON: {\"notes\": [...]}.",
}


def resolve(overrides: Mapping[str, str] | tuple[tuple[str, str], ...] = ()) -> dict[str, str]:
    """Each editable part's text: the saved override, or the default."""
    saved = dict(overrides)
    return {key: (saved.get(key) or "").strip() or default for key, default in DEFAULTS.items()}


def validate_overrides(values: Mapping[str, str | None]) -> dict[str, str]:
    """Keep real changes only: unknown parts are refused, defaults and blanks are dropped."""
    kept = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown prompt part: {key}")
        text = (value or "").strip()
        if len(text) > MAX_PROMPT_CHARS:
            raise ValueError(f"{key} must be at most {MAX_PROMPT_CHARS} characters")
        if text and text != DEFAULTS[key]:
            kept[key] = text
    return kept
