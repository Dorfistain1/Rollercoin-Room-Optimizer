"""
main.py — Parse rooms and inventory, download only your miners, visualize.

Drop your RollerCoin HTML pages into html_page/ then run:
  python main.py

How HTML pages are classified:
  • Each unique set of placed miners = one room.
  • If a page's placed miners exactly match a room already seen, it is treated
    as an inventory capture of that room (opens the inventory modal, copies HTML).
  • Limitation: two genuinely different rooms with identical miner layouts would
    be misclassified. This is extremely unlikely in practice.

Flow:
  1. Classify every .html in html_page/ as a room page or inventory page.
  2. Parse room pages -> placed_room*.json
  3. Download any missing miner images / data from minaryganar.com.
  4. Re-parse rooms with fresh miners_data for accurate display names.
  5. Merge all inventory pages -> count per miner name.
  6. Download any inventory miners not yet in miners/ .
  7. Sort merged inventory by power (highest first) -> inventory.json.
  8. Render each room -> vis/room1.png, vis/room2.png, ...
  9. Clean up html_page/ companion _files/ folders.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

# Ensure UTF-8 on Windows (miner names can contain non-ASCII characters)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── Import helpers from sibling modules ──────────────────────────────────────
from parse_room import (
    HTML_DIR,
    OUT_DIR,
    build_slug_index,
    build_placed_output,
    cleanup_html_dir,
    load_miners_index,
    parse_inventory,
    parse_placed_miners,
    print_placed_summary,
)
from scrape_miners import lookup_miner, lookup_miner_by_slug, MATCH_LOG as _MATCH_LOG_PATH
from visualize_room import VIS_DIR, load_first_frame, render_one, find_all_placed


# ── Download progress helper ──────────────────────────────────────────────────

def _run_downloads(label: str, tasks: list, fetch_fn, desc_fn=None) -> None:
    """
    Run fetch_fn(task) for each task with a live two-line display:
      Line 1: progress bar with % complete and ETA
      Line 2: last status line from the scraper (updates in place)

    fetch_fn receives the raw task item.
    desc_fn(task) -> str  optional label shown on line 2 before each fetch.
    """
    from tqdm import tqdm as _tqdm

    _orig = sys.stdout  # hold direct reference before any redirect

    prog = _tqdm(
        total=len(tasks),
        desc=label,
        unit="miner",
        position=0,
        leave=True,
        file=_orig,
        dynamic_ncols=True,
        bar_format="{desc}: {percentage:3.0f}%|█{bar}█| {n}/{total} [{elapsed}<{remaining}]",
    )
    stat = _tqdm(
        total=0,
        bar_format="  {desc}",
        position=1,
        leave=False,
        file=_orig,
    )

    class _Redirect:
        """Intercepts scraper print() calls; feeds last line to stat bar."""
        _buf = ""

        def write(self, s):
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                clean = line.strip()
                if clean:
                    stat.set_description_str(clean[:120], refresh=True)
            return len(s)

        def flush(self):
            pass

    rd = _Redirect()
    for task in tasks:
        if desc_fn:
            stat.set_description_str(desc_fn(task), refresh=True)
        sys.stdout = rd
        try:
            fetch_fn(task)
        finally:
            sys.stdout = _orig
        prog.update(1)

    stat.close()
    prog.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def room_fingerprint(racks: list[list[dict]]) -> frozenset:
    """Canonical slug set — used to detect duplicate room saves."""
    return frozenset(m["slug"] for rack in racks for m in rack)


def parse_one(path: Path, miners_index: dict, room_num: int) -> list[list[dict]]:
    """Parse a single HTML file and save placed_room<N>.json."""
    slug_index = build_slug_index(miners_index)
    soup = BeautifulSoup(path.read_bytes(), "lxml")
    racks = parse_placed_miners(soup, slug_index)
    if not racks:
        return []
    output = build_placed_output(racks, path.name)
    out = OUT_DIR / f"placed_room{room_num}.json"
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    return racks


def collect_slugs(racks: list[list[dict]]) -> dict[str, str]:
    """Return {slug: display_name} for every miner in racks."""
    seen: dict[str, str] = {}
    for rack in racks:
        for miner in rack:
            if miner["slug"] not in seen:
                seen[miner["slug"]] = miner["name"]
    return seen


def _is_sorted_inventory(soup) -> bool:
    """
    Return True if the page contains an inventory modal whose entries appear
    to be sorted high→low by power or bonus.

    The game sorts inventory by power (default) or bonus before the user saves.
    A strongly-descending sequence (≤10% inversions) is a reliable signal.
    At least 3 entries with values are required to avoid false positives.
    """
    batch = parse_inventory(soup)
    if not batch:
        return False

    entries = list(batch.values())

    def _mostly_descending(vals: list[float]) -> bool:
        if len(vals) < 3:
            return False
        inv = sum(1 for i in range(len(vals) - 1) if vals[i] < vals[i + 1])
        return inv / (len(vals) - 1) <= 0.10

    powers = [e["power_th"] for e in entries if e.get("power_th") is not None]
    if _mostly_descending(powers):
        return True

    bonuses = [e["bonus_pct"] for e in entries if e.get("bonus_pct") is not None]
    if _mostly_descending(bonuses):
        return True

    return False


def _is_forced_inventory(path: Path) -> bool:
    """Return True if the filename stem signals an inventory capture.

    Files named  power, power1, power2, … or bonus, bonus1, bonus2, …
    (case-insensitive) are always treated as inventory pages regardless of
    their placed-miner content.  This lets the user handle the edge case
    of two rooms with identical miner layouts.
    """
    return bool(re.fullmatch(r"(power|bonus)\d*", path.stem, re.IGNORECASE))


def classify_pages(paths: list[Path], miners_index: dict) -> tuple[
    list[tuple[int, Path]],   # [(room_num, path), ...]
    list[Path],               # inventory pages
]:
    """
    Split HTML files into room pages and inventory pages.

    Classification order (first match wins):
      1. Filename stem matches power / power1 / bonus / bonus2 / …  → inventory
      2. Placed-miner fingerprint matches a previously seen room      → check sort
         2a. If the new file has a sorted inventory (and the old one doesn't)
             → new file is inventory  (expected case)
         2b. If the old file has a sorted inventory (and the new one doesn't)
             → old file was misclassified; swap: old → inventory, new → room
         2c. If neither / both are sorted → keep first-seen as room
      3. Otherwise                                                    → new room
    """
    slug_index = build_slug_index(miners_index)
    seen: dict[frozenset, tuple[int, Path]] = {}   # fingerprint -> (room_num, path)
    room_pages: list[tuple[int, Path]] = []
    inv_pages: list[Path] = []
    room_num = 1

    for path in paths:
        if _is_forced_inventory(path):
            print(f"  [Inventory] {path.name}  (filename convention)")
            inv_pages.append(path)
            continue

        soup = BeautifulSoup(path.read_bytes(), "lxml")
        racks = parse_placed_miners(soup, slug_index)

        if not racks:
            print(f"  [!] {path.name}: no placed miners found, skipping.")
            continue

        fp = room_fingerprint(racks)
        if fp in seen:
            prev_room_num, prev_path = seen[fp]

            # Determine which file is the inventory by checking sort order.
            cur_sorted  = _is_sorted_inventory(soup)
            prev_sorted = _is_sorted_inventory(
                BeautifulSoup(prev_path.read_bytes(), "lxml")
            )

            if prev_sorted and not cur_sorted:
                # The file we already called a room is actually the inventory — swap.
                print(
                    f"  [!] Reclassifying: '{prev_path.name}' is inventory "
                    f"(sorted by power/bonus), '{path.name}' is Room {prev_room_num}"
                )
                room_pages = [(rn, rp) for rn, rp in room_pages if rp != prev_path]
                inv_pages.append(prev_path)
                room_pages.append((prev_room_num, path))
                seen[fp] = (prev_room_num, path)
            else:
                # Current file is inventory (or we can't tell — keep first-seen as room)
                reason = "sorted inventory" if cur_sorted else f"matches Room {prev_room_num}"
                print(f"  [Inventory] {path.name}  ({reason})")
                inv_pages.append(path)
        else:
            seen[fp] = (room_num, path)
            room_pages.append((room_num, path))
            print(f"  [Room {room_num}]    {path.name}")
            room_num += 1

    room_pages.sort(key=lambda x: x[0])   # restore sequential order after any swaps
    return room_pages, inv_pages


def build_inventory_output(
    merged: dict[str, dict],
    source_files: list[str],
) -> dict:
    """Build the inventory dict sorted by actual power_th (highest first)."""
    entries = list(merged.values())   # values already contain 'name', 'rarity', etc.

    # Sort: known power descending, unknowns last
    entries.sort(
        key=lambda e: (e.get("power_th") is not None, e.get("power_th") or 0),
        reverse=True,
    )

    return {
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "source_files": source_files,
        "total_miners": sum(e["count"] for e in entries),
        "unique_miners": len(entries),
        "miners": entries,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Ensure required directories exist (fresh installs from zip won't have them)
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    VIS_DIR.mkdir(parents=True, exist_ok=True)
    (HTML_DIR.parent / "miners").mkdir(parents=True, exist_ok=True)

    all_paths = sorted(HTML_DIR.glob("*.html"), reverse=True)
    if not all_paths:
        print(f"No .html files found in {HTML_DIR}/")
        print("Drop RollerCoin HTML pages there and re-run.")
        return

    print(f"Found {len(all_paths)} HTML file(s).\n")

    # ── Phase 1: classify pages ───────────────────────────────────────────
    print("=== Phase 1: classifying pages ===")
    miners_index = load_miners_index()
    room_pages, inv_pages = classify_pages(all_paths, miners_index)
    print(f"\n  {len(room_pages)} room(s), {len(inv_pages)} inventory page(s)\n")

    if not room_pages:
        print("No room pages found — nothing to do.")
        return

    # ── Phase 2: parse rooms (first pass) ────────────────────────────────
    print("=== Phase 2: parsing rooms ===")
    all_slugs: dict[str, str] = {}
    for room_num, path in room_pages:
        print(f"[Room {room_num}] {path.name}")
        racks = parse_one(path, miners_index, room_num)
        all_slugs.update(collect_slugs(racks))

    # ── Phase 3: download missing room miners ─────────────────────────────
    missing_room = [
        (slug, name)
        for slug, name in sorted(all_slugs.items())
        if load_first_frame(slug) is None
    ]
    if missing_room:
        print(f"\n=== Phase 3: downloading {len(missing_room)} missing room miner(s) ===")
        _run_downloads(
            label="Downloading",
            tasks=missing_room,
            fetch_fn=lambda t: lookup_miner_by_slug(t[0], html_name=t[1]),
            desc_fn=lambda t: t[1],
        )
        print()  # blank line after bars clear
    else:
        print("\n=== Phase 3: all room miner images present ===")

    # ── Phase 4: re-parse rooms with fresh miners_data ───────────────────
    print("\n=== Phase 4: re-parsing rooms ===")
    miners_index = load_miners_index()
    for room_num, path in room_pages:
        print(f"[Room {room_num}]")
        racks = parse_one(path, miners_index, room_num)
        if racks:
            print_placed_summary(racks)

    # ── Phase 5: merge inventory pages ───────────────────────────────────
    if inv_pages:
        print("=== Phase 5: merging inventory pages ===")
        merged: dict[str, dict] = {}
        for path in inv_pages:
            soup = BeautifulSoup(path.read_bytes(), "lxml")
            batch = parse_inventory(soup, miners_index)
            if batch:
                total_cards = sum(v["count"] for v in batch.values())
                print(f"  {path.name}: {total_cards} cards ({len(batch)} unique)")
                # Use max() for count — every save shows full stock, so
                # taking the max avoids double-counting across multiple saves.
                for key, data in batch.items():   # key = "name\x1frarity"
                    if key not in merged:
                        merged[key] = dict(data)
                    else:
                        merged[key]["count"] = max(merged[key]["count"], data["count"])
                        if merged[key]["power_th"] is None and data["power_th"] is not None:
                            merged[key]["power_th"] = data["power_th"]
                        if merged[key]["bonus_pct"] is None and data["bonus_pct"] is not None:
                            merged[key]["bonus_pct"] = data["bonus_pct"]
            else:
                print(f"  {path.name}: no inventory modal found")
        print(f"  Merged total: {sum(v['count'] for v in merged.values())} cards, {len(merged)} unique miners\n")

        # ── Phase 6: download inventory-only miners ───────────────────────
        # Normalize apostrophe variants and Cyrillic look-alike letters so names
        # like "Battle of the Сurrents" (Cyrillic С = U+0421) match the DB entry
        # that was stored with a Latin C.
        _CONFUSABLES = str.maketrans({
            '\u0410': 'A', '\u0412': 'B', '\u0421': 'C', '\u0415': 'E',
            '\u041A': 'K', '\u041C': 'M', '\u041D': 'H', '\u041E': 'O',
            '\u0420': 'P', '\u0422': 'T', '\u0425': 'X',
            '\u0430': 'a', '\u0435': 'e', '\u043E': 'o',
            '\u0440': 'p', '\u0441': 'c', '\u0445': 'x',
        })
        def _ninv(s: str) -> str:
            return s.translate(_CONFUSABLES).lower().replace("'", "").replace("\u2019", "")
        _mi_norm = {_ninv(n) for n in miners_index}
        inv_names_to_fetch = [
            data["name"] for data in merged.values()
            if _ninv(data["name"]) not in _mi_norm
        ]
        if inv_names_to_fetch:
            print(f"=== Phase 6: downloading {len(inv_names_to_fetch)} inventory-only miner(s) ===")
            _run_downloads(
                label="Downloading",
                tasks=sorted(inv_names_to_fetch),
                fetch_fn=lambda name: lookup_miner(name, expected_name=name),
                desc_fn=lambda name: name,
            )
            print()  # blank line after bars clear
        else:
            print("=== Phase 6: all inventory miners already downloaded ===")

        # ── Phase 7: save inventory.json sorted by power ─────────────────
        print("\n=== Phase 7: saving inventory.json ===")
        inv_output = build_inventory_output(
            merged,
            [p.name for p in inv_pages],
        )
        inv_path = OUT_DIR / "inventory.json"
        inv_path.write_text(json.dumps(inv_output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Saved -> {inv_path}")
        print(f"  {inv_output['total_miners']} total cards, {inv_output['unique_miners']} unique miners\n")
    else:
        print("=== Phase 5–7: no inventory pages found, skipping ===\n")

    # ── Phase 8: render rooms ─────────────────────────────────────────────
    print("=== Phase 8: rendering rooms ===")
    VIS_DIR.mkdir(exist_ok=True)
    for placed_path in find_all_placed():
        render_one(placed_path)

    # ── Phase 9: verify miner matches ────────────────────────────────────
    import verify_matches as _vm
    _vm._collect_legacy_miners()        # queue any legacy-rarity miners for manual entry
    _vm._collect_missing_data_miners()  # queue miners with missing/zero DB data
    _log_path = Path(_MATCH_LOG_PATH)
    if _log_path.exists():
        import json as _json
        _log_entries = _json.loads(_log_path.read_text(encoding="utf-8"))
        if any(e.get("status") in ("pending", "legacy", "missing_data") for e in _log_entries):
            print("\n=== Phase 9: verifying miner name matches & legacy data ===")
            _vm.main()
        else:
            print("\n=== Phase 9: no pending match verifications ===")
    else:
        print("\n=== Phase 9: no pending match verifications ===")

    # ── Phase 10: select locked miners ───────────────────────────────────
    print("\n=== Phase 10: select locked miners ===")
    import select_locked as _sl
    _sl.main()

    # ── Phase 10.5: define miner set groups ──────────────────────────────
    print("\n=== Phase 10.5: define miner set groups ===")
    import select_sets as _ss
    _ss.main()

    # ── Phase 11: run optimizer ───────────────────────────────────────────
    print("\n=== Phase 11: running optimizer ===")
    import optimizer as _opt
    _opt.main()

    # ── Phase 12: visualise swaps ───────────────────────────────────────
    _swaps_path = OUT_DIR / "optimizer_swaps.json"
    if _swaps_path.exists():
        print("\n=== Phase 12: visualising swaps ===")
        import vis_swaps as _vs
        _vs.main()
    else:
        print("\n=== Phase 12: skipped (no swaps to visualise) ===")

    # ── Phase 13: cleanup ─────────────────────────────────────────────────
    print()
    cleanup_html_dir(HTML_DIR)


if __name__ == "__main__":
    main()

