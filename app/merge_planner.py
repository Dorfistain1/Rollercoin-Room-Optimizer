"""
merge_planner.py — Find optimal miner merges and produce a combined merge + placement plan.

Activated by  python main.py --merge <rlt_budget>

Flow:
  0. Detect and parse parts.html from html_page/ (rollercoin.com/storage page).
  1. Fetch/cache merge costs from minaryganar.com API per-miner.
  2. Build merge candidate pool from placed rooms + inventory.
  3. Ask user for minimum efficiency threshold (Th/s gained per RLT spent).
  4. Greedy selection of merges within budget, parts, and threshold.
  5. Build virtual post-merge state (placed + inventory).
  6. Run optimizer loop on virtual state → placement plan.
  7. Print combined plan + save JSON + render merge_steps.png.

Output files:
  data/parts.json           — parsed owned parts
  data/merge_costs.json     — cached API merge costs (slug-keyed)
  data/merge_plan.json      — combined merge steps + swap plan
  data/optimizer_swaps.json — swap plan for vis_swaps.py (subset of above)
  output/merge_steps.png    — visual merge step chart
"""

import json
import re
import statistics
import sys
import time
from copy import deepcopy
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

# ── sibling imports ────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from optimizer import (
    Miner,
    _delta_power,
    _norm_name,
    _prompt_set_bonus,
    apply_swap,
    build_inventory_pool,
    build_state,
    compute_swaps,
    find_best_swap,
    load_locked,
    load_miners_data,
    load_set_bonus,
    load_set_groups,
    miner_cells,
    miner_stats,
    save_set_bonus,
    set_group_bonus,
    total_power,
)
from scrape_miners import API_BASE, HEADERS, RARITIES, fetch_with_retry, name_to_slug
from visualize_room import (
    CELL_H,
    CELL_W,
    MINERS_DIR,
    RARITY_BADGE,
    RARITY_DIR,
    _load_font,
    load_badge,
    load_first_frame,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).parent.parent

MERGE_COSTS_JSON    = _ROOT / "data/merge_costs.json"
MERGE_PLAN_JSON     = _ROOT / "data/merge_plan.json"
PARTS_JSON          = _ROOT / "data/parts.json"
OPTIMIZER_SWAPS_JSON = _ROOT / "data/optimizer_swaps.json"
OUTPUT_DIR          = _ROOT / "output"
MINERS_DATA_PATH    = _ROOT / "miners/miners_data.json"

_RARITY_TO_LEVEL: dict[str, int] = {r: i + 1 for i, r in enumerate(RARITIES)}
_LEVEL_TO_RARITY: dict[int, str] = {i + 1: r for i, r in enumerate(RARITIES)}


# ── Part key helpers ───────────────────────────────────────────────────────────

def _part_key_str(part_type: str, level: int) -> str:
    return f"{part_type.lower()}:{level}"


def _parse_part_key(s: str) -> tuple[str, int]:
    t, lvl = s.rsplit(":", 1)
    return t, int(lvl)


# ── Phase 0: Parse parts.html ─────────────────────────────────────────────────

def parse_parts_html(html_dir: Path) -> dict | None:
    """
    Scan html_dir for rollercoin.com/storage HTML page.
    Returns {(part_type_lower, level): count} or None if not found.

    Parts HTML structure:
      .inventory-parts-container
        .part-card-wrapper
          p.rarity  → "Common" / "Uncommon" / ...
          p.name    → "Wire" / "Fan" / "Hashboard" / ...
          p.number  → quantity
    """
    candidates = sorted(html_dir.glob("*.html"))
    for p in candidates:
        raw = p.read_bytes()
        if b"rollercoin.com/storage" not in raw[:800] and b"inventory-parts-container" not in raw:
            continue
        soup = BeautifulSoup(raw, "lxml")
        container = soup.find("div", class_="inventory-parts-container")
        if container is None:
            continue
        owned: dict[tuple, int] = {}
        for card in container.find_all("div", class_="part-card-wrapper"):
            rarity_el = card.find("p", class_="rarity")
            name_el   = card.find("p", class_="name")
            qty_el    = card.find("p", class_="number")
            if not (rarity_el and name_el and qty_el):
                continue
            try:
                qty = int(qty_el.text.strip().replace(",", ""))
            except ValueError:
                continue
            rarity_name = rarity_el.text.strip().lower()
            level = _RARITY_TO_LEVEL.get(rarity_name)
            if level is None:
                continue
            part_type = name_el.text.strip().lower()
            key = (part_type, level)
            owned[key] = owned.get(key, 0) + qty

        if owned:
            PARTS_JSON.parent.mkdir(exist_ok=True)
            PARTS_JSON.write_text(
                json.dumps({_part_key_str(k[0], k[1]): v for k, v in owned.items()}, indent=2),
                encoding="utf-8",
            )
            return owned

    return None


