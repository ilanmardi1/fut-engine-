"""
compositor.py
--------------
Builds one flat PNG "slide" per player/interval: background + a REAL
gold-shield FUT card (frame + face + OVR/position/name/stats/nation/club
baked in) + rank badge + (for comparison videos) an up/down arrow between
two card versions.

WHY WE DRAW THE SHIELD OURSELVES: fut.gg's site shows a full FUT-style
card, but that visual is assembled in the BROWSER from separate pieces --
a shared gold "rarities-level-3-large" frame graphic underneath, and a
player-specific transparent face/portrait cutout ("player-item") layered
on top via CSS, with the OVR/name/stats drawn as HTML text over both. A
plain scrape of the face cutout alone (which is what the first version of
this pipeline did) looks like a cropped headshot with no card around it
-- because that's literally all it is. This version reconstructs the
whole card ourselves: draws the gold shield shape, pastes the downloaded
face cutout into it, and lays out OVR/position/name/6 stats/nation
flag/club crest the way the real card does.

Design choice: the shield shape, rank badge, and arrow are all DRAWN with
PIL (vector shapes/gradients), not pasted from a fixed template image.
That guarantees the exact same card shape and badge on every single slide
of every video (the "consistency" you asked for originally), and it means
there's no separate template asset to keep in sync -- only the player's
own face/flag/crest images (downloaded from fut.gg) and stat numbers
(from the ratings cache) change between slides.

Output filenames follow your existing capcut_timeframe_trim.py
convention: MM-SS.jpg, where the timestamp is when that slide should
appear on the timeline. build_video.py (the orchestrator) computes
those timestamps from --interval-seconds and hands them to this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


# ---------------------------------------------------------------------------
# Style config -- change these once, every slide in every video stays
# consistent automatically.
# ---------------------------------------------------------------------------

@dataclass
class SlideStyle:
    canvas_size: tuple[int, int] = (1920, 1080)
    bg_color_top: tuple[int, int, int] = (10, 20, 45)
    bg_color_bottom: tuple[int, int, int] = (2, 6, 16)
    bg_image_path: Optional[str] = None  # if set, used instead of the gradient

    card_height: int = 820  # the whole shield card, OVR through crest
    card_shadow_blur: int = 24
    card_shadow_alpha: int = 140

    # Gold "Rare" card colors -- change these for a different rarity look
    # (e.g. silver/bronze/special-card colors), same everywhere.
    gold_top: tuple[int, int, int] = (255, 224, 138)
    gold_bottom: tuple[int, int, int] = (196, 138, 42)
    gold_border: tuple[int, int, int] = (120, 78, 18)

    up_color: tuple[int, int, int] = (46, 204, 113)
    down_color: tuple[int, int, int] = (231, 76, 60)
    neutral_color: tuple[int, int, int] = (149, 165, 166)

    rank_badge_diameter: int = 130
    rank_badge_color: tuple[int, int, int] = (255, 200, 0)
    rank_badge_text_color: tuple[int, int, int] = (10, 10, 10)

    price_tag_font_size: int = 46
    year_label_font_size: int = 50
    price_tag_bg_color: tuple[int, int, int] = (15, 15, 20)
    price_tag_coin_color: tuple[int, int, int] = (255, 205, 60)
    price_tag_coin_ring_color: tuple[int, int, int] = (185, 130, 20)
    price_tag_text_color: tuple[int, int, int] = (255, 255, 255)

    font_path: Optional[str] = None  # None -> tries common Windows fonts, then PIL default
    name_text_color: tuple[int, int, int] = (40, 24, 4)
    ovr_text_color: tuple[int, int, int] = (40, 24, 4)
    stat_label_color: tuple[int, int, int] = (90, 60, 20)
    stat_value_color: tuple[int, int, int] = (40, 24, 4)
    highlight_stat_color: tuple[int, int, int] = (20, 110, 40)  # the stat this video is ranking by

    below_card_name_color: tuple[int, int, int] = (255, 255, 255)

    watermark_text: Optional[str] = None  # e.g. your channel handle
    watermark_font_size: int = 36
    watermark_color: tuple[int, int, int] = (255, 255, 255, 140)


def _font(path: Optional[str], size: int) -> ImageFont.ImageFont:
    if path and os.path.exists(path):
        return ImageFont.truetype(path, size)
    for candidate in (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _make_background(style: SlideStyle) -> Image.Image:
    w, h = style.canvas_size
    if style.bg_image_path and os.path.exists(style.bg_image_path):
        bg = Image.open(style.bg_image_path).convert("RGB")
        bg = ImageOps.fit(bg, (w, h), method=Image.LANCZOS)
        return bg
    bg = Image.new("RGB", (w, h), style.bg_color_top)
    top, bottom = style.bg_color_top, style.bg_color_bottom
    for y in range(h):
        t = y / max(h - 1, 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        ImageDraw.Draw(bg).line([(0, y), (w, y)], fill=row)
    return bg


def _draw_centered_text(base: Image.Image, text: str, center_x: int, y: int, font, color) -> None:
    d = ImageDraw.Draw(base)
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    d.text((center_x - tw / 2 - bbox[0], y), text, font=font, fill=color)


def _draw_watermark(base: Image.Image, style: SlideStyle) -> None:
    if not style.watermark_text:
        return
    font = _font(style.font_path, style.watermark_font_size)
    w, h = style.canvas_size
    ImageDraw.Draw(base).text((24, h - style.watermark_font_size - 20), style.watermark_text,
                               font=font, fill=style.watermark_color)


# ---------------------------------------------------------------------------
# The shield card itself
# ---------------------------------------------------------------------------

def _shield_polygon(w: int, h: int) -> list[tuple[float, float]]:
    """A gold-card / crest silhouette: flat-ish top, straight sides,
    tapering to a point at the bottom -- distinct from a plain rectangle
    at a glance, which is the whole point. The taper is kept SHORT and
    LATE (only the final ~14% of height) so the stat row sitting above it
    stays in the full-width zone -- an earlier version tapered too early
    and clipped the rightmost stat column."""
    return [
        (0.07 * w, 0.00 * h), (0.93 * w, 0.00 * h),
        (1.00 * w, 0.08 * h), (1.00 * w, 0.86 * h),
        (0.90 * w, 0.94 * h), (0.50 * w, 1.00 * h),
        (0.10 * w, 0.94 * h), (0.00 * w, 0.86 * h),
        (0.00 * w, 0.08 * h),
    ]


def _shield_mask(w: int, h: int, supersample: int = 4) -> Image.Image:
    """Anti-aliased mask via supersampling -- polygons drawn at 1x have
    visibly jagged diagonal edges, which matters here since the shield's
    sides ARE diagonals."""
    big = Image.new("L", (w * supersample, h * supersample), 0)
    ImageDraw.Draw(big).polygon(_shield_polygon(w * supersample, h * supersample), fill=255)
    return big.resize((w, h), Image.LANCZOS)


def _shield_fill(w: int, h: int, style: SlideStyle) -> Image.Image:
    grad = Image.new("RGB", (w, h), style.gold_top)
    top, bottom = style.gold_top, style.gold_bottom
    for y in range(h):
        t = y / max(h - 1, 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        ImageDraw.Draw(grad).line([(0, y), (w, y)], fill=row)
    return grad


def render_fut_card(
    ovr: int,
    position: str,
    name: str,
    stats: dict,
    face_path: str,
    style: SlideStyle,
    nation_icon_path: Optional[str] = None,
    club_icon_path: Optional[str] = None,
    highlight_stat: Optional[str] = None,
    width: Optional[int] = None,
) -> Image.Image:
    """Renders one complete gold FUT-style card as an RGBA image: shield
    frame, face cutout, OVR, position, name, the 6-stat row, and
    nation/club icons if provided. This is the piece that was missing
    before -- everything the reference card shows, baked into one image,
    not just the bare face photo."""
    h = style.card_height
    w = width or int(h * 0.685)  # matches the reference card's proportions

    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = _shield_mask(w, h)
    fill = _shield_fill(w, h, style).convert("RGBA")
    card.paste(fill, (0, 0), mask)

    # Subtle border along the shield edge.
    border = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(border).polygon(_shield_polygon(w, h), outline=(*style.gold_border, 255), width=max(2, w // 90))
    card.alpha_composite(border)

    # Face cutout, centered in the shield's middle band.
    if face_path and os.path.exists(face_path):
        face = Image.open(face_path).convert("RGBA")
        face_h = int(h * 0.42)
        ratio = face_h / face.height
        face = face.resize((int(face.width * ratio), face_h), Image.LANCZOS)
        fx = (w - face.width) // 2
        fy = int(h * 0.22)
        card.alpha_composite(face, (fx, fy))

    d = ImageDraw.Draw(card)

    ovr_font = _font(style.font_path, int(h * 0.10))
    pos_font = _font(style.font_path, int(h * 0.048))
    name_font = _font(style.font_path, int(h * 0.058))
    stat_label_font = _font(style.font_path, int(h * 0.026))
    stat_value_font = _font(style.font_path, int(h * 0.042))

    # Fixed fractional y-positions, NOT computed from the OVR text's own
    # measured bbox -- an earlier version derived the position-text y from
    # ovr_font's textbbox height, which looked fine with the DejaVu font
    # this was tested with, but overlapped badly with Windows fonts
    # (Segoe/Arial) whose ascent/descent metrics differ. A fixed gap is
    # portable across fonts.
    d.text((0.16 * w, 0.05 * h), str(ovr), font=ovr_font, fill=style.ovr_text_color)
    d.text((0.16 * w, 0.19 * h), position, font=pos_font, fill=style.name_text_color)

    _draw_centered_text(card, name, w // 2, int(h * 0.655), name_font, style.name_text_color)

    # Six-column stat row -- placed here (right below the name, well above
    # the taper that starts at 0.86h) so nothing clips against the shield's
    # narrowing edges.
    order = ["PAC", "SHO", "PAS", "DRI", "DEF", "PHY"] if "PAC" in stats else ["DIV", "HAN", "KIC", "REF", "SPD", "POS"]
    row_y = int(h * 0.745)
    col_w = w / 6
    for i, label in enumerate(order):
        val = stats.get(label)
        if val is None:
            continue
        cx = int(col_w * i + col_w / 2)
        is_hl = highlight_stat and label.upper() == highlight_stat.upper()
        val_color = style.highlight_stat_color if is_hl else style.stat_value_color
        _draw_centered_text(card, label, cx, row_y, stat_label_font, style.stat_label_color)
        _draw_centered_text(card, str(val), cx, row_y + int(h * 0.032), stat_value_font, val_color)
        if is_hl:
            val_y = row_y + int(h * 0.032)
            vb0 = d.textbbox((0, 0), str(val), font=stat_value_font)
            tw = vb0[2] - vb0[0]
            actual_x = cx - tw / 2 - vb0[0]
            vb = d.textbbox((actual_x, val_y), str(val), font=stat_value_font)
            pad = int(h * 0.008)
            d.rectangle([vb[0] - pad, vb[1] - pad, vb[2] + pad, vb[3] + pad],
                        outline=style.highlight_stat_color, width=max(2, int(h * 0.004)))

    # Nation flag + club crest, centered, near the bottom of the shield.
    icon_h = int(h * 0.045)
    icons = []
    for p in (nation_icon_path, club_icon_path):
        if p and os.path.exists(p):
            icon = Image.open(p).convert("RGBA")
            ratio = icon_h / icon.height
            icon = icon.resize((max(1, int(icon.width * ratio)), icon_h), Image.LANCZOS)
            icons.append(icon)
    if icons:
        gap = int(w * 0.02)
        total_w = sum(i.width for i in icons) + gap * (len(icons) - 1)
        ix = (w - total_w) // 2
        iy = int(h * 0.87)
        for icon in icons:
            card.alpha_composite(icon, (ix, iy))
            ix += icon.width + gap

    return card


def _paste_with_shadow(base: Image.Image, card: Image.Image, xy: tuple[int, int], style: SlideStyle) -> None:
    x, y = xy
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    alpha = card.split()[-1].point(lambda a: min(a, style.card_shadow_alpha))
    black = Image.new("RGBA", card.size, (0, 0, 0, 255))
    black.putalpha(alpha)
    shadow.paste(black, (x + 10, y + 16), black)
    shadow = shadow.filter(ImageFilter.GaussianBlur(style.card_shadow_blur))
    base.alpha_composite(shadow)
    base.alpha_composite(card, (x, y))


def draw_rank_badge(base: Image.Image, center: tuple[int, int], rank: int, style: SlideStyle) -> None:
    d = style.rank_badge_diameter
    badge = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse([0, 0, d, d], fill=(*style.rank_badge_color, 255))
    font = _font(style.font_path, int(d * 0.45))
    text = f"#{rank}"
    bbox = bd.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bd.text(((d - tw) / 2 - bbox[0], (d - th) / 2 - bbox[1]), text,
             font=font, fill=(*style.rank_badge_text_color, 255))
    base.alpha_composite(badge, (int(center[0] - d / 2), int(center[1] - d / 2)))


def _trim_flat_background(img: Image.Image, tolerance: int = 30) -> Image.Image:
    """Detects the background by sampling the image's own BORDER pixels
    (not just the 4 corners) and treating whichever colors dominate
    there as background, then converts matching pixels to transparent
    and crops to the remaining content's bounding box.

    Sampling the whole border, not just corners, and allowing MULTIPLE
    dominant colors (not just one) matters for a real reason: a real
    downloaded icon turned out to have a TRANSPARENCY CHECKERBOARD
    PATTERN baked into its actual pixels -- two alternating colors (e.g.
    light gray and white), not one flat color. This is a common mistake
    when an icon is exported from a photo editor without realizing the
    editor's own "this is transparent" preview grid got included as real
    pixel content. A single corner-sampled color can only ever remove
    ONE of the two checker colors, leaving a visible grid of the other
    -- confirmed exactly this way from a real generated slide (a second
    report, after a first fix that only handled a single flat color).
    Sampling many border pixels and taking the top few dominant colors
    (not just one) covers solid backgrounds and two-color checkerboards
    with the same logic, without needing to special-case either.

    Safety net: if trimming would erase the ENTIRE image (e.g. a solid-
    color image with no separable background, or every dominant border
    color happens to also be the icon's own color), returns the
    original, untouched image instead of a blank result."""
    img = img.convert("RGBA")
    original = img.copy()
    w, h = img.size

    border_pixels = []
    for x in range(w):
        border_pixels.append(img.getpixel((x, 0))[:3])
        border_pixels.append(img.getpixel((x, h - 1))[:3])
    for y in range(h):
        border_pixels.append(img.getpixel((0, y))[:3])
        border_pixels.append(img.getpixel((w - 1, y))[:3])

    from collections import Counter
    counts = Counter(border_pixels)
    total = len(border_pixels)
    bg_colors = []
    covered = 0
    for color, count in counts.most_common(4):
        if count / total < 0.03:
            break  # not a meaningfully common border color, stop considering more
        bg_colors.append(color)
        covered += count
        if covered / total > 0.9:
            break  # already accounts for nearly the whole border

    def is_background(r, g, b) -> bool:
        return any(max(abs(r - bc[0]), abs(g - bc[1]), abs(b - bc[2])) <= tolerance for bc in bg_colors)

    data = img.getdata()
    new_data = [(r, g, b, 0) if is_background(r, g, b) else (r, g, b, a) for (r, g, b, a) in data]
    img.putdata(new_data)
    bbox = img.getbbox()
    if not bbox:
        return original  # trimming would have erased everything -- not appropriate here
    return img.crop(bbox)


def _load_icon_fit(path: str, target_size: int, trim_flat_bg: bool = False) -> Image.Image:
    """Loads an icon and fits it within a target_size x target_size
    square, preserving aspect ratio and centering it, rather than
    stretching -- a third-party icon download isn't guaranteed to be
    perfectly square. trim_flat_bg=True additionally strips a baked-in
    flat background color (whatever it actually is -- see
    _trim_flat_background) and crops to content first -- needed for the
    real coin icon download, which has this exact problem."""
    img = Image.open(path).convert("RGBA")
    if trim_flat_bg:
        img = _trim_flat_background(img)
    ratio = min(target_size / img.width, target_size / img.height)
    new_w, new_h = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((target_size - new_w) // 2, (target_size - new_h) // 2))
    return canvas


def draw_year_label(base: Image.Image, top_center: tuple[int, int], year_label: str, style: SlideStyle) -> None:
    """A dark rounded-rect pill with just the year/version text (e.g.
    'FIFA 17', 'EA FC 27') -- for the player-evolution scenario, where
    each slide is one year of a player's card history. Same drawn-pill
    look as the price tag/rank badge for visual consistency across
    scenarios, just simpler (no icon needed)."""
    font = _font(style.font_path, style.year_label_font_size)
    d = ImageDraw.Draw(base)
    text_bbox = d.textbbox((0, 0), year_label, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]

    pad_x, pad_y = 32, 16
    pill_w = text_w + pad_x * 2
    pill_h = text_h + pad_y * 2

    x0 = top_center[0] - pill_w // 2
    y0 = top_center[1]
    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pd.rounded_rectangle([0, 0, pill_w - 1, pill_h - 1], radius=pill_h // 2,
                          fill=(*style.price_tag_bg_color, 235))
    pd.text((pad_x - text_bbox[0], pad_y - text_bbox[1]), year_label,
            font=font, fill=(*style.price_tag_text_color, 255))

    base.alpha_composite(pill, (x0, y0))


def draw_price_tag(base: Image.Image, top_center: tuple[int, int], price_text: str, style: SlideStyle,
                    coin_icon_path: Optional[str] = None) -> None:
    """A small dark rounded-rect pill with a coin icon and the price
    text. When coin_icon_path is given (the real coin image, downloaded
    once and cached -- see build_video.py's run_price), pastes that
    real asset instead of a hand-drawn approximation -- this replaced
    an earlier hand-drawn circle+"UT" text version after two rounds of
    feedback confirmed a drawn approximation wasn't matching closely
    enough. Falls back to the hand-drawn version if no path is given,
    or if loading it fails for any reason (corrupt download, missing
    file, etc.) -- one player's coin icon failing to load shouldn't
    break the whole batch."""
    font = _font(style.font_path, style.price_tag_font_size)
    d = ImageDraw.Draw(base)
    text_bbox = d.textbbox((0, 0), price_text, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]

    coin_d = int(style.price_tag_font_size * 1.45)
    pad_x, pad_y, gap = 20, 12, 12
    pill_w = coin_d + gap + text_w + pad_x * 2
    pill_h = max(coin_d, text_h) + pad_y * 2

    x0 = top_center[0] - pill_w // 2
    y0 = top_center[1]
    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pd.rounded_rectangle([0, 0, pill_w - 1, pill_h - 1], radius=pill_h // 2,
                          fill=(*style.price_tag_bg_color, 235))

    coin_cy = pill_h // 2
    coin_drawn = False
    if coin_icon_path:
        try:
            coin_img = _load_icon_fit(coin_icon_path, coin_d, trim_flat_bg=True)
            pill.alpha_composite(coin_img, (pad_x, coin_cy - coin_d // 2))
            coin_drawn = True
        except Exception:
            coin_drawn = False  # fall through to the hand-drawn version below

    if not coin_drawn:
        # Hand-drawn fallback: darker gold ring, then a lighter gold fill
        # slightly inset (an embossed two-tone edge), with "UT" text --
        # FIFA Ultimate Team's own currency icon shows this, not a dollar
        # sign (confirmed from a real easysbc.io screenshot).
        ring_width = max(2, int(coin_d * 0.09))
        pd.ellipse([pad_x - ring_width, coin_cy - coin_d // 2 - ring_width,
                    pad_x + coin_d + ring_width, coin_cy + coin_d // 2 + ring_width],
                   fill=(*style.price_tag_coin_ring_color, 255))
        pd.ellipse([pad_x, coin_cy - coin_d // 2, pad_x + coin_d, coin_cy + coin_d // 2],
                   fill=(*style.price_tag_coin_color, 255))
        coin_font = _font(style.font_path, int(coin_d * 0.42))
        coin_bbox = pd.textbbox((0, 0), "UT", font=coin_font)
        cw, ch = coin_bbox[2] - coin_bbox[0], coin_bbox[3] - coin_bbox[1]
        pd.text((pad_x + coin_d / 2 - cw / 2 - coin_bbox[0], coin_cy - ch / 2 - coin_bbox[1]),
                "UT", font=coin_font, fill=(60, 45, 5, 255))

    text_x = pad_x + coin_d + gap
    pd.text((text_x - text_bbox[0], (pill_h - text_h) / 2 - text_bbox[1]),
             price_text, font=font, fill=(*style.price_tag_text_color, 255))

    base.alpha_composite(pill, (x0, y0))


def draw_arrow(base: Image.Image, center: tuple[int, int], direction: str, style: SlideStyle,
                size: int = 140) -> None:
    """direction: 'up', 'down', or 'flat'."""
    color = {"up": style.up_color, "down": style.down_color, "flat": style.neutral_color}[direction]
    arrow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arrow)
    w = size
    if direction == "up":
        pts = [(w * 0.5, 0), (w, w * 0.55), (w * 0.68, w * 0.55), (w * 0.68, w), (w * 0.32, w), (w * 0.32, w * 0.55), (0, w * 0.55)]
    elif direction == "down":
        pts = [(w * 0.5, w), (0, w * 0.45), (w * 0.32, w * 0.45), (w * 0.32, 0), (w * 0.68, 0), (w * 0.68, w * 0.45), (w, w * 0.45)]
    else:
        pts = [(0, w * 0.45), (w, w * 0.45), (w, w * 0.55), (0, w * 0.55)]
    ad.polygon(pts, fill=(*color, 255))
    base.alpha_composite(arrow, (int(center[0] - size / 2), int(center[1] - size / 2)))


# ---------------------------------------------------------------------------
# transparent-canvas support: for laying your OWN background image/video
# behind the cards in CapCut instead of the flat navy gradient below.
# When transparent=True, callers must pass an out_path ending in .png
# (JPG has no alpha channel and would silently discard the transparency)
# and, for the *_from_photo(s) builders, the real_card_path(s) must
# ALREADY have their background removed (see bg_remove.py) -- these
# functions only composite, they don't call rembg themselves.
# ---------------------------------------------------------------------------

def _make_canvas(style: SlideStyle, transparent: bool) -> Image.Image:
    if transparent:
        w, h = style.canvas_size
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return _make_background(style).convert("RGBA")


def _paste_card(base: Image.Image, card: Image.Image, xy: tuple[int, int], style: SlideStyle, transparent: bool) -> None:
    """Drop shadow only makes sense against a background we control --
    a soft black blur baked into a transparent PNG can look like a dirty
    smudge once composited over an arbitrary background in CapCut, so
    transparent mode skips it and just pastes the card directly. (CapCut
    itself almost certainly has its own drop-shadow clip effect if you
    want that look back -- apply it once to the whole track there.)"""
    if transparent:
        base.alpha_composite(card, xy)
    else:
        _paste_with_shadow(base, card, xy, style)


def _save_slide(base: Image.Image, out_path: str, transparent: bool) -> None:
    if transparent:
        if not out_path.lower().endswith(".png"):
            raise ValueError(f"transparent=True needs a .png out_path (got {out_path!r})")
        base.save(out_path)
    else:
        base.convert("RGB").save(out_path, quality=92)


# ---------------------------------------------------------------------------
# Public slide builders
# ---------------------------------------------------------------------------

def build_topn_slide(out_path: str, rank: int, player_name: str, position: str, ovr: int,
                      stats: dict, face_path: str, style: SlideStyle,
                      nation_icon_path: Optional[str] = None, club_icon_path: Optional[str] = None,
                      highlight_stat: Optional[str] = None, transparent: bool = False) -> None:
    """One slide for a 'Top N <attribute>' style video, e.g. 'Top 20 Pace
    Players FC27'. Renders the full shield card (not just the face) with
    the ranked stat highlighted."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card = render_fut_card(ovr, position, player_name, stats, face_path, style,
                            nation_icon_path, club_icon_path, highlight_stat=highlight_stat)
    card_x = (w - card.width) // 2
    card_y = (h - card.height) // 2
    _paste_card(base, card, (card_x, card_y), style, transparent)

    draw_rank_badge(base, (card_x - 15, card_y - 15), rank, style)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def build_comparison_slide(out_path: str, player_name: str,
                            position: str, stats_old: dict, stats_new: dict,
                            face_old_path: str, face_new_path: str,
                            ovr_old: int, ovr_new: int, style: SlideStyle,
                            nation_icon_path: Optional[str] = None, club_icon_path: Optional[str] = None,
                            transparent: bool = False) -> None:
    """One slide for an 'FC26 vs FC27' comparison video: old card on the
    left, new card on the right, colored arrow in between -- both fully
    rendered shield cards, not bare face photos. Both cards are drawn at
    the SAME size (a deliberate change: an earlier version shrank the old
    card to 82% of the new card's size, which looked asymmetric and
    off-putting once seen with real cards side by side -- reported
    directly and fixed)."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card_old = render_fut_card(ovr_old, position, player_name, stats_old, face_old_path, style,
                                nation_icon_path, club_icon_path)
    card_new = render_fut_card(ovr_new, position, player_name, stats_new, face_new_path, style,
                                nation_icon_path, club_icon_path)

    gap = 200
    total_w = card_old.width + gap + card_new.width
    start_x = (w - total_w) // 2
    y = (h - card_new.height) // 2

    _paste_card(base, card_old, (start_x, y), style, transparent)
    _paste_card(base, card_new, (start_x + card_old.width + gap, y), style, transparent)

    direction = "up" if ovr_new > ovr_old else ("down" if ovr_new < ovr_old else "flat")
    arrow_cx = start_x + card_old.width + gap // 2
    arrow_cy = y + card_new.height // 2
    draw_arrow(base, (arrow_cx, arrow_cy), direction, style, size=int(gap * 0.65))

    delta = ovr_new - ovr_old
    delta_text = f"{'+' if delta > 0 else ''}{delta}"
    delta_font = _font(style.font_path, int(style.card_height * 0.05))
    delta_color = style.up_color if delta > 0 else (style.down_color if delta < 0 else style.neutral_color)
    _draw_centered_text(base, delta_text, arrow_cx, arrow_cy + int(gap * 0.4), delta_font, delta_color)

    name_font = _font(style.font_path, int(style.card_height * 0.062))
    _draw_centered_text(base, player_name, w // 2, y - int(style.card_height * 0.09), name_font, style.below_card_name_color)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def _load_real_card(path: str, target_height: int) -> Image.Image:
    """Loads an already-fully-rendered card image (e.g. a fut.gg
    screenshot, or a real card with its background already removed via
    bg_remove.py) and scales it to a target height, preserving its own
    aspect ratio -- unlike render_fut_card's shield, this image's
    proportions come from the source, not from SlideStyle."""
    img = Image.open(path).convert("RGBA")
    ratio = target_height / img.height
    return img.resize((max(1, int(img.width * ratio)), target_height), Image.LANCZOS)


def build_topn_slide_from_photo(out_path: str, rank: int, real_card_path: str, style: SlideStyle,
                                 transparent: bool = False) -> None:
    """Same as build_topn_slide, but pastes an ALREADY-COMPLETE card image
    (a real screenshot, via screenshot_card.py, or fut.gg's social-preview
    render as a fallback) instead of drawing one ourselves. If
    transparent=True, real_card_path must already have its background
    removed (see bg_remove.py) -- this function only composites."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card = _load_real_card(real_card_path, style.card_height)
    card_x = (w - card.width) // 2
    card_y = (h - card.height) // 2
    _paste_card(base, card, (card_x, card_y), style, transparent)

    draw_rank_badge(base, (card_x - 15, card_y - 15), rank, style)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def build_price_slide_from_photo(out_path: str, rank: int, real_card_path: str, price_text: str,
                                  style: SlideStyle, transparent: bool = False,
                                  coin_icon_path: Optional[str] = None) -> None:
    """A price-prediction slide (no FC26 comparison, no rank badge --
    removed per direct feedback): the real card + a price tag underneath,
    e.g. for an easysbc.io-driven "cheapest beasts" or "most expensive in
    the league" video. Same real-photo/transparent handling as
    build_topn_slide_from_photo. `rank` is accepted but no longer drawn --
    kept as a parameter so callers/tests don't need to change, in case a
    future style option brings it back."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card = _load_real_card(real_card_path, style.card_height)
    card_x = (w - card.width) // 2
    card_y = (h - card.height) // 2
    _paste_card(base, card, (card_x, card_y), style, transparent)

    draw_price_tag(base, (w // 2, card_y + card.height + 24), price_text, style, coin_icon_path)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def build_price_slide(out_path: str, rank: int, player_name: str, position: str, ovr: int,
                       stats: dict, face_path: str, price_text: str, style: SlideStyle,
                       nation_icon_path: Optional[str] = None, club_icon_path: Optional[str] = None,
                       transparent: bool = False, coin_icon_path: Optional[str] = None) -> None:
    """Hand-drawn-shield fallback for the price scenario, used when no
    real screenshot is available for a player (e.g. not found in the
    local fut.gg cache by resource id) -- reuses render_fut_card exactly
    like build_topn_slide's fallback, just with a price tag instead of a
    highlighted stat, and no rank badge (removed per direct feedback)."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card = render_fut_card(ovr, position, player_name, stats, face_path, style,
                            nation_icon_path, club_icon_path)
    card_x = (w - card.width) // 2
    card_y = (h - card.height) // 2
    _paste_card(base, card, (card_x, card_y), style, transparent)

    draw_price_tag(base, (w // 2, card_y + card.height + 24), price_text, style, coin_icon_path)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def build_evolution_slide_from_photo(out_path: str, year_label: str, real_card_path: str,
                                      style: SlideStyle, transparent: bool = False) -> None:
    """A player-evolution slide (real screenshot path): the real card +
    a year/version label underneath (e.g. 'FIFA 17', 'EA FC 27'). Same
    real-photo/transparent handling as build_topn_slide_from_photo/
    build_price_slide_from_photo."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card = _load_real_card(real_card_path, style.card_height)
    card_x = (w - card.width) // 2
    card_y = (h - card.height) // 2
    _paste_card(base, card, (card_x, card_y), style, transparent)

    draw_year_label(base, (w // 2, card_y + card.height + 24), year_label, style)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def build_evolution_slide(out_path: str, year_label: str, player_name: str, position: str, ovr: int,
                           stats: dict, face_path: str, style: SlideStyle,
                           nation_icon_path: Optional[str] = None, club_icon_path: Optional[str] = None,
                           transparent: bool = False) -> None:
    """Hand-drawn-shield fallback for the player-evolution scenario, used
    for years with no individually-linked detail page on fut.gg to
    screenshot (confirmed real case: FIFA 17-21 for at least one real
    player) -- reuses render_fut_card with the REAL face image, OVR,
    position and stats scraped directly from the FIFA History page for
    that year, just not fut.gg's own visual card design for that
    specific year."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card = render_fut_card(ovr, position, player_name, stats, face_path, style,
                            nation_icon_path, club_icon_path)
    card_x = (w - card.width) // 2
    card_y = (h - card.height) // 2
    _paste_card(base, card, (card_x, card_y), style, transparent)

    draw_year_label(base, (w // 2, card_y + card.height + 24), year_label, style)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)


def build_comparison_slide_from_photos(out_path: str, player_name: str,
                                        card_old_path: str, card_new_path: str,
                                        ovr_old: int, ovr_new: int, style: SlideStyle,
                                        transparent: bool = False) -> None:
    """Same as build_comparison_slide, but pastes two ALREADY-COMPLETE card
    images instead of drawing shields ourselves. If transparent=True, both
    card paths must already have their backgrounds removed. The player's
    name is already baked into each real card image, so it's not drawn
    again below them here. Both cards are drawn at the SAME height (a
    deliberate change: an earlier version shrank the old card to 82%,
    which looked visibly asymmetric and off-putting with real card
    screenshots side by side -- reported directly and fixed)."""
    w, h = style.canvas_size
    base = _make_canvas(style, transparent)

    card_new = _load_real_card(card_new_path, style.card_height)
    card_old = _load_real_card(card_old_path, style.card_height)

    gap = 200
    total_w = card_old.width + gap + card_new.width
    start_x = (w - total_w) // 2
    y = (h - card_new.height) // 2

    _paste_card(base, card_old, (start_x, y), style, transparent)
    _paste_card(base, card_new, (start_x + card_old.width + gap, y), style, transparent)

    direction = "up" if ovr_new > ovr_old else ("down" if ovr_new < ovr_old else "flat")
    arrow_cx = start_x + card_old.width + gap // 2
    arrow_cy = y + card_new.height // 2
    draw_arrow(base, (arrow_cx, arrow_cy), direction, style, size=int(gap * 0.65))

    delta = ovr_new - ovr_old
    delta_text = f"{'+' if delta > 0 else ''}{delta}"
    delta_font = _font(style.font_path, int(style.card_height * 0.05))
    delta_color = style.up_color if delta > 0 else (style.down_color if delta < 0 else style.neutral_color)
    _draw_centered_text(base, delta_text, arrow_cx, arrow_cy + int(gap * 0.4), delta_font, delta_color)

    _draw_watermark(base, style)
    _save_slide(base, out_path, transparent)
