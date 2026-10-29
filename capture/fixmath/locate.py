"""Selection geometry recovery from screen pixels.

The application we capture from has already painted the text selection, so the
highlight itself is the ground truth for "what is selected".  The mouse drag is
only a seed: it says *where* to look, never *how far* the selection reaches.

Pipeline:
    frame pixels
      -> background estimate
      -> adaptive ink mask (works on light and dark themes, ignores highlight)
      -> highlight mask (coloured, thick, not ink)
      -> connected components -> component that overlaps the drag
      -> trim to ink (drops line-end whitespace and the paragraph mark)
      -> pad for OCR
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

Rect = Tuple[int, int, int, int]
Point = Tuple[int, int]

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


# --------------------------------------------------------------------------- #
# result
# --------------------------------------------------------------------------- #
@dataclass
class LocateResult:
    box: Rect
    mode: str                      # highlight | drag | line | image
    hl_box: Optional[Rect] = None
    ink: Optional[np.ndarray] = field(default=None, repr=False)
    hl_mask: Optional[np.ndarray] = field(default=None, repr=False)
    bg_ref: Optional[np.ndarray] = field(default=None, repr=False)
    bg: Tuple[int, int, int] = (255, 255, 255)
    dark: bool = False


# --------------------------------------------------------------------------- #
# small geometry helpers
# --------------------------------------------------------------------------- #
def rect_area(r: Rect) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def rect_intersection(a: Rect, b: Rect) -> Rect:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def rect_union(a: Rect, b: Rect) -> Rect:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def expand_rect(r: Rect, dx: int, dy: Optional[int] = None) -> Rect:
    dy = dx if dy is None else dy
    return (r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy)


def clamp_rect(r: Rect, w: int, h: int) -> Rect:
    l = max(0, min(w - 1, r[0]))
    t = max(0, min(h - 1, r[1]))
    rr = max(l + 1, min(w, r[2]))
    bb = max(t + 1, min(h, r[3]))
    return (l, t, rr, bb)


def normalize_rect(a: Point, b: Point) -> Rect:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _contains(r: Rect, p: Point) -> bool:
    return r[0] <= p[0] < r[2] and r[1] <= p[1] < r[3]


# --------------------------------------------------------------------------- #
# morphological helpers (numpy only: no OpenCV / scipy available offline)
# --------------------------------------------------------------------------- #
def _dilate(mask: np.ndarray, kx: int, ky: int) -> np.ndarray:
    out = mask.copy()
    if kx > 1:
        half = kx // 2
        for s in range(1, half + 1):
            out[:, s:] |= mask[:, :-s]
            out[:, :-s] |= mask[:, s:]
    if ky > 1:
        half = ky // 2
        src = out.copy()
        for s in range(1, half + 1):
            out[s:, :] |= src[:-s, :]
            out[:-s, :] |= src[s:, :]
    return out


def _erode(mask: np.ndarray, kx: int, ky: int) -> np.ndarray:
    return ~_dilate(~mask, kx, ky)


def _close(mask: np.ndarray, kx: int, ky: int) -> np.ndarray:
    return _erode(_dilate(mask, kx, ky), kx, ky)


# --------------------------------------------------------------------------- #
# colour helpers
# --------------------------------------------------------------------------- #
def _border_median(arr: np.ndarray, thickness: int = 4) -> np.ndarray:
    h, w, _ = arr.shape
    t = max(1, min(thickness, h // 2, w // 2))
    strips = [
        arr[:t, :, :].reshape(-1, 3),
        arr[-t:, :, :].reshape(-1, 3),
        arr[:, :t, :].reshape(-1, 3),
        arr[:, -t:, :].reshape(-1, 3),
    ]
    return np.median(np.concatenate(strips, axis=0), axis=0)


def _luma(arr: np.ndarray) -> np.ndarray:
    return arr.astype(np.float32) @ _LUMA


def _ink_mask(luma: np.ndarray, radius: int, dark: bool) -> np.ndarray:
    """Adaptive: a pixel is ink when it is much darker/lighter than its local
    neighbourhood.  Comparing with the local mean (not the page background)
    keeps glyphs detectable even inside the selection highlight."""
    from PIL import Image as _Image

    base = np.clip(luma, 0, 255).astype(np.uint8)
    local = np.asarray(
        _Image.fromarray(base).filter(ImageFilter.BoxBlur(max(3, int(radius)))),
        dtype=np.float32,
    )
    if dark:
        return luma > local + 16.0
    return luma < local - 16.0


# --------------------------------------------------------------------------- #
# connected components (two-pass union find, on a downscaled mask)
# --------------------------------------------------------------------------- #
def _components(mask: np.ndarray, min_area: int = 80) -> List[Tuple[Rect, int, float]]:
    h, w = mask.shape
    if h == 0 or w == 0:
        return []

    step = max(1, int(math.ceil(w / 480.0)))
    small = np.asarray(
        Image.fromarray(mask.astype(np.uint8) * 255).resize(
            (max(1, w // step), max(1, h // step)), Image.NEAREST
        ),
        dtype=np.uint8,
    ) > 0

    sh, sw = small.shape
    labels = np.zeros((sh, sw), dtype=np.int32)
    parent = [0]

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    nxt = 1
    for y in range(sh):
        row = small[y]
        lab = labels[y]
        up = labels[y - 1] if y else None
        for x in range(sw):
            if not row[x]:
                continue
            a = 0
            if x and lab[x - 1]:
                a = lab[x - 1]
            if up is not None:
                if up[x]:
                    if a:
                        union(a, up[x])
                    else:
                        a = up[x]
                if x and up[x - 1]:
                    if a:
                        union(a, up[x - 1])
                    else:
                        a = up[x - 1]
                if x + 1 < sw and up[x + 1]:
                    if a:
                        union(a, up[x + 1])
                    else:
                        a = up[x + 1]
            if a:
                lab[x] = find(a)
            else:
                lab[x] = nxt
                parent.append(nxt)
                nxt += 1

    if nxt <= 1:
        return []

    flat = labels.ravel()
    areas = np.bincount(flat, minlength=nxt)
    # bbox accumulation
    minx = np.full(nxt, sw, dtype=np.int32)
    maxx = np.full(nxt, -1, dtype=np.int32)
    miny = np.full(nxt, sh, dtype=np.int32)
    maxy = np.full(nxt, -1, dtype=np.int32)

    ys, xs = np.nonzero(labels)
    lab_vals = labels[ys, xs]
    np.minimum.at(minx, lab_vals, xs)
    np.maximum.at(maxx, lab_vals, xs)
    np.minimum.at(miny, lab_vals, ys)
    np.maximum.at(maxy, lab_vals, ys)

    out: List[Tuple[Rect, int, float]] = []
    for i in range(1, nxt):
        area = int(areas[i])
        if area < min_area / (step * step):
            continue
        if maxx[i] < 0:
            continue
        bw = (maxx[i] - minx[i] + 1) * step
        bh = (maxy[i] - miny[i] + 1) * step
        fill = area / float((maxx[i] - minx[i] + 1) * (maxy[i] - miny[i] + 1))
        box = (int(minx[i] * step), int(miny[i] * step),
               int((maxx[i] + 1) * step), int((maxy[i] + 1) * step))
        out.append((box, bw * bh, float(fill)))
    return out


# --------------------------------------------------------------------------- #
# highlight / line recovery
# --------------------------------------------------------------------------- #
def _dist_to_rect(p: Point, r: Rect) -> float:
    dx = max(r[0] - p[0], 0, p[0] - r[2])
    dy = max(r[1] - p[1], 0, p[1] - r[3])
    return (dx * dx + dy * dy) ** 0.5


def _grow_union(seed: Rect, candidates: List[Rect], max_gap: int) -> Rect:
    """Attach nearby fragments that belong to the same selected block.

    A piecewise definition can be split into one highlighted fragment per row;
    the user's drag often crosses only one row, but the whole block is what the
    PDF has selected.  Grow by a small absolute gap, never page-wide.
    """
    union = seed
    remaining = [b for b in candidates if b != seed]
    changed = True
    while changed:
        changed = False
        probe = expand_rect(union, max_gap)
        for box in list(remaining):
            if rect_area(rect_intersection(probe, box)) > 0:
                union = rect_union(union, box)
                remaining.remove(box)
                changed = True
    return union


def _pick_highlight(
    hl: np.ndarray, drag: Optional[Rect], click: Optional[Point], dpi_scale: float = 1.0,
    arr: Optional[np.ndarray] = None, bg: Optional[np.ndarray] = None,
) -> Optional[Rect]:
    h, w = hl.shape
    comps = _components(hl, min_area=200)
    if not comps:
        return None

    candidates: List[Rect] = []
    for box, _, fill in comps:
        bw = box[2] - box[0]
        bh = box[3] - box[1]
        if bw < 12 or bh < 6:
            continue
        if bw >= w * 0.97 and bh >= h * 0.97:       # whole frame: background
            continue
        if fill < 0.25:                              # thin strokes, not a band
            continue
        if arr is not None and bg is not None:
            sub = hl[box[1]:box[3], box[0]:box[2]]
            vals = arr[box[1]:box[3], box[0]:box[2]][sub].astype(np.float32)
            if vals.shape[0] < 50:
                continue
            # A real selection band is one flat colour.  Use the median colour
            # and require most band pixels to be close to it; ClearType fringes
            # and text edges fail that, and a band that is just page background
            # fails the distance-from-background check.
            med = np.median(vals, axis=0)
            close = np.abs(vals - med).max(axis=1) < 20.0
            if float(close.mean()) < 0.40:
                continue
            if float(np.sqrt(((med - bg.astype(np.float32)) ** 2).sum())) < 22.0:
                continue
        candidates.append(box)

    if not candidates:
        return None

    if drag is not None:
        # Union *every* highlight fragment the drag touches.  A selected
        # formula is often split into pieces by the PDF renderer (fractions,
        # matrices, inline images), so an area-percentage gate would keep only
        # the largest fragment and crop a tiny slice of the formula.
        hits = [b for b in candidates if rect_area(rect_intersection(b, drag)) > 0]
        if hits:
            u = hits[0]
            for b in hits[1:]:
                u = rect_union(u, b)
            return _grow_union(u, candidates, max_gap=max(10, int(20 * dpi_scale)))

        # A short drag may not touch the selection at all (the user clicked, or
        # the selection drag happened before the hook was ready).  Only accept
        # a *nearby* band, and only for a short drag, otherwise unrelated
        # coloured UI on the desktop wins.
        if rect_area(drag) <= 60000:
            cx = (drag[0] + drag[2]) / 2.0
            cy = (drag[1] + drag[3]) / 2.0
            nearest = min(candidates, key=lambda b: _dist_to_rect((cx, cy), b))
            if _dist_to_rect((cx, cy), nearest) <= 140 * dpi_scale:
                return nearest
        return None

    if click is not None:
        hit = [b for b in candidates if _contains(b, click)]
        if hit:
            u = hit[0]
            for b in hit[1:]:
                u = rect_union(u, b)
            return u
    return None


def _band_for(ink: np.ndarray, box: Rect) -> Optional[Rect]:
    h, w = ink.shape
    rows = ink.sum(axis=1)
    thr = max(1, int(w * 0.001))
    on = rows > thr
    runs: List[Tuple[int, int]] = []
    y = 0
    while y < h:
        if not on[y]:
            y += 1
            continue
        y0 = y
        while y < h and on[y]:
            y += 1
        runs.append((y0, y))
    if not runs:
        return None
    touching = [r for r in runs if r[0] < box[3] and r[1] > box[1]]
    if not touching:
        return None
    top = min(r[0] for r in touching)
    bot = max(r[1] for r in touching)
    return (0, top, w, bot)


def _expand_columns(
    ink: np.ndarray, band: Rect, l: int, r: int, gap_thr: int, limit: Optional[int] = None
) -> Tuple[int, int]:
    h, w = ink.shape
    cols = ink[band[1]:band[3], :].sum(axis=0)
    max_scan = min(int(w * 0.9), limit) if limit else int(w * 0.9)

    x = max(0, min(w - 1, l))
    gap = 0
    steps = 0
    while x > 0 and steps < max_scan:
        if cols[x] == 0:
            gap += 1
            if gap > gap_thr:
                break
        else:
            gap = 0
        x -= 1
        steps += 1
    left = x + gap + 1

    x = max(0, min(w - 1, r))
    gap = 0
    steps = 0
    while x < w - 1 and steps < max_scan:
        if cols[x] == 0:
            gap += 1
            if gap > gap_thr:
                break
        else:
            gap = 0
        x += 1
        steps += 1
    right = x - gap
    if right <= left:
        return (l, r)
    return (left, right)


def _ink_bbox(ink: np.ndarray, probe: Rect) -> Optional[Rect]:
    h, w = ink.shape
    l, t, r, b = clamp_rect(probe, w, h)
    sub = ink[t:b, l:r]
    if not sub.any():
        return None
    ys = np.nonzero(sub.sum(axis=1) > 0)[0]
    xs = np.nonzero(sub.sum(axis=0) > 0)[0]
    if len(ys) == 0 or len(xs) == 0:
        return None
    return (l + int(xs[0]), t + int(ys[0]), l + int(xs[-1]) + 1, t + int(ys[-1]) + 1)


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def _point_rect_dist(rect: Rect, point: Tuple[int, int]) -> float:
    x0, y0, x1, y1 = rect
    x, y = point
    dx = max(x0 - x, 0, x - x1)
    dy = max(y0 - y, 0, y - y1)
    return (dx * dx + dy * dy) ** 0.5


def expand_to_ink(shot: Image.Image, box: Rect, pad: int = 6) -> Rect:
    """Grow a drag rectangle across small blank gaps to the whole formula.

    Stops at gaps larger than a line/word space, so the neighbouring prose
    line is not swallowed.  This is still the user's rectangle, just completed
    to the edges of the connected ink.
    """
    x0, y0, x1, y1 = box
    w, h = shot.width, shot.height
    arr = np.asarray(shot.convert("RGB"), dtype=np.float32)
    dark = _luma(arr) < 150.0

    band = dark[:, max(0, x0):min(w, x1)]
    rows = band.any(axis=1)
    top, bottom = y0, y1
    while top > 0 and rows[max(0, top - 16):top].any() and y0 - top < 300:
        top -= 1
    while bottom < h and rows[bottom:min(h, bottom + 16)].any() and bottom - y1 < 200:
        bottom += 1

    cols = dark[top:bottom, :].any(axis=0)
    left, right = x0, x1
    while left > 0 and cols[max(0, left - 22):left].any() and x0 - left < 400:
        left -= 1
    while right < w and cols[right:min(w, right + 22)].any() and right - x1 < 500:
        right += 1

    return (
        max(0, left - pad),
        max(0, top - pad),
        min(w, right + pad),
        min(h, bottom + pad),
    )


def locate_hover(shot: Image.Image, point: Tuple[int, int],
                 dpi_scale: float = 1.0) -> Optional[LocateResult]:
    """Find the formula block under the cursor without any text selection.

    DeepSeek renders formulas in blue while prose is black, so the blue text
    mask isolates the formula line; a small dilation joins the glyphs into one
    block.  No selection means no highlight rectangles, hence no fake hats or
    underlines in the cleaned crop.
    """
    arr = np.asarray(shot.convert("RGB"), dtype=np.float32)
    luma = _luma(arr)
    # Deselected formulas are dark glyphs on a light page; the blue-only mask
    # split a line into individual tokens, so use darkness and join tokens with
    # a wide horizontal dilation.
    mask = luma < 150.0
    grown = _dilate(mask, 25, 9)
    comps = _components(grown, min_area=150)
    if not comps:
        return None
    best = min(comps, key=lambda c: _point_rect_dist(c[0], point))
    box = expand_rect(best[0], 8, 8)
    box = (
        max(0, box[0]),
        max(0, box[1]),
        min(shot.width, box[2]),
        min(shot.height, box[3]),
    )
    if rect_area(box) < 400:
        return None
    return LocateResult(box=box, mode="hover")


def locate(
    frame: Image.Image,
    drag: Optional[Rect] = None,
    click: Optional[Point] = None,
    dpi_scale: float = 1.0,
) -> LocateResult:
    img = frame.convert("RGB")
    w, h = img.size
    arr = np.asarray(img, dtype=np.int16)
    bg = _border_median(arr)
    luma = _luma(arr)
    bg_lum = float(bg.astype(np.float32) @ _LUMA)
    dark = bg_lum < 110

    dist = np.sqrt(((arr.astype(np.float32) - bg.astype(np.float32)) ** 2).sum(axis=2))

    # Text is dark *and* neutral; a selection band is either coloured or much
    # brighter.  Requiring some saturation keeps a flat gray page/panel from
    # being mistaken for a selection (it would otherwise swallow the equation
    # number on the right).
    mx = arr.max(axis=2).astype(np.float32)
    mn = arr.min(axis=2).astype(np.float32)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    if dark:
        text_like = (luma > bg_lum + 70) & (sat < 0.30)
    else:
        text_like = (luma < bg_lum - 70) & (sat < 0.30)
    colored = (dist > 24.0) & (sat > 0.05)
    hl = colored & ~text_like
    hl = _close(hl, _odd(max(7, int(15 * dpi_scale))), _odd(max(3, int(3 * dpi_scale))))

    hl_box = _pick_highlight(hl, drag, click, dpi_scale, arr, bg)

    if hl_box is not None:
        # A thin selection border can stretch the bounding box across the
        # equation number "(13)".  Keep only columns where the highlight band
        # actually has vertical mass.
        sub = hl[hl_box[1]:hl_box[3], hl_box[0]:hl_box[2]]
        if sub.size:
            density = sub.mean(axis=0)
            # Only drop columns that contain *no* highlight at all.  A sparse
            # band (e.g. an upper-branch condition) must survive: it occupies
            # few rows but is part of the selection.
            good = np.nonzero(density > 0.01)[0]
            if good.size > 1:
                left = hl_box[0] + int(good[0])
                right = hl_box[0] + int(good[-1]) + 1
                if right - left >= 24:
                    hl_box = (left, hl_box[1], right, hl_box[3])

    # Local reference luminance: page background outside the selection, the
    # highlight colour inside it.  A single global threshold cannot serve both.
    ref = np.full(luma.shape, bg_lum, dtype=np.float32)
    band = None
    if hl_box is not None:
        l, t, r, b = hl_box
        region = np.zeros(luma.shape, dtype=bool)
        region[max(0, t - 6):b + 6, max(0, l - 6):r + 6] = True
        vals = luma[hl & region]
        if vals.size:
            # Paint the highlight colour only where the band actually is (plus
            # a little dilation for glyph holes).  Filling the whole bounding
            # box would turn white page gaps inside the box into "text".
            band = _dilate(hl, max(3, int(3 * dpi_scale)),
                           max(3, int(3 * dpi_scale))) & region
            ref[band] = float(np.median(vals))
            # Smooth the page/highlight boundary; a hard step there maps the
            # transition pixels to black and draws boxes around the selection.
            ref_img = Image.fromarray(np.clip(ref, 0, 255).astype(np.uint8))
            ref_img = ref_img.filter(ImageFilter.GaussianBlur(1.5))
            ref = np.asarray(ref_img, dtype=np.float32)

    ink_base = (luma > ref + 45.0) if dark else (luma < ref - 45.0)
    ink = ink_base
    if band is not None:
        # Inside a selection the text can be *lighter* than the band (white on
        # blue), so polarity has to be decided per pixel: anything far from the
        # local reference counts as ink.
        ink = np.where(band, np.abs(luma - ref) > 45.0, ink_base)

    if hl_box is not None:
        base, mode = hl_box, "highlight"
    elif drag is not None:
        band = _band_for(ink, drag)
        if band is not None:
            gap = max(12, int((band[3] - band[1]) * 0.55))
            l, r = _expand_columns(ink, band, drag[0], drag[2], gap)
            base = (l, band[1], r, band[3])
        else:
            base = drag
        mode = "drag"
    elif click is not None:
        half = max(30, int(40 * dpi_scale))
        seed = (click[0] - 4, click[1] - half, click[0] + 4, click[1] + half)
        band = _band_for(ink, seed)
        if band is not None:
            gap = max(12, int((band[3] - band[1]) * 0.55))
            reach = max(120, int(320 * dpi_scale))
            l, r = _expand_columns(ink, band, click[0], click[0] + 1, gap, limit=reach)
            base = (l, band[1], r, band[3])
        else:
            base = (max(0, click[0] - 200), max(0, click[1] - 40),
                    min(w, click[0] + 200), min(h, click[1] + 40))
        mode = "line"
    else:
        base, mode = (0, 0, w, h), "image"

    if mode == "highlight":
        # The band is already the selection's own extent.  Expanding it would
        # pull glyphs from the neighbouring lines into the ink bbox.
        probe = clamp_rect(base, w, h)
    else:
        probe = clamp_rect(expand_rect(base, max(4, int(8 * dpi_scale))), w, h)
    tight = _ink_bbox(ink, probe)
    if tight is not None and rect_area(tight) >= rect_area(base) * 0.12:
        base = tight

    pad = max(4, int(round(6 * dpi_scale)))
    final = clamp_rect(expand_rect(base, pad), w, h)
    if rect_area(final) <= 0:
        final = (0, 0, w, h)

    # crop debug masks to the final box
    fl, ft, fr, fb = final
    return LocateResult(
        box=final,
        mode=mode,
        hl_box=hl_box,
        ink=ink[ft:fb, fl:fr],
        hl_mask=hl[ft:fb, fl:fr],
        bg_ref=ref[ft:fb, fl:fr],
        bg=tuple(int(v) for v in bg),
        dark=dark,
    )


def _odd(n: int) -> int:
    return n if n % 2 == 1 else n + 1


def _flatten_background(img: Image.Image) -> Image.Image:
    """Force any leftover shading (selection shadows, gradients) to white.

    After the affine normalisation the paper is near 255 and glyphs near 0, but
    a PDF selection shadow sits around 200-240 and survives.  A fixed
    black/white point stretch maps that whole band to white without a spatial
    filter (which tended to keep the shadow's own level).
    """
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    out = np.clip((gray - 30.0) * 255.0 / 170.0, 0, 255).astype(np.uint8)
    return Image.fromarray(out).convert("RGB")


def _hull_fill(mask: np.ndarray, min_area: int = 15) -> np.ndarray:
    """Fill each connected component's convex hull.

    White glyphs on a blue selection are enclosed by the blue of their own
    rectangle, but the rectangles are separate: a global hole fill leaks
    through anti-aliased edges, and a big closing bridges the page gaps.
    Per-component hulls fill the glyphs without touching the gaps.
    """
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    inside = np.zeros_like(mask, dtype=bool)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def hull(points):
        pts = sorted(set(points))
        if len(pts) <= 2:
            return pts
        lower, upper = [], []
        for p in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
        for p in reversed(pts):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)
        return lower[:-1] + upper[:-1]

    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        stack = [(y0, x0)]
        points = []
        while stack:
            cy, cx = stack.pop()
            points.append((cx, cy))
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(points) < min_area:
            continue
        piece = Image.new("L", (w, h), 0)
        ImageDraw.Draw(piece).polygon(hull(points), fill=255)
        inside |= np.asarray(piece) > 0
    return inside


def clean_crop(frame: Image.Image, res: LocateResult) -> Image.Image:
    """Turn the selected region into a clean black-on-white bitmap.

    A pixel is text when it is far from the local background:
      * inside the selection band: white glyphs on a saturated blue band, or
        dark glyphs on a light band;
      * outside: dark text on a light page, or light text on a dark page.
    Binarising this way also drops selection shadows and fragment borders,
    which a per-pixel affine mapping used to turn into black boxes.
    """
    crop = frame.convert("RGB").crop(res.box)
    arr = np.asarray(crop, dtype=np.float32)
    luma = np.clip(_luma(arr), 0, 255)

    # Strong blue selection (browser "white text on blue"): the band is the
    # only reliable mask.  Everything outside it is page, so no fragment
    # borders or shadows can leak into the glyph mask.
    blue = (arr[..., 2] > arr[..., 0] + 50.0) & (arr[..., 2] > 140.0)
    if blue.mean() > 0.25:
        # Seal 1-2 px anti-alias leaks, then fill enclosed glyph holes.  Open
        # gaps between separate selection rectangles stay outside, so they do
        # not turn into black bars.
        region = _hull_fill(blue)
        inner = region.copy()
        inner[1:, :] &= region[:-1, :]
        inner[:-1, :] &= region[1:, :]
        inner[:, 1:] &= region[:, :-1]
        inner[:, :-1] &= region[:, 1:]
        border = region & ~inner
        out = np.full(luma.shape, 255, dtype=np.uint8)
        blue_lum = float(np.median(luma[blue]))
        if blue_lum < 170.0:
            # White text on a saturated blue band.  Reconstruct a grey alpha
            # instead of binarising: binary strokes look bold/blocky when the
            # crop is small, while the red-channel ratio keeps anti-aliasing.
            red = arr[..., 0]
            base = float(np.median(red[blue]))
            alpha = np.clip((red - base) / max(1.0, 255.0 - base), 0.0, 1.0)
            alpha[~region] = 0.0
            out = (255.0 * (1.0 - alpha)).astype(np.uint8)
            # Only clear the band's own frame; eroding the whole region would
            # also clip thin hats/accents that sit near the rectangle edge.
            out[border] = 255
            return Image.fromarray(out).convert("RGB")
        # Light-blue band with dark glyphs: fall through to the contrast
        # stretch below, which keeps anti-aliased strokes.

    if not res.dark:
        # Light page: stretch the band's grey level to white while preserving
        # glyph anti-aliasing.  Binarising here thins 1-2 px strokes and breaks
        # OCR on WPS/PDF selections.
        if res.mode in ("web", "hover"):
            if float(np.median(luma)) < 128.0:
                # Dark-mode page: keep light text on a dark page readable.
                out = (255.0 - np.clip((luma - 20.0) * 255.0 / 180.0, 0, 255)).astype(np.uint8)
            else:
                out = np.clip((luma - 60.0) * 255.0 / 140.0, 0, 255).astype(np.uint8)
        else:
            out = np.clip((luma - 20.0) * 255.0 / 180.0, 0, 255).astype(np.uint8)
        return Image.fromarray(out).convert("RGB")

    out = np.full(luma.shape, 255, dtype=np.uint8)
    bg = res.bg_ref
    if bg is not None and bg.shape == luma.shape:
        band = bg < 245.0
        inside = np.zeros(band.shape, dtype=bool)
        if band.any():
            band_lum = float(np.median(bg[band]))
            # Close small glyph holes (white text) but keep large page gaps
            # outside, so a white page margin is not mistaken for glyphs.
            inside = _close(band, 7, 7)
            if band_lum < 150.0:        # white text on a saturated band
                # Use the red channel: a blue band has almost no red, while
                # white glyphs do.  Selection borders and anti-aliased edges are
                # blue, so they disappear instead of becoming black frames.
                mask = inside & (arr[..., 0] > 150.0)
            else:                        # dark text on a light band
                mask = inside & (luma < 120.0)
        else:
            mask = np.zeros(luma.shape, dtype=bool)
        if res.dark:
            mask |= (~_dilate(band, 9, 9)) & (luma > 150.0)
        else:
            mask |= (~_dilate(band, 9, 9)) & (luma < 120.0)
        out[mask] = 0
        # Erase the band's own outline: some PDF/browser selections draw a thin
        # border which otherwise survives as a black box around each fragment
        # (and the OCR model then reads it as \boxed{...}).
        edge = _dilate(band, 5, 5) & ~_erode(band, 5, 5)
        out[edge] = 255
    else:
        out[luma < 120.0] = 0

    return Image.fromarray(out).convert("RGB")
