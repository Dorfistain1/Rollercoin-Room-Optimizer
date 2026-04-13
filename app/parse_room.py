"""
parse_room.py — Parse a saved RollerCoin game page for both:
  1. PLACED miners  — miners currently on racks in the room
  2. INVENTORY miners — miners visible in the inventory modal (all owned)

How to save the page correctly:
  • Open the RollerCoin game in Chrome
  • For placed miners: just be on the /game page with the room visible
  • Open DevTools console and run:  copy(document.documentElement.outerHTML)
  • Paste into a  .html  file and drop it into  html_pages/

The Ctrl+S method captures pre-JS HTML and will NOT contain placed miner data.
The console copy captures the live rendered DOM and WILL contain it.

Usage:
  python parse_room.py                       # parse all .html in html_pages/
  python parse_room.py path/to/file.html     # parse a specific file

Output:
  Prints a summary and writes:
    placed_<stem>.json    — miners on racks
    inventory_<stem>.json — miners in inventory modal (if modal was open)

Placed miner HTML structure (div.miners-block-wrapper per rack):
  div.miner-img-wrapper  style="top:Npx; left:Npx"
    img.miner-item  src=".../<slug>.gif"   ← slug → miner name
    div.miners-badges.size-N               ← N = rack slot size (1 or 2)
      img  alt="<level>"                   ← upgrade level (0–5), no img = level 0

Inventory modal HTML structure (div.item-card-wrapper per owned card):
  div.item-badges.miner                    ← marks this card as a miner
  div.item-card-info
    p.item-card-name                       ← miner name
    span.item-card-power                   ← e.g. "1.054  Ph/s"
    span.item-card-bonus                   ← e.g. "1%"

Power units and conversion to Th/s:
  Gh/s  × 0.001

Power units and conversion to Th/s:
  Gh/s  × 0.001
  Th/s  × 1
  Ph/s  × 1 000
  Eh/s  × 1 000 000

Note: the 4 game rooms all render on a Phaser canvas, so room layout cannot
be read from HTML. The inventory modal gives per-miner totals across all rooms.
"""

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

# Ensure UTF-8 output on Windows (miner names may contain non-ASCII characters)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent   # roomBuilder/

# ── Paths ───────────────────────────────────────────────────────────────────
MINERS_DATA = ROOT / "miners/miners_data.json"
HTML_DIR    = ROOT / "html_page"
OUT_DIR     = ROOT / "data"
RARITIES    = ["common", "uncommon", "rare", "epic", "legendary", "unreal"]

# Power unit → multiplier to convert to Th/s
UNIT_TO_TH: dict[str, float] = {
    "gh/s": 0.001,
    "th/s": 1.0,
    "ph/s": 1_000.0,
    "eh/s": 1_000_000.0,
}


# ── Power parsing ────────────────────────────────────────────────────────────

def parse_power_to_th(raw: str) -> float | None:
    """
    Parse a power string like "1.054  Ph/s" or "104.000  Th/s" and return
    the value converted to Th/s. Returns None if the string is unrecognisable.
    Uses only local string operations — never opens any URL.
    """
    raw = raw.strip()
    m = re.match(r"([\d.,]+)\s*([A-Za-z/]+)", raw)
    if not m:
        return None
    value_str = m.group(1).replace(",", "")
    unit = m.group(2).lower()
    try:
        value = float(value_str)
    except ValueError:
        return None
    multiplier = UNIT_TO_TH.get(unit)
    if multiplier is None:
        return None
    return round(value * multiplier, 6)


def parse_bonus(raw: str) -> float | None:
    """Parse "1%" or "0.5%" → float. Returns None on failure."""
    m = re.search(r"([\d.,]+)", raw)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ── Slug → name matching ─────────────────────────────────────────────────────

def _norm_slug(s: str) -> str:
    """
    Canonical slug key: lowercase, strip apostrophes/quotes,
    treat hyphens as underscores.
    e.g. "Valhalla's Vault" → "valhallaس vault" → ... → "valhallas_vault"
         "Nano-Node Extractor" → "nanonode_extractor"
    """
    s = s.lower()
    s = s.replace("'", "").replace("\u2019", "")   # apostrophe variants
    s = s.replace("-", "_")
    s = re.sub(r"[^\w]+", "_", s).strip("_")       # collapse non-word to _
    return s


def build_slug_index(miners_index: dict[str, dict]) -> dict[str, dict]:
    """
    Build a slug → miner record map where slug is the GIF filename stem
    normalised to lowercase with underscores/hyphens replaced as spaces.
    Also keyed by the image filename stem from miners_data (e.g. "TRX_Bull").
    Multiple normalisation keys are added so apostrophes/hyphens in DB names
    don't break matching against game slugs.
    """
    slug_index: dict[str, dict] = {}
    for record in miners_index.values():
        name = record["name"]
        # Standard keys
        slug_index[name.lower().replace(" ", "_")] = record
        slug_index[name.lower().replace(" ", "-")] = record
        # Fully-normalised key (strips apostrophes, collapses hyphens/spaces to _)
        slug_index[_norm_slug(name)] = record
        # Key by image stem (e.g. "TRX_Bull" → "trx_bull")
        img_stem = Path(record.get("image", "")).stem.lower()
        if img_stem:
            slug_index[img_stem] = record
            slug_index[_norm_slug(img_stem)] = record
    return slug_index


