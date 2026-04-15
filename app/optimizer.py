"""
optimizer.py — Find optimal miner swaps to maximise total power.

Usage:
  python optimizer.py            # reads all placed_room*.json + inventory.json
  python optimizer.py --dry-run  # same but prints plan without writing anything

How total power is calculated:
  raw_power   = sum of every placed miner's power_th  (duplicates count)
  total_bonus = sum of each UNIQUE miner type's bonus_pct  (duplicates ignored)
  total_power = raw_power * (1 + total_bonus / 100)

Rack model:
  Each rack is a sequence of pair-slots.  A pair-slot is either:
    • one 2-cell miner,  OR
    • two consecutive 1-cell miners (paired by position order in the rack)
  Pair-slots are independent: a 2-cell can only swap with another 2-cell occupant
  or with a pair of 1-cell occupants, and vice-versa.

  Cross-rack 1-cell balancing:
    If replacing a 2-cell with a 1-cell leaves an odd empty cell in a rack, the
    optimizer checks whether another rack also has an odd empty cell.  If so, it
    "donates" a 1-cell miner from that rack to fill the gap, freeing a 2-cell
    slot — which can then be filled by a 2-cell inventory candidate.  This is
    handled as a combined swap evaluated atomically.

Swap chain collapsing:
  The algorithm works on an in-memory copy of the room state, so chains like
  A→B→B→C collapse automatically — the final diff vs the original state gives
  the minimal list of physical swaps for the user.
"""

import json
import re
import sys
from copy import deepcopy
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PLACED_GLOB    = "placed_room*.json"
_ROOT          = Path(__file__).parent.parent   # roomBuilder/
INVENTORY_JSON = _ROOT / "data/inventory.json"
MINERS_DATA    = _ROOT / "miners/miners_data.json"
LOCKED_JSON    = _ROOT / "data/locked.json"
PLACED_DIR     = _ROOT / "data"
SET_BONUS_JSON  = _ROOT / "data/set_bonus.json"
SET_GROUPS_JSON = _ROOT / "data/set_groups.json"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    """Canonical comparison key: lowercase, no apostrophes, hyphens→underscores."""
    s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
    return re.sub(r"[^\w]+", "_", s).strip("_")


def load_miners_data() -> dict[str, dict]:
    """Return {name.lower(): record} from miners_data.json, plus normalised-name keys."""
    if not MINERS_DATA.exists():
        return {}
    with open(MINERS_DATA, encoding="utf-8") as f:
        data = json.load(f)
    index: dict[str, dict] = {}
    for m in data:
        index[m["name"].lower()] = m
        index[_norm_name(m["name"])] = m   # catches apostrophe/hyphen mismatches
    return index


def load_inventory() -> list[dict]:
    """Return the miners list from inventory.json."""
    if not INVENTORY_JSON.exists():
        return []
    with open(INVENTORY_JSON, encoding="utf-8") as f:
        return json.load(f)["miners"]


def load_all_rooms() -> list[dict]:
    """Return all placed_room*.json contents, sorted by filename."""
    paths = sorted(PLACED_DIR.glob(PLACED_GLOB))
    rooms = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            rooms.append(json.load(f))
    return rooms


def load_locked() -> set[tuple[int, int, int]]:
    """
    Load locked.json and return a set of (room_idx, rack_idx, miner_idx)
    tuples (0-based).  Returns an empty set if the file doesn't exist.
    """
    if not LOCKED_JSON.exists():
        return set()
    entries = json.loads(LOCKED_JSON.read_text(encoding="utf-8"))
    return {
        (e["room"] - 1, e["rack"], e["miner_idx"])
        for e in entries
    }


