"""
select_sets.py -- Define set bonuses rack by rack.

For each rack that has locked miners, select which of those locked miners
form a set, then define tier bonuses for that set.

Flow per rack:
  1. Click a locked miner on the canvas or in the right panel to toggle it
     into or out of the set.  Green = in set.  Blue = locked but not in set.
  2. Add threshold rows: >=N members placed -> bonus type -> value.

Output: data/set_groups.json  (read by optimizer.py)

Usage:
    python select_sets.py
"""

import json
import re
import sys
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageTk

from visualize_room import (
    CELL_W, CELL_H, TOTAL_LABEL_H, PADDING,
    find_all_placed,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT           = Path(__file__).parent.parent
SET_GROUPS_JSON = _ROOT / "data/set_groups.json"
LOCKED_JSON     = _ROOT / "data/locked.json"
CELL_TOTAL_H    = CELL_H + TOTAL_LABEL_H

# Per-miner highlight colors (RGBA)
_COLOR_IN_SET      = (46,  204, 113, 160)   # green -- locked + selected for set
_COLOR_LOCKED_ONLY = (52,  152, 219, 110)   # blue  -- locked but not in set
_COLOR_OTHER_SET   = (46,  204, 113,  50)   # faint green -- other racks that have a set


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_font(size: int = 10) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


_FONT = _load_font(10)


def _miner_bbox(rack_idx: int, miner_idx: int) -> tuple[int, int, int, int]:
    """Pixel bounding box for one miner cell."""
    x0 = PADDING + rack_idx * (CELL_W + PADDING)
    y0 = PADDING + miner_idx * (CELL_TOTAL_H + PADDING)
    return x0, y0, x0 + CELL_W, y0 + CELL_TOTAL_H


def _rack_bbox(rack_idx: int, num_miners: int) -> tuple[int, int, int, int] | None:
    """Pixel bounding box for an entire rack column."""
    if num_miners == 0:
        return None
    x0 = PADDING + rack_idx * (CELL_W + PADDING)
    y0 = PADDING
    y1 = PADDING + num_miners * (CELL_TOTAL_H + PADDING) - PADDING
    return x0, y0, x0 + CELL_W, y1


def _render_room(
    racks: list[list[dict]],
    active_rack_idx: int,
    locked_indices: set[int],
    selected_names: set[str],
    set_rack_indices: set[int],
) -> Image.Image:
    """
    Render the full room with per-miner highlight overlays.

    active_rack_idx  -- the rack currently being edited
    locked_indices   -- miner_idx values within active_rack_idx that are locked
    selected_names   -- names of locked miners chosen for the set
    set_rack_indices -- rack_idx values (other than active) that already have sets
    """
    from visualize_room import render as _render
    base    = _render(racks).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    # Faint green column for other racks that have a set
    for rack_idx, rack in enumerate(racks):
        if rack_idx == active_rack_idx or rack_idx not in set_rack_indices:
            continue
        bbox = _rack_bbox(rack_idx, len(rack))
        if bbox:
            draw.rectangle([bbox[0], bbox[1], bbox[2]-1, bbox[3]-1],
                           fill=_COLOR_OTHER_SET)
            draw.text((bbox[0] + CELL_W // 2, bbox[1] + 4), "SET",
                      fill=(255, 255, 255, 180), anchor="mt", font=_FONT)

    # Per-miner highlight within the active rack (locked miners only)
    active_rack = racks[active_rack_idx] if active_rack_idx < len(racks) else []
    lower_selected = {n.lower() for n in selected_names}
    for miner_idx, miner in enumerate(active_rack):
        if miner_idx not in locked_indices:
            continue
        in_set = miner["name"].lower() in lower_selected
        color  = _COLOR_IN_SET if in_set else _COLOR_LOCKED_ONLY
        x0, y0, x1, y1 = _miner_bbox(active_rack_idx, miner_idx)
        border = (color[0], color[1], color[2], 230)
        draw.rectangle([x0, y0, x1-1, y0+CELL_H-1],
                       fill=color, outline=border, width=2)
        label = "SET" if in_set else "LOCK"
        draw.text(((x0+x1)//2, y0 + CELL_H//2), label,
                  fill=(255, 255, 255, 230), anchor="mm", font=_FONT)

    return Image.alpha_composite(base, overlay).convert("RGB")


# ---------------------------------------------------------------------------
# UI class
# ---------------------------------------------------------------------------

class RackSetEditor:
    def __init__(self, root: tk.Tk, placed_paths: list[Path]) -> None:
        self.root = root

        # Load room data
        self.rooms: list[dict] = []
        self.room_nums: list[int] = []
        for p in sorted(placed_paths):
            data = json.loads(p.read_text(encoding="utf-8"))
            self.rooms.append(data)
            m = re.search(r"(\d+)", p.stem)
            self.room_nums.append(int(m.group(1)) if m else len(self.rooms))

        # Load locked.json:
        #   locked_miner_idx[(room_num, rack_idx)] = set of miner_idx that are locked
        #   locked_rack_keys = which (room_num, rack_idx) pairs have any locked miner
        self.locked_miner_idx: dict[tuple[int, int], set[int]] = {}
        locked_rack_keys: set[tuple[int, int]] = set()
        if LOCKED_JSON.exists():
            try:
                for entry in json.loads(LOCKED_JSON.read_text(encoding="utf-8")):
                    rn  = entry.get("room")
                    rik = entry.get("rack")
                    mi  = entry.get("miner_idx")
                    if rn is not None and rik is not None:
                        locked_rack_keys.add((rn, rik))
                        if mi is not None:
                            self.locked_miner_idx.setdefault((rn, rik), set()).add(mi)
            except Exception:
                pass

        # Only show racks with locked miners
        self.all_racks: list[tuple[int, int]] = []
        for ri, room in enumerate(self.rooms):
            rn = self.room_nums[ri]
            for rack_idx in range(len(room.get("racks", []))):
                if (rn, rack_idx) in locked_rack_keys:
                    self.all_racks.append((ri, rack_idx))

        self.cur: int = 0

        # rack_sets[(room_num, rack_idx)] = {"selected_names": [...], "thresholds": [...]}
        self.rack_sets: dict[tuple[int, int], dict] = {}
        self._load_existing()

        self._thresh_vars: list[dict[str, tk.Variable]] = []
        self._photo = None

        root.title("RollerCoin -- Set Member Selection & Bonuses")
        self._build_ui()

        if not self.all_racks:
            messagebox.showinfo(
                "No locked racks",
                "No racks with locked miners were found.\n\n"
                "Go back to Lock Miners and lock every miner that belongs "
                "to a set rack. Only those racks will appear here.",
            )
            root.destroy()
            return

        self._refresh()

        root.update_idletasks()
        w = min(int(root.winfo_screenwidth() * 0.92), 1440)
        h = min(int(root.winfo_screenheight() * 0.88), 960)
        root.geometry(f"{w}x{h}")

    # __ Persistence ____________________________________________________________

    def _load_existing(self) -> None:
        if not SET_GROUPS_JSON.exists():
            return
        try:
            existing = json.loads(SET_GROUPS_JSON.read_text(encoding="utf-8"))
        except Exception:
            return
        for sg in existing:
            rn     = sg.get("room")
            ri_key = sg.get("rack")
            if rn is None or ri_key is None:
                continue
            self.rack_sets[(rn, ri_key)] = {
                "selected_names": list(sg.get("member_names", [])),
                "thresholds":     sg.get("thresholds", []),
            }

    # __ State helpers ___________________________________________________________

    def _cur_key(self) -> tuple[int, int]:
        ri, rack_idx = self.all_racks[self.cur]
        return (self.room_nums[ri], rack_idx)

    def _cur_locked_miners(self) -> list[tuple[int, str]]:
        """Return [(miner_idx, name), ...] for every locked miner in the current rack."""
        ri, rack_idx = self.all_racks[self.cur]
        rn   = self.room_nums[ri]
        rack = self.rooms[ri].get("racks", [])[rack_idx] if rack_idx < len(self.rooms[ri].get("racks", [])) else []
        locked_idx = self.locked_miner_idx.get((rn, rack_idx), set())
        return [(mi, m["name"]) for mi, m in enumerate(rack) if mi in locked_idx]

    def _cur_selected_names(self) -> set[str]:
        key = self._cur_key()
        return set(self.rack_sets.get(key, {}).get("selected_names", []))

    def _set_rack_indices_for_room(self, ri: int) -> set[int]:
        rn = self.room_nums[ri]
        return {
            rack_idx
            for (room_num, rack_idx), data in self.rack_sets.items()
            if room_num == rn and data.get("selected_names")
        }

    # __ UI construction _________________________________________________________

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, pady=4)
        top.pack(fill=tk.X, padx=8)

        self.prev_btn = tk.Button(top, text="< Prev Rack", width=12, command=self._prev)
        self.prev_btn.pack(side=tk.LEFT)
        self.nav_lbl = tk.Label(top, text="", font=("Arial", 11, "bold"), width=32)
        self.nav_lbl.pack(side=tk.LEFT, padx=8)
        self.next_btn = tk.Button(top, text="Next Rack >", width=12, command=self._next)
        self.next_btn.pack(side=tk.LEFT)
        tk.Button(
            top, text="Done -- save & close",
            bg="#27ae60", fg="white", font=("Arial", 10, "bold"), padx=10,
            command=self._finish,
        ).pack(side=tk.RIGHT)

        pane = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=5, bg="#bbb")
        pane.pack(fill=tk.BOTH, expand=True)

        # Left: canvas
        canvas_frame = tk.Frame(pane)
        pane.add(canvas_frame, minsize=300)
        hbar = tk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        vbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(
            canvas_frame, bg="white", highlightthickness=0,
            xscrollcommand=hbar.set, yscrollcommand=vbar.set,
        )
        hbar.config(command=self.canvas.xview)
        vbar.config(command=self.canvas.yview)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.root.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"),
        )

        # Right: set editor
        right = tk.Frame(pane, width=360, padx=10, pady=8, bg="#f5f5f5")
        pane.add(right, minsize=300)
        right.pack_propagate(False)

        tk.Label(right, text="Set Member Selection",
                 font=("Arial", 12, "bold"), bg="#f5f5f5").pack(anchor="w", pady=(0, 4))
        tk.Label(
            right,
            text=(
                "Click a locked miner (on the canvas or below) to add/remove it "
                "from the set.  Green = in set.  Blue = locked but not in set."
            ),
            fg="#555", font=("Arial", 8), bg="#f5f5f5", wraplength=330, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        # Member buttons (rebuilt on navigation)
        self.members_outer = tk.Frame(right, bg="#f5f5f5")
        self.members_outer.pack(fill=tk.X)

        tk.Frame(right, height=1, bg="#cccccc").pack(fill=tk.X, pady=8)

        tk.Label(right, text="Thresholds:", font=("Arial", 10, "bold"),
                 bg="#f5f5f5").pack(anchor="w")
        tk.Label(
            right,
            text=(
                "Each threshold activates when >= N of this set's members are placed.\n"
                "  pct    -- adds % to the bonus multiplier\n"
                "  raw_th -- adds TH directly after multiplication"
            ),
            fg="#666", font=("Arial", 8), bg="#f5f5f5", wraplength=330, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        thresh_outer = tk.Frame(right, bg="#f5f5f5")
        thresh_outer.pack(fill=tk.BOTH, expand=True)
        thresh_vbar = tk.Scrollbar(thresh_outer, orient=tk.VERTICAL)
        self._thresh_canvas = tk.Canvas(
            thresh_outer, bg="#f5f5f5", highlightthickness=0, bd=0,
            yscrollcommand=thresh_vbar.set,
        )
        thresh_vbar.config(command=self._thresh_canvas.yview)
        thresh_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._thresh_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.thresh_frame = tk.Frame(self._thresh_canvas, bg="#f5f5f5")
        self._thresh_canvas.create_window((0, 0), window=self.thresh_frame, anchor="nw")
        self.thresh_frame.bind(
            "<Configure>",
            lambda e: self._thresh_canvas.configure(
                scrollregion=self._thresh_canvas.bbox("all")
            ),
        )

        tk.Button(right, text="+ Add Threshold",
                  command=self._add_threshold).pack(anchor="w", pady=(6, 2))

        self.state_lbl = tk.Label(
            right, text="", fg="#888", font=("Arial", 8, "italic"),
            bg="#f5f5f5", wraplength=330,
        )
        self.state_lbl.pack(anchor="w", pady=(4, 0))

    # __ Navigation _____________________________________________________________

    def _prev(self) -> None:
        if self.cur > 0:
            self._commit_thresholds()
            self.cur -= 1
            self._refresh()

    def _next(self) -> None:
        if self.cur < len(self.all_racks) - 1:
            self._commit_thresholds()
            self.cur += 1
            self._refresh()

    # __ Canvas click ____________________________________________________________

    def _on_canvas_click(self, event: tk.Event) -> None:
        cx = int(self.canvas.canvasx(event.x))
        cy = int(self.canvas.canvasy(event.y))

        ri, rack_idx = self.all_racks[self.cur]
        rn   = self.room_nums[ri]
        rack = self.rooms[ri].get("racks", [])
        if rack_idx >= len(rack):
            return
        locked_idx = self.locked_miner_idx.get((rn, rack_idx), set())

        for miner_idx, miner in enumerate(rack[rack_idx]):
            if miner_idx not in locked_idx:
                continue
            x0, y0, x1, y1 = _miner_bbox(rack_idx, miner_idx)
            if x0 <= cx < x1 and y0 <= cy < y1:
                self._toggle_member(miner["name"])
                return

    def _toggle_member(self, name: str) -> None:
        key  = self._cur_key()
        data = self.rack_sets.setdefault(key, {"selected_names": [], "thresholds": []})
        names     = data["selected_names"]
        name_lower = name.lower()
        if any(n.lower() == name_lower for n in names):
            data["selected_names"] = [n for n in names if n.lower() != name_lower]
        else:
            data["selected_names"].append(name)
        self._refresh_right(rebuild_thresholds=False)
        self._redraw()

    # __ Refresh _________________________________________________________________

    def _refresh(self) -> None:
        if not self.all_racks:
            return
        ri, rack_idx = self.all_racks[self.cur]
        rn    = self.room_nums[ri]
        total = len(self.all_racks)
        self.nav_lbl.config(
            text=f"Room {rn} -- Rack {rack_idx + 1}  ({self.cur + 1} / {total})"
        )
        self.prev_btn.config(state=tk.NORMAL if self.cur > 0 else tk.DISABLED)
        self.next_btn.config(state=tk.NORMAL if self.cur < total - 1 else tk.DISABLED)
        self._refresh_right(rebuild_thresholds=True)
        self._redraw()

    def _refresh_right(self, rebuild_thresholds: bool = False) -> None:
        self._build_member_rows()
        if rebuild_thresholds:
            self._build_thresh_rows()

        key      = self._cur_key()
        selected = self._cur_selected_names()
        locked   = self._cur_locked_miners()
        n_thresh = len(self.rack_sets.get(key, {}).get("thresholds", []))

        if selected:
            self.state_lbl.config(
                text=f"{len(selected)} / {len(locked)} locked miner(s) in set  *  {n_thresh} threshold(s)"
            )
        else:
            self.state_lbl.config(text="No miners selected for this set yet -- click one above")

    # __ Member rows _____________________________________________________________

    def _build_member_rows(self) -> None:
        for w in self.members_outer.winfo_children():
            w.destroy()

        locked   = self._cur_locked_miners()
        selected = self._cur_selected_names()

        if not locked:
            tk.Label(self.members_outer, text="(no locked miners found in this rack)",
                     fg="#aaa", font=("Arial", 8, "italic"), bg="#f5f5f5").pack(anchor="w")
            return

        for _, name in locked:
            in_set = name.lower() in {n.lower() for n in selected}
            if in_set:
                row_bg = "#d5f5e3"; row_fg = "#1e8449"; label = f"[SET]  {name}"; weight = "bold"
            else:
                row_bg = "#eaf4fb"; row_fg = "#1a5276"; label = f"         {name}"; weight = "normal"
            tk.Button(
                self.members_outer,
                text=label,
                bg=row_bg, fg=row_fg, relief=tk.FLAT,
                font=("Arial", 9, weight),
                anchor="w", padx=6, pady=3,
                command=lambda n=name: self._toggle_member(n),
            ).pack(fill=tk.X, pady=1)

    # __ Canvas __________________________________________________________________

    def _redraw(self) -> None:
        ri, rack_idx = self.all_racks[self.cur]
        rn     = self.room_nums[ri]
        racks  = self.rooms[ri].get("racks", [])
        locked = self.locked_miner_idx.get((rn, rack_idx), set())
        sel    = self._cur_selected_names()
        other  = self._set_rack_indices_for_room(ri) - {rack_idx}

        img = _render_room(racks, rack_idx, locked, sel, other)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, img.width, img.height))

    # __ Threshold rows __________________________________________________________

    def _build_thresh_rows(self) -> None:
        for w in self.thresh_frame.winfo_children():
            w.destroy()
        self._thresh_vars.clear()

        key        = self._cur_key()
        thresholds = self.rack_sets.get(key, {}).get("thresholds", [])

        if not thresholds:
            tk.Label(
                self.thresh_frame,
                text='(no thresholds yet -- click "+ Add Threshold")',
                fg="#aaa", font=("Arial", 8, "italic"), bg="#f5f5f5",
            ).pack(anchor="w", padx=4, pady=4)

        for i, thresh in enumerate(thresholds):
            self._add_thresh_row(i, thresh)

    def _add_thresh_row(self, idx: int, data: dict | None = None) -> None:
        row   = tk.Frame(self.thresh_frame, bg="#f5f5f5")
        row.pack(fill=tk.X, pady=2, padx=2)

        n_var = tk.IntVar(value=(data or {}).get("min_members", 3))
        t_var = tk.StringVar(value=(data or {}).get("type", "pct"))
        v_var = tk.StringVar(value=str((data or {}).get("value", 0.0)))

        tk.Label(row, text=">=", bg="#f5f5f5", width=2).pack(side=tk.LEFT)
        tk.Spinbox(row, textvariable=n_var, from_=1, to=99, width=4).pack(side=tk.LEFT, padx=2)
        tk.Label(row, text="miners ->", bg="#f5f5f5").pack(side=tk.LEFT)
        tk.OptionMenu(row, t_var, "pct", "raw_th").pack(side=tk.LEFT, padx=2)
        tk.Label(row, text="->", bg="#f5f5f5").pack(side=tk.LEFT)
        tk.Entry(row, textvariable=v_var, width=9).pack(side=tk.LEFT, padx=2)

        def _remove(i: int = idx) -> None:
            self._commit_thresholds()
            key = self._cur_key()
            if key in self.rack_sets:
                tl = self.rack_sets[key].get("thresholds", [])
                if i < len(tl):
                    tl.pop(i)
            self._build_thresh_rows()
            self._refresh_right(rebuild_thresholds=False)

        tk.Button(row, text="X", fg="#c0392b", pady=0,
                  command=_remove).pack(side=tk.LEFT, padx=2)
        self._thresh_vars.append({"n": n_var, "t": t_var, "v": v_var})

    def _add_threshold(self) -> None:
        self._commit_thresholds()
        key = self._cur_key()
        self.rack_sets.setdefault(key, {"selected_names": [], "thresholds": []})
        self.rack_sets[key]["thresholds"].append(
            {"min_members": 3, "type": "pct", "value": 0.0}
        )
        self._build_thresh_rows()
        self._refresh_right(rebuild_thresholds=False)

    def _commit_thresholds(self) -> None:
        if not self._thresh_vars:
            return
        key = self._cur_key()
        new_thresholds = []
        for tv in self._thresh_vars:
            try:
                new_thresholds.append({
                    "min_members": int(tv["n"].get()),
                    "type":        tv["t"].get(),
                    "value":       float(tv["v"].get()),
                })
            except (tk.TclError, ValueError):
                pass
        if key in self.rack_sets:
            self.rack_sets[key]["thresholds"] = new_thresholds

    # __ Save ____________________________________________________________________

    def _finish(self) -> None:
        self._commit_thresholds()

        result = []
        for (room_num, rack_idx), data in sorted(self.rack_sets.items()):
            selected_names = data.get("selected_names", [])
            thresholds     = data.get("thresholds", [])
            if not selected_names or not thresholds:
                continue   # skip racks with no members selected or no thresholds

            result.append({
                "name":         f"Room {room_num} Rack {rack_idx + 1}",
                "room":         room_num,
                "rack":         rack_idx,
                "member_names": selected_names,
                "thresholds":   thresholds,
            })

        SET_GROUPS_JSON.parent.mkdir(parents=True, exist_ok=True)
        SET_GROUPS_JSON.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[select_sets] Saved {len(result)} rack set(s) to {SET_GROUPS_JSON}")
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    placed_paths = find_all_placed()
    if not placed_paths:
        print("[select_sets] No placed_room*.json files found in data/.")
        return

    root = tk.Tk()
    RackSetEditor(root, placed_paths)
    root.mainloop()


if __name__ == "__main__":
    main()
