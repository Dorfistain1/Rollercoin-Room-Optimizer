"""
Miner scraper for minaryganar.com — API-based (v2)

Uses the public JSON API at https://api.minaryganar.com/api/public/miners
instead of scraping HTML listing pages.  The API was discovered from the
site's own JavaScript bundle (miners.api-*.js).

Endpoints used:
  GET /api/public/miners?page=N&per_page=N   — paginated full list
  GET /api/public/miners/{slug}              — single miner by slug
  GET /api/public/miners/search?q=term&limit=N — search

Output:
  ./miners/               - Downloaded miner images
  ./miners/miners_data.json - All miner data with power/bonus per rarity

Rarity / merge-level mapping (API level 1 = base miner):
  Level 1 → common      (base power/bonus from the miner record itself)
  Level 2 → uncommon
  Level 3 → rare
  Level 4 → epic
  Level 5 → legendary
  Level 6 → unreal
"""

import requests
import json
import os
import re
import time
from pathlib import Path

API_BASE    = "https://api.minaryganar.com/api/public/miners"
IMAGE_BASE  = "https://api.minaryganar.com/assets/"
DETAIL_PAGE = "https://minaryganar.com/rollercoin/miners/"

_ROOT = Path(__file__).parent.parent   # roomBuilder/
MINERS_DIR  = str(_ROOT / "miners")
OUTPUT_JSON = str(_ROOT / "miners/miners_data.json")

RARITIES = ["common", "uncommon", "rare", "epic", "legendary", "unreal"]

# API merge level → rarity name  (level 1 is the base miner = common)
_LEVEL_TO_RARITY: dict[int, str] = {i + 1: r for i, r in enumerate(RARITIES)}

