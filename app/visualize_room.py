"""
visualize_room.py — Render placed miners as rack columns on a white canvas.

Reads placed_room*.json files produced by parse_room.py, loads the first
frame of each miner's GIF from ./miners/, overlays the rarity badge from
./rarity_indicators/, and arranges them as vertical rack columns side by side:
  • Each rack = one column (miners stacked top-to-bottom)
  • Racks placed left-to-right in DOM order

Usage:
  python visualize_room.py                     # renders all placed_room*.json
  python visualize_room.py placed_room1.json   # specific file only

Output:
  vis/room1.png, vis/room2.png, …  (directory created automatically)
"""

import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# scrape_miners lives in the same directory; import lazily to avoid heavy
# network imports at module level — only used when a GIF is missing.
def _lookup_miner(name: str):
    """Call scrape_miners.lookup_miner, importing it on first use."""
    try:
        from scrape_miners import lookup_miner          # noqa: PLC0415
        return lookup_miner(name)
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] Could not import scrape_miners: {exc}")
        return None

# ── Paths ────────────────────────────────────────────────────────────────────
_ROOT           = Path(__file__).parent.parent   # roomBuilder/
MINERS_DIR      = _ROOT / "miners"
RARITY_DIR      = _ROOT / "rarity_indicators"
PLACED_DIR      = _ROOT / "data"          # placed_room*.json
VIS_DIR         = _ROOT / "vis"           # output directory for rendered PNGs

# Layout
CELL_W          = 126                # native miner GIF width
CELL_H          = 100                # native miner GIF height
NAME_HEIGHT     = 12                 # pixels for the name label
STATS_HEIGHT    = 11                 # pixels for the power/bonus label
TOTAL_LABEL_H   = NAME_HEIGHT + STATS_HEIGHT  # total text area below image
BADGE_MARGIN    = 2                  # pixels from top-left corner
PADDING         = 6                  # gap between cells
BACKGROUND      = (255, 255, 255)    # white canvas
NAME_COLOR      = (30, 30, 30)       # dark grey text
STATS_COLOR     = (80, 80, 160)      # muted blue for power/bonus line


def _load_font(size: int = 9) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


FONT = _load_font(9)

# Rarity → badge filename (common has no badge)
RARITY_BADGE: dict[str, str] = {
    "uncommon":  "uncommon.png",
    "rare":      "rare.png",
    "epic":      "epic.png",
    "legendary": "legendary.png",
    "unreal":    "unreal.png",
    "set":       "set.png",
    "legacy":    "legacy.png",
}


# ── Power formatting ─────────────────────────────────────────────────────────

_UNITS = [(1_000_000.0, "EH"), (1_000.0, "PH"), (1.0, "TH"), (0.001, "GH"), (0.000_001, "MH")]


def format_power(th: float | None) -> str:
    """Format a TH/s value with the largest unit that keeps the number >= 1."""
    if th is None or th == 0.0:
        return "0 TH"
    for divisor, unit in _UNITS:
        if th >= divisor:
            v = th / divisor
            # Show up to 2 decimal places, strip trailing zeros
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{s} {unit}"
    # Smaller than MH: show in GH
    v = th / 0.001
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return f"{s} GH"


# ── Miners DB (lazy-loaded for stats overlay) ─────────────────────────────────

_MINERS_DB: dict[str, dict] | None = None


def _get_miners_db() -> dict[str, dict]:
    global _MINERS_DB
    if _MINERS_DB is not None:
        return _MINERS_DB
    _MINERS_DB = {}
    db_path = _ROOT / "miners/miners_data.json"
    if db_path.exists():
        import json as _json
        for entry in _json.loads(db_path.read_text(encoding="utf-8")):
            key = _norm_stem(entry["name"])
            _MINERS_DB[key] = entry
    return _MINERS_DB


def get_miner_stats(name: str, rarity: str | None) -> tuple[float | None, float | None]:
    """Return (power_th, bonus_pct) for a miner from miners_data.json."""
    db = _get_miners_db()
    rec = db.get(_norm_stem(name))
    if rec is None:
        return None, None
    rarities = rec.get("rarities", {})
    tier = rarities.get(rarity or "common") or rarities.get("common") or {}
    return tier.get("power_th"), tier.get("bonus_pct")


# ── Match-log (lazy-loaded for rejected-miner overlay) ───────────────────────

_MATCH_LOG: dict[str, dict] | None = None   # {slug: entry}


def _get_match_log() -> dict[str, dict]:
    """Return {slug: entry} for all entries in match_log.json."""
    global _MATCH_LOG
    if _MATCH_LOG is not None:
        return _MATCH_LOG
    _MATCH_LOG = {}
    log_path = _ROOT / "data/match_log.json"
    if log_path.exists():
        import json as _j
        try:
            for entry in _j.loads(log_path.read_text(encoding="utf-8")):
                _MATCH_LOG[entry["slug"]] = entry
        except Exception:
            pass
    return _MATCH_LOG