def slug_to_name(slug: str, slug_index: dict[str, dict]) -> tuple[str, dict | None]:
    """
    Convert a GIF filename stem like 'quantum_conductor' to a display name
    and its miners_data record. Falls back to capitalised slug if no match.
    """
    key = slug.lower()
    record = slug_index.get(key) or slug_index.get(_norm_slug(key))
    if record:
        return record["name"], record
    # Human-readable fallback: replace underscores, title-case each word
    display = " ".join(w.capitalize() for w in slug.replace("-", "_").split("_"))
    return display, None


# ── Placed miner parsing ─────────────────────────────────────────────────────

def parse_placed_miners(soup: BeautifulSoup, slug_index: dict[str, dict]) -> list[list[dict]]:
    """
    Find all miners on racks in the live-rendered game room.

    Returns a list of racks, each rack being a list of miner dicts in DOM order.
    Rarity comes from the badge img alt attribute:
      no img → common (index 0)
      alt="1" → uncommon, alt="2" → rare, alt="3" → epic,
      alt="4" → legendary, alt="5" → unreal
    """
    racks: list[list[dict]] = []

    for rack in soup.find_all("div", class_=lambda c: c and "miners-block-wrapper" in c):
        rack_miners: list[dict] = []
        for slot in rack.find_all("div", class_=lambda c: c and "miner-img-wrapper" in c):
            img = slot.find("img", class_=lambda c: c and "miner-item" in c)
            if not img:
                continue

            # Extract slug from src, e.g. ".../quantum_conductor.gif?v=…"
            src = img.get("src", "")
            slug = Path(src.split("?")[0]).stem

            name, record = slug_to_name(slug, slug_index)

            # Rarity from badge img alt (absent = index 0 = common)
            badge_div = slot.find("div", class_=lambda c: c and "miners-badges" in c)
            rarity = RARITIES[0]  # default: common
            if badge_div:
                lvl_img = badge_div.find("img")
                if lvl_img:
                    try:
                        idx = int(lvl_img.get("alt", "0"))
                        rarity = RARITIES[idx] if 0 <= idx < len(RARITIES) else RARITIES[0]
                    except ValueError:
                        pass

            # Slot size from size-N class on the badges div
            slot_size = 1
            if badge_div:
                for cls in badge_div.get("class", []):
                    if cls.startswith("size-"):
                        try:
                            slot_size = int(cls.split("-")[1])
                        except ValueError:
                            pass

            rack_miners.append({
                "name": name,
                "slug": slug,
                "rarity": rarity,
                "slot_size": slot_size,
                "_record": record,
            })

        if rack_miners:
            racks.append(list(reversed(rack_miners)))

    return racks


# ── Rarity matching from miners_data ─────────────────────────────────────────

def guess_rarity(power_th: float, miner_record: dict) -> str | None:
    if not miner_record:
        return None
    best_rarity = None
    best_diff = float("inf")
    for rarity in RARITIES:
        tier = miner_record.get("rarities", {}).get(rarity, {})
        ref = tier.get("power_th")
        if ref is None:
            continue
        diff = abs(ref - power_th)
        if diff < best_diff:
            best_diff = diff
            best_rarity = rarity
    return best_rarity


# ── Inventory parsing (disabled — kept for future use) ───────────────────────

def load_miners_index() -> dict[str, dict]:
    if not MINERS_DATA.exists():
        return {}
    with open(MINERS_DATA, encoding="utf-8") as f:
        data = json.load(f)
    return {m["name"].lower(): m for m in data}


