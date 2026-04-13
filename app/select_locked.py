"""
select_locked.py — Interactive UI to mark placed miners as locked.

Locked miners cannot be swapped by optimizer.py.  Use this to protect
miners that are placed on SET racks (position-dependent bonuses) or any
miner you just don't want to move.

Usage:
    python select_locked.py      # shows every room, page through with buttons

Controls:
    Click a miner       — toggle locked (red overlay = locked)
    Prev / Next room    — switch rooms
    Done                — save locked.json and close

Output:
    locked.json — list of locked miner positions read by optimizer.py
"""

import json
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageTk

# Reuse layout constants from the visualiser
from visualize_room import (
    CELL_W, CELL_H, NAME_HEIGHT, TOTAL_LABEL_H, PADDING, FONT, BACKGROUND,
    load_first_frame, load_badge, fetch_missing_gifs, find_all_placed,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOCKED_JSON  = Path(__file__).parent.parent / "data/locked.json"
CELL_TOTAL_H = CELL_H + TOTAL_LABEL_H      # full cell height including name + stats label
LOCK_FILL    = (220, 50, 50, 130)          # semi-transparent red
LOCK_BORDER  = (200, 20, 20, 255)
LOCK_TEXT    = (255, 255, 255, 255)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def miner_bbox(rack_idx: int, slot_idx: int) -> tuple[int, int, int, int]:
    """Pixel bounding box (x0, y0, x1, y1) for a miner cell on the canvas."""
    x0 = PADDING + rack_idx * (CELL_W + PADDING)
    y0 = PADDING + slot_idx * (CELL_TOTAL_H + PADDING)
    return x0, y0, x0 + CELL_W, y0 + CELL_TOTAL_H


def hit_test(cx: int, cy: int, racks: list[list[dict]]) -> tuple[int, int] | None:
    """Return (rack_idx, miner_idx) for the miner at canvas pixel (cx, cy), or None."""
    for rack_idx, rack in enumerate(racks):
        for miner_idx in range(len(rack)):
            x0, y0, x1, y1 = miner_bbox(rack_idx, miner_idx)
            if x0 <= cx < x1 and y0 <= cy < y1:
                return rack_idx, miner_idx
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _load_lock_font(size: int = 10) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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

LOCK_FONT = _load_lock_font(10)


def render_room_base(racks: list[list[dict]]) -> Image.Image:
    """Render the room into a PIL image (replicates visualize_room.render logic)."""
    from visualize_room import render as _render
    return _render(racks).convert("RGBA")


def apply_lock_overlays(base: Image.Image,
                        racks: list[list[dict]],
                        locked: set[tuple[int, int]]) -> Image.Image:
    """Draw red overlays on locked miners over the base image."""
    if not locked:
        return base

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for rack_idx, miner_idx in locked:
        if rack_idx >= len(racks) or miner_idx >= len(racks[rack_idx]):
            continue
        x0, y0, x1, y1 = miner_bbox(rack_idx, miner_idx)
        # Fill + border only over the image portion (not name label)
        draw.rectangle([x0, y0, x1 - 1, y0 + CELL_H - 1],
                       fill=LOCK_FILL, outline=LOCK_BORDER, width=2)
        draw.text(
            ((x0 + x1) // 2, y0 + CELL_H // 2),
            "LOCK",
            fill=LOCK_TEXT,
            anchor="mm",
            font=LOCK_FONT,
        )

    return Image.alpha_composite(base, overlay)


# ---------------------------------------------------------------------------
# Main GUI
# ---------------------------------------------------------------------------

class RoomSelector:
    def __init__(self, root: tk.Tk, placed_paths: list[Path]) -> None:
        self.root = root
        self.paths = placed_paths
        self.room_idx = 0
        self.rooms: list[dict] = []
        # locked[(room_i, rack_i, miner_i)] = True
        self.locked: list[set[tuple[int, int]]] = []
        self._photo = None  # must hold a reference to prevent GC

        for p in placed_paths:
            data = json.loads(p.read_text(encoding="utf-8"))
            self.rooms.append(data)
            self.locked.append(set())

        # Pre-load existing locked.json if present
        if LOCKED_JSON.exists():
            self._load_existing()

        root.title("RollerCoin — Lock miners  (click to lock / unlock)")

        # ── Top bar ──────────────────────────────────────────────────────
        top = tk.Frame(root, pady=4)
        top.pack(fill=tk.X, padx=8)

        self.prev_btn = tk.Button(top, text="◀ Prev", width=8, command=self._prev)
        self.prev_btn.pack(side=tk.LEFT)

        self.room_lbl = tk.Label(top, text="", font=("Arial", 11, "bold"), width=24)
        self.room_lbl.pack(side=tk.LEFT, padx=8)

        self.next_btn = tk.Button(top, text="Next ▶", width=8, command=self._next)
        self.next_btn.pack(side=tk.LEFT)

        tk.Label(top, text="  Click a miner to lock it from being swapped",
                 fg="#666").pack(side=tk.LEFT, padx=16)

        tk.Button(top, text="Done — save & close",
                  bg="#27ae60", fg="white", font=("Arial", 10, "bold"),
                  padx=10, command=self._finish).pack(side=tk.RIGHT)

        # ── Scrollable canvas ─────────────────────────────────────────────
        frame = tk.Frame(root)
        frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(frame, bg="white", cursor="hand2",
                                highlightthickness=0)
        hbar = tk.Scrollbar(frame, orient=tk.HORIZONTAL,
                            command=self.canvas.xview)
        vbar = tk.Scrollbar(frame, orient=tk.VERTICAL,
                            command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<Button-1>", self._on_click)
        # Mouse-wheel scroll
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        self._draw()

    # ── Navigation ────────────────────────────────────────────────────────

    def _prev(self) -> None:
        if self.room_idx > 0:
            self.room_idx -= 1
            self._draw()

    def _next(self) -> None:
        if self.room_idx < len(self.rooms) - 1:
            self.room_idx += 1
            self._draw()

    # ── Drawing ───────────────────────────────────────────────────────────

    def _draw(self) -> None:
        data  = self.rooms[self.room_idx]
        racks = data.get("racks", [])

        base    = render_room_base(racks)
        img     = apply_lock_overlays(base, racks, self.locked[self.room_idx])
        img_rgb = img.convert("RGB")

        self._photo = ImageTk.PhotoImage(img_rgb)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, img.width, img.height))

        # Resize window to fit (capped at screen size)
        w = min(img.width  + 30, self.root.winfo_screenwidth()  - 40)
        h = min(img.height + 70, self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{w}x{h}")

        n = self.room_idx + 1
        lk = len(self.locked[self.room_idx])
        self.room_lbl.config(text=f"Room {n} / {len(self.rooms)}  —  {lk} locked")
        self.prev_btn.config(state=tk.NORMAL if self.room_idx > 0 else tk.DISABLED)
        self.next_btn.config(
            state=tk.NORMAL if self.room_idx < len(self.rooms) - 1 else tk.DISABLED
        )

    # ── Click handler ─────────────────────────────────────────────────────

    def _on_click(self, event: tk.Event) -> None:
        cx = int(self.canvas.canvasx(event.x))
        cy = int(self.canvas.canvasy(event.y))
        racks = self.rooms[self.room_idx].get("racks", [])
        hit = hit_test(cx, cy, racks)
        if hit is None:
            return
        locked = self.locked[self.room_idx]
        if hit in locked:
            locked.discard(hit)
        else:
            locked.add(hit)
        self._draw()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_existing(self) -> None:
        """Restore a previously saved locked.json into self.locked."""
        try:
            entries = json.loads(LOCKED_JSON.read_text(encoding="utf-8"))
            for e in entries:
                room_i = e["room"] - 1   # stored 1-based
                if 0 <= room_i < len(self.locked):
                    self.locked[room_i].add((e["rack"], e["miner_idx"]))
        except Exception as exc:
            print(f"[!] Could not load existing locked.json: {exc}")

    def _save(self) -> None:
        output = []
        for room_i, (path, room_data) in enumerate(zip(self.paths, self.rooms)):
            racks = room_data.get("racks", [])
            for rack_idx, miner_idx in sorted(self.locked[room_i]):
                if rack_idx < len(racks) and miner_idx < len(racks[rack_idx]):
                    miner = racks[rack_idx][miner_idx]
                    output.append({
                        "room":      room_i + 1,
                        "rack":      rack_idx,
                        "miner_idx": miner_idx,
                        "name":      miner["name"],
                        "slug":      miner.get("slug", ""),
                    })
        LOCKED_JSON.write_text(
            json.dumps(output, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        total = sum(len(s) for s in self.locked)
        print(f"Saved {total} locked miner(s) to {LOCKED_JSON}")

    def _finish(self) -> None:
        self._save()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    paths = find_all_placed()
    if not paths:
        print("No placed_room*.json found. Run main.py first.")
        return

    print("Pre-fetching any missing miner GIFs...")
    for p in paths:
        data = json.loads(p.read_text(encoding="utf-8"))
        fetch_missing_gifs(data.get("racks", []))

    root = tk.Tk()
    app = RoomSelector(root, paths)
    root.protocol("WM_DELETE_WINDOW", app._finish)
    root.mainloop()


if __name__ == "__main__":
    main()
