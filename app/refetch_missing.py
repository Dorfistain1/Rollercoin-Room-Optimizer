"""
Re-fetch full rarity data (power + bonus) for miners in miners_data.json
that have null power/bonus at common rarity.
Run from the roomBuilder directory.
"""
import json
from pathlib import Path
from scrape_miners import lookup_miner

_ROOT = Path(__file__).parent.parent   # roomBuilder/
data = json.load(open(_ROOT / "miners/miners_data.json", encoding="utf-8"))
needs_refetch = [
    m["name"] for m in data
    if (m.get("rarities", {}).get("common") or {}).get("power_th") is None
    or (m.get("rarities", {}).get("common") or {}).get("bonus_pct") is None
]

print(f"Miners needing re-fetch: {len(needs_refetch)}")
for name in needs_refetch:
    print(f"\n--- {name} ---")
    lookup_miner(name)