# ── Phase 1: Fetch / cache merge costs ────────────────────────────────────────

def _fetch_costs_for_slug(slug: str) -> dict | None:
    """Fetch one miner from API, extract merge costs. Returns {to_rarity: {rlt, parts}} or None."""
    url = f"{API_BASE}/{requests.utils.quote(slug, safe='')}"
    resp = fetch_with_retry(url, retries=2, wait=True)
    if resp is None:
        return None
    try:
        item = resp.json()
    except Exception:
        return None

    costs: dict[str, dict] = {}
    for merge in item.get("merges", []):
        level    = merge.get("level", 0)
        to_rarity = _LEVEL_TO_RARITY.get(level)
        if not to_rarity or to_rarity == "common":
            continue
        try:
            rlt = float(merge.get("merge_fee") or 0)
        except (ValueError, TypeError):
            rlt = 0.0
        parts_list = []
        for p in merge.get("parts", []):
            ptype  = (p.get("part_type") or "").lower()
            plevel = p.get("level", 0)
            qty    = p.get("quantity", 0)
            if ptype and plevel and qty:
                parts_list.append([ptype, plevel, qty])
        costs[to_rarity] = {"rlt": rlt, "parts": parts_list}

    return costs or None


def load_merge_costs(candidate_names_lower: list[str], miners_db: dict) -> dict:
    """
    Load cached merge costs; fetch from API for any not yet cached.
    Returns {name_lower: {to_rarity: {"rlt": float, "parts": [[type, level, qty], ...]}}}
    """
    cache: dict = {}
    if MERGE_COSTS_JSON.exists():
        try:
            cache = json.loads(MERGE_COSTS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass

    missing = [n for n in candidate_names_lower if n not in cache]
    if missing:
        from tqdm import tqdm as _tqdm
        _bar = _tqdm(missing, desc="  Fetching merge costs", unit="miner", leave=True)
        _iter: list | object = _bar
    else:
        _iter = []
    for name_lower in _iter:
        rec = miners_db.get(name_lower)
        if rec is None:
            continue
        # Derive slug from _detail_url if present, else from name
        detail_url = rec.get("_detail_url", "")
        slug = detail_url.rstrip("/").split("/")[-1] if detail_url else name_to_slug(name_lower)
        costs = _fetch_costs_for_slug(slug)
        if costs:
            cache[name_lower] = costs
        time.sleep(0.3)

    MERGE_COSTS_JSON.parent.mkdir(exist_ok=True)
    MERGE_COSTS_JSON.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache


# ── Phase 2: Build merge candidate pool ───────────────────────────────────────

def build_merge_pool(rooms: list[dict], inv_pool: dict) -> dict:
    """
    Returns {(name_lower, from_rarity): {"name", "placed_count", "inv_count"}}.
    Only includes entries with total >= 2 and from_rarity != "unreal".
    """
    counts: dict[tuple, dict] = {}

    for room in rooms:
        for rack in room.get("racks", []):
            for m in rack:
                name   = m["name"]
                rarity = m.get("rarity") or "common"
                key = (name.lower(), rarity)
                if key not in counts:
                    counts[key] = {"name": name, "placed_count": 0, "inv_count": 0}
                counts[key]["placed_count"] += 1

    for (name_lower, rarity), entry in inv_pool.items():
        key = (name_lower, rarity)
        if key not in counts:
            counts[key] = {"name": entry["name"], "placed_count": 0, "inv_count": 0}
        counts[key]["inv_count"] += entry.get("count", 0)

    return {
        k: v for k, v in counts.items()
        if (v["placed_count"] + v["inv_count"]) >= 2
        and k[1] != "unreal"
        and _RARITY_TO_LEVEL.get(k[1], 99) < len(RARITIES)
    }


# ── Parts check helpers ────────────────────────────────────────────────────────

def _can_afford_parts(parts_needed: list, remaining_parts: dict) -> bool:
    """parts_needed: [[type_lower, level, qty], ...]; remaining_parts: {(type, level): count}"""
    for ptype, plevel, qty in parts_needed:
        if remaining_parts.get((ptype, plevel), 0) < qty:
            return False
    return True


def _deduct_parts(parts_needed: list, remaining_parts: dict) -> None:
    for ptype, plevel, qty in parts_needed:
        remaining_parts[(ptype, plevel)] = remaining_parts.get((ptype, plevel), 0) - qty


# ── Phase 3: Efficiency threshold ─────────────────────────────────────────────

def determine_efficiency_threshold(
    merge_pool: dict,
    merge_costs: dict,
    miners_db: dict,
) -> float:
    """
    Compute efficiency for each possible merge, show distribution, ask user for threshold.
    Efficiency = (result_power_th - source_power_th) / rlt_cost.
    This represents how much power you gain (net, upgrading one slot) per RLT spent.
    """
    efficiencies: list[float] = []

    for (name_lower, from_rarity), info in merge_pool.items():
        from_level = _RARITY_TO_LEVEL.get(from_rarity, 0)
        to_rarity  = _LEVEL_TO_RARITY.get(from_level + 1)
        if not to_rarity:
            continue

        costs = merge_costs.get(name_lower, {}).get(to_rarity)
        if not costs or costs["rlt"] <= 0:
            continue

        rec = miners_db.get(name_lower)
        if rec is None:
            continue
        from_power = (rec.get("rarities", {}).get(from_rarity) or {}).get("power_th") or 0.0
        to_power   = (rec.get("rarities", {}).get(to_rarity)   or {}).get("power_th") or 0.0
        if to_power <= 0:
            continue

        eff = (to_power - from_power) / costs["rlt"]
        efficiencies.append(eff)

    if not efficiencies:
        print("  [merge] No efficiency data available — using threshold 0.")
        return 0.0

    efficiencies.sort()
    median = statistics.median(efficiencies)
    p25    = efficiencies[max(0, len(efficiencies) // 4)]
    p75    = efficiencies[min(len(efficiencies) - 1, 3 * len(efficiencies) // 4)]

    print(f"\n  Merge efficiency range : {efficiencies[0]:>12,.1f}  –  {efficiencies[-1]:>12,.1f}  Th/s per RLT")
    print(f"  25th pct / median / 75th: {p25:>10,.1f}  /  {median:>10,.1f}  /  {p75:>10,.1f}")

    recommended = round(max(0.0, p25), 1)
    raw = input(f"\n  Min efficiency threshold (Th/s per RLT)  [recommended: {recommended:,.1f}] > ").strip().replace(",", ".")
    if not raw:
        threshold = recommended
    else:
        try:
            threshold = float(raw)
        except ValueError:
            print("  [!] Invalid — using recommended.")
            threshold = recommended

    print(f"  Threshold set: {threshold:,.1f} Th/s per RLT\n")
    return threshold


# ── Phase 4: Greedy merge selection ───────────────────────────────────────────

def _next_rarity(rarity: str) -> str | None:
    lvl = _RARITY_TO_LEVEL.get(rarity, 0)
    return _LEVEL_TO_RARITY.get(lvl + 1)


def select_merges(
    merge_pool: dict,
    merge_costs: dict,
    owned_parts: dict,
    rlt_budget: float,
    efficiency_threshold: float,
    initial_placed: list,
    initial_inv_pool: dict,
    miners_db: dict,
    set_groups: list,
) -> tuple[list, list, dict, dict]:
    """
    Greedily select merges that:
      - Are affordable (RLT + parts).
      - Have efficiency >= threshold.
      - Produce a miner that would improve the virtual room (gain > 0).
    Returns (merge_steps, virtual_placed, virtual_inv_pool, merged_positions).
    merged_positions: {(room_idx, rack_idx, slot_idx): {"name": str, "rarity": str}}
    """
    steps: list[dict] = []
    remaining_rlt = rlt_budget
    remaining_parts = dict(owned_parts)
    merged_positions: dict[tuple, dict] = {}

    # Virtual state
    virtual_placed  = list(initial_placed)
    virtual_inv     = deepcopy(initial_inv_pool)

    # Virtual total counts (placed + inventory), updated as merges are selected.
    virtual_counts: dict[tuple, int] = {}
    for (n, r), info in merge_pool.items():
        virtual_counts[(n, r)] = info["placed_count"] + info["inv_count"]
    # Also include inventory entries not in the pool (low-count items that might gain chain)
    for (n, r), entry in initial_inv_pool.items():
        if (n, r) not in virtual_counts:
            virtual_counts[(n, r)] = entry.get("count", 0)

    # Track which step produced which (name, rarity) for chain labelling
    chain_produced: dict[tuple, int] = {}  # (name_lower, rarity): step_number

    while True:
        best: dict | None = None
        best_eff = efficiency_threshold  # strictly above threshold

        for (name_lower, from_rarity), cnt in list(virtual_counts.items()):
            if cnt < 2:
                continue
            to_rarity = _next_rarity(from_rarity)
            if to_rarity is None:
                continue  # unreal, can't merge further

            costs = merge_costs.get(name_lower, {}).get(to_rarity)
            if not costs:
                continue
            rlt   = costs["rlt"]
            parts = costs["parts"]  # [[type, level, qty], ...]

            if rlt > remaining_rlt:
                continue
            if not _can_afford_parts(parts, remaining_parts):
                continue

            rec = miners_db.get(name_lower)
            if rec is None:
                continue

            to_data   = rec.get("rarities", {}).get(to_rarity, {}) or {}
            from_data = rec.get("rarities", {}).get(from_rarity, {}) or {}
            to_power  = to_data.get("power_th") or 0.0
            to_bonus  = to_data.get("bonus_pct") or 0.0
            if to_power <= 0:
                continue

            # Determine how many source miners are in inventory vs room
            n_inv_avail = virtual_inv.get((name_lower, from_rarity), {}).get("count", 0)
            n_from_inv  = min(2, n_inv_avail)
            n_from_room = 2 - n_from_inv

            # Find placed miners to use as room sources
            placed_cands = [
                m for m in virtual_placed
                if m.name.lower() == name_lower
                and (m.rarity or "common") == from_rarity
                and not m.locked
                and m.name != "[empty]"
            ]
            if len(placed_cands) < n_from_room:
                continue

            remove_miners = placed_cands[:n_from_room]

            # Compute gain with _delta_power
            if remove_miners:
                gain = _delta_power(
                    virtual_placed, remove_miners,
                    [rec["name"]], [to_power], [to_bonus], [to_rarity],
                    set_groups=set_groups,
                )
            else:
                # All inventory sources — merged miner goes to inventory.
                # Estimate gain: assume it replaces the weakest non-locked placed miner.
                non_locked = [m for m in virtual_placed if not m.locked and m.name != "[empty]"]
                if not non_locked:
                    gain = to_power
                else:
                    non_locked.sort(key=lambda m: m.power_th)
                    worst = non_locked[0]
                    gain = _delta_power(
                        virtual_placed, [worst],
                        [rec["name"]], [to_power], [to_bonus], [to_rarity],
                        set_groups=set_groups,
                    )

            if gain <= 0 or rlt <= 0:
                continue

            eff_actual = gain / rlt
            if eff_actual > best_eff:
                best_eff = eff_actual
                best = {
                    "name_lower":    name_lower,
                    "name":          rec["name"],
                    "from_rarity":   from_rarity,
                    "to_rarity":     to_rarity,
                    "rlt":           rlt,
                    "parts":         parts,
                    "remove_miners": remove_miners,
                    "n_from_inv":    n_from_inv,
                    "n_from_room":   n_from_room,
                    "gain":          gain,
                    "eff":           eff_actual,
                    "to_power":      to_power,
                    "to_bonus":      to_bonus,
                    "cells":         rec.get("cells", 2),
                }

        if best is None:
            break

        # ── Apply the selected merge ──────────────────────────────────────────
        n  = best["name_lower"]
        fr = best["from_rarity"]
        tr = best["to_rarity"]

        remaining_rlt -= best["rlt"]
        _deduct_parts(best["parts"], remaining_parts)

        # Update virtual counts
        virtual_counts[(n, fr)] = max(0, virtual_counts.get((n, fr), 0) - 2)
        virtual_counts[(n, tr)] = virtual_counts.get((n, tr), 0) + 1

        # Remove placed miners (replace with tiny-power placeholders the optimizer can fill)
        for m in best["remove_miners"]:
            merged_positions[(m.room_idx, m.rack_idx, m.slot_idx)] = {
                "name":   m.name,
                "rarity": m.rarity or "common",
            }
            virtual_placed = [p for p in virtual_placed if p is not m]
            virtual_placed.append(Miner(
                "[empty]", 0.001, 0.0, m.cells, "common",
                m.room_idx, m.rack_idx, m.slot_idx, m.position_in_slot,
                m.miner_rank, locked=False,
            ))

        # Remove from virtual inventory
        if best["n_from_inv"] > 0:
            key = (n, fr)
            if key in virtual_inv:
                virtual_inv[key]["count"] -= best["n_from_inv"]
                if virtual_inv[key]["count"] <= 0:
                    del virtual_inv[key]

        # Add merged miner to virtual inventory
        to_key = (n, tr)
        if to_key in virtual_inv:
            virtual_inv[to_key]["count"] += 1
        else:
            virtual_inv[to_key] = {
                "name":      best["name"],
                "rarity":    tr,
                "count":     1,
                "power_th":  best["to_power"],
                "bonus_pct": best["to_bonus"],
                "cells":     best["cells"],
            }

        # Build source descriptions
        step_num = len(steps) + 1
        source_descs: list[str] = []
        depends_on: int | None = None

        # Was one of the inv copies produced by a prior chain step?
        prior_step = chain_produced.get((n, fr))
        n_from_inv_actual = best["n_from_inv"]

        if prior_step is not None and n_from_inv_actual > 0:
            source_descs.append(f"step {prior_step} output")
            n_from_inv_actual -= 1
            depends_on = prior_step

        for _ in range(n_from_inv_actual):
            source_descs.append("inventory")
        for _ in range(best["n_from_room"]):
            source_descs.append("room (auto-removed)")

        chain_produced[(n, tr)] = step_num

        steps.append({
            "step":             step_num,
            "miner_name":       best["name"],
            "from_rarity":      fr,
            "to_rarity":        tr,
            "cost_rlt":         round(best["rlt"], 4),
            "cost_parts":       [{"type": p[0], "level": p[1], "qty": p[2]} for p in best["parts"]],
            "source_locations": source_descs,
            "depends_on_step":  depends_on,
            "merged_power_th":  round(best["to_power"], 2),
            "merged_bonus_pct": round(best["to_bonus"], 2),
            "net_power_gain_th": round(best["gain"], 1),
            "efficiency_ratio": round(best["eff"], 1),
        })

    return steps, virtual_placed, virtual_inv, merged_positions


# ── Phase 5: Run optimizer on virtual state ───────────────────────────────────

def _run_optimizer_virtual(
    placed: list,
    inv_pool: dict,
    rooms: list[dict],
    miners_db: dict,
    set_groups: list,
    locked_set: set,
    set_bonus_pct: float = 0.0,
) -> tuple[list, list, float]:
    """
    Run the greedy optimizer loop on the virtual post-merge state.
    Returns (original_placed, final_placed, set_bonus_pct).
    set_bonus_pct must be calibrated against the REAL room state before calling.
    """
    original_placed = list(placed)
    current = list(placed)

    raw0, bonus0, eff0 = total_power(current, set_groups)
    set_pct_0, set_raw_0 = (
        set_group_bonus([m.name.lower() for m in current], set_groups)
        if set_groups else (0.0, 0.0)
    )
    adj_pct = bonus0 + set_pct_0 + set_bonus_pct
    current_adj = raw0 * (1.0 + adj_pct / 100.0) + set_raw_0

    print(f"\n  Virtual post-merge state (after merges, before new placements):")
    print(f"    Effective power : {current_adj:>12,.1f} Th/s")
    print(f"    (lower than actual room — merged miners not yet replaced)")

    iteration = 0
    while True:
        result = find_best_swap(current, inv_pool, rooms, miners_db, set_groups,
                                set_bonus_pct=set_bonus_pct)
        if result is None:
            break
        delta, remove, add_entries = result
        add_names = ", ".join(e["name"] for e in add_entries)
        rem_names = ", ".join(m.name for m in remove)
        print(f"  Swap {iteration + 1}: remove [{rem_names}]  +{delta:,.1f} Th/s")
        print(f"           add    [{add_names}]")
        current = apply_swap(current, inv_pool, remove, add_entries, miners_db)
        _r, _b, _ = total_power(current, set_groups)
        _sp, _sr  = set_group_bonus([m.name.lower() for m in current], set_groups) if set_groups else (0.0, 0.0)
        current_adj = _r * (1.0 + (_b + _sp + set_bonus_pct) / 100.0) + _sr
        iteration += 1

    if iteration == 0:
        print("  Virtual room is already optimal.")

    return original_placed, current, set_bonus_pct


# ── Printing ───────────────────────────────────────────────────────────────────

def _parts_str(parts: list) -> str:
    if not parts:
        return "—"
    return "  +  ".join(f"{p['type'].capitalize()} L{p['level']} ×{p['qty']:,}" for p in parts)


def print_merge_plan(
    merge_steps: list,
    swaps: list,
    owned_parts: dict,
    rlt_budget: float,
    merge_dependent_slots: set,
) -> None:
    total_rlt   = sum(s["cost_rlt"] for s in merge_steps)
    total_parts: dict[tuple, int] = {}
    for s in merge_steps:
        for p in s["cost_parts"]:
            k = (p["type"], p["level"])
            total_parts[k] = total_parts.get(k, 0) + p["qty"]

    print(f"\n{'=' * 60}")
    print(f"  MERGE + PLACEMENT PLAN  (budget: {rlt_budget:,.3f} RLT)")
    print(f"{'=' * 60}")

    if merge_steps:
        print("\n  MERGES (do these first in-game):\n")
        for s in merge_steps:
            chain = f"  [chain from step {s['depends_on_step']}]" if s["depends_on_step"] else ""
            print(f"  [{s['step']}] {s['miner_name']}  {s['from_rarity']} → {s['to_rarity']}{chain}")
            print(f"      Cost    : {s['cost_rlt']:.3f} RLT")
            print(f"      Parts   : {_parts_str(s['cost_parts'])}")
            src = ", ".join(s["source_locations"])
            print(f"      Sources : {src}")
            print(f"      Result  : {s['merged_power_th']:,.1f} Th/s  {s['merged_bonus_pct']:.2f}%")
            print(f"      Gain    : +{s['net_power_gain_th']:,.1f} Th/s  ({s['efficiency_ratio']:,.1f} Th/s per RLT)")
            print()

        print(f"  Budget used: {total_rlt:.3f} / {rlt_budget:.3f} RLT")
        if total_parts:
            parts_list = "  |  ".join(
                f"{t.capitalize()} L{lvl} ×{qty:,}" for (t, lvl), qty in sorted(total_parts.items())
            )
            print(f"  Parts used : {parts_list}")
    else:
        print("\n  No merges selected (budget / parts / efficiency constraints).")

    if swaps:
        print(f"\n  PLACEMENTS (after merges):\n")
        for idx, s in enumerate(swaps, 1):
            rem   = " + ".join(f"{e['name']} ({e['rarity']})" for e in s["remove"])
            add   = " + ".join(f"{e['name']} ({e['rarity']})" for e in s["add"])
            d_str = f"+{s['delta_eff']:,.1f}" if s["delta_eff"] >= 0 else f"{s['delta_eff']:,.1f}"
            badge = " [MERGE FIRST ⚡]" if s.get("requires_merge") else ""
            print(f"  [{idx}]{badge}  Room {s['room']}  Rack {s['rack']}  Pos {s['rack_positions']}")
            # Skip empty-placeholder remove entries in display
            rem_clean = " + ".join(
                f"{e['name']} ({e['rarity']})" for e in s["remove"] if e["name"] != "[empty]"
            ) or "(freed slot)"
            print(f"    Remove : {rem_clean}")
            print(f"    Add    : {add}")
            print(f"    Gain   : {d_str} Th/s")
            print()

    print(f"{'=' * 60}\n")


# ── Rendering ─────────────────────────────────────────────────────────────────

_RARITY_COLORS: dict[str, tuple] = {
    "common":    (150, 150, 150),
    "uncommon":  (80, 200, 80),
    "rare":      (80, 120, 255),
    "epic":      (180, 80, 255),
    "legendary": (255, 180, 0),
    "unreal":    (255, 80, 80),
}

_THUMB_W  = 90
_THUMB_H  = 72
_BADGE_SZ = 22
_STEP_COL = 50
_ARROW_W  = 66
_INFO_W   = 230
_PAD      = 12

# Fixed x-positions (derived once so layout is consistent)
_X_STEP   = _PAD
_X_SRC_A  = _X_STEP + _STEP_COL
_X_SRC_B  = _X_SRC_A + _THUMB_W + _PAD
_X_ARROW  = _X_SRC_B + _THUMB_W + _PAD
_X_RESULT = _X_ARROW + _ARROW_W + _PAD
_X_INFO   = _X_RESULT + _THUMB_W + _PAD
_CANVAS_W = _X_INFO + _INFO_W + _PAD

# Row height: thumb + badge + name + 3 text lines + bottom pad
_ROW_H    = _THUMB_H + _BADGE_SZ + 6 + 13 + 13 + 13 + 14

_BG       = (28, 30, 52)
_FG       = (220, 220, 230)
_ACCENT   = (85, 200, 245)


def _thumb(name: str, rarity: str, miners_db: dict) -> Image.Image:
    """Return a thumbnail image for a miner at the given rarity."""
    name_lower = name.lower()
    rec = miners_db.get(name_lower)
    stem = ""
    if rec:
        detail = rec.get("_detail_url", "")
        if detail:
            stem = detail.rstrip("/").split("/")[-1]
    if not stem:
        stem = name_lower.replace(" ", "_").replace("'", "").replace("’", "")
    img = load_first_frame(stem)
    if img is None:
        img = Image.new("RGBA", (_THUMB_W, _THUMB_H), (80, 80, 80, 255))
    return img.convert("RGBA").resize((_THUMB_W, _THUMB_H), Image.LANCZOS)


def _paste_badge(canvas: Image.Image, rarity: str, x: int, y: int) -> None:
    """Paste a rarity badge image at (x, y). Falls back to nothing if unavailable."""
    badge = load_badge(rarity)
    if badge:
        badge = badge.convert("RGBA").resize((_BADGE_SZ, _BADGE_SZ), Image.LANCZOS)
        canvas.paste(badge, (x, y), badge)


def _draw_thumb_cell(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    name: str,
    rarity: str,
    x: int,
    y: int,
    font_name,
    extra_lines: list[tuple] | None = None,
) -> None:
    """Paste thumb, badge below it, miner name below badge, then optional extra lines."""
    canvas.paste(img, (x, y), img)
    badge_y = y + _THUMB_H + 2
    _paste_badge(canvas, rarity, x + (_THUMB_W - _BADGE_SZ) // 2, badge_y)
    name_y = badge_y + _BADGE_SZ + 3
    # Truncate long names so they don't overflow the column
    max_chars = _THUMB_W // 6
    display_name = name if len(name) <= max_chars else name[:max_chars - 1] + "…"
    draw.text((x, name_y), display_name, fill=_FG, font=font_name)
    if extra_lines:
        line_y = name_y + 14
        for text, color in extra_lines:
            draw.text((x, line_y), text, fill=color, font=font_name)
            line_y += 13


def render_merge_steps(merge_steps: list, miners_db: dict) -> None:
    """Render merge_steps.png — one row per merge step."""
    if not merge_steps:
        return

    font_sm = _load_font(10)
    font_md = _load_font(12)
    font_lg = _load_font(14)

    n_steps = len(merge_steps)
    h = _PAD + 30 + n_steps * (_ROW_H + _PAD) + _PAD

    canvas = Image.new("RGB", (_CANVAS_W, h), _BG)
    draw   = ImageDraw.Draw(canvas)

    # Header
    draw.text((_X_SRC_A, _PAD + 4), "MERGE PLAN", fill=_ACCENT, font=font_lg)

    for i, step in enumerate(merge_steps):
        y = _PAD + 30 + i * (_ROW_H + _PAD)

        # Step number (vertically centered on thumb)
        draw.text((_X_STEP, y + _THUMB_H // 2 - 8), f"[{step['step']}]", fill=_ACCENT, font=font_md)

        # Source thumbnails (same image, same rarity — two copies being merged)
        src_img = _thumb(step["miner_name"], step["from_rarity"], miners_db)
        _draw_thumb_cell(canvas, draw, src_img, step["miner_name"], step["from_rarity"],
                         _X_SRC_A, y, font_sm)
        _draw_thumb_cell(canvas, draw, src_img, step["miner_name"], step["from_rarity"],
                         _X_SRC_B, y, font_sm)

        # Arrow + cost
        arr_cy = y + _THUMB_H // 2
        draw.text((_X_ARROW + 12, arr_cy - 10), "→", fill=_ACCENT, font=font_lg)
        draw.text((_X_ARROW + 2, arr_cy + 6), f"{step['cost_rlt']:.3f} RLT", fill=(200, 200, 60), font=font_sm)

        # Result thumbnail with power + gain extra lines
        result_img = _thumb(step["miner_name"], step["to_rarity"], miners_db)
        _draw_thumb_cell(
            canvas, draw, result_img,
            step["miner_name"], step["to_rarity"],
            _X_RESULT, y, font_sm,
            extra_lines=[
                (f"{step['merged_power_th']:,.0f} Th/s", _FG),
                (f"+{step['net_power_gain_th']:,.0f} Th/s gain", (80, 220, 80)),
            ],
        )

        # Info column: parts, efficiency, chain
        ix = _X_INFO
        iy = y + 4
        parts_lines = [
            f"{p['type'].capitalize()} L{p['level']} × {p['qty']:,}"
            for p in step["cost_parts"]
        ] or ["No parts required"]
        for pl in parts_lines:
            draw.text((ix, iy), pl, fill=(180, 160, 100), font=font_sm)
            iy += 13
        draw.text((ix, iy), f"Eff: {step['efficiency_ratio']:,.1f} Th/s per RLT",
                  fill=(120, 200, 120), font=font_sm)
        iy += 13
        if step.get("depends_on_step"):
            draw.text((ix, iy), f"Chain from step {step['depends_on_step']}",
                      fill=(100, 200, 255), font=font_sm)

        # Thin separator line between rows
        if i < n_steps - 1:
            sep_y = y + _ROW_H + _PAD // 2
            draw.line((_PAD, sep_y, _CANVAS_W - _PAD, sep_y), fill=(60, 65, 100), width=1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "merge_steps.png"
    canvas.save(str(out))
    print(f"  Merge steps image → {out}")


# ── Main entry point ───────────────────────────────────────────────────────────

def run_merge_planning(
    html_dir: Path,
    rooms: list[dict],
    inv_list: list[dict],
    miners_db: dict,
    rlt_budget: float,
    set_groups: list | None = None,
    locked_set: set | None = None,
) -> dict | None:
    """
    Full merge planning pipeline. Returns a result dict with merge steps and
    swap plan, or None if planning was aborted.

    Saves:
      data/merge_plan.json
      data/optimizer_swaps.json   (for vis_swaps.py)
      output/merge_steps.png
    """
    set_groups  = set_groups  or []
    locked_set  = locked_set  or set()

    # ── Phase 0: parse parts ──────────────────────────────────────────────────
    print("\n=== Merge Phase 0: parsing parts inventory ===")
    owned_parts = parse_parts_html(html_dir)
    if owned_parts is None:
        print("  [!] No parts.html found in html_page/")
        print("  [!] To enable merge planning, save rollercoin.com/storage as 'parts.html' in html_page/")
        print("  Merge planning skipped — running normal optimizer.\n")
        return None

    print("  Owned parts:")
    for (ptype, level), qty in sorted(owned_parts.items()):
        print(f"    {ptype.capitalize()} L{level} ({RARITIES[level - 1]}): {qty:,}")

    # ── Phase 1: build pool + fetch costs ────────────────────────────────────
    print("\n=== Merge Phase 1: building candidate pool ===")
    inv_pool = build_inventory_pool(inv_list, miners_db)
    merge_pool = build_merge_pool(rooms, inv_pool)
    print(f"  {len(merge_pool)} unique (miner, rarity) pair(s) with 2+ copies")

    if not merge_pool:
        print("  No merge candidates found.")
        return None

    candidate_names = list({k[0] for k in merge_pool.keys()})
    print(f"\n=== Merge Phase 2: fetching merge costs ===")
    merge_costs = load_merge_costs(candidate_names, miners_db)

    # ── Phase 3: efficiency threshold ────────────────────────────────────────
    print("\n=== Merge Phase 3: efficiency threshold ===")
    threshold = determine_efficiency_threshold(merge_pool, merge_costs, miners_db)

    # ── Phase 4: select merges ────────────────────────────────────────────────
    print("=== Merge Phase 4: selecting merges ===")
    placed = build_state(rooms, miners_db, locked_set)
    merge_steps, virtual_placed, virtual_inv, merged_positions = select_merges(
        merge_pool, merge_costs, owned_parts, rlt_budget,
        threshold, placed, inv_pool, miners_db, set_groups,
    )
    print(f"  Selected {len(merge_steps)} merge(s)")

    # ── Phase 5: optimizer on virtual state ──────────────────────────────────
    print("\n=== Merge Phase 5: optimizer on post-merge state ===")

    # Calibrate set-bonus offset against the REAL (pre-merge) placed state so
    # that [empty] placeholder miners don't distort the bonus baseline.
    # The saved value is then used as-is inside _run_optimizer_virtual.
    _real_placed = build_state(rooms, miners_db, locked_set)
    _real_raw, _real_bonus, _ = total_power(_real_placed, set_groups)
    _real_set_pct, _real_set_raw = (
        set_group_bonus([m.name.lower() for m in _real_placed], set_groups)
        if set_groups else (0.0, 0.0)
    )
    _pre_eff = _real_raw * (1.0 + (_real_bonus + _real_set_pct) / 100.0) + _real_set_raw
    print(f"\n  Current room (pre-merge baseline):")
    print(f"    Raw power      : {_real_raw:>12,.1f} Th/s")
    print(f"    Effective power: {_pre_eff:>12,.1f} Th/s")
    set_bonus = _prompt_set_bonus(_real_raw, _real_bonus + _real_set_pct, load_set_bonus())
    current_power_adj = round(
        _real_raw * (1.0 + (_real_bonus + _real_set_pct + set_bonus) / 100.0) + _real_set_raw, 1
    )
    print(f"    Adjusted power : {current_power_adj:>12,.1f} Th/s")

    inv_pool_copy = deepcopy(virtual_inv)
    original_virtual, final_placed, set_bonus = _run_optimizer_virtual(
        virtual_placed, inv_pool_copy, rooms, miners_db, set_groups, locked_set,
        set_bonus_pct=set_bonus,
    )

    # Compute physical swaps
    raw_swaps = compute_swaps(original_virtual, final_placed, set_groups)

    # Annotate swaps that fill slots freed by merges
    merged_miner_names = {s["miner_name"].lower() for s in merge_steps}
    # Swaps where the removed miner is [empty] were freed by a merge
    for s in raw_swaps:
        s["requires_merge"] = any(e["name"] == "[empty]" for e in s["remove"])
        # Clean up [empty] from display
    merge_dependent_slots = {i for i, s in enumerate(raw_swaps) if s.get("requires_merge")}

    # Build clean_swaps: replace [empty] remove entries with the original merged miner.
    # slot in compute_swaps is 1-based; merged_positions keys are 0-based.
    clean_swaps = []
    for s in raw_swaps:
        cs = dict(s)
        new_remove = []
        for e in s["remove"]:
            if e["name"] == "[empty]":
                mkey = (s["room"] - 1, s["rack"] - 1, s["slot"] - 1)
                orig = merged_positions.get(mkey)
                if orig:
                    new_remove.append({
                        "name":   orig["name"],
                        "rarity": orig["rarity"],
                        "merged": True,
                    })
                # if no match, drop the [empty] entry
            else:
                new_remove.append(e)
        cs["remove"] = new_remove
        clean_swaps.append(cs)

    OPTIMIZER_SWAPS_JSON.parent.mkdir(exist_ok=True)
    OPTIMIZER_SWAPS_JSON.write_text(
        json.dumps(clean_swaps, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Save full merge plan
    merge_plan = {
        "rlt_budget":        rlt_budget,
        "rlt_used":          round(sum(s["cost_rlt"] for s in merge_steps), 4),
        "current_power_adj": current_power_adj,
        "merge_steps":       merge_steps,
        "optimizer_swaps":   clean_swaps,
    }
    MERGE_PLAN_JSON.write_text(
        json.dumps(merge_plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── Output ────────────────────────────────────────────────────────────────
    print_merge_plan(merge_steps, clean_swaps, owned_parts, rlt_budget, merge_dependent_slots)

    print("=== Merge: rendering merge steps image ===")
    render_merge_steps(merge_steps, miners_db)

    print(f"\nMerge plan saved → {MERGE_PLAN_JSON}")
    print(f"Swap plan saved  → {OPTIMIZER_SWAPS_JSON}")

    return merge_plan
