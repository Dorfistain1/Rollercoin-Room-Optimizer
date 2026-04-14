"""
verify_matches.py — UI to verify that secondary-search miner matches are correct,
and to enter stats for legacy-rarity miners that are not available on minaryganar.com.

When main.py fetches a missing miner and the name found on minaryganar.com
differs from the name in the game HTML, it logs the pairing to match_log.json.
This window lets the user confirm the match is correct, or reject it and supply
manual power/bonus values.

Legacy miners (badge alt="Rating star") cannot be fetched from minaryganar.com.
They are queued here automatically so the user can enter their power and bonus.

Rejected miners:
  - Show a placeholder image in the visualizer (with a red tint)
  - Use the manually entered power/bonus in the optimizer

Usage (automatic via main.py, or standalone):
  python verify_matches.py
"""

import json
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT      = Path(__file__).parent.parent   # roomBuilder/
MATCH_LOG  = _ROOT / "data/match_log.json"
MINERS_DIR = _ROOT / "miners"

# Thumbnail size inside each card
THUMB_W, THUMB_H = 88, 78


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_log() -> list[dict]:
    if not MATCH_LOG.exists():
        return []
    try:
        return json.loads(MATCH_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_log(entries: list[dict]) -> None:
    MATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    MATCH_LOG.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _find_image_for_slug(slug: str) -> str:
    """Return the image filename for *slug* from the miners directory, or a .gif placeholder."""
    for ext in (".gif", ".png", ".jpg", ".webp"):
        p = MINERS_DIR / (slug + ext)
        if p.exists():
            return p.name
    # Case-insensitive scan
    slug_lower = slug.lower()
    for p in MINERS_DIR.iterdir():
        if p.suffix.lower() in (".gif", ".png", ".jpg", ".webp"):
            if p.stem.lower() == slug_lower:
                return p.name
    return slug + ".gif"  # fallback placeholder name


def _collect_legacy_miners() -> None:
    """
    Scan all placed_room*.json files for miners with rarity 'legacy' and
    add them to match_log.json (status 'legacy') if not already present.
    Called once before opening the verify window so the user can fill in
    power and bonus for miners the minaryganar.com site cannot provide.
    """
    entries = _load_log()
    existing_slugs = {e["slug"] for e in entries}

    placed_dir = _ROOT / "data"
    added = 0
    for placed_path in sorted(placed_dir.glob("placed_room*.json")):
        try:
            data = json.loads(placed_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for rack in data.get("racks", []):
            for miner in rack:
                if miner.get("rarity") != "legacy":
                    continue
                slug = miner["slug"]
                if slug in existing_slugs:
                    continue
                entries.append({
                    "slug":       slug,
                    "html_name":  miner.get("name", slug),
                    "found_name": "",
                    "image":      _find_image_for_slug(slug),
                    "status":     "legacy",
                })
                existing_slugs.add(slug)
                added += 1

    if added:
        _save_log(entries)
        print(f"  Queued {added} legacy miner(s) for manual data entry.")


def _collect_missing_data_miners() -> None:
    """
    Scan all placed_room*.json files for miners whose power is missing or zero in
    miners_data.json (fetch failed or data incomplete).  Adds them to match_log.json
    with status 'missing_data' so the user can fill in values manually.
    """
    import re as _re

    entries = _load_log()
    existing_slugs = {e["slug"] for e in entries}

    db_path = _ROOT / "miners/miners_data.json"
    db: list[dict] = []
    if db_path.exists():
        try:
            db = json.loads(db_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _n(s: str) -> str:
        s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
        return _re.sub(r"[^\w]+", "_", s).strip("_")

    db_index = {_n(m["name"]): m for m in db}

    placed_dir = _ROOT / "data"
    added = 0
    for placed_path in sorted(placed_dir.glob("placed_room*.json")):
        try:
            data = json.loads(placed_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for rack in data.get("racks", []):
            for miner in rack:
                if miner.get("rarity") == "legacy":
                    continue  # handled by _collect_legacy_miners
                slug = miner.get("slug", "")
                if slug in existing_slugs:
                    continue
                name   = miner.get("name", slug)
                rarity = miner.get("rarity") or "common"
                rec    = db_index.get(_n(name))
                power  = None
                if rec:
                    tier  = (rec.get("rarities") or {}).get(rarity) or {}
                    power = tier.get("power_th")
                if power:
                    continue  # DB has valid data — skip
                entries.append({
                    "slug":       slug,
                    "html_name":  name,
                    "found_name": rec["name"] if rec else "",
                    "image":      _find_image_for_slug(slug),
                    "rarity":     rarity,
                    "status":     "missing_data",
                })
                existing_slugs.add(slug)
                added += 1

    if added:
        _save_log(entries)
        print(f"  Queued {added} miner(s) with missing DB data for manual entry.")


def _add_missing_data_db_record(entry: dict) -> None:
    """Write or update a miner's rarity entry in miners_data.json with manually supplied stats."""
    import re as _re

    db_path = _ROOT / "miners/miners_data.json"

    def _n(s: str) -> str:
        s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
        return _re.sub(r"[^\w]+", "_", s).strip("_")

    data: list[dict] = []
    if db_path.exists():
        try:
            data = json.loads(db_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    html_name = entry.get("html_name", "")
    rarity    = entry.get("rarity", "common")
    p         = entry.get("manual_power_th")
    b         = entry.get("manual_bonus_pct")
    c         = entry.get("manual_cells", 2)
    image     = entry.get("image", f"{entry.get('slug', '')}.gif")

    existing = next((m for m in data if _n(m["name"]) == _n(html_name)), None)
    if existing:
        existing.setdefault("rarities", {})[rarity] = {"power_th": p, "bonus_pct": b}
        existing["_manual"] = True
        if c:
            existing["cells"] = c
    else:
        data.append({
            "name":     html_name,
            "image":    image,
            "cells":    c,
            "rarities": {rarity: {"power_th": p, "bonus_pct": b}},
            "_manual":  True,
        })

    db_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Missing-data DB record saved for '{html_name}' ({rarity}, {c} cell, {p} TH, {b}%)")


def _add_legacy_db_record(entry: dict) -> None:
    """Write (or update) a legacy miner's record in miners_data.json with manual stats."""
    import re as _re

    db_path = _ROOT / "miners/miners_data.json"

    def _n(s: str) -> str:
        s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
        return _re.sub(r"[^\w]+", "_", s).strip("_")

    data: list[dict] = []
    if db_path.exists():
        try:
            data = json.loads(db_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    html_name = entry.get("html_name", "")
    html_key  = _n(html_name)
    p = entry.get("manual_power_th")
    b = entry.get("manual_bonus_pct")
    c = entry.get("manual_cells", 2)
    image = entry.get("image", f"{entry.get('slug', '')}.gif")

    existing = next((m for m in data if _n(m["name"]) == html_key), None)
    if existing:
        existing.setdefault("rarities", {})["legacy"] = {"power_th": p, "bonus_pct": b}
        existing["_manual"] = True
        if c:
            existing["cells"] = c
    else:
        data.append({
            "name":     html_name,
            "image":    image,
            "cells":    c,
            "rarities": {"legacy": {"power_th": p, "bonus_pct": b}},
            "_manual":  True,
        })

    db_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Legacy DB record saved for '{html_name}' ({c} cell, {p} TH, {b}%)")


def _load_thumb(image_filename: str) -> ImageTk.PhotoImage | None:
    path = MINERS_DIR / image_filename
    if not path.exists():
        return None
    try:
        img = Image.open(str(path))
        if hasattr(img, "is_animated") and img.is_animated:
            img.seek(0)
        img = img.convert("RGBA")
        img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
        # Pad to fixed size on a light-grey background
        padded = Image.new("RGBA", (THUMB_W, THUMB_H), (230, 230, 230, 255))
        ox = (THUMB_W - img.width)  // 2
        oy = (THUMB_H - img.height) // 2
        padded.paste(img, (ox, oy), img)
        return ImageTk.PhotoImage(padded.convert("RGB"))
    except Exception:
        return None


def _fmt_power(th) -> str:
    if th is None:
        return "?"
    for div, unit in [(1e6, "EH"), (1000, "PH"), (1.0, "TH"), (0.001, "GH")]:
        if th >= div:
            v = th / div
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{s} {unit}"
    return f"{th} TH"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class VerifyWindow:
    def __init__(self, root: tk.Tk, entries: list[dict]) -> None:
        self.root   = root
        self.entries = entries        # mutable — updated in place
        self._photos: list = []       # keep PhotoImage refs alive
        self._vars:   list[dict] = [] # per-card tkinter variable dicts

        root.title("RollerCoin — Verify Miner Matches")

        # ── Top bar ───────────────────────────────────────────────────────
        top = tk.Frame(root, bg="#2c3e50", pady=8, padx=12)
        top.pack(fill=tk.X)
        tk.Label(
            top, text="Verify Miner Matches",
            fg="white", bg="#2c3e50", font=("Arial", 13, "bold"),
        ).pack(side=tk.LEFT)
        pending = sum(1 for e in entries if e.get("status") == "pending")
        legacy  = sum(1 for e in entries if e.get("status") == "legacy")
        missing = sum(1 for e in entries if e.get("status") == "missing_data")
        parts = []
        if pending:
            parts.append(f"{pending} pending match{'es' if pending != 1 else ''}")
        if legacy:
            parts.append(f"{legacy} legacy miner{'s' if legacy != 1 else ''}")
        if missing:
            parts.append(f"{missing} missing data")
        status_str = ", ".join(parts) if parts else "all reviewed"
        tk.Label(
            top, text=f"  {len(entries)} miner(s) to review  ({status_str})",
            fg="#bdc3c7", bg="#2c3e50", font=("Arial", 10),
        ).pack(side=tk.LEFT)
        tk.Button(
            top, text="Save & Continue", bg="#27ae60", fg="white",
            font=("Arial", 10, "bold"), padx=14, pady=2,
            command=self._finish,
        ).pack(side=tk.RIGHT, padx=8)

        # ── Scrollable content ────────────────────────────────────────────
        outer = tk.Frame(root)
        outer.pack(fill=tk.BOTH, expand=True)

        vbar = tk.Scrollbar(outer, orient=tk.VERTICAL)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas = tk.Canvas(
            outer, yscrollcommand=vbar.set,
            bg="#f0f0f0", highlightthickness=0,
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.config(command=self.canvas.yview)

        self.inner = tk.Frame(self.canvas, bg="#f0f0f0")
        self._win_id = self.canvas.create_window(
            (0, 0), window=self.inner, anchor=tk.NW,
        )

        self.inner.bind("<Configure>", self._on_inner_resize)
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self._win_id, width=e.width),
        )
        # Bind mouse wheel to the whole window so scroll works anywhere
        root.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"),
        )

        self._build_cards()

        root.update_idletasks()
        w = min(820, root.winfo_screenwidth()  - 40)
        h = min(700, root.winfo_screenheight() - 80)
        root.geometry(f"{w}x{h}")

    def _on_inner_resize(self, event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    # ── Cards ─────────────────────────────────────────────────────────────

    def _build_cards(self) -> None:
        for i, entry in enumerate(self.entries):
            if entry.get("status") == "legacy":
                self._build_legacy_card(i, entry)
            elif entry.get("status") == "missing_data":
                self._build_missing_data_card(i, entry)
            else:
                self._build_card(i, entry)

    def _build_card(self, idx: int, entry: dict) -> None:
        bg = "#ffffff" if idx % 2 == 0 else "#f7f7f7"

        card = tk.Frame(
            self.inner, bg=bg, bd=1, relief=tk.GROOVE,
            padx=10, pady=8,
        )
        card.pack(fill=tk.X, padx=10, pady=4, ipady=2)
        card.columnconfigure(2, weight=1)

        # ── Thumbnail ─────────────────────────────────────────────────────
        photo = _load_thumb(entry.get("image", ""))
        self._photos.append(photo)
        img_lbl = tk.Label(
            card, bg=bg,
            image=photo if photo else None,
            text="" if photo else "[no image]",
            fg="#999", width=THUMB_W if not photo else 0,
        )
        img_lbl.grid(row=0, column=0, rowspan=5, padx=(0, 14), sticky="nw")

        # ── Name rows ─────────────────────────────────────────────────────
        tk.Label(
            card, text="Parsed from game:", fg="#888", bg=bg,
            font=("Arial", 8),
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            card, text=entry.get("html_name", "?"),
            font=("Arial", 10, "bold"), bg=bg, fg="#2c3e50",
        ).grid(row=0, column=2, sticky="w", padx=(4, 0))

        html_n = _norm(entry.get("html_name",  ""))
        found_n =_norm(entry.get("found_name", ""))
        match_color = "#27ae60" if html_n == found_n else "#e67e22"

        tk.Label(
            card, text="Found on site as:", fg="#888", bg=bg,
            font=("Arial", 8),
        ).grid(row=1, column=1, sticky="w")
        tk.Label(
            card, text=entry.get("found_name", "?"),
            font=("Arial", 10, "bold"), bg=bg, fg=match_color,
        ).grid(row=1, column=2, sticky="w", padx=(4, 0))

        # ── Power / bonus ─────────────────────────────────────────────────
        common  = (entry.get("rarities") or {}).get("common") or {}
        pwr_str = _fmt_power(common.get("power_th"))
        bon_val = common.get("bonus_pct")
        bon_str = f"{bon_val}%" if bon_val is not None else "?"
        tk.Label(
            card,
            text=f"Common  \u2192  Power: {pwr_str}  |  Bonus: {bon_str}",
            fg="#555", bg=bg, font=("Arial", 9),
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 4))

        # ── Tkinter state vars ────────────────────────────────────────────
        status_var = tk.StringVar(value=entry.get("status", "pending"))
        pwr_var    = tk.StringVar(
            value=str(entry.get("manual_power_th") or common.get("power_th") or ""),
        )
        bon_var    = tk.StringVar(
            value=str(entry.get("manual_bonus_pct") or bon_val or ""),
        )
        cell_var   = tk.StringVar(value=str(entry.get("manual_cells") or 2))
        self._vars.append({"status": status_var, "power_th": pwr_var, "bonus_pct": bon_var, "cells": cell_var})

        # ── Button row ────────────────────────────────────────────────────
        btn_row = tk.Frame(card, bg=bg)
        btn_row.grid(row=3, column=1, columnspan=2, sticky="w")

        # Manual entry row (hidden until rejected)
        manual_row = tk.Frame(card, bg=bg)
        manual_row.grid(row=4, column=1, columnspan=2, sticky="w")

        tk.Label(manual_row, text="Power (TH):", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.Entry(manual_row, textvariable=pwr_var, width=12, font=("Arial", 9)).pack(
            side=tk.LEFT, padx=(2, 10),
        )
        tk.Label(manual_row, text="Bonus (%):", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.Entry(manual_row, textvariable=bon_var, width=8, font=("Arial", 9)).pack(
            side=tk.LEFT, padx=(2, 0),
        )
        tk.Label(manual_row, text="  Cells:", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.OptionMenu(manual_row, cell_var, "1", "2").pack(side=tk.LEFT, padx=(2, 0))

        def _apply_manual(i=idx, pv=pwr_var, bv=bon_var, cv=cell_var) -> None:
            try:
                pwr = float(pv.get().replace(",", ".") or "0")
                self.entries[i]["manual_power_th"] = pwr if pwr > 0 else None
            except ValueError:
                self.entries[i]["manual_power_th"] = None
            try:
                bon = float(bv.get().replace(",", ".") or "0")
                self.entries[i]["manual_bonus_pct"] = bon if bon > 0 else None
            except ValueError:
                self.entries[i]["manual_bonus_pct"] = None
            try:
                self.entries[i]["manual_cells"] = int(cv.get())
            except (ValueError, KeyError):
                self.entries[i]["manual_cells"] = 2

        tk.Button(
            manual_row, text="✓ Apply", bg="#27ae60", fg="white",
            font=("Arial", 9, "bold"), relief=tk.FLAT, padx=6, pady=1,
            command=_apply_manual,
        ).pack(side=tk.LEFT, padx=(8, 0))

        def _show_manual(show: bool, row=manual_row) -> None:
            if show:
                row.grid()
            else:
                row.grid_remove()

        status_lbl = tk.Label(
            btn_row, textvariable=status_var,
            fg="#888", bg=bg, font=("Arial", 8, "italic"),
        )

        def _confirm(i=idx, sv=status_var) -> None:
            sv.set("confirmed")
            self.entries[i]["status"] = "confirmed"
            _show_manual(False)
            status_lbl.config(fg="#27ae60")

        def _reject(i=idx, sv=status_var) -> None:
            sv.set("rejected")
            self.entries[i]["status"] = "rejected"
            _show_manual(True)
            status_lbl.config(fg="#e74c3c")

        tk.Button(
            btn_row, text="  Confirm  ", bg="#2ecc71", fg="white",
            font=("Arial", 9, "bold"), relief=tk.FLAT, padx=6, pady=2,
            command=_confirm,
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(
            btn_row, text="  Reject  ", bg="#e74c3c", fg="white",
            font=("Arial", 9, "bold"), relief=tk.FLAT, padx=6, pady=2,
            command=_reject,
        ).pack(side=tk.LEFT, padx=(0, 8))
        status_lbl.pack(side=tk.LEFT)

        # Initialise visibility
        _show_manual(entry.get("status") == "rejected")

    # ── Legacy card ────────────────────────────────────────────────────────

    def _build_legacy_card(self, idx: int, entry: dict) -> None:
        """Build a card for a legacy-rarity miner that needs manual power/bonus entry."""
        bg = "#fff8e6"  # warm amber background to distinguish from match cards

        card = tk.Frame(
            self.inner, bg=bg, bd=1, relief=tk.GROOVE,
            padx=10, pady=8,
        )
        card.pack(fill=tk.X, padx=10, pady=4, ipady=2)
        card.columnconfigure(2, weight=1)

        # ── Thumbnail ──────────────────────────────────────────────────────
        photo = _load_thumb(entry.get("image", ""))
        self._photos.append(photo)
        tk.Label(
            card, bg=bg,
            image=photo if photo else None,
            text="" if photo else "[no image]",
            fg="#999", width=THUMB_W if not photo else 0,
        ).grid(row=0, column=0, rowspan=4, padx=(0, 14), sticky="nw")

        # ── Header ─────────────────────────────────────────────────────────
        tk.Label(
            card, text="\u2b50 LEGACY MINER", fg="#b8780a", bg=bg,
            font=("Arial", 9, "bold"),
        ).grid(row=0, column=1, columnspan=2, sticky="w")
        tk.Label(
            card, text=entry.get("html_name", "?"),
            font=("Arial", 10, "bold"), bg=bg, fg="#2c3e50",
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(1, 0))
        tk.Label(
            card,
            text="Not available on minaryganar.com \u2014 enter power and bonus manually.",
            fg="#888", bg=bg, font=("Arial", 8, "italic"),
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 4))

        # ── Input fields ───────────────────────────────────────────────────
        pwr_var  = tk.StringVar(value=str(entry.get("manual_power_th") or ""))
        bon_var  = tk.StringVar(value=str(entry.get("manual_bonus_pct") or ""))
        cell_var = tk.StringVar(value=str(entry.get("manual_cells") or 2))
        self._vars.append({
            "status":    tk.StringVar(value="legacy"),
            "power_th":  pwr_var,
            "bonus_pct": bon_var,
            "cells":     cell_var,
        })

        manual_row = tk.Frame(card, bg=bg)
        manual_row.grid(row=3, column=1, columnspan=2, sticky="w")
        tk.Label(manual_row, text="Power (TH):", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.Entry(manual_row, textvariable=pwr_var, width=12, font=("Arial", 9)).pack(
            side=tk.LEFT, padx=(2, 10),
        )
        tk.Label(manual_row, text="Bonus (%):", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.Entry(manual_row, textvariable=bon_var, width=8, font=("Arial", 9)).pack(
            side=tk.LEFT, padx=(2, 0),
        )
        tk.Label(manual_row, text="  Cells:", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.OptionMenu(manual_row, cell_var, "1", "2").pack(side=tk.LEFT, padx=(2, 0))

    # ── Missing-data card ─────────────────────────────────────────────────

    def _build_missing_data_card(self, idx: int, entry: dict) -> None:
        """Build a card for a miner whose DB data is missing or zero — user fills it in."""
        bg = "#fde8e8"  # light red background

        card = tk.Frame(
            self.inner, bg=bg, bd=1, relief=tk.GROOVE,
            padx=10, pady=8,
        )
        card.pack(fill=tk.X, padx=10, pady=4, ipady=2)
        card.columnconfigure(2, weight=1)

        # ── Thumbnail ──────────────────────────────────────────────────────
        photo = _load_thumb(entry.get("image", ""))
        self._photos.append(photo)
        tk.Label(
            card, bg=bg,
            image=photo if photo else None,
            text="" if photo else "[no image]",
            fg="#999", width=THUMB_W if not photo else 0,
        ).grid(row=0, column=0, rowspan=4, padx=(0, 14), sticky="nw")

        # ── Header ─────────────────────────────────────────────────────────
        rarity = entry.get("rarity", "common")
        tk.Label(
            card, text="\u26a0 FETCH FAILED \u2014 ENTER DATA MANUALLY", fg="#c0392b", bg=bg,
            font=("Arial", 9, "bold"),
        ).grid(row=0, column=1, columnspan=2, sticky="w")
        tk.Label(
            card, text=entry.get("html_name", "?"),
            font=("Arial", 10, "bold"), bg=bg, fg="#2c3e50",
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(1, 0))
        tk.Label(
            card,
            text=f"Rarity: {rarity}  \u2014  power/bonus data is missing or zero in the DB.",
            fg="#888", bg=bg, font=("Arial", 8, "italic"),
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 4))

        # ── Input fields ───────────────────────────────────────────────────
        pwr_var  = tk.StringVar(value=str(entry.get("manual_power_th") or ""))
        bon_var  = tk.StringVar(value=str(entry.get("manual_bonus_pct") or ""))
        cell_var = tk.StringVar(value=str(entry.get("manual_cells") or 2))
        self._vars.append({
            "status":    tk.StringVar(value="missing_data"),
            "power_th":  pwr_var,
            "bonus_pct": bon_var,
            "cells":     cell_var,
        })

        manual_row = tk.Frame(card, bg=bg)
        manual_row.grid(row=3, column=1, columnspan=2, sticky="w")
        tk.Label(manual_row, text="Power (TH):", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.Entry(manual_row, textvariable=pwr_var, width=12, font=("Arial", 9)).pack(
            side=tk.LEFT, padx=(2, 10),
        )
        tk.Label(manual_row, text="Bonus (%):", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.Entry(manual_row, textvariable=bon_var, width=8, font=("Arial", 9)).pack(
            side=tk.LEFT, padx=(2, 0),
        )
        tk.Label(manual_row, text="  Cells:", bg=bg, font=("Arial", 8)).pack(side=tk.LEFT)
        tk.OptionMenu(manual_row, cell_var, "1", "2").pack(side=tk.LEFT, padx=(2, 0))

    # ── Persistence ───────────────────────────────────────────────────────

    def _finish(self) -> None:
        # ── Validate completeness before saving ───────────────────────────
        import tkinter.messagebox as _mb
        issues: list[str] = []
        for i, entry in enumerate(self.entries):
            v      = self._vars[i]
            status = v["status"].get()
            name   = entry.get("html_name", f"miner {i + 1}")
            if status == "pending":
                issues.append(f"'{name}' \u2014 not yet confirmed or rejected")
            elif status in ("rejected", "legacy", "missing_data"):
                if not v["power_th"].get().strip():
                    issues.append(f"'{name}' \u2014 Power (TH) is required")
        if issues:
            _mb.showerror(
                "Incomplete entries",
                "Please resolve all miners before saving:\n\n"
                + "\n".join(f"\u2022 {s}" for s in issues),
            )
            return
        for i, entry in enumerate(self.entries):
            v = self._vars[i]
            entry["status"] = v["status"].get()
            if entry["status"] == "rejected":
                try:
                    pwr = float(v["power_th"].get().replace(",", ".") or "0")
                    entry["manual_power_th"] = pwr if pwr > 0 else None
                except ValueError:
                    entry["manual_power_th"] = None
                try:
                    bon = float(v["bonus_pct"].get().replace(",", ".") or "0")
                    entry["manual_bonus_pct"] = bon if bon > 0 else None
                except ValueError:
                    entry["manual_bonus_pct"] = None
                try:
                    entry["manual_cells"] = int(v["cells"].get())
                except (ValueError, KeyError):
                    entry["manual_cells"] = 2
                _replace_db_record(entry)
            elif entry["status"] in ("legacy", "missing_data"):
                try:
                    pwr = float(v["power_th"].get().replace(",", ".") or "0")
                    entry["manual_power_th"] = pwr if pwr > 0 else None
                except ValueError:
                    entry["manual_power_th"] = None
                try:
                    bon = float(v["bonus_pct"].get().replace(",", ".") or "0")
                    entry["manual_bonus_pct"] = bon if bon > 0 else None
                except ValueError:
                    entry["manual_bonus_pct"] = None
                try:
                    entry["manual_cells"] = int(v["cells"].get())
                except (ValueError, KeyError):
                    entry["manual_cells"] = 2
                if entry["status"] == "legacy":
                    _add_legacy_db_record(entry)
                else:
                    _add_missing_data_db_record(entry)
            else:
                # Clear any stale manual overrides
                entry.pop("manual_power_th", None)
                entry.pop("manual_bonus_pct", None)
                entry.pop("manual_cells", None)

        _save_log(self.entries)
        n_ok   = sum(1 for e in self.entries if e["status"] == "confirmed")
        n_rej  = sum(1 for e in self.entries if e["status"] == "rejected")
        n_leg  = sum(1 for e in self.entries if e["status"] == "legacy")
        n_miss = sum(1 for e in self.entries if e["status"] == "missing_data")
        n_pnd  = sum(1 for e in self.entries if e["status"] == "pending")
        print(f"Match log saved: {n_ok} confirmed, {n_rej} rejected, "
              f"{n_leg} legacy, {n_miss} missing data, {n_pnd} still pending")
        self.root.destroy()


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _replace_db_record(entry: dict) -> None:
    """
    For a rejected match: remove the wrong (found_name) record from miners_data.json
    and insert a minimal manual record for the real miner (html_name).
    """
    db_path = _ROOT / "miners/miners_data.json"
    if not db_path.exists():
        return
    try:
        data = json.loads(db_path.read_text(encoding="utf-8"))
    except Exception:
        return

    def _n(s: str) -> str:
        import re as _re
        s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
        return _re.sub(r"[^\w]+", "_", s).strip("_")

    found_key = _n(entry.get("found_name", ""))
    html_name = entry.get("html_name", "")

    # Remove the wrong-match record
    before = len(data)
    data = [m for m in data if _n(m["name"]) != found_key]
    removed = before - len(data)

    slug     = entry.get("slug", "")
    img_file = entry.get("image", "")
    ext      = Path(img_file).suffix if img_file else ".gif"
    miners_dir = _ROOT / "miners"

    # Resolve the image name this manual record should use:
    # prefer a slug-named alias (e.g. freoner_gen_0.gif) so that build_slug_index
    # maps the game slug directly to this manual record without ambiguity.
    import shutil as _shutil
    slug_img_name = f"{slug}{ext}" if slug else img_file
    slug_img_path = miners_dir / slug_img_name if slug_img_name else None
    orig_img_path = miners_dir / img_file if img_file else None

    # Copy original → slug alias if needed
    if slug_img_path and orig_img_path and orig_img_path.exists() and not slug_img_path.exists():
        _shutil.copy2(orig_img_path, slug_img_path)
        print(f"  Aliased image {img_file!r} -> '{slug_img_name}' for slug '{slug}'")

    # Delete the original found-miner image if it is no longer referenced by any DB record
    if orig_img_path and orig_img_path.exists() and slug_img_name != img_file:
        still_used = any(m.get("image") == img_file for m in data)
        if not still_used:
            orig_img_path.unlink()
            print(f"  Deleted stale image '{img_file}' (no longer referenced)")

    # Use slug image name for the new manual record
    manual_image = slug_img_name if (slug_img_path and slug_img_path.exists()) else img_file

    # Insert a minimal manual record for the real miner (if not already present)
    html_key = _n(html_name)
    if html_name and not any(_n(m["name"]) == html_key for m in data):
        p = entry.get("manual_power_th")
        b = entry.get("manual_bonus_pct")
        c = entry.get("manual_cells", 2)
        data.append({
            "name":  html_name,
            "image": manual_image,  # slug-named alias so slug_index maps correctly
            "cells": c,
            "rarities": {
                r: {
                    "power_th":  p if r == "common" else None,
                    "bonus_pct": b if r == "common" else None,
                }
                for r in ["common", "uncommon", "rare", "epic", "legendary", "unreal"]
            },
            "_manual": True,
        })
        print(f"  DB: removed {removed} record(s) for '{entry.get('found_name')}'"
              f" -> added manual record for '{html_name}' ({c} cell, {p} TH, {b}%)")
    db_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm(s: str) -> str:
    import re
    s = s.lower().replace("'", "").replace("\u2019", "").replace("-", "_")
    return re.sub(r"[^\w]+", "_", s).strip("_")


def main() -> None:
    _collect_legacy_miners()
    _collect_missing_data_miners()
    entries = _load_log()
    if not entries:
        print("No entries in match_log.json — nothing to verify.")
        return

    print(f"Opening verification window ({len(entries)} entries)...")
    root = tk.Tk()
    app  = VerifyWindow(root, entries)
    root.protocol("WM_DELETE_WINDOW", app._finish)
    root.mainloop()


if __name__ == "__main__":
    main()