MATCH_LOG = str(_ROOT / "data/match_log.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://minaryganar.com/",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_filename(name: str) -> str:
    """Turn a miner name into a filename-safe string (spaces → underscores)."""
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_")


# Power unit multipliers relative to TH/s  (kept for legacy parse_power usage)
_POWER_UNITS: dict[str, float] = {
    "gh": 0.001,
    "th": 1.0,
    "ph": 1_000.0,
    "eh": 1_000_000.0,
    "mh": 0.000_001,
    "kh": 0.000_000_001,
}


def parse_power(text: str) -> float | None:
    """Extract power from a human-readable string and convert to TH/s."""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*([KMGTEП][Hh])", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if value == 0.0:
        return None
    unit = m.group(2).lower()
    multiplier = _POWER_UNITS.get(unit, 1.0)
    return value * multiplier


def parse_bonus(text: str) -> float | None:
    """Extract the numeric % value from text like '4.00 %' or '0.50%'."""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*%", text)
    return float(m.group(1).replace(",", "")) if m else None


def name_to_slug(name: str) -> str:
    """Convert a miner name to a minaryganar.com URL slug (lowercase, spaces→hyphens)."""
    slug = name.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug


def _image_stem_to_slug(image_filename: str) -> str:
    """e.g. 'Valhallas_Vault.gif' → 'valhallas-vault'"""
    stem = re.sub(r"\.\w+$", "", image_filename)
    return stem.lower().replace("_", "-").strip("-")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_power_to_th(power_str: str | None) -> float | None:
    """Convert API power value (GH/s, may be scientific notation) to TH/s.

    The minaryganar.com API stores power in GH/s as a string like "1.8000E+8".
    Returns None for missing or zero values.
    """
    if power_str is None:
        return None
    try:
        gh = float(power_str)
        if gh <= 0:
            return None
        return gh * 0.001   # 1 GH = 0.001 TH
    except (ValueError, TypeError):
        return None


def api_bonus_to_pct(bonus_str: str | None) -> float | None:
    """Convert API bonus string to float percentage.

    Returns None only for missing/unparseable values; 0.0 is a valid bonus.
    """
    if bonus_str is None:
        return None
    try:
        return float(bonus_str)
    except (ValueError, TypeError):
        return None


def _api_to_record(item: dict, download_images: bool = True) -> dict:
    """Convert a single API miner item to the internal miners_data.json format.

    API item shape (simplified):
      { "name": "Silent Spiral", "slug": "silent-spiral", "cells": 2,
        "power": "1.8000E+8",  # GH/s — base (common) power
        "bonus": "8.00",        # % — base (common) bonus
        "image_path": "rollercoin/miners/silent_spiral.gif",
        "merges": [
          {"level": 2, "power": "4.7500E+8", "bonus": "25.00", ...},
          {"level": 3, "power": "1.3E+9",    "bonus": "60.00", ...},
          ...
        ] }
    """
    name       = item.get("name", "")
    slug       = item.get("slug", name_to_slug(name))
    cells      = item.get("cells")
    image_path = item.get("image_path", "")   # e.g. "rollercoin/miners/silent_spiral.gif"

    img_filename = os.path.basename(image_path) if image_path else f"{safe_filename(name)}.gif"
    img_url      = f"{IMAGE_BASE}{image_path}" if image_path else None

    # Base (common) rarity
    rarities: dict[str, dict] = {
        "common": {
            "power_th":  api_power_to_th(item.get("power")),
            "bonus_pct": api_bonus_to_pct(item.get("bonus")),
        }
    }

    # Merge levels → higher rarity tiers
    for merge in item.get("merges", []):
        level  = merge.get("level", 0)
        rarity = _LEVEL_TO_RARITY.get(level)
        if rarity and rarity != "common":
            rarities[rarity] = {
                "power_th":  api_power_to_th(merge.get("power")),
                "bonus_pct": api_bonus_to_pct(merge.get("bonus")),
            }

    # Fill any missing rarity tiers with None
    for r in RARITIES:
        rarities.setdefault(r, {"power_th": None, "bonus_pct": None})

    # Download image if requested
    if download_images and img_url and img_filename:
        img_path = os.path.join(MINERS_DIR, img_filename)
        if not os.path.exists(img_path):
            os.makedirs(MINERS_DIR, exist_ok=True)
            print(f"    Downloading image: {img_filename}")
            download_image(img_url, img_path)

    validation = validate_rarity_scaling(rarities)

    rec: dict = {
        "name":       name,
        "image":      img_filename,
        "rarities":   {r: rarities[r] for r in RARITIES},
        "_validation": validation,
        "_detail_url": f"{DETAIL_PAGE}{slug}",
    }
    if cells is not None:
        rec["cells"] = cells

    return rec


def _fetch_by_api_slug(slug: str, wait_on_retry: bool = True) -> dict | None:
    """Fetch a single miner from the API by its URL slug (e.g. 'silent-spiral').

    Returns an internal-format record, or None if not found.
    """
    url = f"{API_BASE}/{requests.utils.quote(slug, safe='')}"
    print(f"  API: {url}")
    response = fetch_with_retry(url, retries=2, wait=wait_on_retry)
    if response is None:
        return None
    try:
        item = response.json()
        if not isinstance(item, dict) or "name" not in item:
            return None
        return _api_to_record(item, download_images=True)
    except Exception as exc:
        print(f"  [!] Failed to parse API response: {exc}")
        return None


def _search_api(query: str, limit: int = 10, wait_on_retry: bool = True) -> dict | None:
    """Search the API for a miner by name.  Returns the best-matching record, or None."""
    url = f"{API_BASE}/search?q={requests.utils.quote(query)}&limit={limit}"
    print(f"  API search: {url}")
    response = fetch_with_retry(url, retries=2, wait=wait_on_retry)
    if response is None:
        return None
    try:
        data = response.json()
        # Search endpoint may return a list or {"items": [...]}
        results: list[dict] = data if isinstance(data, list) else data.get("items", [])
        if not results:
            return None
        # Prefer exact name match, fall back to first result
        target_lower = query.lower()
        best = next(
            (r for r in results if r.get("name", "").lower() == target_lower),
            results[0],
        )
        return _api_to_record(best, download_images=True)
    except Exception as exc:
        print(f"  [!] Search API error: {exc}")
        return None


def download_image(url: str, dest_path: str) -> bool:
    """Download a file from *url* and write it to *dest_path*."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return True
    except KeyboardInterrupt:
        print(f"    [!] Image download interrupted, skipping: {url}")
        return False
    except Exception as exc:
        print(f"    [!] Image download failed ({url}): {exc}")
        return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_rarity_scaling(rarities: dict) -> dict:
    """
    Check that power and bonus are non-decreasing across available rarities.
    Missing rarities (None) and 0-value tiers are excluded from the check.
    Returns a dict with two boolean flags and the count of populated rarities.
    """
    powers = [
        rarities[r]["power_th"]
        for r in RARITIES
        if rarities.get(r, {}).get("power_th") is not None
        and rarities[r]["power_th"] > 0
    ]
    bonuses = [
        rarities[r]["bonus_pct"]
        for r in RARITIES
        if rarities.get(r, {}).get("bonus_pct") is not None
        and rarities[r]["bonus_pct"] > 0
    ]

    power_ok = all(powers[i] <= powers[i + 1] for i in range(len(powers) - 1))
    bonus_ok = all(bonuses[i] <= bonuses[i + 1] for i in range(len(bonuses) - 1))
    populated = sum(1 for r in RARITIES if rarities.get(r, {}).get("power_th") is not None)

    return {"power_scaling_ok": power_ok, "bonus_scaling_ok": bonus_ok, "rarities_populated": populated}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_with_retry(url: str, retries: int = 4, backoff: float = 5.0, wait: bool = True) -> requests.Response | None:
    """GET *url* with up to *retries* attempts and exponential backoff.

    404 Not Found is treated as permanent after 2 attempts.
    If `wait` is False, no sleep between retries.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    delay = backoff
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            print(f"  [Attempt {attempt}/{retries}] HTTP {status}: {exc}")
            if status == 404 and attempt >= 2:
                break   # permanent
            if attempt < retries:
                print(f"  Retrying in {delay:.0f}s...")
                if wait:
                    time.sleep(delay)
                delay *= 2
        except Exception as exc:
            print(f"  [Attempt {attempt}/{retries}] Error: {exc}")
            if attempt < retries:
                print(f"  Retrying in {delay:.0f}s...")
                if wait:
                    time.sleep(delay)
                delay *= 2
    return None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def save_json(all_miners: list[dict]) -> None:
    os.makedirs(MINERS_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_miners, f, indent=2, ensure_ascii=False)


def _log_match(slug: str, html_name: str, found_name: str, match: dict) -> None:
    """Record a miner found via secondary/fuzzy lookup for user verification."""
    entries: list[dict] = []
    if os.path.exists(MATCH_LOG):
        try:
            with open(MATCH_LOG, encoding="utf-8") as fh:
                entries = json.load(fh)
        except Exception:
            entries = []

    existing = next((e for e in entries if e.get("slug") == slug), None)
    if existing and existing.get("status") == "confirmed":
        return
    if existing and existing.get("status") == "rejected":
        img_file = existing.get("image", "")
        ext = os.path.splitext(img_file)[1] if img_file else ".gif"
        alias_path = os.path.join(MINERS_DIR, f"{slug}{ext}")
        if os.path.exists(alias_path):
            return
        print(f"  [!] Slug alias '{slug}{ext}' missing after rejection — re-queuing for verification")

    entries = [e for e in entries if e.get("slug") != slug]
    entries.append({
        "slug":       slug,
        "html_name":  html_name,
        "found_name": found_name,
        "image":      match.get("image", ""),
        "cells":      match.get("cells"),
        "rarities":   match.get("rarities", {}),
        "status":     "pending",
    })
    with open(MATCH_LOG, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main loop  (full scrape via paginated API)
# ---------------------------------------------------------------------------

def main() -> None:
    all_miners: list[dict] = []
    page = 1
    per_page = 100   # request larger pages; API may cap it

    print("Starting miner scrape from minaryganar.com API...\n")

    try:
        while True:
            url = f"{API_BASE}?page={page}&per_page={per_page}"
            print(f"[Page {page}] {url}")

            response = fetch_with_retry(url)
            if response is None:
                print(f"  API request failed — stopping after page {page - 1}.")
                break

            try:
                data = response.json()
            except Exception as exc:
                print(f"  [!] Could not parse API response: {exc}")
                break

            items = data.get("items", [])
            if not items:
                print(f"  No miners found — stopping after page {page - 1}.")
                break

            os.makedirs(MINERS_DIR, exist_ok=True)
            for item in items:
                rec = _api_to_record(item, download_images=True)
                all_miners.append(rec)
                vld = rec.get("_validation", {})
                flags = ""
                if not vld.get("power_scaling_ok", True):
                    flags += " [!power]"
                if not vld.get("bonus_scaling_ok", True):
                    flags += " [!bonus]"
                print(f"  + {rec['name']}{flags}")

            print(f"  +{len(items)} miners  |  total so far: {len(all_miners)}")
            save_json(all_miners)

            if not data.get("has_next", False):
                print(f"  Reached last page (total: {data.get('total', '?')}).")
                break

            page += 1
            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    except Exception as exc:
        print(f"\n[!] Unexpected error: {exc}")
    finally:
        if all_miners:
            save_json(all_miners)
            print(f"\nSaved {len(all_miners)} miners to {OUTPUT_JSON}")
        else:
            print("\nNo miners collected, nothing saved.")



# ---------------------------------------------------------------------------
# Single-miner lookup
# ---------------------------------------------------------------------------

def _norm_stem(s: str) -> str:
    """Canonical image-stem key: lowercase, apostrophes removed, hyphens→underscores."""
    s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
    return re.sub(r"[^\w]+", "_", s).strip("_")


# ---------------------------------------------------------------------------
# Single-miner lookup (API-based)
# ---------------------------------------------------------------------------

def lookup_miner(
    name: str,
    expected_name: str = "",
    log_slug: str | None = None,
    wait_on_retry: bool = True,
) -> dict | None:
    """Fetch a single miner by name from the minaryganar.com API.

    Strategy:
      1. Try exact slug lookup   → GET /api/public/miners/{name-to-slug(name)}
      2. Fall back to search API → GET /api/public/miners/search?q={name}

    The result is merged into miners_data.json. Returns the record or None.

    expected_name: display name from game HTML (used to detect mismatches).
    log_slug:      key written to match_log.json (defaults to _norm_stem of name).
    """
    print(f"Looking up '{name}' via API...")

    api_slug = name_to_slug(name)
    match = _fetch_by_api_slug(api_slug, wait_on_retry=wait_on_retry)
    used_secondary = False

    if match is None:
        print(f"  Slug lookup failed — trying search API...")
        match = _search_api(name, limit=10, wait_on_retry=wait_on_retry)
        if match:
            used_secondary = True

    if match is None:
        print(f"  [!] Could not find '{name}' on minaryganar.com.")
        return None

    print(f"  Found: {match['name']}")

    # Log for verification when a secondary search was used or name mismatch detected
    _searched  = expected_name or name
    _slug_key  = log_slug if log_slug is not None else _norm_stem(_searched)
    name_mismatch = bool(expected_name and _norm_stem(match["name"]) != _norm_stem(expected_name))
    if used_secondary or name_mismatch:
        _log_match(_slug_key, _searched, match["name"], match)
        if name_mismatch:
            print(f"  [!] Name mismatch flagged for verification: '{_searched}' -> '{match['name']}'")
        else:
            print(f"  [!] Secondary search result flagged for verification: '{_searched}'")

    # Merge into miners_data.json
    existing: list[dict] = []
    if os.path.exists(OUTPUT_JSON):
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            existing = json.load(f)

    existing_lower = {m["name"].lower(): i for i, m in enumerate(existing)}
    idx = existing_lower.get(match["name"].lower())
    if idx is not None:
        existing[idx] = match
        print(f"  Updated existing entry in {OUTPUT_JSON}")
    else:
        existing.append(match)
        print(f"  Added new entry to {OUTPUT_JSON}")

    save_json(existing)
    return match


def lookup_miner_by_slug(
    slug: str, html_name: str = "", wait_on_retry: bool = True
) -> dict | None:
    """Like lookup_miner() but takes the raw game slug (e.g. 'valhallas_vault').

    After finding the miner, saves the image under '{slug}.{ext}' so that
    load_first_frame(slug) always finds it on the first exact-match attempt —
    regardless of what the third-party site calls the file.

    html_name: display name from game HTML — used to detect when the
    minaryganar.com match has a different name (flagged for review).
    """
    slug = slug.replace("-", "_")
    display = " ".join(w.capitalize() for w in slug.split("_"))
    effective_html = html_name or display

    match = lookup_miner(display, expected_name=effective_html, log_slug=slug,
                         wait_on_retry=wait_on_retry)
    if match is None:
        return None

    # Check whether this slug has been marked as rejected in match_log
    _slug_rejected = False
    if os.path.exists(MATCH_LOG):
        try:
            with open(MATCH_LOG, encoding="utf-8") as _mf:
                _ml = json.load(_mf)
            _slug_rejected = any(
                e.get("slug") == slug and e.get("status") == "rejected"
                for e in _ml
            )
        except Exception:
            pass

    existing_img = match.get("image", "")
    if existing_img:
        ext = os.path.splitext(existing_img)[1]          # e.g. ".gif"
        slug_filename = f"{slug}{ext}"
        existing_path = os.path.join(MINERS_DIR, existing_img)
        slug_path = os.path.join(MINERS_DIR, slug_filename)

        if not os.path.exists(slug_path) and os.path.exists(existing_path):
            import shutil
            shutil.copy2(existing_path, slug_path)
            print(f"  Aliased image -> '{slug_filename}'")

        if not _slug_rejected and os.path.exists(slug_path) and match["image"] != slug_filename:
            match["image"] = slug_filename
            if os.path.exists(OUTPUT_JSON):
                with open(OUTPUT_JSON, encoding="utf-8") as f:
                    existing_data = json.load(f)
                for i, m in enumerate(existing_data):
                    if m["name"].lower() == match["name"].lower():
                        existing_data[i]["image"] = slug_filename
                        break
                save_json(existing_data)
        elif _slug_rejected:
            print(f"  Slug '{slug}' is rejected — skipping image alias update on '{match['name']}'.")

    return match


# ---------------------------------------------------------------------------
# Backfill: re-fetch any miner missing data via the API
# ---------------------------------------------------------------------------

def backfill_cells() -> int:
    """For every miner in miners_data.json that is missing 'cells', fetch it
    from the API and fill it in.  Returns the number of miners updated.

    The API now returns cells directly, so no detail-page scraping needed.
    """
    if not os.path.exists(OUTPUT_JSON):
        print("[backfill_cells] No miners_data.json found, nothing to backfill.")
        return 0

    with open(OUTPUT_JSON, encoding="utf-8") as f:
        miners = json.load(f)

    updated = 0
    for i, miner in enumerate(miners):
        if miner.get("cells") is not None:
            continue
        name = miner["name"]
        print(f"  [{i+1}/{len(miners)}] Fetching cells for: {name}")
        rec = _fetch_by_api_slug(name_to_slug(name), wait_on_retry=True)
        if rec is not None and rec.get("cells") is not None:
            miner["cells"] = rec["cells"]
            print(f"    -> Cells: {miner['cells']}")
            updated += 1
            save_json(miners)
        else:
            print(f"    [!] Not found via API — skipping")
        time.sleep(0.5)

    print(f"[backfill_cells] Done. {updated} miners updated.")
    return updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--backfill-cells" in sys.argv:
        backfill_cells()
    elif len(sys.argv) > 1:
        miner_name = " ".join(sys.argv[1:])
        result = lookup_miner(miner_name)
        if result:
            print(f"\nRarities for {result['name']}:")
            for rarity, data in result["rarities"].items():
                if data.get("power_th") is not None:
                    print(f"  {rarity:<12} {data['power_th']:>10,.0f} Th  {data['bonus_pct']:>6.2f}%")
    else:
        main()