def parse_inventory(
    soup: BeautifulSoup,
    miners_index: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """
    Parse the inventory modal and return per-(name, rarity) entry dicts:
      {composite_key: {"name", "rarity", "count", "power_th", "bonus_pct"}}
    where composite_key = f"{name}\\x1f{rarity}".

    Rarity is read from the badge img alt attribute (same convention as placed
    miners: alt="0"→common, "1"→uncommon, "2"→rare, "3"→epic, …).
    If no badge is found and miners_index is provided, rarity is inferred via
    guess_rarity(power_th, record); otherwise defaults to "common".

    Different rarities of the same miner become separate entries so the
    optimizer can track them independently.
    """
    results: dict[str, dict] = {}
    for card in soup.find_all("div", class_=lambda c: c and "item-card-wrapper" in c):
        badge = card.find("div", class_=lambda c: c and "item-badges" in c)
        if badge is None:
            continue
        if "miner" not in badge.get("class", []):
            continue
        name_el = card.find("p", class_=lambda c: c and "item-card-name" in c)
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue

        # Parse actual power and bonus from the card (at the miner's real rarity)
        power_th: float | None = None
        bonus_pct: float | None = None
        power_el = card.find("span", class_=lambda c: c and "item-card-power" in c)
        if power_el:
            power_th = parse_power_to_th(power_el.get_text(strip=True))
        bonus_el = card.find("span", class_=lambda c: c and "item-card-bonus" in c)
        if bonus_el:
            raw_bonus = bonus_el.get_text(strip=True)
            m = re.search(r"([\d.,]+)\s*%", raw_bonus)
            if m:
                bonus_pct = float(m.group(1).replace(",", ""))

        # Try to read rarity from badge img alt (same convention as placed miners)
        rarity = "common"
        lvl_img = badge.find("img")
        if lvl_img:
            try:
                idx = int(lvl_img.get("alt", "0"))
                rarity = RARITIES[idx] if 0 <= idx < len(RARITIES) else "common"
            except (ValueError, TypeError):
                rarity = "common"
        # Fallback: infer rarity from power_th if miners_index is available
        if rarity == "common" and power_th and miners_index:
            rec = miners_index.get(name.lower())
            if rec:
                guessed = guess_rarity(power_th, rec)
                if guessed:
                    rarity = guessed

        key = f"{name}\x1f{rarity}"
        if key not in results:
            results[key] = {
                "name":      name,
                "rarity":    rarity,
                "count":     0,
                "power_th":  power_th,
                "bonus_pct": bonus_pct,
            }
        else:
            if results[key]["power_th"] is None and power_th is not None:
                results[key]["power_th"] = power_th
            if results[key]["bonus_pct"] is None and bonus_pct is not None:
                results[key]["bonus_pct"] = bonus_pct
        results[key]["count"] += 1
    return results


# ── Output helpers ────────────────────────────────────────────────────────────

def build_placed_output(racks: list[list[dict]], source_file: str) -> dict:
    total = sum(len(r) for r in racks)
    clean_racks = [
        [{k: v for k, v in m.items() if k != "_record"} for m in rack]
        for rack in racks
    ]
    return {
        "source_file": source_file,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "total_placed": total,
        "racks": clean_racks,
    }


def print_placed_summary(racks: list[list[dict]]) -> None:
    total = sum(len(r) for r in racks)
    print(f"  Racks: {len(racks)}  |  Total placed miners: {total}")
    for rack_idx, rack in enumerate(racks):
        print(f"  Rack {rack_idx} ({len(rack)} miners):")
        for m in rack:
            print(f"    {m['name']:<36}  {m['rarity']:<12}  size-{m['slot_size']}")
    print()


# ── File parsing ──────────────────────────────────────────────────────────────

def parse_file(path: Path, miners_index: dict[str, dict], room_num: int) -> dict | None:
    print(f"[Room {room_num}] {path.name}")
    html = path.read_bytes()
    soup = BeautifulSoup(html, "lxml")

    slug_index = build_slug_index(miners_index)

    racks = parse_placed_miners(soup, slug_index)
    if not racks:
        print("  No placed miners found.")
        print("  Use: copy(document.documentElement.outerHTML) in DevTools console,")
        print("  paste into a .html file and drop it in html_page/\n")
        return None

    output = build_placed_output(racks, path.name)
    print_placed_summary(racks)
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"placed_room{room_num}.json"
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved -> {out}\n")
    return output


# ── Cleanup ──────────────────────────────────────────────────────────────────────

def cleanup_html_dir(html_dir: Path) -> None:
    """
    Remove everything in *html_dir* that is not a .html file.
    Browsers save a companion '<name>_files/' folder full of images, CSS, and
    JS alongside the HTML — none of that is needed for parsing.
    The .html files themselves are kept.
    """
    if not html_dir.exists():
        return

    removed_dirs = 0
    removed_files = 0
    freed_bytes = 0

    for item in html_dir.iterdir():
        if item.is_dir():
            size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            shutil.rmtree(item)
            freed_bytes += size
            removed_dirs += 1
        elif item.suffix.lower() != ".html":
            freed_bytes += item.stat().st_size
            item.unlink()
            removed_files += 1

    if removed_dirs or removed_files:
        freed_mb = freed_bytes / 1_048_576
        parts = []
        if removed_dirs:
            parts.append(f"{removed_dirs} folder{'s' if removed_dirs > 1 else ''}")
        if removed_files:
            parts.append(f"{removed_files} file{'s' if removed_files > 1 else ''}")
        print(f"[Cleanup] Removed {', '.join(parts)} from {html_dir}/ ({freed_mb:.1f} MB freed)")
    else:
        print(f"[Cleanup] Nothing to remove in {html_dir}/")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    miners_index = load_miners_index()
    if miners_index:
        print(f"Loaded {len(miners_index)} miners from {MINERS_DATA}\n")
    else:
        print(f"[!] {MINERS_DATA} not found — rarity matching disabled.\n")

    if len(sys.argv) > 1:
        paths = [Path(sys.argv[1])]
    else:
        paths = sorted(HTML_DIR.glob("*.html"), reverse=True)
        if not paths:
            print(f"No .html files found in {HTML_DIR}/")
            print("Run: copy(document.documentElement.outerHTML) in DevTools, paste to a .html file.")
            return

    for room_num, path in enumerate(paths, start=1):
        parse_file(path, miners_index, room_num)

    cleanup_html_dir(HTML_DIR)


if __name__ == "__main__":
    main()