def is_rejected(slug: str) -> bool:
    """True if the miner was marked 'rejected' in the verification window."""
    return _get_match_log().get(slug, {}).get("status") == "rejected"


# ── Asset loading ─────────────────────────────────────────────────────────────

def _norm_stem(s: str) -> str:
    """Canonical stem: lowercase, apostrophes removed, hyphens/non-word → underscores."""
    s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
    return re.sub(r"[^\w]+", "_", s).strip("_")


# Lazy cache: normalised image stem → Path, built once from miners_data.json
_IMAGE_CACHE: dict[str, Path] | None = None


def _get_image_cache() -> dict[str, Path]:
    """Return a {norm_stem → Path} map built from miners_data.json image filenames."""
    global _IMAGE_CACHE
    if _IMAGE_CACHE is not None:
        return _IMAGE_CACHE
    _IMAGE_CACHE = {}
    db_path = _ROOT / "miners/miners_data.json"
    if db_path.exists():
        import json as _json
        for entry in _json.loads(db_path.read_text(encoding="utf-8")):
            img = entry.get("image", "")
            if img:
                stem = Path(img).stem
                _IMAGE_CACHE[_norm_stem(stem)] = MINERS_DIR / img
    # Also index every file actually on disk (handles files not in DB)
    for p in MINERS_DIR.iterdir():
        if p.suffix.lower() in (".gif", ".png", ".jpg", ".webp"):
            _IMAGE_CACHE.setdefault(_norm_stem(p.stem), p)
    return _IMAGE_CACHE


def load_first_frame(slug: str, name: str = "") -> Image.Image | None:
    """
    Load the first frame of a miner's GIF by slug (filename stem).
    Falls back to normalised stem matching, then to DB image field lookup,
    then to name-based lookup. Returns an RGBA image or None if not found.
    """
    def _open(p: Path) -> Image.Image:
        img = Image.open(p)
        img.seek(0)
        return img.convert("RGBA")

    # 1. Exact match (fast path)
    for ext in (".gif", ".png", ".jpg", ".webp"):
        p = MINERS_DIR / (slug + ext)
        if p.exists():
            return _open(p)

    # 2. Normalised stem match (handles hyphens, apostrophes, extra punct)
    cache = _get_image_cache()
    slug_norm = _norm_stem(slug)
    p = cache.get(slug_norm)
    if p and p.exists():
        return _open(p)

    # 3. Name-based fallback (e.g. 'declarator_407plus' → 'Declarator 407+')
    if name:
        name_norm = _norm_stem(name)
        p = cache.get(name_norm)
        if p and p.exists():
            return _open(p)

    return None


def load_badge(rarity: str | None) -> Image.Image | None:
    """Load the rarity badge PNG, or None for common / unknown."""
    if not rarity:
        return None
    fname = RARITY_BADGE.get(rarity.lower())
    if not fname:
        return None
    p = RARITY_DIR / fname
    if not p.exists():
        return None
    return Image.open(p).convert("RGBA")


# ── Missing-GIF fetcher ───────────────────────────────────────────────────────

def fetch_missing_gifs(racks: list[list[dict]]) -> None:
    """
    Before rendering, check every miner for a local GIF.
    If one is missing, call lookup_miner(name) which scrapes minaryganar.com
    and downloads the image into ./miners/.
    """
    seen: set[str] = set()   # avoid duplicate lookups for same slug
    for rack in racks:
        for miner in rack:
            slug = miner["slug"]
            name = miner.get("name", "")
            if slug in seen:
                continue
            seen.add(slug)
            if load_first_frame(slug, name) is None:
                print(f"  [!] Missing GIF for '{name}' — fetching from minaryganar.com…")
                _lookup_miner(name)
                global _IMAGE_CACHE
                _IMAGE_CACHE = None  # invalidate cache after download
                if load_first_frame(slug, name) is not None:
                    print(f"      Downloaded successfully.")
                else:
                    print(f"      Still not found — will render as placeholder.")


# ── Canvas rendering ──────────────────────────────────────────────────────────

