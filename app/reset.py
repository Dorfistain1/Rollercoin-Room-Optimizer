"""
reset.py — Delete all generated data and return the project to a clean state.

By default this keeps miners/ (images and miners_data.json) so they don't
need to be re-downloaded on the next run — most re-runs only change part of
the rooms, so the existing miner data is still valid.

Optional flags:
  --all   Also delete miners/ images and miners_data.json (full wipe).
          Use this only when you've swapped in many new miners and want a
          completely fresh download.

Usage (from the roomBuilder/ directory):
  python app/reset.py        # reset data/vis/output/html_page, keep miners
  python app/reset.py --all  # full reset including miners/ images & data
"""

import sys
import shutil
from pathlib import Path

_ROOT = Path(__file__).parent.parent   # roomBuilder/

# ── What gets deleted ─────────────────────────────────────────────────────────

# Individual files in data/ (not the directory itself)
_DATA_FILES = [
    _ROOT / "data/inventory.json",
    _ROOT / "data/locked.json",
    _ROOT / "data/match_log.json",
    _ROOT / "data/optimizer_swaps.json",
    _ROOT / "data/set_bonus.json",
    _ROOT / "data/set_groups.json",
]
_DATA_GLOB_PATTERNS = [
    ("data", "placed_room*.json"),
]

# Directories whose entire CONTENTS are cleared (dirs kept so git tracks them)
_CLEAR_DIRS = [
    _ROOT / "vis",
    _ROOT / "output",
    _ROOT / "html_page",
]

# Miners directory — only cleared when --keep-miners is NOT set
_MINERS_DIR = _ROOT / "miners"
_MINERS_DATA = _ROOT / "miners/miners_data.json"

# ─────────────────────────────────────────────────────────────────────────────


def _remove_file(p: Path) -> bool:
    """Delete a single file. Returns True if removed, False if it didn't exist."""
    if p.exists():
        p.unlink()
        return True
    return False


def _clear_dir_contents(d: Path) -> int:
    """Delete all files (and subdirs) inside *d*. Returns count removed."""
    if not d.exists():
        return 0
    removed = 0
    for child in d.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


def main() -> None:
    delete_miners = "--all" in sys.argv

    print("RollerCoin Room Builder — Reset")
    print("=" * 40)
    if delete_miners:
        print("Mode: full reset (all generated files including miners/)\n")
    else:
        print("Mode: reset data/vis/output/html_page — miners/ kept (use --all to also wipe miners)\n")

    total_removed = 0

    # ── data/ individual files ────────────────────────────────────────────
    for p in _DATA_FILES:
        if _remove_file(p):
            print(f"  Deleted  {p.relative_to(_ROOT)}")
            total_removed += 1

    # ── data/ glob patterns ───────────────────────────────────────────────
    for folder, pattern in _DATA_GLOB_PATTERNS:
        for p in sorted((_ROOT / folder).glob(pattern)):
            if _remove_file(p):
                print(f"  Deleted  {p.relative_to(_ROOT)}")
                total_removed += 1

    # ── vis/, output/, html_page/ ─────────────────────────────────────────
    for d in _CLEAR_DIRS:
        n = _clear_dir_contents(d)
        if n:
            print(f"  Cleared  {d.relative_to(_ROOT)}/  ({n} item{'s' if n != 1 else ''})")
            total_removed += n

    # ── miners/ ───────────────────────────────────────────────────────────
    if delete_miners:
        n = _clear_dir_contents(_MINERS_DIR)
        if n:
            print(f"  Cleared  miners/  ({n} item{'s' if n != 1 else ''})")
            total_removed += n
    else:
        print(f"  Kept     miners/  (pass --all to also wipe miners)")

    print()
    if total_removed:
        print(f"Done — {total_removed} item{'s' if total_removed != 1 else ''} removed.")
    else:
        print("Nothing to remove — already clean.")

    print("\nThe project is ready for a fresh run.")
    print("Drop your new .html pages into html_page/ and run: python app/main.py")


if __name__ == "__main__":
    main()
