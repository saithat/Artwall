"""Deterministic wallpaper composition and small editorial captions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import colorsys
from io import BytesIO
from pathlib import Path
import re
from typing import Mapping, NamedTuple

from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from artwall.models import ANCHORS, Anchor, Artwork, Crop, CuratorDecision, Layout


RGB = tuple[int, int, int]
Rect = tuple[int, int, int, int]
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
RESAMPLE = Image.Resampling.LANCZOS
PAPER = (235, 219, 178)
SECONDARY = (213, 196, 161)
MUTED = (168, 153, 132)
BACKGROUND = (29, 32, 33)
GUTTER = (40, 40, 40)
ACCENT = (215, 153, 33)


@dataclass(frozen=True)
class RenderSettings:
    width: int = 3440
    height: int = 1440
    gutter: int = 8
    caption_max_width: int = 690
    caption_margin: int = 80
    safe_crop_fraction: float = 0.25
    font_title: str | None = None
    font_body: str | None = None
    show_medium: bool = False
    body_max_lines: int = 6
    # Common centered password area. Corner captions never intentionally enter it.
    lock_ui_region: tuple[float, float, float, float] = (0.34, 0.30, 0.66, 0.76)


@dataclass(frozen=True)
class RenderResult:
    path: Path
    caption_anchor: Anchor
    crops: dict[str, Crop | None]
    caption_placement: str = "overlay"
    artwork_rect: Rect | None = None
    matte_color: RGB | None = None


class _Line(NamedTuple):
    text: str
    font: Font
    color: RGB
    spacing: int


def _retained(ratio: float, target: float) -> float:
    """The fraction of an image kept when cropping it to the target ratio."""
    return min(ratio / target, target / ratio)


def _source_crop(origin: tuple[int, int], box: Rect, size: tuple[int, int]) -> Crop:
    """The region of an original image shown by a box within its region at origin."""
    (left, top), (width, height) = origin, size
    return Crop((left + box[0]) / width, (top + box[1]) / height,
                (box[2] - box[0]) / width, (box[3] - box[1]) / height)


def _font(path: str | None, family: str, size: int) -> Font:
    choices = [path, f"/usr/share/fonts/google-noto/{family}-Regular.ttf"]
    for choice in choices:
        if choice:
            try:
                return ImageFont.truetype(choice, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def _open_art(art: Artwork) -> Image.Image:
    if not art.cached_image_path:
        raise FileNotFoundError(f"No cached image for {art.id}")
    with Image.open(art.cached_image_path) as original:
        source = ImageOps.exif_transpose(original)
        profile = source.info.get("icc_profile")
        if profile:
            try:
                source = ImageCms.profileToProfile(
                    source, ImageCms.ImageCmsProfile(BytesIO(profile)),
                    ImageCms.createProfile("sRGB"), outputMode="RGB",
                ) or source
            except (OSError, ValueError, TypeError):
                # A damaged profile should not prevent displaying an artwork.
                pass
        return source.convert("RGB")


def _focus_crop(image: Image.Image, target_ratio: float) -> Rect:
    """Choose a crop that retains the most local detail without center bias."""
    width, height = image.size
    ratio = width / height
    if abs(ratio - target_ratio) < 0.002:
        return (0, 0, width, height)
    if ratio > target_ratio:
        crop_w, crop_h = round(height * target_ratio), height
        span = width - crop_w
        windows = [(round(span * i / 8), 0) for i in range(9)]
    else:
        crop_w, crop_h = width, round(width / target_ratio)
        span = height - crop_h
        windows = [(0, round(span * i / 8)) for i in range(9)]

    thumb = image.copy()
    thumb.thumbnail((320, 240), RESAMPLE)
    gray = ImageOps.grayscale(thumb)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    # Window coverage of edge energy approximates saliency. Tie breaks favour
    # the center, but the highest-scoring offset can move the crop off-center.
    best_score = -1.0
    best = windows[len(windows) // 2]
    sx, sy = thumb.width / width, thumb.height / height
    for left, top in windows:
        box = (round(left * sx), round(top * sy),
               round((left + crop_w) * sx), round((top + crop_h) * sy))
        region = edges.crop(box)
        score = ImageStat.Stat(region).mean[0] - 0.25 * abs(left - span / 2 if ratio > target_ratio else top - span / 2) / max(span, 1)
        if score > best_score:
            best_score, best = score, (left, top)
    left, top = best
    return (left, top, left + crop_w, top + crop_h)


def _auto_detail_crop(image: Image.Image, panel_ratio: float, main_width: int, detail_width: int) -> Crop:
    """Find a textured region and magnify it clearly beside the main work."""
    width_fraction = max(0.20, min(0.42, detail_width / max(main_width, 1) / 1.8))
    height_fraction = width_fraction * image.width / image.height / panel_ratio
    if height_fraction > 0.70:
        width_fraction *= 0.70 / height_fraction
        height_fraction = 0.70
    width_fraction = max(0.08, width_fraction)
    thumb = image.copy()
    thumb.thumbnail((400, 320), RESAMPLE)
    edges = ImageOps.grayscale(thumb).filter(ImageFilter.FIND_EDGES)
    tw = max(1, round(thumb.width * width_fraction))
    th = max(1, round(thumb.height * height_fraction))
    best = (-1.0, 0, 0)
    for yi in range(7):
        top = round((thumb.height - th) * yi / 6)
        for xi in range(9):
            left = round((thumb.width - tw) * xi / 8)
            region = edges.crop((left, top, left + tw, top + th))
            # A light central bias avoids selecting only the engraving border.
            centrality = 1 - (abs(xi - 4) / 4 + abs(yi - 3) / 3) / 2
            score = ImageStat.Stat(region).mean[0] + 2 * centrality
            if score > best[0]:
                best = (score, left, top)
    _, left, top = best
    return _source_crop((0, 0), (left, top, left + tw, top + th), thumb.size)


def _fit_panel(image: Image.Image, box: Rect, *, explicit_crop: Crop | None,
               safe_crop_fraction: float, allow_large_crop: bool = False) -> tuple[Image.Image, Crop | None]:
    panel_w, panel_h = box[2] - box[0], box[3] - box[1]
    target_ratio = panel_w / panel_h
    region = explicit_crop.box(*image.size) if explicit_crop else (0, 0, *image.size)
    part = image.crop(region) if explicit_crop else image
    retained = _retained(part.width / part.height, target_ratio)
    if explicit_crop and not allow_large_crop and explicit_crop.area * retained < 1 - safe_crop_fraction:
        # The curator's ROI plus the panel fit would remove too much of the
        # original. Revert to the whole work before considering any panel crop.
        explicit_crop, region, part = None, (0, 0, *image.size), image
        retained = _retained(image.width / image.height, target_ratio)
    if retained >= 1 - safe_crop_fraction or allow_large_crop:
        crop_box = _focus_crop(part, target_ratio)
        return (part.crop(crop_box).resize((panel_w, panel_h), RESAMPLE),
                _source_crop(region[:2], crop_box, image.size))
    # Important compositions stay whole. The surrounding exposed canvas is
    # kept quiet; the artwork itself is neither stretched nor blurred.
    scale = min(panel_w / part.width, panel_h / part.height)
    new_size = (max(1, round(part.width * scale)), max(1, round(part.height * scale)))
    fitted = Image.new("RGB", (panel_w, panel_h), BACKGROUND)
    fitted.paste(part.resize(new_size, RESAMPLE), ((panel_w - new_size[0]) // 2, (panel_h - new_size[1]) // 2))
    return fitted, explicit_crop


def _matte_colors(image: Image.Image) -> tuple[RGB, RGB]:
    """Pick a muted color from the work without changing any artwork pixels."""
    sample = image.copy()
    sample.thumbnail((160, 160), RESAMPLE)
    means = ImageStat.Stat(sample).mean[:3]
    hue, _, saturation = colorsys.rgb_to_hls(*(channel / 255 for channel in means))
    # A work with complementary colors can average to gray. Prefer a common,
    # moderately saturated paint color when one exists in the image.
    indexed = sample.quantize(colors=12)
    palette = indexed.getpalette() or []
    swatches = []
    for count, index in indexed.getcolors() or []:
        if not isinstance(index, int):
            continue
        red, green, blue = palette[index * 3:index * 3 + 3]
        swatch_hue, swatch_light, swatch_sat = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
        if 0.09 <= swatch_light <= 0.87:
            swatches.append((count * swatch_sat, swatch_hue, swatch_sat))
    if swatches:
        _, sampled_hue, sampled_sat = max(swatches)
        if sampled_sat >= 0.18:
            hue, saturation = sampled_hue, sampled_sat
    mean_light = sum(means) / 3
    # Dark images need a slightly lighter surround so their edge remains clear.
    lightness = 0.23 if mean_light < 100 else 0.16
    saturation = max(0.055, min(0.30, saturation * 0.70))
    red, green, blue = (round(channel * 255) for channel in colorsys.hls_to_rgb(hue, lightness, saturation))
    # The fine edge should remain visible even when the art has dark borders.
    border = (min(230, round(red * 0.55 + 113)), min(230, round(green * 0.55 + 113)),
              min(230, round(blue * 0.55 + 113)))
    return (red, green, blue), border


def _fit_single(image: Image.Image, settings: RenderSettings, explicit_crop: Crop | None,
                ) -> tuple[Image.Image, Rect, Crop | None, RGB | None]:
    """Fill only for a very mild crop; otherwise keep the complete work."""
    size = image.size
    target = settings.width / settings.height
    # A curator crop may be useful for a slightly mismatched wide painting,
    # but a single-work wallpaper should never quietly discard a large area.
    crop = explicit_crop if explicit_crop and explicit_crop.area >= 0.96 else None
    region = crop.box(*size) if crop else (0, 0, *size)
    if crop and crop.area * _retained((region[2] - region[0]) / (region[3] - region[1]), target) < 0.96:
        crop, region = None, (0, 0, *size)
    if crop:
        image = image.crop(region)
    source_ratio = image.width / image.height
    if _retained(source_ratio, target) >= 0.96:
        crop_box = _focus_crop(image, target)
        filled = image.crop(crop_box).resize((settings.width, settings.height), RESAMPLE)
        return filled, (0, 0, settings.width, settings.height), _source_crop(region[:2], crop_box, size), None

    matte, border = _matte_colors(image)
    canvas = Image.new("RGB", (settings.width, settings.height), matte)
    inset = max(2, round(min(settings.width, settings.height) * 0.006))
    scale = min((settings.width - 2 * inset) / image.width,
                (settings.height - 2 * inset) / image.height)
    fitted_w = max(1, round(image.width * scale))
    fitted_h = max(1, round(image.height * scale))
    if source_ratio > target and settings.height - fitted_h >= 180:
        # An extra-wide original gets one useful lower band for a caption.
        top = inset
    else:
        top = (settings.height - fitted_h) // 2
    left = (settings.width - fitted_w) // 2
    box = (left, top, left + fitted_w, top + fitted_h)
    line_width = max(2, round(inset * 0.35))
    ImageDraw.Draw(canvas).rectangle(
        (max(0, left - line_width), max(0, top - line_width),
         min(settings.width - 1, box[2] + line_width - 1),
         min(settings.height - 1, box[3] + line_width - 1)),
        outline=border, width=line_width,
    )
    canvas.paste(image.resize((fitted_w, fitted_h), RESAMPLE), (left, top))
    return canvas, box, crop, matte


def _panels(layout: Layout, ids: list[str], settings: RenderSettings,
            ratios: Mapping[str, float]) -> list[tuple[str, Rect, bool]]:
    """Each panel's artwork ID, box, and whether it shows an enlarged detail."""
    w, h, g = settings.width, settings.height, settings.gutter
    if layout == "artwork_detail":
        split = round(w * (0.42 if ratios[ids[0]] < 0.95 else 0.69))
        return [(ids[0], (0, 0, split - g // 2, h), False),
                (ids[0], (split + g // 2, 0, w, h), True)]
    if layout in ("diptych_50", "diptych_60"):
        desired = 0.6 if layout == "diptych_60" else 0.5
        first_ratio, second_ratio = ratios[ids[0]], ratios[ids[1]]
        desired_left = desired * (w - g) / h
        desired_right = (1 - desired) * (w - g) / h
        # Rescue an otherwise very empty panel by giving each original an
        # amount of width proportional to its shape. The resulting split can
        # be asymmetric, like an art-book spread, while both works fill height.
        if min(_retained(first_ratio, desired_left), _retained(second_ratio, desired_right)) < 0.75:
            desired = max(0.28, min(0.72, first_ratio / (first_ratio + second_ratio)))
        split = round(w * desired)
        return [(ids[0], (0, 0, split - g // 2, h), False),
                (ids[1], (split + g // 2, 0, w, h), False)]
    if layout == "triptych":
        item_ratios = [ratios[i] for i in ids]
        panel_ratio = ((w - 2 * g) / 3) / h
        if min(_retained(r, panel_ratio) for r in item_ratios) < 0.75:
            shares = [max(0.20, min(0.50, r / sum(item_ratios))) for r in item_ratios]
            shares = [share / sum(shares) for share in shares]
        else:
            shares = [1 / 3] * 3
        usable = w - 2 * g
        first = round(usable * shares[0])
        second = round(usable * (shares[0] + shares[1]))
        return [(ids[0], (0, 0, first, h), False),
                (ids[1], (first + g, 0, second + g, h), False),
                (ids[2], (second + 2 * g, 0, w, h), False)]
    return [(ids[0], (0, 0, w, h), False)]


def _text_wrap(draw: ImageDraw.ImageDraw, text: str, font: Font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        current = ""
        for word in words:
            proposal = f"{current} {word}".strip()
            if current and draw.textlength(proposal, font=font) > max_width:
                lines.append(current)
                current = ""
            if draw.textlength(word, font=font) <= max_width:
                current = f"{current} {word}".strip()
                continue
            for character in word:
                if current and draw.textlength(current + character, font=font) > max_width:
                    lines.append(current)
                    current = ""
                current += character
        if current:
            lines.append(current)
    return lines


def _limit_lines(draw: ImageDraw.ImageDraw, lines: list[str], font: Font,
                 max_lines: int, max_width: int) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1].rstrip()
    while last and draw.textlength(last + "…", font=font) > max_width:
        last = last[:-1].rstrip()
    kept[-1] = last + "…"
    return kept


def _display_title(title: str) -> str:
    """Use an authoritative series name when the object title is a long list."""
    if len(title) > 105 and "(" in title:
        short = title.split("(", 1)[0].strip(" ,")
        if 8 < len(short) < 75:
            return short
    if len(title) > 105:
        initial = title.split(",", 1)[0].strip()
        if 10 < len(initial) < 75 and not re.match(r"^\d+[.)]", initial):
            return initial
    if len(title) > 105 and (match := re.search(r"\bfrom\s+(.+)$", title, re.IGNORECASE)):
        series = re.sub(r"^the\s+series\s+", "", match.group(1), flags=re.IGNORECASE).strip(" .\"")
        if 8 < len(series) < 100:
            return series
    return title


def _companion_title(title: str) -> str:
    """Keep the museum's work title while dropping a long catalog qualifier."""
    title = _display_title(title.strip())
    if len(title) > 75:
        for separator in ("(", ", from the series ", ", from the "):
            short = title.split(separator, 1)[0].strip(" ,")
            if 5 < len(short) < len(title):
                return short
    return title


def _context_label(artwork: Artwork) -> tuple[str | None, bool]:
    """Favor a useful museum term over a generic catalog culture label."""
    for value in (artwork.movement, artwork.style, artwork.period):
        if value and "culture or style" not in value.casefold():
            return value, False
    if artwork.medium:
        return artwork.medium.split(";", 1)[0].strip(), True
    return None, False


def _ink_for_region(canvas: Image.Image, rect: Rect) -> bool:
    """A quiet pale print reads more naturally with dark book-like ink."""
    region = ImageOps.grayscale(canvas.crop(rect))
    region.thumbnail((120, 80), RESAMPLE)
    stats = ImageStat.Stat(region)
    return stats.mean[0] >= 155 and stats.stddev[0] <= 48


def _caption_lines(canvas: Image.Image, decision: CuratorDecision, primary: Artwork,
                   companions: list[Artwork], settings: RenderSettings) -> tuple[list[_Line], int, int]:
    title_font = _font(settings.font_title, "NotoSerif", max(23, round(settings.height * 0.024)))
    meta_font = _font(settings.font_body, "NotoSans", max(17, round(settings.height * 0.015)))
    body_font = _font(settings.font_body, "NotoSans", max(16, round(settings.height * 0.014)))
    draw = ImageDraw.Draw(canvas)
    width = settings.caption_max_width

    def wrapped(text: str, font: Font, max_lines: int) -> list[str]:
        return _limit_lines(draw, _text_wrap(draw, text, font, width), font, max_lines, width)

    style, medium_as_style = _context_label(primary)
    meta = " · ".join(x for x in (primary.artist or "Artist unknown", primary.date_display, style) if x)
    lines = [_Line(line, title_font, PAPER, 39) for line in wrapped(_display_title(primary.title or "Untitled"), title_font, 2)]
    lines += [_Line(line, meta_font, SECONDARY, 27) for line in wrapped(meta, meta_font, 2)]
    if settings.show_medium and not medium_as_style and primary.medium:
        lines += [_Line(line, body_font, MUTED, 23) for line in _text_wrap(draw, primary.medium, body_font, width)[:1]]
    for companion in companions:
        if companion.title:
            label = f"With: {_companion_title(companion.title)}"
            lines += [_Line(line, body_font, SECONDARY, 23) for line in wrapped(label, body_font, 1)]
    # Museum text is often markup or much longer than a wallpaper can hold.
    description = " ".join(decision.description.split())
    if description:
        available = settings.height - 2 * settings.caption_margin - sum(line.spacing for line in lines)
        max_body_lines = max(0, min(settings.body_max_lines, (available - 13) // 25))
        body_lines = wrapped(description, body_font, max_body_lines) if max_body_lines else []
        if body_lines:
            lines.append(_Line("", body_font, PAPER, 13))
            lines += [_Line(line, body_font, PAPER, 25) for line in body_lines]
    max_w = max((int(draw.textlength(line.text, font=line.font)) for line in lines), default=0)
    return lines, max_w, sum(line.spacing for line in lines)


def _anchor_rect(anchor: Anchor, content_w: int, content_h: int, settings: RenderSettings) -> Rect:
    m = settings.caption_margin
    x = m if anchor.endswith("left") else settings.width - m - content_w
    y = m if anchor.startswith("top") else settings.height - m - content_h
    return (x, y, x + content_w, y + content_h)


def _score_anchor(canvas: Image.Image, rect: Rect, settings: RenderSettings) -> float:
    x0, y0, x1, y1 = rect
    sample = canvas.crop((max(0, x0 - 25), max(0, y0 - 20), min(settings.width, x1 + 25), min(settings.height, y1 + 20)))
    sample.thumbnail((120, 80), RESAMPLE)
    gray = ImageOps.grayscale(sample)
    stat = ImageStat.Stat(gray)
    brightness = stat.mean[0]
    variance = stat.stddev[0]
    edge_energy = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    # Protect line art and focal detail first. A bright but empty paper margin
    # becomes readable with a localized gradient; busy dark art does not.
    score = brightness * 0.24 + variance * 0.7 + edge_energy * 1.9
    lx0, ly0, lx1, ly1 = settings.lock_ui_region
    lock = (lx0 * settings.width, ly0 * settings.height, lx1 * settings.width, ly1 * settings.height)
    overlap = max(0, min(x1, lock[2]) - max(x0, lock[0])) * max(0, min(y1, lock[3]) - max(y0, lock[1]))
    score += 200 * overlap / max((x1 - x0) * (y1 - y0), 1)
    return score


def _gradient(canvas: Image.Image, anchor: Anchor, rect: Rect, strength: int) -> None:
    x0, y0, x1, y1 = rect
    region_w = min(canvas.width, max(900, x1 - x0 + 180))
    region_h = min(canvas.height, max(430, y1 - y0 + 150))
    horizontal = Image.linear_gradient("L").rotate(90, expand=True).resize((region_w, region_h))
    vertical = Image.linear_gradient("L").resize((region_w, region_h))
    if anchor.endswith("left"):
        horizontal = ImageOps.mirror(horizontal)
    if anchor.startswith("top"):
        vertical = ImageOps.flip(vertical)
    # Keep a quiet dark field directly behind the type, then fade into the art.
    horizontal = horizontal.point(lambda v: round(255 * (v / 255) ** 0.5))
    vertical = vertical.point(lambda v: round(255 * (v / 255) ** 0.5))
    alpha = ImageChops.multiply(horizontal, vertical).point(lambda v: round(v * strength / 255))
    black = Image.new("RGB", (region_w, region_h), BACKGROUND)
    ox = 0 if anchor.endswith("left") else canvas.width - region_w
    oy = 0 if anchor.startswith("top") else canvas.height - region_h
    canvas.paste(black, (ox, oy), alpha)


def _matte_caption(canvas: Image.Image, artwork_rect: Rect, decision: CuratorDecision, primary: Artwork,
                   companions: list[Artwork], settings: RenderSettings,
                   ) -> tuple[list[_Line], Rect, Anchor, str] | None:
    """Use an exposed side or lower band only when all caption lines fit."""
    left, top, right, bottom = artwork_rect
    requested = decision.caption_anchor
    edge_pad = max(24, round(settings.height * 0.028))
    outer_pad = max(36, min(settings.caption_margin, round(settings.height * 0.055)))
    if left > edge_pad * 2:
        side_width = left - edge_pad - outer_pad
        if side_width >= 320:
            local = replace(settings, caption_max_width=min(settings.caption_max_width, side_width),
                            body_max_lines=10)
            lines, content_w, content_h = _caption_lines(canvas, decision, primary, companions, local)
            if content_h + 2 * outer_pad <= settings.height:
                side = "left" if requested.endswith("left") else "right"
                x = outer_pad if side == "left" else right + edge_pad
                on_top = requested.startswith("top")
                y = outer_pad if on_top else settings.height - outer_pad - content_h
                anchor: Anchor = ("top_left" if side == "left" else "top_right") if on_top else (
                    "bottom_left" if side == "left" else "bottom_right")
                return lines, (x, y, x + content_w, y + content_h), anchor, f"matte_{side}"

    # A very wide original leaves a horizontal band. Keep the image anchored
    # high so the explanation can sit below it without obscuring the artwork.
    lower_height = settings.height - bottom
    if lower_height > 0:
        local = replace(settings, caption_max_width=min(max(800, settings.caption_max_width),
                                                        settings.width - 2 * outer_pad),
                        body_max_lines=8)
        lines, content_w, content_h = _caption_lines(canvas, decision, primary, companions, local)
        if lower_height >= content_h + 2 * edge_pad:
            on_right = requested.endswith("right")
            x = settings.width - outer_pad - content_w if on_right else outer_pad
            y = bottom + (lower_height - content_h) // 2
            anchor = "bottom_right" if on_right else "bottom_left"
            return lines, (x, y, x + content_w, y + content_h), anchor, "matte_bottom"
    return None


def render_wallpaper(decision: CuratorDecision, artworks: Mapping[str, Artwork], output_path: str | Path,
                     settings: RenderSettings | None = None) -> RenderResult:
    """Render a validated curator decision from cached, original artwork files."""
    settings = settings or RenderSettings()
    if settings.width < 320 or settings.height < 240:
        raise ValueError("Display dimensions are too small")
    settings = replace(settings, caption_max_width=min(decision.caption_max_width_px,
                                                       settings.width - 2 * settings.caption_margin))
    ids = decision.artwork_ids
    if any(artwork_id not in artworks for artwork_id in ids):
        raise ValueError("Composition references an unknown artwork")
    images = {artwork_id: _open_art(artworks[artwork_id]) for artwork_id in ids}
    panels = _panels(decision.layout, ids, settings,
                     {artwork_id: image.width / image.height for artwork_id, image in images.items()})
    canvas = Image.new("RGB", (settings.width, settings.height), BACKGROUND)
    if len(panels) > 1:
        ImageDraw.Draw(canvas).rectangle((0, 0, settings.width, settings.height), fill=GUTTER)
    crops: dict[str, Crop | None] = {}
    entry_crops = {entry.id: entry.crop for entry in decision.artworks}
    artwork_rect: Rect | None = None
    matte_color: RGB | None = None
    if len(panels) == 1:
        artwork_id = panels[0][0]
        fitted, artwork_rect, crops[artwork_id], matte_color = _fit_single(
            images[artwork_id], settings, entry_crops[artwork_id])
        canvas.paste(fitted, (0, 0))
    else:
        for artwork_id, box, detail in panels:
            image = images[artwork_id]
            crop = decision.detail_crop if detail else entry_crops[artwork_id]
            if detail and crop is None:
                # A deterministic enlarged detail. The title always names the same
                # work, and a subtle in-image label clarifies the second panel.
                main_width = panels[0][1][2] - panels[0][1][0]
                detail_width = box[2] - box[0]
                crop = _auto_detail_crop(image, detail_width / (box[3] - box[1]), main_width, detail_width)
            fitted, used = _fit_panel(image, box, explicit_crop=crop,
                                      safe_crop_fraction=settings.safe_crop_fraction,
                                      allow_large_crop=detail)
            canvas.paste(fitted, box[:2])
            crops[artwork_id + (":detail" if detail else "")] = used
            if detail:
                ImageDraw.Draw(canvas).text(
                    (box[0] + 28, 27), "DETAIL OF THE SAME WORK", font=_font(settings.font_body, "NotoSans", 16),
                    fill=MUTED, stroke_width=1, stroke_fill=BACKGROUND)

    primary = artworks[decision.primary_artwork_id]
    # Only identify other works actually shown. A detail panel is the same artwork.
    companions = [artworks[artwork_id] for artwork_id in dict.fromkeys(
        artwork_id for artwork_id, _, detail in panels if not detail and artwork_id != primary.id)]
    matte_caption = (_matte_caption(canvas, artwork_rect, decision, primary, companions, settings)
                     if artwork_rect and matte_color else None)
    if matte_caption:
        lines, rect, anchor, placement = matte_caption
        dark_ink = False
    else:
        placement = "overlay"
        lines, content_w, content_h = _caption_lines(canvas, decision, primary, companions, settings)
        scores = {option: _score_anchor(canvas, _anchor_rect(option, content_w, content_h, settings), settings)
                  for option in ANCHORS}
        scores[decision.caption_anchor] -= 11  # Respect the model when the region is comparable.
        anchor = min(scores, key=lambda option: scores[option])
        rect = _anchor_rect(anchor, content_w, content_h, settings)
        dark_ink = _ink_for_region(canvas, rect)
        if not dark_ink and (decision.use_gradient or scores[anchor] > 58):
            brightness = ImageStat.Stat(ImageOps.grayscale(canvas.crop(rect))).mean[0]
            _gradient(canvas, anchor, rect, min(205, max(110, round(80 + brightness * 0.47))))
    draw = ImageDraw.Draw(canvas)
    x, y = rect[:2]
    draw.rounded_rectangle((x, y - 16, x + 57, y - 13), radius=1, fill=ACCENT)
    for line, font, color, spacing in lines:
        if line:
            if dark_ink:
                ink = (40, 40, 40) if color == PAPER else (60, 56, 54)
                draw.text((x + 1, y + 1), line, font=font, fill=(245, 240, 225))
                draw.text((x, y), line, font=font, fill=ink)
            else:
                draw.text((x + 1, y + 2), line, font=font, fill=(16, 17, 17))
                draw.text((x, y), line, font=font, fill=color)
        y += spacing
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    canvas.save(temp, format="PNG", optimize=True)
    temp.replace(path)
    return RenderResult(path, anchor, crops, placement, artwork_rect, matte_color)