def render(racks: list[list[dict]]) -> Image.Image:
    """Render each rack as a vertical column; racks placed side by side."""
    total_cell_h = CELL_H + TOTAL_LABEL_H
    num_racks  = len(racks)
    max_height = max((len(r) for r in racks), default=0)

    canvas_w = num_racks  * (CELL_W + PADDING) + PADDING
    canvas_h = max_height * (total_cell_h + PADDING) + PADDING
    canvas = Image.new("RGBA", (canvas_w, canvas_h), BACKGROUND + (255,))
    draw = ImageDraw.Draw(canvas)

    for rack_idx, rack in enumerate(racks):
        x = PADDING + rack_idx * (CELL_W + PADDING)

        for slot_idx, miner in enumerate(rack):
            y = PADDING + slot_idx * (total_cell_h + PADDING)

            # ── Miner image ───────────────────────────────────────────────
            _rejected = is_rejected(miner["slug"])
            if _rejected:
                frame = Image.new("RGBA", (CELL_W, CELL_H), (200, 200, 200, 220))
            else:
                frame = load_first_frame(miner["slug"], miner.get("name", ""))
                if frame is None:
                    frame = Image.new("RGBA", (CELL_W, CELL_H), (200, 200, 200, 200))
                else:
                    frame.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
                    if frame.size != (CELL_W, CELL_H):
                        padded = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
                        ox = (CELL_W - frame.width)  // 2
                        oy = (CELL_H - frame.height) // 2
                        padded.paste(frame, (ox, oy), frame)
                        frame = padded

            if _rejected:
                tint = Image.new("RGBA", (CELL_W, CELL_H), (220, 50, 50, 100))
                frame = Image.alpha_composite(frame, tint)

            canvas.paste(frame, (x, y), frame)

            # ── Rarity badge (top-left corner) ────────────────────────────
            badge = load_badge(miner.get("rarity"))
            if badge:
                canvas.paste(badge, (x + BADGE_MARGIN, y + BADGE_MARGIN), badge)

            # ── Miner name ───────────────────────────────────────────────
            name = miner.get("name", miner["slug"])
            # Truncate with ellipsis to fit within the cell width
            while draw.textlength(name, font=FONT) > CELL_W - 2 and len(name) > 1:
                name = name[:-1]
            if name != miner.get("name", miner["slug"]):
                name = name.rstrip() + "…"
            draw.text(
                (x + CELL_W // 2, y + CELL_H + 1),
                name,
                fill=NAME_COLOR,
                anchor="mt",
                font=FONT,
            )

            # ── Power + bonus stats line ──────────────────────────────────
            if _rejected:
                _ml_entry = _get_match_log().get(miner["slug"], {})
                power_th  = _ml_entry.get("manual_power_th")
                bonus_pct = _ml_entry.get("manual_bonus_pct")
            elif miner.get("rarity") == "legacy":
                _ml_entry = _get_match_log().get(miner["slug"], {})
                power_th  = _ml_entry.get("manual_power_th")
                bonus_pct = _ml_entry.get("manual_bonus_pct")
                if power_th is None and bonus_pct is None:
                    power_th, bonus_pct = get_miner_stats(
                        miner.get("name", ""), "legacy"
                    )
            else:
                power_th, bonus_pct = get_miner_stats(
                    miner.get("name", ""), miner.get("rarity")
                )
            if power_th is not None or bonus_pct is not None:
                pwr_str = format_power(power_th) if power_th else "?"
                if bonus_pct is not None:
                    bon_str = f"+{bonus_pct:.2f}%"
                    if "." in bon_str:
                        bon_str = bon_str.rstrip("0").rstrip(".")
                    if not bon_str.endswith("%"):
                        bon_str += "%"
                else:
                    bon_str = "?%"
                stats_text = f"{pwr_str}  {bon_str}"
                font_stats = _load_font(8)
                while draw.textlength(stats_text, font=font_stats) > CELL_W - 2 and len(stats_text) > 3:
                    stats_text = stats_text[:-1]
                draw.text(
                    (x + CELL_W // 2, y + CELL_H + NAME_HEIGHT + 1),
                    stats_text,
                    fill=STATS_COLOR,
                    anchor="mt",
                    font=font_stats,
                )

    return canvas


# ── Entry point ───────────────────────────────────────────────────────────────

def find_all_placed() -> list[Path]:
    return sorted(PLACED_DIR.glob("placed_room*.json"))


def render_one(placed_path: Path) -> None:
    """Load one placed_room*.json, auto-fetch missing GIFs, render and save."""
    data = json.loads(placed_path.read_text(encoding="utf-8"))
    racks = data.get("racks", [])
    if not racks:
        print(f"  No racks found in {placed_path.name}, skipping.")
        return

    total = sum(len(r) for r in racks)
    # Derive "room1" from "placed_room1"
    room_label = placed_path.stem.replace("placed_", "")   # e.g. "room1"
    print(f"[{room_label}] {len(racks)} racks, {total} miners")

    fetch_missing_gifs(racks)

    canvas = render(racks)
    VIS_DIR.mkdir(exist_ok=True)
    out_path = VIS_DIR / f"{room_label}.png"
    canvas.convert("RGB").save(out_path, "PNG")
    print(f"  Saved -> {out_path}  ({canvas.width}x{canvas.height} px)")


def main() -> None:
    if len(sys.argv) > 1:
        paths = [Path(arg) for arg in sys.argv[1:]]
    else:
        paths = find_all_placed()

    if not paths:
        print("No placed_room*.json found. Run parse_room.py first.")
        return

    for p in paths:
        if not p.exists():
            print(f"[!] File not found: {p}")
            continue
        render_one(p)


if __name__ == "__main__":
    main()
