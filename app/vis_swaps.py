"""
vis_swaps.py — Render the optimizer swap plan as an annotated room image.

For each room that has pending swaps, produces:
  vis/swaps_room<N>.png  — room image with coloured outlines on every swap slot
                           + a legend strip below: [remove img] → [add img] +Δ

Reads:
  optimizer_swaps.json   — swap plan written by optimizer.py
  placed_room*.json      — room layouts
  miners/                — miner GIF images (already fetched)

Usage:
  python vis_swaps.py          # all rooms
  python vis_swaps.py 2        # only room 2
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from visualize_room import (
    CELL_W, CELL_H, TOTAL_LABEL_H, PADDING, BACKGROUND,
    PLACED_DIR,
    render, load_first_frame, load_badge, _load_font,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT       = Path(__file__).parent.parent   # roomBuilder/
SWAPS_JSON  = _ROOT / "data/optimizer_swaps.json"
OUTPUT_DIR  = _ROOT / "output"

# ── Swap-number palette (cycles if more than 8 swaps) ────────────────────────
_PALETTE: list[tuple[int, int, int]] = [
    (231,  76,  60),   # red
    ( 52, 152, 219),   # blue
    ( 46, 204, 113),   # green
    (230, 126,  34),   # orange
    (155,  89, 182),   # purple
    ( 26, 188, 156),   # teal
    (241, 196,  15),   # gold
    (  0, 172, 193),   # cyan
]

# ── Legend card metrics ───────────────────────────────────────────────────────
_TW  = 80   # thumbnail width
_TH  = 68   # thumbnail height
_AW  = 28   # arrow section width
_LP  = 10   # padding between sections
_LH  = _TH + 42            # legend row height (thumb + up to 3 text lines)
_CW  = _TW * 2 + _AW + _LP * 4   # one card width
_LBG = (240, 242, 248)    # legend background colour

# Lightweight name → image-stem lookup so _thumb can resolve unusual filenames
# like MrPresident.gif for "Mr.President" without an underscore.
_MINERS_DATA = _ROOT / "miners/miners_data.json"


def _load_image_stems() -> dict[str, str]:
    """Return {name.lower(): image_stem} from miners_data.json."""
    if not _MINERS_DATA.exists():
        return {}
    try:
        data = json.loads(_MINERS_DATA.read_text(encoding="utf-8"))
        return {m["name"].lower(): Path(m.get("image", "")).stem
                for m in data if m.get("image")}
    except Exception:
        return {}


_IMAGE_STEMS: dict[str, str] = _load_image_stems()

# Short rarity labels for compact in-cell overlay
_RARITY_ABBR: dict[str, str] = {
    "common":    "",
    "uncommon":  "U",
    "rare":      "R",
    "epic":      "E",
    "legendary": "L",
    "unreal":    "UR",
    "legacy":    "LGC",
}


def _e_name(e) -> str:
    """Extract miner name from a swap entry (dict or legacy str)."""
    return e["name"] if isinstance(e, dict) else e


def _e_rarity(e) -> str:
    """Extract miner rarity from a swap entry (dict or legacy str)."""
    return (e.get("rarity") if isinstance(e, dict) else None) or "common"


def _col(i: int) -> tuple[int, int, int]:
    return _PALETTE[i % len(_PALETTE)]


def _thumb(name: str, rarity: str | None = None) -> Image.Image:
    """Load a fixed-size RGBA thumbnail for a miner by name, with optional rarity badge."""
    bg = Image.new("RGBA", (_TW, _TH), (210, 210, 210, 255))
    # Try DB image stem first (handles names like "Mr.President" → "MrPresident")
    db_stem = _IMAGE_STEMS.get(name.lower(), "")
    img = (load_first_frame(db_stem) if db_stem else None) or load_first_frame("", name)
    if img:
        img = img.convert("RGBA")
        img.thumbnail((_TW, _TH), Image.LANCZOS)
        ox = (_TW - img.width)  // 2
        oy = (_TH - img.height) // 2
        bg.paste(img, (ox, oy), img)
    badge = load_badge(rarity)
    if badge:
        badge = badge.copy()
        badge.thumbnail((22, 22), Image.LANCZOS)
        bg.paste(badge, (2, 2), badge)
    return bg


def _multi_thumb(entries: list) -> Image.Image:
    """
    Render 1 or 2 swap entries into a single _TW×_TH image.
    Two entries are shown side-by-side at half width, split by a white divider.
    """
    if len(entries) <= 1:
        e = entries[0] if entries else {}
        return _thumb(_e_name(e) if e else "?", _e_rarity(e) if e else None)

    # Use half-width panels but preserve aspect ratio for each thumb.
    half_w = (_TW - 2) // 2
    panel = Image.new("RGBA", (_TW, _TH), (200, 200, 200, 255))
    for idx, e in enumerate(entries[:2]):
        t = _thumb(_e_name(e), _e_rarity(e))
        # Preserve aspect ratio: scale to fit within half_w x _TH
        t = t.copy()
        t.thumbnail((half_w, _TH), Image.LANCZOS)
        ox = idx * (half_w + 2) + (half_w - t.width) // 2
        oy = (_TH - t.height) // 2
        panel.paste(t, (ox, oy), t)

    # Divider line between the two thumbs
    pd = ImageDraw.Draw(panel)
    div_x = half_w + 1
    pd.line([(div_x, 2), (div_x, _TH - 3)], fill=(255, 255, 255, 200), width=2)
    return panel


def _trunc_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def render_swap_image(
    room_num: int,
    racks: list[list[dict]],
    room_swaps: list[dict],
) -> Image.Image:
    """Render base room + coloured overlays on swap slots + legend strip."""

    total_cell_h = CELL_H + TOTAL_LABEL_H

    # ── 1. Base room ──────────────────────────────────────────────────────────
    base = render(racks).convert("RGBA")
    draw = ImageDraw.Draw(base)
    fn11 = _load_font(11)
    fn9  = _load_font(9)
    fn16 = _load_font(16)

    # ── 2. Swap overlays ──────────────────────────────────────────────────────
    for swap_i, swap in enumerate(room_swaps):
        c  = _col(swap_i)
        ri = swap["rack"] - 1
        x0 = PADDING + ri * (CELL_W + PADDING)

        for pos in swap.get("rack_positions", []):
            y0 = PADDING + pos * (total_cell_h + PADDING)

            # Thick coloured border (3 px wide)
            for t in range(3):
                draw.rectangle(
                    [x0 - t, y0 - t, x0 + CELL_W + t, y0 + CELL_H + t],
                    outline=c + (255,),
                )

            # Numbered circle badge (top-right corner)
            br = 11
            bx = x0 + CELL_W - br - 2
            by = y0 + br + 2
            draw.ellipse(
                [bx - br, by - br, bx + br, by + br],
                fill=c + (220,), outline=(255, 255, 255, 255),
            )
            draw.text((bx, by), str(swap_i + 1),
                      fill=(255, 255, 255, 255), anchor="mm", font=fn11)

            # If swap adds two miners (pair), draw two mini-slots side-by-side
            _add_items = swap.get("add", [])
            if len(_add_items) == 2:
                left = _add_items[0]
                right = _add_items[1]
                # thumbnails inside half-width panels, preserve aspect
                pad = 6
                thumb_w = (CELL_W - pad * 3) // 2
                thumb_h = CELL_H - 28
                # left thumb
                lt = _thumb(_e_name(left), _e_rarity(left) if left else None)
                lt = lt.copy()
                lt.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
                lx = x0 + pad + (thumb_w - lt.width) // 2
                ly = y0 + 18 + (thumb_h - lt.height) // 2
                base.paste(lt, (lx, ly), lt)
                # right thumb
                rt = _thumb(_e_name(right), _e_rarity(right) if right else None)
                rt = rt.copy()
                rt.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
                rx = x0 + pad * 2 + thumb_w + (thumb_w - rt.width) // 2
                ry = y0 + 18 + (thumb_h - rt.height) // 2
                base.paste(rt, (rx, ry), rt)
                # plus sign between them
                px = x0 + CELL_W // 2
                py = y0 + CELL_H // 2
                draw.text((px, py), "+", fill=(80, 80, 80, 255), anchor="mm", font=fn16)
                # small label under thumbs
                add_label = _trunc_text(draw, _e_name(left) + " + " + _e_name(right), fn9, CELL_W - 4)
                draw.text((x0 + CELL_W // 2, y0 + 3), add_label, fill=(255,255,255,255), anchor="mt", font=fn9)
            else:
                add_label = " + ".join(_e_name(e) for e in _add_items)
                if add_label:
                    al = _trunc_text(draw, add_label, fn9, CELL_W - 4)
                    draw.text(
                        (x0 + CELL_W // 2, y0 + 3),
                        al, fill=(255, 255, 255, 255), anchor="mt", font=fn9,
                    )

            # Size-change pill at bottom of cell
            n_rem = len(swap.get("remove", []))
            n_add = len(swap.get("add",    []))
            if n_rem != n_add:
                tag    = "\u2192 PAIR" if n_add > n_rem else "\u2192 2-CELL"
                tw_tag = draw.textlength(tag, font=fn9)
                ty     = y0 + CELL_H - 15
                draw.rectangle(
                    [x0 + (CELL_W - tw_tag) / 2 - 3, ty - 2,
                     x0 + (CELL_W + tw_tag) / 2 + 3, ty + 10],
                    fill=c + (220,),
                )
                draw.text((x0 + CELL_W // 2, ty),
                          tag, fill=(255, 255, 255, 255), anchor="mt", font=fn9)

    # ── 3. Legend strip ───────────────────────────────────────────────────────
    n = len(room_swaps)
    if n == 0:
        return base.convert("RGB")

    cols   = max(1, base.width // (_CW + _LP))
    rows   = (n + cols - 1) // cols
    leg_h  = rows * (_LH + _LP * 2) + _LP
    legend = Image.new("RGB", (base.width, leg_h), _LBG)
    ldraw  = ImageDraw.Draw(legend)
    fn8    = _load_font(8)
    fn9l   = _load_font(9)

    for swap_i, swap in enumerate(room_swaps):
        c   = _col(swap_i)
        col = swap_i % cols
        row = swap_i // cols
        cx  = _LP + col * (_CW + _LP)
        cy  = _LP + row * (_LH + _LP * 2)

        # Number badge
        ldraw.ellipse([cx, cy + 2, cx + 18, cy + 20], fill=c)
        ldraw.text((cx + 9, cy + 11), str(swap_i + 1),
                   fill=(255, 255, 255), anchor="mm", font=fn8)
        cx += 22

        rem_entries = swap.get("remove", [])
        add_entries = swap.get("add",    [])
        delta_val   = swap.get("delta_eff", 0.0)

        # Remove thumbnail(s)
        rem_img = _multi_thumb(rem_entries)
        legend.paste(rem_img, (cx, cy), rem_img)
        rem_lbl = _trunc_text(
            ldraw,
            " + ".join(_e_name(e) for e in rem_entries),
            fn8, _TW - 2,
        )
        ldraw.text((cx + _TW // 2, cy + _TH + 2), rem_lbl,
                   fill=(50, 50, 50), anchor="mt", font=fn8)

        # Arrow
        ax = cx + _TW + 2
        ldraw.text((ax + _AW // 2, cy + _TH // 2), "→",
                   fill=(80, 80, 80), anchor="mm", font=fn16)

        # Add thumbnail(s) + label + power delta
        ax2     = ax + _AW
        add_img = _multi_thumb(add_entries)
        legend.paste(add_img, (ax2, cy), add_img)
        add_lbl = _trunc_text(
            ldraw,
            " + ".join(_e_name(e) for e in add_entries),
            fn8, _TW - 2,
        )
        ldraw.text((ax2 + _TW // 2, cy + _TH + 2), add_lbl,
                   fill=(50, 50, 50), anchor="mt", font=fn8)

        dcol  = (0, 140, 0) if delta_val >= 0 else (180, 0, 0)
        d_str = (f"+{delta_val:,.1f}" if delta_val >= 0 else f"{delta_val:,.1f}") + " TH/s"
        ldraw.text((ax2 + _TW // 2, cy + _TH + 14), d_str,
                   fill=dcol, anchor="mt", font=fn9l)

        # Placement hint for cell-size-change swaps
        n_rem_l = len(rem_entries)
        n_add_l = len(add_entries)
        if n_rem_l != n_add_l:
            hint = "place PAIR" if n_add_l > n_rem_l else "place 2-CELL"
            ldraw.text((ax2 + _TW // 2, cy + _TH + 28), hint,
                       fill=(180, 50, 50), anchor="mt", font=fn8)

    # ── 4. Stack ──────────────────────────────────────────────────────────────
    out = Image.new("RGB", (base.width, base.height + leg_h), BACKGROUND)
    out.paste(base.convert("RGB"), (0, 0))
    out.paste(legend, (0, base.height))
    return out


def main() -> None:
    if not SWAPS_JSON.exists():
        print("optimizer_swaps.json not found. Run optimizer.py first.")
        return

    swaps = json.loads(SWAPS_JSON.read_text(encoding="utf-8"))
    if not swaps:
        print("No swaps to visualise.")
        return

    filter_room: int | None = None
    if len(sys.argv) > 1:
        try:
            filter_room = int(sys.argv[1])
        except ValueError:
            pass

    room_nums = sorted({s["room"] for s in swaps})
    OUTPUT_DIR.mkdir(exist_ok=True)

    for room_num in room_nums:
        if filter_room is not None and room_num != filter_room:
            continue

        placed_path = PLACED_DIR / f"placed_room{room_num}.json"
        if not placed_path.exists():
            print(f"[!] {placed_path} not found — skipping room {room_num}")
            continue

        data  = json.loads(placed_path.read_text(encoding="utf-8"))
        racks = data.get("racks", [])
        if not racks:
            print(f"[!] No racks in {placed_path.name}")
            continue

        room_swaps = [s for s in swaps if s["room"] == room_num]
        print(f"[room{room_num}] {len(room_swaps)} swap(s)")

        img = render_swap_image(room_num, racks, room_swaps)
        out = OUTPUT_DIR / f"swaps_room{room_num}.png"
        img.save(out, "PNG")
        print(f"  Saved → {out}  ({img.width}×{img.height} px)")


if __name__ == "__main__":
    main()
