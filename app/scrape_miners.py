"""
Miner scraper for minaryganar.com
Scrapes all miner cards across all pages, downloads images, and saves data to JSON.

Output:
  ./miners/          - Downloaded miner images (named after the miner)
  ./miners/miners_data.json - All miner data with power/bonus per rarity

Rarity order (lowest → highest):
  common, uncommon, rare, epic, legendary, unreal
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import re
import time
from pathlib import Path

BASE_URL = "https://minaryganar.com/miner/"
_ROOT = Path(__file__).parent.parent   # roomBuilder/
MINERS_DIR  = str(_ROOT / "miners")
OUTPUT_JSON = str(_ROOT / "miners/miners_data.json")

RARITIES = ["common", "uncommon", "rare", "epic", "legendary", "unreal"]
MATCH_LOG = str(_ROOT / "data/match_log.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://minaryganar.com/miner/",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_filename(name: str) -> str:
    """Turn a miner name into a filename-safe string (spaces → underscores)."""
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_")


# Power unit multipliers relative to TH/s
_POWER_UNITS: dict[str, float] = {
    "gh": 0.001,       # 1 GH  = 0.001 TH
    "th": 1.0,
    "ph": 1_000.0,
    "eh": 1_000_000.0,
    "mh": 0.000_001,   # 1 MH  = 0.000001 TH (unlikely but safe)
    "kh": 0.000_000_001,
}


def parse_power(text: str) -> float | None:
    """Extract power and convert to TH/s. Handles GH, TH, PH, EH, MH, KH.
    Returns None for 0 values — the site uses '0 Th' as a placeholder for
    rarity tiers that have no published data.
    """
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*([KMGTEП][Hh])", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if value == 0.0:
        return None  # site placeholder; real miners never have 0 power
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


def fetch_cells(name: str, image_filename: str | None = None,
                detail_url: str | None = None) -> int | None:
    """
    Fetch the individual miner detail page and return the cell count, or None.
    URL: https://minaryganar.com/miner/<slug>/

    detail_url: direct URL from the listing card (most reliable — bypasses slug guessing).
    """
    if detail_url:
        url = detail_url
    elif image_filename:
        slug = _image_stem_to_slug(image_filename)
        url = f"{BASE_URL}{slug}/"
    else:
        slug = name_to_slug(name)
        url = f"{BASE_URL}{slug}/"
    response = fetch_with_retry(url)
    if response is None:
        return None
    soup = BeautifulSoup(response.text, "lxml")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Cells\s*:\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def fetch_miner_by_game_slug(game_slug: str) -> dict | None:
    """
    Build a full miner record by going directly to the miner detail page at:
      https://minaryganar.com/miner/<game-slug>/

    Used when the site search returns no results (e.g. miners the search
    index doesn't list but whose detail pages still exist).

    game_slug — underscore form from the game HTML, e.g. 'valhallas_vault'
    Returns a record in the same format as scrape_page(), or None.
    """
    url_slug = game_slug.lower().replace("_", "-")
    url = f"{BASE_URL}{url_slug}/"
    print(f"  Direct detail page: {url}")
    response = fetch_with_retry(url)
    if response is None:
        return None

    soup = BeautifulSoup(response.text, "lxml")
    text = soup.get_text(" ", strip=True)

    # Name from <title>  e.g. "Valhalla's Vault – Piero"
    name = None
    if soup.title:
        raw_title = soup.title.string or ""
        name = raw_title.split("–")[0].split("|")[0].strip() or None

    if not name:
        print(f"  [!] Could not parse name from detail page title")
        return None

    # Cells
    cells_m = re.search(r"Cells\s*:\s*(\d+)", text, re.IGNORECASE)
    cells = int(cells_m.group(1)) if cells_m else None

    # Image — try og:image or first wp-post-image
    img_url = None
    og = soup.find("meta", property="og:image")
    if og:
        img_url = og.get("content")
    if not img_url:
        img_el = soup.find("img", class_=lambda c: c and "wp-post-image" in c)
        if img_el:
            img_url = img_el.get("src") or img_el.get("data-src")

    # Power/bonus — try using the listing-page scraper helpers on the page
    rarities: dict[str, dict] = {}
    # Look for the same brxe-hyyqbi / brxe-elnjof structure used on listing pages
    common_container = soup.find(
        lambda tag: tag.has_attr("class") and "brxe-hyyqbi" in tag["class"]
    )
    if common_container:
        els = common_container.find_all(
            lambda tag: tag.has_attr("class") and "brxe-text-basic" in tag["class"]
        )
        common_power, common_bonus = parse_power_bonus(els)
        rarities["common"] = {"power_th": common_power, "bonus_pct": common_bonus}

    dropdown_ul = soup.find(
        lambda tag: tag.has_attr("class") and "brxe-pbvklb" in tag["class"]
    )
    dropdown = (
        dropdown_ul.find(
            lambda tag: tag.has_attr("class") and "brxe-elnjof" in tag["class"]
        )
        if dropdown_ul
        else None
    )
    if dropdown:
        pairs = extract_rarity_pairs(dropdown)
        other_rarities = ["uncommon", "rare", "epic", "legendary", "unreal"]
        for idx, (pwr, bon) in enumerate(pairs[:5]):
            rarities[other_rarities[idx]] = {"power_th": pwr, "bonus_pct": bon}

    # Fallback: some miners use a simple "Basic power / Basic bonus" table
    # instead of the CSS-class rarity structure used above.
    if not rarities.get("common", {}).get("power_th"):
        page_text = soup.get_text(" ", strip=True)
        pwr_m = re.search(
            r"Basic\s+power[^\d]*?([\d,.]+)\s*(EH|PH|TH|GH|MH|KH)",
            page_text, re.IGNORECASE,
        )
        bon_m = re.search(
            r"Basic\s+bonus[^\d]*?([\d,.]+)\s*%", page_text, re.IGNORECASE
        )
        if pwr_m or bon_m:
            raw_pwr = parse_power(f"{pwr_m.group(1)} {pwr_m.group(2)}") if pwr_m else None
            raw_bon = float(bon_m.group(1).replace(",", ".")) if bon_m else None
            rarities["common"] = {"power_th": raw_pwr, "bonus_pct": raw_bon}
            print(f"  [fallback] Basic table: power={raw_pwr} TH, bonus={raw_bon}%")

    # Download image
    safe_name = safe_filename(name)
    ext = "gif"
    img_filename = f"{safe_name}.{ext}"
    if img_url:
        ext_m = re.search(r"\.(png|jpg|jpeg|gif|webp)", img_url, re.IGNORECASE)
        if ext_m:
            ext = ext_m.group(1).lower()
        img_filename = f"{safe_name}.{ext}"
        img_path = os.path.join(MINERS_DIR, img_filename)
        if not os.path.exists(img_path):
            os.makedirs(MINERS_DIR, exist_ok=True)
            print(f"  Downloading image: {img_filename}")
            download_image(img_url, img_path)
    else:
        img_filename = f"{safe_filename(game_slug)}.gif"   # placeholder name

    validation = validate_rarity_scaling(rarities) if rarities else {}

    record = {
        "name": name,
        "image": img_filename,
        "rarities": {
            r: rarities.get(r, {"power_th": None, "bonus_pct": None})
            for r in RARITIES
        },
        "_validation": validation,
    }
    if cells is not None:
        record["cells"] = cells

    return record


    return int(m.group(1)) if m else None


def parse_power_bonus(elements) -> tuple[float | None, float | None]:
    """
    Given a list of BeautifulSoup tags whose text contains either a Th or %
    value, return (power_th, bonus_pct).
    """
    power = None
    bonus = None
    for el in elements:
        text = el.get_text(strip=True)
        if re.search(r"[KMGTEП][Hh]", text) and power is None:
            power = parse_power(text)
        elif "%" in text and bonus is None:
            bonus = parse_bonus(text)
    return power, bonus


def extract_rarity_pairs(container) -> list[tuple[float | None, float | None]]:
    """
    Extract ordered (power_th, bonus_pct) pairs from *container* (brxe-elnjof).

    The container's direct children are the 5 rarity-tier groups (uncommon →
    unreal), each holding exactly 2 brxe-text-basic elements: power and bonus.
    Class names of those child divs differ per tier (brxe-keyive, brxe-qdbdff,
    brxe-qddzks, brxe-sjluxx, brxe-ylbuzw) so we rely on position, not name.

    Fallback: if the direct-children strategy yields < 5 pairs, collect ALL
    brxe-text-basic leaf elements and pair them up in document order.
    """
    # Primary: each direct child of brxe-elnjof is one rarity group
    pairs = []
    for child in container.find_all(recursive=False):
        if not child.has_attr("class"):
            continue
        els = child.find_all(
            lambda tag: tag.has_attr("class") and "brxe-text-basic" in tag["class"]
        )
        if els:
            pairs.append(parse_power_bonus(els))

    if len(pairs) >= 5:
        return pairs[:5]

    # Fallback – flatten all brxe-text-basic leaf elements and pair them up
    all_els = container.find_all(
        lambda tag: tag.has_attr("class") and "brxe-text-basic" in tag["class"]
    )
    leaves = [
        el for el in all_els
        if not el.find(lambda t: t.has_attr("class") and "brxe-text-basic" in t["class"])
    ]

    pairs = []
    pending_power = None
    for el in leaves:
        text = el.get_text(strip=True)
        if re.search(r"[KMGTEП][Hh]", text, re.IGNORECASE):
            pending_power = parse_power(text)
        elif "%" in text:
            pairs.append((pending_power, parse_bonus(text)))
            pending_power = None

    return pairs

    return pairs


def download_image(url: str, dest_path: str) -> bool:
    """Download a file from *url* and write it to *dest_path*."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return True
    except KeyboardInterrupt:
        # Don't let a Ctrl+C mid-download kill the whole run silently
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
    Missing rarities (None) and 0-value tiers are excluded from the check
    because some miners lack higher rarities or have a legitimate 0 bonus.
    Returns a dict with two boolean flags and the count of populated rarities.
    """
    # Only include pairs where the value is a positive number
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


def _parse_filter_sidebar(soup) -> dict[str, float]:
    """
    Extract per-rarity bonus values from the page-level filter sidebar.

    The sidebar contains blocks like:
      "Common bonus  1 2   Uncommon bonus  2 3   Rare bonus  3 4  ..."
    where the two numbers are the min/max slider bounds for that rarity's bonus.
    When the page shows a single miner, these bounds bracket that miner's
    actual bonus value, so we use the lower bound as the floor approximation.
    Returns a dict {rarity_name: bonus_pct_lower_bound} for every rarity found.
    """
    text = soup.get_text(" ", strip=True)
    result: dict[str, float] = {}
    for rarity in ("common", "uncommon", "rare", "epic", "legendary", "unreal"):
        m = re.search(
            rf"{rarity}\s+bonus\s+([\d]+(?:\.\d+)?)\s+([\d]+(?:\.\d+)?)",
            text,
            re.IGNORECASE,
        )
        if m:
            result[rarity] = float(m.group(1))
    return result


# ---------------------------------------------------------------------------
# Scraping a single page
# ---------------------------------------------------------------------------

def fetch_with_retry(url: str, retries: int = 4, backoff: float = 5.0) -> requests.Response | None:
    """GET *url* with up to *retries* attempts and exponential backoff.
    404 Not Found is treated as permanent — only 2 attempts are made before
    giving up immediately (no point waiting for a page that doesn't exist).
    """
    import requests as _req
    session = requests.Session()
    session.headers.update(HEADERS)
    delay = backoff
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
            return r
        except _req.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            print(f"  [Attempt {attempt}/{retries}] Error: {exc}")
            if status == 404:
                if attempt >= 2:
                    break  # 404 is permanent — stop after 2 tries
            if attempt < retries:
                print(f"  Retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
        except Exception as exc:
            print(f"  [Attempt {attempt}/{retries}] Error: {exc}")
            if attempt < retries:
                print(f"  Retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
    return None


def scrape_page(url: str, download_images: bool = True) -> list[dict]:
    """
    Fetch one listing page and return a list of miner records.
    Returns an empty list when no miner cards are found (end of pages).

    When download_images=False, images are NOT saved to disk but each record
    will contain a temporary '_img_url' key so the caller can download
    only the specific image it needs.
    """
    response = fetch_with_retry(url)
    if response is None:
        print(f"  [!] Giving up on {url}")
        return []

    soup = BeautifulSoup(response.text, "lxml")

    # Each miner is wrapped in a block with the class 'brxe-gzrnkg'.
    # Bricks Builder generates these hash-based class names and they can change
    # on theme updates.  Try the known class first, then auto-detect.
    cards = soup.find_all(
        lambda tag: tag.has_attr("class") and "brxe-gzrnkg" in tag["class"]
    )

    if not cards:
        # Auto-detect card container: find any brxe-* class (length > 8 rules out
        # generic utility classes) that appears ≥ 3 times AND whose elements
        # contain both a JetEngine name field and a link to /miner/.
        from collections import Counter as _Counter
        brxe_counts: _Counter = _Counter(
            cls
            for tag in soup.find_all(lambda t: t.has_attr("class"))
            for cls in tag["class"]
            if cls.startswith("brxe-") and len(cls) > 8
        )
        for cls, count in brxe_counts.most_common(30):
            if count < 3:
                break
            candidates = soup.find_all(
                lambda tag, _c=cls: tag.has_attr("class") and _c in tag["class"]
            )
            has_name = any(
                c.find(
                    lambda t: t.has_attr("class")
                    and "jet-listing-dynamic-field__content" in t["class"]
                )
                for c in candidates
            )
            has_link = any(
                c.find("a", href=lambda h: h and "/miner/" in h)
                for c in candidates
            )
            if has_name and has_link:
                print(f"  [auto-detect] Card class changed: using '{cls}' ({count} cards)")
                cards = candidates
                break

    if not cards:
        # Last-resort: each wp-post-image img's parent might be the card
        imgs = soup.find_all("img", class_=lambda c: c and "wp-post-image" in c)
        if imgs:
            seen: set = set()
            parents = []
            for img in imgs:
                p = img.parent
                if id(p) not in seen:
                    seen.add(id(p))
                    parents.append(p)
            if len(parents) >= 2:
                print(f"  [auto-detect] Falling back to wp-post-image parents ({len(parents)} cards)")
                cards = parents

    if not cards:
        return []

    miners = []
    os.makedirs(MINERS_DIR, exist_ok=True)

    for card in cards:
        # ── Detail page URL (from card link) ───────────────────────────────
        # Prefer the outermost <a> with an href pointing to the miner detail page.
        detail_url = None
        card_link = card.find("a", href=lambda h: h and "/miner/" in h)
        if card_link:
            href = card_link.get("href", "")
            if href.startswith("/"):
                href = f"https://minaryganar.com{href}"
            detail_url = href

        # ── Name ────────────────────────────────────────────────────────────
        name_el = card.find(
            lambda tag: tag.has_attr("class")
            and "jet-listing-dynamic-field__content" in tag["class"]
        )
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        # ── Image ────────────────────────────────────────────────────────────
        img_el = card.find(
            "img",
            class_=lambda c: c and "wp-post-image" in c,
        )
        img_url = None
        if img_el:
            # Prefer the highest-res src; fall back to data-src for lazy-loaded
            img_url = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src")

        # ── Common rarity (outside the dropdown) ────────────────────────────
        common_container = card.find(
            lambda tag: tag.has_attr("class") and "brxe-hyyqbi" in tag["class"]
        )
        common_power, common_bonus = (None, None)
        if common_container:
            els = common_container.find_all(
                lambda tag: tag.has_attr("class") and "brxe-text-basic" in tag["class"]
            )
            common_power, common_bonus = parse_power_bonus(els)

        # Fallback: parse common stats from raw card text (for miners that
        # omit the brxe-hyyqbi container but still show "XXXX Th Y.YY %" inline)
        if common_power is None or common_bonus is None:
            card_text = card.get_text(" ", strip=True)
            pwr_m = re.search(
                r"([\d,]+(?:\.\d+)?)\s*(EH|PH|TH|GH|MH|KH)", card_text, re.IGNORECASE
            )
            bon_m = re.search(r"([\d,]+(?:\.\d+)?)\s*%", card_text)
            if pwr_m and common_power is None:
                common_power = parse_power(f"{pwr_m.group(1)} {pwr_m.group(2)}")
            if bon_m and common_bonus is None:
                common_bonus = float(bon_m.group(1).replace(",", "."))

        rarities: dict[str, dict] = {
            "common": {"power_th": common_power, "bonus_pct": common_bonus}
        }

        # ── Other rarities (inside the dropdown) ────────────────────────────
        # Anchor on brxe-pbvklb (the dropdown UL), then find brxe-elnjof inside it.
        # This matches the documented page structure from Miners – Piero.html.
        dropdown_ul = card.find(
            lambda tag: tag.has_attr("class") and "brxe-pbvklb" in tag["class"]
        )
        dropdown = (
            dropdown_ul.find(
                lambda tag: tag.has_attr("class") and "brxe-elnjof" in tag["class"]
            )
            if dropdown_ul
            else None
        )
        if dropdown:
            # extract_rarity_pairs tries multiple strategies to get 5 pairs
            pairs = extract_rarity_pairs(dropdown)
            other_rarities = ["uncommon", "rare", "epic", "legendary", "unreal"]
            for idx, (pwr, bon) in enumerate(pairs[:5]):
                rarities[other_rarities[idx]] = {"power_th": pwr, "bonus_pct": bon}
            if len(pairs) < 5:
                print(
                    f"    [!] Only {len(pairs)}/5 rarity pairs found for '{name}'"
                )

        # ── Download image ───────────────────────────────────────────────────
        safe_name = safe_filename(name)
        ext = "png"
        if img_url:
            ext_match = re.search(r"\.(png|jpg|jpeg|gif|webp)", img_url, re.IGNORECASE)
            if ext_match:
                ext = ext_match.group(1).lower()
        img_filename = f"{safe_name}.{ext}"
        img_path = os.path.join(MINERS_DIR, img_filename)

        if not img_url:
            print(f"    [!] No image found for: {name}")
        elif download_images:
            if not os.path.exists(img_path):
                print(f"    Downloading image: {img_filename}")
                download_image(img_url, img_path)

        # ── Assemble record ──────────────────────────────────────────────────
        validation = validate_rarity_scaling(rarities)
        if not validation["power_scaling_ok"] or not validation["bonus_scaling_ok"]:
            print(
                f"    [!] Scaling issue for '{name}': "
                f"power_ok={validation['power_scaling_ok']}, "
                f"bonus_ok={validation['bonus_scaling_ok']}"
            )

        rec: dict = {
            "name": name,
            "image": img_filename,
            "rarities": {
                rarity: {
                    "power_th": rarities.get(rarity, {}).get("power_th"),
                    "bonus_pct": rarities.get(rarity, {}).get("bonus_pct"),
                }
                for rarity in RARITIES
            },
            "_validation": validation,
        }
        if detail_url:
            rec["_detail_url"] = detail_url
        if not download_images and img_url:
            rec["_img_url"] = img_url
        miners.append(rec)

    # ── Filter sidebar fallback for non-common rarity bonuses ─────────────────
    # When exactly one miner is on the page (e.g. a targeted search) and its
    # non-common rarity data is all None, the page-level filter sidebar contains
    # per-rarity bonus lower-bounds that we can use as an approximation.
    if len(miners) == 1:
        m = miners[0]
        other_rarities = [r for r in RARITIES if r != "common"]
        missing = all(
            m["rarities"].get(r, {}).get("bonus_pct") is None for r in other_rarities
        )
        if missing:
            sidebar = _parse_filter_sidebar(soup)
            if sidebar:
                for rarity, bonus_val in sidebar.items():
                    if rarity in m["rarities"]:
                        if m["rarities"][rarity]["bonus_pct"] is None:
                            m["rarities"][rarity]["bonus_pct"] = bonus_val
                print(f"    [sidebar fallback] filled rarity bonuses for '{m['name']}': "
                      + ", ".join(f"{r}={v}%" for r, v in sorted(sidebar.items())))

    return miners


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def save_json(all_miners: list[dict]) -> None:
    os.makedirs(MINERS_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_miners, f, indent=2, ensure_ascii=False)


def _log_match(slug: str, html_name: str, found_name: str, match: dict) -> None:
    """Record a miner found via non-exact secondary search for user verification."""
    entries: list[dict] = []
    if os.path.exists(MATCH_LOG):
        try:
            with open(MATCH_LOG, encoding="utf-8") as fh:
                entries = json.load(fh)
        except Exception:
            entries = []
    # Never overwrite an already-reviewed entry — preserves confirmed/rejected state.
    # Exception: if status is "rejected" but the slug-aliased image file is STILL
    # missing from disk, the _replace_db_record alias step may not have run yet
    # (e.g. image was deleted or the alias was never created).  In that case
    # re-queue as pending so the user gets another chance to verify.
    existing = next((e for e in entries if e.get("slug") == slug), None)
    if existing and existing.get("status") == "confirmed":
        return
    if existing and existing.get("status") == "rejected":
        # Check whether the slug alias actually exists on disk
        img_file = existing.get("image", "")
        ext = os.path.splitext(img_file)[1] if img_file else ".gif"
        alias_path = os.path.join(
            os.path.dirname(MATCH_LOG) or ".",
            "miners",
            f"{slug}{ext}",
        )
        if os.path.exists(alias_path):
            # Alias is present — the rejection was fully processed, skip re-logging
            return
        # Alias missing — re-queue so the user can re-verify (and _finish will re-alias)
        print(f"  [!] Slug alias '{slug}{ext}' missing after rejection — re-queuing for verification")
    # Replace any pending entry for this slug so re-runs are idempotent
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


def main() -> None:
    all_miners: list[dict] = []
    page = 1

    print("Starting miner scrape from minaryganar.com...\n")

    try:
        while True:
            url = BASE_URL if page == 1 else f"{BASE_URL}page/{page}/"
            print(f"[Page {page}] {url}")

            miners = scrape_page(url)

            if not miners:
                print(f"  No miners found – stopping after page {page - 1}.")
                break

            all_miners.extend(miners)
            print(f"  +{len(miners)} miners  |  total so far: {len(all_miners)}")

            # Save progress after every page so partial data is never lost
            save_json(all_miners)

            page += 1
            time.sleep(2.0)  # Be polite — avoid triggering the WAF/rate-limiter

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    except Exception as exc:
        print(f"\n[!] Unexpected error: {exc}")
    finally:
        # Always save whatever was collected before any crash/interrupt
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


def _search_progressive(slug: str) -> dict | None:
    """
    Two-phase progressive search — always via the listing search endpoint so
    that full per-rarity data is returned (individual miner detail pages only
    have Basic/common data).  Images are NOT downloaded for non-matching results.

    Phase 1 — word-by-word:
      Build the query by adding one word at a time ("BTC" → "BTC Bull").
      Stop immediately if a query returns 0 results and switch to Phase 2.

    Phase 2 — letter-by-letter:
      Extend the full query string one character at a time.
      Stop immediately if a query returns 0 results.
    """
    query_base = slug.replace("-", "_").replace("_", " ").strip()
    target = _norm_stem(slug)
    words = query_base.split()

    def _try_search(query: str) -> list[dict]:
        url = f"{BASE_URL}?search={requests.utils.quote(query)}"
        return scrape_page(url, download_images=False)

    def _find_match(results: list[dict]) -> dict | None:
        for rec in results:
            img_stem = _norm_stem(re.sub(r"\.\w+$", "", rec.get("image", "")))
            if img_stem == target:
                print(f"  Matched: {rec['name']!r} (image slug)")
                return rec
        if len(results) == 1:
            print(f"  Matched: {results[0]['name']!r} (single result)")
            return results[0]
        return None

    def _finalize(rec: dict) -> dict:
        img_url = rec.pop("_img_url", None)
        if img_url:
            img_path = os.path.join(MINERS_DIR, rec["image"])
            if not os.path.exists(img_path):
                os.makedirs(MINERS_DIR, exist_ok=True)
                print(f"    Downloading image: {rec['image']}")
                download_image(img_url, img_path)
        return rec

    # ── Phase 1: word-by-word ─────────────────────────────────────────────────
    print(f"  Word-by-word search ({len(words)} word(s))...")
    for n in range(1, len(words) + 1):
        query = " ".join(words[:n])
        results = _try_search(query)
        count = len(results)
        first = results[0]["name"] if results else "—"
        print(f"  [word {n}/{len(words)}] {query!r} → {count} result(s)" +
              (f", first: {first!r}" if count else ""))
        if count == 0:
            print(f"  Word search returned 0 — switching to letter search")
            break
        match = _find_match(results)
        if match:
            return _finalize(match)
        time.sleep(0.5)

    # ── Phase 2: letter-by-letter ─────────────────────────────────────────────
    print(f"  Letter-by-letter search ({len(query_base)} char(s))...")
    prev_count = -1
    for length in range(1, len(query_base) + 1):
        query = query_base[:length]
        results = _try_search(query)
        count = len(results)
        if count != prev_count:
            first = results[0]["name"] if results else "—"
            print(f"  [char {length}/{len(query_base)}] {query!r} → {count} result(s)" +
                  (f", first: {first!r}" if count else ""))
        prev_count = count
        if count == 0:
            print(f"  Letter search returned 0 at length {length} — giving up")
            break
        match = _find_match(results)
        if match:
            return _finalize(match)
        time.sleep(0.5)

    return None


def lookup_miner(name: str, expected_name: str = "", log_slug: str | None = None) -> dict | None:
    """
    Fetch a single miner by name using the minaryganar.com search endpoint.
    Falls back to progressive slug search if the exact name search fails.

    The result is merged into miners_data.json (added if new, updated if
    already present). Returns the miner record, or None if not found.

    expected_name: display name from the game HTML — used to detect mismatches.
    log_slug:      key to store in match_log.json (defaults to _norm_stem of name).
    """
    search_url = f"{BASE_URL}?search={requests.utils.quote(name)}"
    print(f"Searching for '{name}'...")
    print(f"  URL: {search_url}")

    miners = scrape_page(search_url, download_images=False)

    # Prefer an exact name match in the results
    used_secondary = False
    match = next(
        (m for m in miners if m["name"].lower() == name.lower()),
        None,
    )

    if match is None and miners:
        # Check if any result's image slug matches the requested name's slug
        target = _norm_stem(name)
        match = next(
            (m for m in miners
             if _norm_stem(re.sub(r"\.\w+$", "", m.get("image", ""))) == target),
            None,
        )
        if match:
            print(f"  Matched by image slug: {match['name']!r}")
            used_secondary = True

    # Download the matched miner's image (only this one, not every search result)
    if match is not None:
        img_url = match.pop("_img_url", None)
        if img_url:
            img_path = os.path.join(MINERS_DIR, match["image"])
            if not os.path.exists(img_path):
                os.makedirs(MINERS_DIR, exist_ok=True)
                print(f"    Downloading image: {match['image']}")
                download_image(img_url, img_path)

    if match is None:
        # Progressive search: word-by-word then letter-by-letter
        # Always via the search endpoint — the detail page only has Basic (common)
        # power and no per-rarity breakdown.
        print(f"  Exact match failed — trying progressive search...")
        match = _search_progressive(_norm_stem(name))
        if match:
            used_secondary = True

    if match is None:
        print(f"  [!] Could not find '{name}' on minaryganar.com.")
        return None

    print(f"  Found: {match['name']}")

    # If non-common rarity data is all null (happens when the miner was found via
    # a multi-result search page where the filter sidebar covers many miners),
    # re-fetch using a targeted single-miner search URL to trigger the filter
    # sidebar fallback in scrape_page.
    other_rarities = [r for r in RARITIES if r != "common"]
    if all(match.get("rarities", {}).get(r, {}).get("bonus_pct") is None for r in other_rarities):
        targeted_url = f"{BASE_URL}?search={requests.utils.quote(match['name'])}"
        print(f"  Null rarities — re-fetching targeted search for filter sidebar data...")
        targeted = scrape_page(targeted_url, download_images=False)
        exact = next(
            (m for m in targeted if m["name"].lower() == match["name"].lower()),
            targeted[0] if len(targeted) == 1 else None,
        )
        if exact:
            # Merge any newly-found rarity data
            for r in RARITIES:
                existing_tier = match.setdefault("rarities", {}).get(r, {})
                new_tier = exact.get("rarities", {}).get(r, {})
                if existing_tier.get("bonus_pct") is None and new_tier.get("bonus_pct") is not None:
                    match.setdefault("rarities", {})[r] = new_tier
            if any(match["rarities"].get(r, {}).get("bonus_pct") is not None for r in other_rarities):
                print(f"  Filled rarity bonuses from targeted search.")

    # Fetch cell count from the individual miner detail page
    if match.get("cells") is None:
        cells = fetch_cells(match["name"], image_filename=match.get("image"),
                            detail_url=match.get("_detail_url"))
        if cells is not None:
            match["cells"] = cells
            print(f"  Cells: {cells}")
        else:
            print(f"  [!] Could not determine cell count for '{match['name']}'")
        time.sleep(1.0)

    # Log for verification when secondary search was used OR name mismatch detected
    _searched  = expected_name or name
    _slug_key  = log_slug if log_slug is not None else _norm_stem(_searched)
    name_mismatch = expected_name and _norm_stem(match["name"]) != _norm_stem(expected_name)
    if used_secondary or name_mismatch:
        _log_match(_slug_key, _searched, match["name"], match)
        if name_mismatch:
            print(f"  [!] Name mismatch flagged for verification: '{_searched}' -> '{match['name']}'")
        else:
            print(f"  [!] Secondary search result flagged for verification: '{_searched}'")

    # Load existing data
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


def lookup_miner_by_slug(slug: str, html_name: str = "") -> dict | None:
    """
    Like lookup_miner() but takes the raw game slug (e.g. 'valhallas_vault').
    After finding the miner, saves the image under '{slug}.{ext}' so that
    load_first_frame(slug) always finds it on the first exact-match attempt —
    regardless of what the third-party site calls the file.

    html_name: the display name as parsed from the game HTML — used to detect
    when the minaryganar.com match has a different name (flagged for review).
    """
    # Normalise to underscores (game slugs in placed JSONs always use underscores)
    slug = slug.replace("-", "_")
    display = " ".join(w.capitalize() for w in slug.split("_"))
    effective_html = html_name or display

    # Always search first — the individual miner detail page only has Basic
    # (common-rarity) power; per-rarity data is only on the listing search page.
    match = lookup_miner(display, expected_name=effective_html, log_slug=slug)
    if match is None:
        return None

    # Ensure the image lives under the game-slug filename so that
    # load_first_frame(slug) always finds it on the first exact-match attempt.
    # IMPORTANT: only update the found miner's image field when this slug has NOT
    # been marked as rejected in match_log.  A rejected slug means the found miner
    # is the WRONG one — we must not alias the slug filename onto its record,
    # because that would make build_slug_index route the slug to the wrong miner.
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
        slug_filename = f"{slug}{ext}"                    # e.g. "declarator_407plus.gif"
        existing_path = os.path.join(MINERS_DIR, existing_img)
        slug_path = os.path.join(MINERS_DIR, slug_filename)

        if not os.path.exists(slug_path) and os.path.exists(existing_path):
            import shutil
            shutil.copy2(existing_path, slug_path)
            print(f"  Aliased image -> '{slug_filename}'")

        if not _slug_rejected and os.path.exists(slug_path) and match["image"] != slug_filename:
            match["image"] = slug_filename
            # Persist the updated image field in the DB
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
# Backfill cell counts for existing miners_data.json entries
# ---------------------------------------------------------------------------

def backfill_cells() -> int:
    """
    For every miner in miners_data.json that is missing a 'cells' value,
    fetch its detail page and add the cell count.
    Returns the number of miners updated.
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
        cells = fetch_cells(name, image_filename=miner.get("image"))
        if cells is not None:
            miner["cells"] = cells
            print(f"    -> Cells: {cells}")
            updated += 1
        else:
            print(f"    [!] Not found — skipping")
        # Save after each update so progress is never lost
        if cells is not None:
            save_json(miners)
        time.sleep(1.5)

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
        # Usage: python scrape_miners.py "Miner Name"
        miner_name = " ".join(sys.argv[1:])
        result = lookup_miner(miner_name)
        if result:
            print(f"\nRarities for {result['name']}:")
            for rarity, data in result["rarities"].items():
                if data.get("power_th") is not None:
                    print(f"  {rarity:<12} {data['power_th']:>10,.0f} Th  {data['bonus_pct']:>6.2f}%")
    else:
        main()