def load_set_groups() -> list[dict]:
    """Load set_groups.json. Returns empty list if file doesn't exist."""
    if not SET_GROUPS_JSON.exists():
        return []
    try:
        return json.loads(SET_GROUPS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []


def set_group_bonus(
    placed_names: list[str],
    set_groups: list[dict],
) -> tuple[float, float]:
    """
    Compute bonus contributions from all active set-group thresholds.

    placed_names: list of lowercased miner names (one entry per placed miner;
                  duplicates count independently).
    Returns (extra_pct, extra_raw_th):
      extra_pct    — added to the bonus multiplier  (e.g. 5.0 → +5%)
      extra_raw_th — added to effective power AFTER multiplication; NOT scaled
                     by the bonus % (so +5 000 TH stays +5 000 regardless of %)
    """
    extra_pct = 0.0
    extra_raw = 0.0
    for sg in set_groups:
        members_lower = {n.lower() for n in sg.get("member_names", [])}
        count = sum(1 for n in placed_names if n in members_lower)
        for threshold in sg.get("thresholds", []):
            # Support both key names for backwards compatibility
            min_m = threshold.get("min_members") or threshold.get("count", 0)
            if count >= min_m:
                if threshold.get("type") == "pct":
                    extra_pct += threshold["value"]
                else:  # raw_th
                    extra_raw += threshold["value"]
    return extra_pct, extra_raw


def load_set_bonus() -> float:
    """Return the saved set-bonus offset (bonus %), or 0.0 if not set."""
    if not SET_BONUS_JSON.exists():
        return 0.0
    try:
        data = json.loads(SET_BONUS_JSON.read_text(encoding="utf-8"))
        # Support old TH/s format gracefully — if only set_bonus_th present, ignore it
        return float(data.get("set_bonus_pct", 0.0))
    except Exception:
        return 0.0


def save_set_bonus(offset_pct: float) -> None:
    SET_BONUS_JSON.write_text(
        json.dumps({"set_bonus_pct": round(offset_pct, 4)}, indent=2),
        encoding="utf-8",
    )


def _prompt_set_bonus(raw_power: float, computed_bonus: float, current_offset_pct: float) -> float:
    """
    Show the computed total bonus and ask the user to confirm or enter
    the actual value shown in-game.  Returns the (possibly updated) bonus % offset.
    The offset accounts for set bonuses the system cannot detect automatically.
    """
    total_pct = computed_bonus + current_offset_pct
    print(f"\n  Computed bonus   : {computed_bonus:>10.2f} %")
    if current_offset_pct != 0.0:
        print(f"  Set-bonus offset : {current_offset_pct:>+10.2f} %")
        print(f"  Adjusted bonus   : {total_pct:>10.2f} %")
    print()
    print("  Enter the TOTAL bonus % shown in-game (e.g. 1031.42).")
    print("  This replaces the stored set-bonus offset — it does not add to it.")
    print("  Press Enter to keep the current value:")
    raw = input("  > ").strip().replace(",", ".").replace(" ", "").rstrip("%")
    if not raw:
        return current_offset_pct
    try:
        actual_bonus = float(raw)
    except ValueError:
        print("  [!] Invalid number — keeping previous set-bonus.")
        return current_offset_pct
    new_offset = actual_bonus - computed_bonus
    if abs(new_offset - current_offset_pct) > 0.01:
        print(f"  Set-bonus offset updated: {current_offset_pct:+.2f}% → {new_offset:+.2f}%")
        save_set_bonus(new_offset)
    return new_offset


# ---------------------------------------------------------------------------
# Miner stats helpers
# ---------------------------------------------------------------------------

def miner_cells(name: str, miners_db: dict[str, dict]) -> int:
    """Return the cell count for a miner (1 or 2). Defaults to 2 if unknown."""
    rec = miners_db.get(name.lower(), {})
    return rec.get("cells", 2)


def miner_stats(name: str, power_th: float | None, bonus_pct: float | None,
                rarity: str | None, miners_db: dict[str, dict]) -> tuple[float, float]:
    """
    Return (power_th, bonus_pct) for a miner.
    Prefers explicitly provided values; falls back to miners_db lookup.
    Tries exact name, then normalised name (strips apostrophes/hyphens).
    """
    if power_th is not None and bonus_pct is not None:
        return power_th, bonus_pct

    rec = miners_db.get(name.lower()) or miners_db.get(_norm_name(name))
    if rec is None:
        print(f"  [!] miner_stats: '{name}' not found in DB — treating as 0 power/bonus")
        return power_th or 0.0, bonus_pct or 0.0
    rarities = rec.get("rarities", {})

    if rarity and rarity in rarities:
        tier = rarities[rarity]
        p = power_th if power_th is not None else tier.get("power_th")
        b = bonus_pct if bonus_pct is not None else (tier.get("bonus_pct") or 0.0)
        return p or 0.0, b

    # No rarity info — pick the best available tier
    best_p, best_b = power_th or 0.0, bonus_pct or 0.0
    found = False
    for tier in rarities.values():
        p = tier.get("power_th") or 0.0
        b = tier.get("bonus_pct") or 0.0
        if p > best_p:
            best_p, best_b, found = p, b, True
    if not found and (power_th is None):
        pass  # genuinely unknown miner
    return best_p, best_b


# ---------------------------------------------------------------------------
# Room state representation
# ---------------------------------------------------------------------------

class Miner:
    """A single placed miner with all info needed for optimisation."""
    __slots__ = ("name", "power_th", "bonus_pct", "cells", "rarity",
                 "room_idx", "rack_idx", "slot_idx", "position_in_slot",
                 "miner_rank", "locked")

    def __init__(self, name, power_th, bonus_pct, cells, rarity,
                 room_idx, rack_idx, slot_idx, position_in_slot=0,
                 miner_rank=0, locked=False):
        self.name = name
        self.power_th = power_th
        self.bonus_pct = bonus_pct
        self.cells = cells
        self.rarity = rarity
        self.room_idx = room_idx
        self.rack_idx = rack_idx
        self.slot_idx = slot_idx
        self.position_in_slot = position_in_slot  # 0 or 1 for 1-cell miners
        self.miner_rank = miner_rank               # index within the rack list
        self.locked = locked                       # True = cannot be swapped out

    def __repr__(self):
        return f"Miner({self.name!r}, {self.power_th:.0f}Th, {self.bonus_pct:.2f}%, {self.cells}cell)"


def build_state(rooms: list[dict], miners_db: dict[str, dict],
                locked_set: set[tuple[int, int, int]] | None = None) -> list[Miner]:
    """
    Build a flat list of Miner objects from all placed rooms.
    Rack miners are grouped into pair-slots:
      - a 2-cell miner gets its own slot (position 0)
      - consecutive 1-cell miners share a slot (positions 0 and 1)
    locked_set: set of (room_idx, rack_idx, miner_rank) that are locked.
    """
    if locked_set is None:
        locked_set = set()
    placed: list[Miner] = []
    for room_idx, room in enumerate(rooms):
        for rack_idx, rack in enumerate(room["racks"]):
            slot_idx = 0
            pos_in_slot = 0
            for miner_rank, miner_data in enumerate(rack):
                name = miner_data["name"]
                rarity = miner_data.get("rarity")
                cells = miner_data.get("slot_size") or miner_cells(name, miners_db)
                p, b = miner_stats(name, None, None, rarity, miners_db)
                if p == 0.0:
                    print(f"  [!] '{name}' ({rarity}) has 0 power in DB — skipping as swap target")
                is_locked = p == 0.0 or (room_idx, rack_idx, miner_rank) in locked_set
                m = Miner(name, p, b, cells, rarity,
                          room_idx, rack_idx, slot_idx, pos_in_slot,
                          miner_rank=miner_rank, locked=is_locked)
                placed.append(m)
                if cells == 2:
                    slot_idx += 1
                    pos_in_slot = 0
                else:  # 1-cell
                    if pos_in_slot == 0:
                        pos_in_slot = 1   # next 1-cell shares this slot
                    else:
                        slot_idx += 1     # pair complete, next slot
                        pos_in_slot = 0
    return placed


def rack_capacity(room: dict, rack_idx: int, miners_db: dict[str, dict]) -> int:
    """Total cells in a rack (inferred from placed miners, assuming full)."""
    rack = room["racks"][rack_idx]
    return sum(m.get("slot_size") or miner_cells(m["name"], miners_db) for m in rack)


# ---------------------------------------------------------------------------
# Power calculation
# ---------------------------------------------------------------------------

def total_power(
    placed: list[Miner],
    set_groups: list[dict] | None = None,
) -> tuple[float, float, float]:
    """Return (raw_power, miner_bonus_pct, effective_power).

    Miner bonus deduplicates by (name, rarity) pair.  Set-group bonuses apply
    on top using the formula:
      effective = raw * (1 + (miner_bonus + set_pct) / 100) + set_raw_th
    where set_raw_th is NOT multiplied by the bonus % (game rule).
    """
    raw = sum(m.power_th for m in placed)
    seen: set[tuple[str, str]] = set()
    bonus = 0.0
    for m in placed:
        key = (m.name.lower(), m.rarity or "common")
        if key not in seen:
            seen.add(key)
            bonus += m.bonus_pct
    set_pct = 0.0
    set_raw = 0.0
    if set_groups:
        placed_names = [m.name.lower() for m in placed]
        set_pct, set_raw = set_group_bonus(placed_names, set_groups)
    effective = raw * (1.0 + (bonus + set_pct) / 100.0) + set_raw
    return raw, bonus, effective


# ---------------------------------------------------------------------------
# Inventory management
# ---------------------------------------------------------------------------

def build_inventory_pool(inv_list: list[dict], miners_db: dict[str, dict]) -> dict[tuple, dict]:
    """
    Return {(name.lower(), rarity): {"name", "rarity", "count", "power_th", "bonus_pct", "cells"}}.
    Keyed by (name, rarity) so different rarities of the same miner are separate entries
    and the bonus dedup in _delta_power uses the actual rarity.
    """
    pool: dict[tuple, dict] = {}
    for entry in inv_list:
        name   = entry["name"]
        rarity = entry.get("rarity") or "common"
        count  = entry.get("count", 0)
        if count <= 0:
            continue
        p_th  = entry.get("power_th")
        b_pct = entry.get("bonus_pct")
        # Fallback to miners_db for the correct rarity tier
        if p_th is None or b_pct is None:
            p_th2, b_pct2 = miner_stats(name, p_th, b_pct, rarity, miners_db)
            p_th  = p_th  or p_th2
            b_pct = b_pct if b_pct is not None else b_pct2
        # If rarity defaulted to common but power_th suggests a different tier,
        # re-guess from miners_db so the correct rarity flows through to swaps.
        if rarity == "common" and p_th:
            rec = miners_db.get(name.lower())
            if rec is None:
                rec = miners_db.get(_norm_name(name))
            if rec:
                best_r, best_diff = "common", float("inf")
                for r_name, tier in rec.get("rarities", {}).items():
                    ref = tier.get("power_th")
                    if ref and abs(ref - p_th) < best_diff:
                        best_diff = abs(ref - p_th)
                        best_r = r_name
                if best_r != "common" and best_diff < p_th * 0.05:  # within 5%
                    rarity = best_r
                    key = (name.lower(), rarity)  # rekey with corrected rarity
        cells = miner_cells(name, miners_db)
        key = (name.lower(), rarity)
        pool[key] = {
            "name":      name,
            "rarity":    rarity,
            "count":     count,
            "power_th":  p_th  or 0.0,
            "bonus_pct": b_pct or 0.0,
            "cells":     cells,
        }
    return pool


# ---------------------------------------------------------------------------
# Swap evaluation helpers
# ---------------------------------------------------------------------------

def _delta_power(placed: list[Miner],
                 remove: list[Miner],
                 add_name: list[str],
                 add_power: list[float],
                 add_bonus: list[float],
                 add_rarity: list[str] | None = None,
                 set_groups: list[dict] | None = None) -> float:
    """
    Compute the change in effective_power if we remove `remove` miners and
    add miners described by (add_name, add_power, add_bonus, add_rarity).
    Returns positive if the swap improves power.

    Uses (name, rarity) as the bonus dedup key — consistent with total_power().
    """
    if add_rarity is None:
        add_rarity = ["common"] * len(add_name)

    raw, bonus, eff = total_power(placed, set_groups)

    # Count remaining (name, rarity) pairs after removal — mirrors total_power() key
    remove_set = {id(m) for m in remove}
    pairs_after_remove: dict[tuple, int] = {}
    for m in placed:
        if id(m) not in remove_set:
            k = (m.name.lower(), m.rarity or "common")
            pairs_after_remove[k] = pairs_after_remove.get(k, 0) + 1

    new_raw = raw
    new_bonus = bonus
    bonus_already_deducted: set[tuple] = set()
    for m in remove:
        new_raw -= m.power_th
        # Bonus disappears only if this (name, rarity) pair has no copies left
        k = (m.name.lower(), m.rarity or "common")
        if pairs_after_remove.get(k, 0) == 0 and k not in bonus_already_deducted:
            new_bonus -= m.bonus_pct
            bonus_already_deducted.add(k)

    # Add incoming miners using their actual rarity for bonus dedup
    already_present = set(pairs_after_remove.keys())
    for aname, ap, ab, ar in zip(add_name, add_power, add_bonus, add_rarity):
        new_raw += ap
        k = (aname.lower(), ar)
        if k not in already_present:
            new_bonus += ab
            already_present.add(k)

    # Set-group bonus for the simulated new state
    new_set_pct = 0.0
    new_set_raw = 0.0
    if set_groups:
        new_placed_names  = [m.name.lower() for m in placed if id(m) not in remove_set]
        new_placed_names += [n.lower() for n in add_name]
        new_set_pct, new_set_raw = set_group_bonus(new_placed_names, set_groups)

    new_eff = new_raw * (1.0 + (new_bonus + new_set_pct) / 100.0) + new_set_raw
    return new_eff - eff


# ---------------------------------------------------------------------------
# Greedy optimiser
# ---------------------------------------------------------------------------

def find_best_swap(placed: list[Miner],
                   inv_pool: dict[str, dict],
                   rooms: list[dict],
                   miners_db: dict[str, dict],
                   set_groups: list[dict] | None = None,
                   ) -> tuple[float, list[Miner], list[dict]] | None:
    """
    Find the single best swap that improves effective power.

    Returns (delta, miners_to_remove, inv_candidates_to_add) or None.

    Swap types considered:
      A. 2-cell placed  →  1× 2-cell inventory
      B. 2-cell placed  →  2× 1-cell inventory (pair fills the slot)
      C. 1-cell pair placed  →  1× 2-cell inventory
      D. 1-cell pair placed  →  2× 1-cell inventory
      E. single 1-cell placed  →  1× 1-cell inventory  (leaves a free cell;
           paired with another rack's free-cell if available, else fill with
           best 1-cell from inventory)
    """
    best_delta = 0.0
    best_remove: list[Miner] = []
    best_add: list[dict] = []   # list of inv_pool entries to take

    # Index placed miners by (room, rack, slot) for easy slot-mate lookups
    slot_map: dict[tuple, list[Miner]] = {}
    for m in placed:
        key = (m.room_idx, m.rack_idx, m.slot_idx)
        slot_map.setdefault(key, []).append(m)

    inv_by_cells: dict[int, list[dict]] = {1: [], 2: []}
    for entry in inv_pool.values():
        c = entry["cells"]
        if c in inv_by_cells:
            inv_by_cells[c].append(entry)

    # Pre-sort inventory candidates by power descending for faster pruning
    for lst in inv_by_cells.values():
        lst.sort(key=lambda e: e["power_th"], reverse=True)

    # Iterate over every placed miner as a potential swap target
    evaluated: set[tuple] = set()  # avoid re-evaluating same slot twice

    for m in placed:
        slot_key = (m.room_idx, m.rack_idx, m.slot_idx)
        if slot_key in evaluated:
            continue
        evaluated.add(slot_key)

        slot_miners = slot_map.get(slot_key, [m])
        # If every miner in this slot is locked, skip the whole slot
        if all(sm.locked for sm in slot_miners):
            continue

        if all(sm.cells == 2 for sm in slot_miners):
            # ── Slot holds a single 2-cell miner ─────────────────────────
            assert len(slot_miners) == 1
            placed_miner = slot_miners[0]
            if placed_miner.locked:
                continue  # 2-cell locked — skip entirely

            # Type A: replace with a 2-cell inventory miner
            for cand in inv_by_cells[2]:
                if (cand["name"].lower() == placed_miner.name.lower() and
                        cand["rarity"] == (placed_miner.rarity or "common")):
                    continue   # exact same (name, rarity) — no benefit
                delta = _delta_power(
                    placed,
                    [placed_miner],
                    [cand["name"]],
                    [cand["power_th"]],
                    [cand["bonus_pct"]],
                    [cand["rarity"]],
                    set_groups=set_groups,
                )
                if delta > best_delta:
                    best_delta = delta
                    best_remove = [placed_miner]
                    best_add = [cand]

            # Type B: replace with two 1-cell inventory miners
            one_cell = inv_by_cells[1]
            for i, c1 in enumerate(one_cell):
                if c1["count"] < 1:
                    continue
                for c2 in one_cell[i:]:  # c2 can equal c1 if count ≥ 2
                    if c2 is c1 and c1["count"] < 2:
                        continue
                    delta = _delta_power(
                        placed,
                        [placed_miner],
                        [c1["name"], c2["name"]],
                        [c1["power_th"], c2["power_th"]],
                        [c1["bonus_pct"], c2["bonus_pct"]],
                        [c1["rarity"], c2["rarity"]],
                        set_groups=set_groups,
                    )
                    if delta > best_delta:
                        best_delta = delta
                        best_remove = [placed_miner]
                        best_add = [c1, c2]
                    # Since lists are sorted by power desc, if c1+c2 doesn't
                    # improve we can skip c2 candidates with even lower power.
                    # (Approximate prune — safe to leave out for correctness.)

        else:
            # ── Slot holds two 1-cell miners (or a lone 1-cell) ──────────
            pair = slot_miners  # 1 or 2 miners

            if len(pair) == 2:
                # Only allow replacing the full pair if neither is locked
                if not any(sm.locked for sm in pair):
                    # Type C: replace pair with a 2-cell inventory miner
                    for cand in inv_by_cells[2]:
                        if any(cand["name"].lower() == sm.name.lower() and
                               cand["rarity"] == (sm.rarity or "common")
                               for sm in pair):
                            continue
                        delta = _delta_power(
                            placed,
                            pair,
                            [cand["name"]],
                            [cand["power_th"]],
                            [cand["bonus_pct"]],
                            [cand["rarity"]],
                            set_groups=set_groups,
                        )
                        if delta > best_delta:
                            best_delta = delta
                            best_remove = list(pair)
                            best_add = [cand]

                    # Type D: replace pair with two better 1-cell inventory miners
                    one_cell = inv_by_cells[1]
                    pair_keys = {(sm.name.lower(), sm.rarity or "common") for sm in pair}
                    for i, c1 in enumerate(one_cell):
                        for c2 in one_cell[i:]:
                            if {(c1["name"].lower(), c1["rarity"]),
                                    (c2["name"].lower(), c2["rarity"])} == pair_keys:
                                continue
                            if c2 is c1 and c1["count"] < 2:
                                continue
                            delta = _delta_power(
                                placed,
                                pair,
                                [c1["name"], c2["name"]],
                                [c1["power_th"], c2["power_th"]],
                                [c1["bonus_pct"], c2["bonus_pct"]],
                                [c1["rarity"], c2["rarity"]],
                                set_groups=set_groups,
                            )
                            if delta > best_delta:
                                best_delta = delta
                                best_remove = list(pair)
                                best_add = [c1, c2]

            # Type E: swap one individual 1-cell miner (skip if locked)
            for target in pair:
                if target.locked:
                    continue
                for cand in inv_by_cells[1]:
                    if (cand["name"].lower() == target.name.lower() and
                            cand["rarity"] == (target.rarity or "common")):
                        continue
                    delta = _delta_power(
                        placed,
                        [target],
                        [cand["name"]],
                        [cand["power_th"]],
                        [cand["bonus_pct"]],
                        [cand["rarity"]],
                        set_groups=set_groups,
                    )
                    if delta > best_delta:
                        best_delta = delta
                        best_remove = [target]
                        best_add = [cand]

    if best_delta <= 0.0:
        return None

    return best_delta, best_remove, best_add


def apply_swap(placed: list[Miner],
               inv_pool: dict[str, dict],
               remove: list[Miner],
               add_entries: list[dict],
               miners_db: dict[str, dict]) -> list[Miner]:
    """
    Return a new placed list with `remove` replaced by miners from `add_entries`.
    Updates inv_pool counts in-place (removed from inventory, returned to inventory).
    """
    # Determine slot position for the first replacement miner
    first = remove[0]
    new_placed = [m for m in placed if m not in remove]

    for i, entry in enumerate(add_entries):
        # Consume from inventory
        key = (entry["name"].lower(), entry.get("rarity", "common"))
        inv_pool[key]["count"] -= 1
        if inv_pool[key]["count"] == 0:
            del inv_pool[key]

        cells = entry["cells"]
        slot_idx = first.slot_idx
        pos_in_slot = i if cells == 1 else 0
        new_m = Miner(
            entry["name"],
            entry["power_th"],
            entry["bonus_pct"],
            cells,
            entry.get("rarity"),          # carry actual rarity from inventory entry
            first.room_idx, first.rack_idx, slot_idx, pos_in_slot,
        )
        new_placed.append(new_m)

    # Return removed miners to inventory
    for m in remove:
        key = (m.name.lower(), m.rarity or "common")
        if key in inv_pool:
            inv_pool[key]["count"] += 1
        else:
            inv_pool[key] = {
                "name":      m.name,
                "rarity":    m.rarity or "common",
                "count":     1,
                "power_th":  m.power_th,
                "bonus_pct": m.bonus_pct,
                "cells":     m.cells,
            }

    return new_placed


# ---------------------------------------------------------------------------
# Swap chain collapsing
# ---------------------------------------------------------------------------

def compute_swaps(
    original: list[Miner],
    final: list[Miner],
    set_groups: list[dict] | None = None,
) -> list[dict]:
    """
    Diff original vs final, applying changes sequentially to compute a marginal
    effective-power delta for each physical swap.

    Each returned dict contains:
      room, rack, slot      — 1-based location (slot = pair-slot index)
      rack_positions        — 0-based miner indices within the rack list
      remove                — names of miners to take out
      add                   — names of miners to put in
      delta_eff             — effective-power gain for this swap (Th/s)
    """
    orig_by_slot: dict[tuple, list[Miner]] = {}
    for m in original:
        k = (m.room_idx, m.rack_idx, m.slot_idx)
        orig_by_slot.setdefault(k, []).append(m)

    final_by_slot: dict[tuple, list[Miner]] = {}
    for m in final:
        k = (m.room_idx, m.rack_idx, m.slot_idx)
        final_by_slot.setdefault(k, []).append(m)

    # Identify changed pair-slots (by miner name set)
    all_keys = set(orig_by_slot) | set(final_by_slot)
    changed_keys = sorted(
        k for k in all_keys
        if sorted(m.name for m in orig_by_slot.get(k, [])) !=
           sorted(m.name for m in final_by_slot.get(k, []))
    )

    # Apply each physical swap sequentially, measuring marginal delta each time
    current = list(original)
    swaps: list[dict] = []
    for slot_key in changed_keys:
        room_i, rack_i, slot_i = slot_key
        orig_ms = orig_by_slot.get(slot_key, [])
        add_ms  = final_by_slot.get(slot_key, [])

        _, _, eff_before = total_power(current, set_groups)
        remove_ids = {id(m) for m in current
                      if (m.room_idx, m.rack_idx, m.slot_idx) == slot_key}
        current = [m for m in current if id(m) not in remove_ids] + add_ms
        _, _, eff_after = total_power(current, set_groups)

        swaps.append({
            "room":           room_i + 1,
            "rack":           rack_i + 1,
            "slot":           slot_i + 1,
            "rack_positions": sorted(m.miner_rank for m in orig_ms),
            "remove":         [{"name": m.name, "rarity": m.rarity or "common"} for m in orig_ms],
            "add":            [{"name": m.name, "rarity": m.rarity or "common"} for m in add_ms],
            "delta_eff":      round(eff_after - eff_before, 1),
        })
    return swaps


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> None:
    print("Loading data...")
    miners_db  = load_miners_data()
    inv_list   = load_inventory()
    rooms      = load_all_rooms()

    if not rooms:
        print("No placed_room*.json files found. Run main.py first.")
        return
    if not inv_list:
        print("inventory.json not found or empty. Run main.py first.")
        return

    locked_set = load_locked()
    set_groups = load_set_groups()

    # Miners that belong to a set group are NOT hard-locked.
    # Locking is used as a UI mechanism to define which miners are set members;
    # the optimizer must still be free to swap them out when the set bonus doesn't
    # justify keeping them.  Remove set members from the locked set so the
    # optimizer can evaluate them on merit (raw power + whatever set bonus applies).
    if locked_set and set_groups:
        _raw_locked = json.loads(LOCKED_JSON.read_text(encoding="utf-8"))
        _set_member_keys: set[tuple[int, int, str]] = {   # (room_idx, rack_idx, name_lower)
            (sg["room"] - 1, sg["rack"], n.lower())
            for sg in set_groups
            for n in sg.get("member_names", [])
        }
        locked_set = {
            (e["room"] - 1, e["rack"], e["miner_idx"])
            for e in _raw_locked
            if (e["room"] - 1, e["rack"], e["name"].lower()) not in _set_member_keys
        }

    if locked_set:
        print(f"Loaded {len(locked_set)} hard-locked miner(s) from {LOCKED_JSON}")
    if set_groups:
        _n_set_members = sum(len(sg.get("member_names", [])) for sg in set_groups)
        print(f"Loaded {len(set_groups)} set group(s) ({_n_set_members} member miner(s)) — swappable by optimizer")

    original_placed = build_state(rooms, miners_db, locked_set)
    inv_pool = build_inventory_pool(inv_list, miners_db)

    raw0, bonus0, eff0 = total_power(original_placed, set_groups)
    set_pct_0, set_raw_0 = (
        set_group_bonus([m.name.lower() for m in original_placed], set_groups)
        if set_groups else (0.0, 0.0)
    )
    set_bonus = load_set_bonus()

    print(f"\nCurrent state:")
    print(f"  Placed miners : {len(original_placed)}")
    print(f"  Raw power     : {raw0:>12,.1f} Th/s")
    print(f"  Miner bonus   : {bonus0:>12.2f} %")
    if set_groups:
        pnames0 = [m.name.lower() for m in original_placed]
        for sg in set_groups:
            mbrs   = {n.lower() for n in sg.get("member_names", [])}
            cnt    = sum(1 for n in pnames0 if n in mbrs)
            active = sum(1 for t in sg.get("thresholds", []) if cnt >= (t.get("min_members") or t.get("count", 0)))
            print(f"  Set '{sg['name']}': {cnt}/{len(mbrs)} members, "
                  f"{active} tier(s) active")
        if set_pct_0:
            print(f"  Set pct bonus : {set_pct_0:>12.2f} %")
        if set_raw_0:
            print(f"  Set raw bonus : {set_raw_0:>12,.1f} Th/s")
    print(f"  Effective     : {eff0:>12,.1f} Th/s")

    set_bonus = _prompt_set_bonus(raw0, bonus0 + set_pct_0, set_bonus)
    adj_total_pct0 = bonus0 + set_pct_0 + set_bonus
    adj0 = raw0 * (1.0 + adj_total_pct0 / 100.0) + set_raw_0
    if set_bonus != 0.0:
        print(f"  Adj total pct : {adj_total_pct0:>10.2f} %  "
              f"(manual offset: {set_bonus:+.2f}%)")
        print(f"  Adjusted eff  : {adj0:>12,.1f} Th/s")
    print(f"\nInventory      : {sum(e['count'] for e in inv_pool.values())} miners "
          f"({len(inv_pool)} unique)\n")

    # Greedy optimisation loop
    placed = list(original_placed)
    iteration = 0

    while True:
        result = find_best_swap(placed, inv_pool, rooms, miners_db, set_groups)
        if result is None:
            print("No further improvements found.")
            break
        delta, remove, add_entries = result
        add_names = ", ".join(e["name"] for e in add_entries)
        rem_names = ", ".join(m.name for m in remove)
        print(f"  Swap {iteration+1}: remove [{rem_names}]  +{delta:,.1f} Th/s")
        print(f"           add    [{add_names}]")
        placed = apply_swap(placed, inv_pool, remove, add_entries, miners_db)
        iteration += 1

    if iteration == 0:
        print("Room is already optimal given available inventory.")
        return

    raw1, bonus1, eff1 = total_power(placed, set_groups)
    gain = eff1 - eff0
    set_pct_1, set_raw_1 = (
        set_group_bonus([m.name.lower() for m in placed], set_groups)
        if set_groups else (0.0, 0.0)
    )
    adj_total_pct1 = bonus1 + set_pct_1 + set_bonus
    adj1 = raw1 * (1.0 + adj_total_pct1 / 100.0) + set_raw_1
    gain_str     = f"+{gain:,.1f}"         if gain >= 0 else f"{gain:,.1f}"
    adj_gain_str = f"+{adj1 - adj0:,.1f}"  if adj1 >= adj0 else f"{adj1 - adj0:,.1f}"
    print(f"\nOptimised state:")
    print(f"  Raw power     : {raw1:>12,.1f} Th/s  (was {raw0:,.1f})")
    print(f"  Miner bonus   : {bonus1:>12.2f} %  (was {bonus0:.2f}%)")
    print(f"  Effective     : {eff1:>12,.1f} Th/s  ({gain_str})")
    if set_bonus != 0.0 or set_groups:
        print(f"  Adj total pct : {adj_total_pct1:>10.2f} %  "
              f"(was {adj_total_pct0:.2f}%, manual offset: {set_bonus:+.2f}%)")
        print(f"  Adj effective : {adj1:>12,.1f} Th/s  ({adj_gain_str})")

    # Collapse chains → minimal physical swaps
    swaps = compute_swaps(original_placed, placed, set_groups)
    print(f"\n=== {len(swaps)} physical swap(s) to make ===")
    for idx, s in enumerate(swaps, 1):
        rem   = " + ".join(f"{e['name']} ({e['rarity']})" for e in s["remove"])
        add   = " + ".join(f"{e['name']} ({e['rarity']})" for e in s["add"])
        d     = s["delta_eff"]
        d_str = f"+{d:,.1f}" if d >= 0 else f"{d:,.1f}"
        print(f"  [{idx}] Room {s['room']}  Rack {s['rack']}  Positions {s['rack_positions']}:")
        print(f"    Remove : {rem}")
        print(f"    Add    : {add}")
        print(f"    Gain   : {d_str} Th/s")

    # Save swap plan for the visualiser
    swaps_path = _ROOT / "data/optimizer_swaps.json"
    swaps_path.write_text(
        json.dumps(swaps, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nSwap plan saved → {swaps_path}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
