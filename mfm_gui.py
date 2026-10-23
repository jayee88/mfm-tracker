"""
Live monitoring window of Free Market Tracker (mfm-tracker). Run with:  python mfm-tracker gui

Leave it running while you play. Every shop you open and scroll through is read
continuously; each listing appears in the Live tab the moment it's logged,
coloured by how it compares with market rate, along with which shop and map
it's in.
"""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
import queue
import threading
import time
import sqlite3
import sys
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import mfm


# ---- fonts --------------------------------------------------------------------
# The app ships the Inter font (packaging/fonts, SIL Open Font License). Tk can only
# use installed fonts, so it's made available to this process alone: on Linux via
# a fontconfig file that adds our folder to the system's fonts, on Windows as a
# private font, on macOS through CoreText for this process only. Nothing is
# installed on the system.
UI_FAMILY = "Inter"


def _font_dir() -> Path | None:
    for d in (mfm.BUNDLE / "fonts", Path(__file__).resolve().parent / "packaging" / "fonts"):
        if d.is_dir() and any(d.glob("*.otf")):
            return d
    return None


def _register_fonts_macos(d: Path) -> None:
    import ctypes
    cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ct = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreText.framework/CoreText")
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                          ctypes.c_long, ctypes.c_bool]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
    ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    for f in d.glob("*.otf"):
        raw = str(f).encode("utf-8")
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
        if url:
            ct.CTFontManagerRegisterFontsForURL(url, 1, None)     # 1 = this process only
            cf.CFRelease(url)


MAC = mfm.sys.platform == "darwin"


def bind_right_click(widget, fn) -> None:
    """Context menus: right button everywhere; on a Mac also Control+click, and
    Button-2, which is the right button in older Tk versions there."""
    widget.bind("<Button-3>", fn)
    if MAC:
        widget.bind("<Button-2>", fn)
        widget.bind("<Control-Button-1>", fn)


def register_fonts() -> None:
    """Must run before the first tk.Tk() (fontconfig is read when Tk starts)."""
    d = _font_dir()
    if d is None:
        return
    try:
        if mfm.sys.platform == "win32":
            import ctypes
            for f in d.glob("*.otf"):
                ctypes.windll.gdi32.AddFontResourceExW(str(f), 0x10, 0)   # FR_PRIVATE
        elif mfm.sys.platform == "darwin":
            _register_fonts_macos(d)
        elif "FONTCONFIG_FILE" not in mfm.os.environ or mfm.FROZEN:
            conf = mfm.CACHE_DIR / "fonts.conf"
            system = "/etc/fonts/fonts.conf"
            conf.write_text(
                '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
                + (f'  <include ignore_missing="yes">{system}</include>\n' if Path(system).exists() else "")
                + f"  <dir>{d}</dir>\n</fontconfig>\n", encoding="utf-8")
            mfm.os.environ["FONTCONFIG_FILE"] = str(conf)
    except Exception:
        pass


register_fonts()


def apply_fonts(root) -> str:
    """Use Inter for every standard Tk font (falls back to the system font if it
    couldn't be loaded). Also defines MfmBold / MfmBig for emphasis and titles."""
    from tkinter import font as tkfont
    fams = set(tkfont.families(root))
    fam = UI_FAMILY if UI_FAMILY in fams else tkfont.nametofont("TkDefaultFont", root).actual("family")
    size = 9 if mfm.sys.platform == "win32" else 13 if MAC else 10
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                 "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
        try:
            f = tkfont.nametofont(name, root)
            f.configure(family=fam, size=size - (1 if name in ("TkSmallCaptionFont", "TkTooltipFont") else 0))
        except tk.TclError:
            pass
    tkfont.nametofont("TkHeadingFont", root).configure(weight="bold")
    keep = getattr(root, "_mfm_fonts", {})   # Tk deletes a named font when its object is freed
    for name, sz, wt in (("MfmBold", size, "bold"), ("MfmBig", size + 2, "bold")):
        try:
            keep[name] = tkfont.Font(root, name=name, family=fam, size=sz, weight=wt)
        except tk.TclError:   # already defined (second window / tests)
            tkfont.nametofont(name, root).configure(family=fam, size=sz, weight=wt)
    root._mfm_fonts = keep
    return fam

COLS = ("item", "each", "market", "diff", "verdict")
HEAD = {"item": "Item", "each": "Price", "market": "Market",
        "diff": "Diff", "verdict": "Verdict", "shop": "Shop", "player": "Player", "map": "Map",
        "channel": "Channel", "best_shop": "Shop", "best_player": "Player", "best_map": "Map",
        "best_channel": "Channel",
        "time": "Time", "low": "Low", "high": "High", "seen": "Seen",
        "source": "Source", "best": "Cheapest", "best_where": "Cheapest at",
        "status": "Status", "listed_med": "Listed med.", "sold_med": "Sold med.",
        "listed_n": "Listed", "sold_n": "Sold", "total": "Bundle price",
        "max_each": "Max price", "last_price": "Last price", "npc_buy": "NPC buy", "npc_sell": "NPC sell",
        "saved": "Saved", "notes": "Notes", "qty": "Qty", "range": "Price range",
        "points": "Data points", "sold24": "Sold 24h", "last3": "Avg last 3 sales", "tab": "Tab",
        "match": "Match / suggestion", "list_price": "List price", "message": "Message",
        "side": "Buying / Selling", "phrase": "Item", "mprice": "Price", "msell": "Megaphones: selling",
        "mbuy": "Megaphones: buying", "aterm": "Megaphone term", "avalue": "Alias", "atype": "Type",
        "suggest": "Suggested item", "score": "Alike", "count": "Listings", "raw": "What OCR read"}
WIDTH = {"item": 155, "each": 82, "market": 82, "diff": 52, "verdict": 62,
         "shop": 140, "player": 100, "map": 265, "channel": 72,
         "best_shop": 140, "best_player": 100, "best_map": 265, "best_channel": 72,
         "time": 126, "low": 95, "high": 95, "seen": 50, "source": 110,
         "best": 90, "best_where": 230, "status": 66, "listed_med": 95, "sold_med": 90,
         "listed_n": 60, "sold_n": 45, "total": 100, "max_each": 110, "last_price": 100, "npc_buy": 85, "npc_sell": 85,
         "saved": 110, "notes": 170, "qty": 55, "range": 170, "points": 90, "sold24": 75,
         "last3": 130, "tab": 70, "match": 230, "list_price": 95, "message": 600,
         "side": 120, "phrase": 220, "mprice": 95, "msell": 140, "mbuy": 135, "aterm": 240, "avalue": 300, "atype": 90, "suggest": 260, "score": 60, "count": 65, "raw": 300}
NUMERIC = {"each", "market", "diff", "low", "high", "seen", "best", "listed_med",
           "sold_med", "listed_n", "sold_n", "total", "score", "count", "channel", "best_channel",
           "max_each", "last_price", "npc_buy", "npc_sell", "qty", "range", "points", "sold24", "last3", "list_price",
           "mprice", "msell", "mbuy"}
CENTER = {"channel", "best_channel"}


def monitor_work_area(widget) -> tuple | None:
    """(left, top, right, bottom) of the monitor the widget is on, without the
    taskbar. Windows only; None elsewhere (one screen is assumed there)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        u = ctypes.windll.user32
        mon = u.MonitorFromWindow(widget.winfo_id(), 2)      # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not u.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        r = info.rcWork
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def shop_text(shop, seller) -> str:
    """ "Title (Seller)", for one-line summaries (status bar, recent shops)."""
    return mfm.ShopFrame([], shop or "", seller or "").shop


def split_shop(text: str) -> tuple[str, str]:
    """ "Title (Seller)" (saved by older versions) -> ("Title", "Seller")."""
    m = re.fullmatch(r"(.*?)\s*\(([^()]*)\)\s*", text or "")
    return (m.group(1), m.group(2)) if m else (text or "", "")


def where_text(shop, seller, mp, channel) -> str:
    return mfm.location(mfm.ShopFrame([], shop or "", seller or "").shop, mp, channel)


def num(s: str) -> float:
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", ""))
    except ValueError:
        return float("-inf")




WATCH_FG = "#d50000"       # rows with a watched item for sale (shops and megaphones): bold red

# the capture areas you can draw (Settings → Reading → Window detection): config key, name, what
# it's for
AREAS = [("region", "Shop", "the store window"),
         ("map_region", "Map name", "the minimap's street and map name"),
         ("chat_region", "Chat", "the chat messages, for megaphones"),
         ("inventory_region", "Item inventory", "your ITEM INVENTORY window, for My Inventory"),
         ("channel_region", "Change Channel window", "where the game's Change Channel window opens")]
AREA_NAMES = {k: n + " area" for k, n, _ in AREAS}


def area_text(region) -> str:
    """[left, top, width, height] as 'x 812, y 340, 520 × 410'."""
    try:
        l, t, w, h = (int(v) for v in region)
    except (TypeError, ValueError):
        return "automatic"
    return f"x {l}, y {t}, {w} × {h}"

class Tooltip:
    """Small help text that appears when the mouse rests on a widget for a moment.
    Only one is ever shown at a time."""
    DELAY_MS = 500
    active = None

    def __init__(self, widget, text: str, wrap: int = 340):
        self.widget, self.text, self.wrap = widget, text, wrap
        self.tip = self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None):
        self._cancel()
        self.job = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self):
        self.job = None
        if self.tip or not self.widget.winfo_exists():
            return
        if Tooltip.active is not None and Tooltip.active is not self:
            Tooltip.active._hide()
        Tooltip.active = self
        x, y = self._pos()
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(tw, text=self.text, justify="left", wraplength=self.wrap, background="#fffbe6",
                 foreground="#222", relief="solid", borderwidth=1, padx=8, pady=5,
                 font="TkTooltipFont").pack()

    def _pos(self) -> tuple[int, int]:
        return (self.widget.winfo_rootx() + 12,
                self.widget.winfo_rooty() + self.widget.winfo_height() + 4)

    def _hide(self, _=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None
        if Tooltip.active is self:
            Tooltip.active = None


class CanvasTip(Tooltip):
    """A Tooltip for the canvas items with this tag; shown by the mouse."""

    def __init__(self, canvas, tag: str, text: str):
        self.widget, self.text, self.wrap = canvas, text, 340
        self.tip = self.job = None
        canvas.tag_bind(tag, "<Enter>", self._schedule, add="+")
        canvas.tag_bind(tag, "<Leave>", self._hide, add="+")
        canvas.tag_bind(tag, "<ButtonPress>", self._hide, add="+")

    def _pos(self) -> tuple[int, int]:
        return self.widget.winfo_pointerx() + 12, self.widget.winfo_pointery() + 18


# Shown in Settings → About. (name, what it is / how it's used, link)
CREDITS = [
    ("Created by", [
        ("Jayee (Discord: @jayee88)", "", None),
    ]),
    ("Made with", [
        ("Claude Opus 5.5 (claude-opus-5-5) by Anthropic",
         "AI model that wrote the code, tests and documentation, working with you in Claude",
         "https://www.anthropic.com/claude"),
    ]),
    ("Game data", [
        ("NiaMeowDB (meowdb.com) — the meowdb team",
         "Item and map lists, item ids and icons for MapleStory Classic World",
         "https://meowdb.com/msclassic/"),
        ("OSMS Data Explorer (osmsdataexplorer.com) — @ohmi",
         "Backup item and map lists and icons, mined from MapleStory Classic World",
         "https://osmsdataexplorer.com/"),
    ]),
    ("Open source", [
        ("Tesseract OCR 5.3.4", "reads the text in shop windows · Apache-2.0",
         "https://github.com/tesseract-ocr/tesseract"),
        ("Tesseract English data (tessdata_fast)", "OCR language model · Apache-2.0",
         "https://github.com/tesseract-ocr/tessdata_fast"),
        ("Leptonica 1.87.0", "image library used by Tesseract · BSD-2-Clause",
         "https://github.com/DanBloomberg/leptonica"),
        ("libpng and zlib", "image compression used by Leptonica · libpng and zlib licenses",
         "https://github.com/pnggroup/libpng"),
        ("Python 3.13", "the language the app is written in · PSF License", "https://www.python.org/"),
        ("python-build-standalone", "portable Python used to build the Linux app · MPL-2.0",
         "https://github.com/astral-sh/python-build-standalone"),
        ("Tcl/Tk 9.0.3", "the window toolkit (tkinter) · Tcl/Tk license (BSD-style)",
         "https://github.com/tcltk/tk"),
        ("libXft 2.3.9", "smooth fonts for Tk on Linux, from OpenBSD's xenocara · HPND (MIT-style)",
         "https://github.com/openbsd/xenocara"),
        ("Pillow", "image processing · MIT-CMU (HPND)", "https://github.com/python-pillow/Pillow"),
        ("pytesseract", "Python bridge to Tesseract · Apache-2.0", "https://github.com/madmaze/pytesseract"),
        ("python-mss", "screen capture · MIT", "https://github.com/BoboTiG/python-mss"),
        ("pyspellchecker", "English word list for shop titles · MIT", "https://github.com/barrust/pyspellchecker"),
        ("SQLite", "the price database · public domain", "https://www.sqlite.org/"),
        ("Inter typeface", "the app's font · SIL Open Font License 1.1", "https://github.com/rsms/inter"),
        ("PyInstaller 6.22 (with pyinstaller-hooks-contrib, altgraph)",
         "packs everything into one app · GPL-2.0 with bootloader exception, Apache-2.0, MIT",
         "https://github.com/pyinstaller/pyinstaller"),
        ("pynput", "the hotkeys (keys that work while the game has focus) and the command-line `hotkey` mode · LGPL-3.0",
         "https://github.com/moses-palmer/pynput"),
    ]),
]


def tip(text: str, *widgets) -> None:
    for w in widgets:
        Tooltip(w, text)


# ------------------------------------------------------------------ hotkeys
# Keys that do their action in whichever window has focus, the game's included
# (Settings → General → Hotkeys), through pynput. Each is kept as shown there
# ("F5", "Ctrl+Shift+M") and turned into pynput's form ("<ctrl>+<shift>+m") to listen.
HOTKEY_ACTIONS = (("monitor", "Start / stop monitoring"),
                  ("channel_up", "Channel up"),
                  ("channel_down", "Channel down"))
_MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                     "Super_L", "Super_R", "Win_L", "Win_R", "Caps_Lock", "Num_Lock", "ISO_Level3_Shift"}
_KEY_NAMES = {"space": "Space", "Escape": "Esc", "Return": "Enter", "KP_Enter": "Enter", "Tab": "Tab",
              "BackSpace": "Backspace", "Delete": "Delete", "Insert": "Insert", "Home": "Home", "End": "End",
              "Prior": "Page Up", "Next": "Page Down", "Up": "Up", "Down": "Down", "Left": "Left",
              "Right": "Right", "Pause": "Pause", "Scroll_Lock": "Scroll Lock", "Print": "Print Screen"}
_PYNPUT_NAMES = {"space": "space", "esc": "esc", "enter": "enter", "tab": "tab", "backspace": "backspace",
                 "delete": "delete", "insert": "insert", "home": "home", "end": "end", "page up": "page_up",
                 "page down": "page_down", "up": "up", "down": "down", "left": "left", "right": "right",
                 "pause": "pause", "scroll lock": "scroll_lock", "print screen": "print_screen",
                 "ctrl": "ctrl", "alt": "alt", "shift": "shift", "cmd": "cmd"}


def hotkey_cfg(cfg: dict) -> dict:
    """Each action's key as set ("F5"; "" = no key), the default for any not saved."""
    return {**mfm.DEFAULT_CONFIG["hotkeys"], **(cfg.get("hotkeys") or {})}


def hotkey_from_event(e) -> str | None:
    """The key of a Tk key press as a hotkey ("F5", "Ctrl+Shift+M"); None for one that
    can't be used (a modifier on its own, the numeric keypad, a shifted symbol)."""
    ks = e.keysym
    if ks in _MODIFIER_KEYSYMS or (ks.startswith("KP_") and ks != "KP_Enter"):
        return None
    if re.fullmatch(r"F([1-9]|1\d|20)", ks):
        key = ks
    elif ks in _KEY_NAMES:
        key = _KEY_NAMES[ks]
    elif len(ks) == 1 and ks.isalnum():
        key = ks.upper()
    elif len(e.char) == 1 and e.char.isprintable() and not e.char.isspace() and e.char != "+" \
            and not e.state & 0x1:
        key = e.char                  # a symbol key, as it reads without Shift
    else:
        return None
    alt = 0x20000 if sys.platform == "win32" else 0x10 if sys.platform == "darwin" else 0x8
    mods = [name for bit, name in ((0x4, "Ctrl"), (alt, "Alt"), (0x1, "Shift")) if e.state & bit]
    if sys.platform == "darwin" and e.state & 0x8:
        mods.append("Cmd")
    return "+".join(mods + [key])


def hotkey_spec(text: str) -> str | None:
    """A hotkey as shown ("Ctrl+F5") in pynput's form ("<ctrl>+<f5>"); None when empty
    or not a key."""
    parts = [p.strip() for p in (text or "").split("+") if p.strip()]
    if not parts:
        return None
    out = []
    for p in parts:
        low = p.lower()
        if re.fullmatch(r"f([1-9]|1\d|20)", low) or low in _PYNPUT_NAMES:
            out.append(f"<{_PYNPUT_NAMES.get(low, low)}>")
        elif len(p) == 1:
            out.append(low)
        else:
            return None
    return "+".join(out)


def ask_value(parent, title, prompt, initial="", choices=None, check=None, secret=False) -> str | None:
    """Small modal dialog with one text box (a dropdown that also takes typing when
    `choices` is given). Returns the text, or None if cancelled. OK stays disabled
    while `check(text)` is False."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    frm = ttk.Frame(win, padding=14)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=prompt, wraplength=360, justify="left").pack(anchor="w")
    var = tk.StringVar(value=initial)
    box = (ttk.Combobox(frm, textvariable=var, values=choices, width=12, height=15) if choices
           else ttk.Entry(frm, textvariable=var, width=28 if secret else 18, show="•" if secret else ""))
    box.pack(anchor="w", pady=(8, 10))
    result = {"v": None}
    btns = ttk.Frame(frm)
    btns.pack(fill="x")

    def ok(_=None):
        if check is None or check(var.get()):
            result["v"] = var.get()
            win.destroy()

    ok_btn = ttk.Button(btns, text="OK", command=ok)
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
    ok_btn.pack(side="right", padx=6)

    def validate(*_):
        ok_btn.state(["!disabled"] if check is None or check(var.get()) else ["disabled"])
    var.trace_add("write", validate)
    validate()
    win.bind("<Return>", ok)
    win.bind("<KP_Enter>", ok)
    win.bind("<Escape>", lambda e: win.destroy())
    box.focus_set()
    box.select_range(0, "end")
    win.update_idletasks()
    try:
        win.geometry(f"+{parent.winfo_rootx() + 80}+{parent.winfo_rooty() + 80}")
        win.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(win)
    return result["v"]


def make_tree(parent, cols) -> ttk.Treeview:
    frame = ttk.Frame(parent)
    # "#0" (the tree column) holds the item's icon
    tree = ttk.Treeview(frame, columns=cols, show="tree headings", selectmode="extended")
    tree.column("#0", width=42, minwidth=42, stretch=False, anchor="center")
    tree.heading("#0", text="")
    for c in cols:
        tree.heading(c, text=HEAD[c], command=lambda c=c: sort_tree(tree, c, False))
        tree.column(c, width=WIDTH[c], anchor="center" if c in CENTER else "e" if c in NUMERIC else "w",
                    stretch=(c == "item"))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=sb.set)

    def xset(lo, hi):        # the horizontal bar only shows when the columns don't fit
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            hsb.pack_forget()
        elif not hsb.winfo_ismapped():
            hsb.pack(side="bottom", fill="x", before=tree)
        hsb.set(lo, hi)
    tree.configure(xscrollcommand=xset)
    sb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    # a watched item for sale: the whole row in bold red. Configured first, so its text
    # colour wins over every other row's (deal, high, a megaphone's own colour), while
    # the row keeps its own background
    tree.tag_configure("watch", foreground=WATCH_FG, font="MfmBold")
    tree.tag_configure("deal", background="#d5f2dc", foreground="#145c2c")
    tree.tag_configure("high", background="#fadad7", foreground="#8c1d16")
    tree.tag_configure("unknown", foreground="#8a8a8a")
    tree.tag_configure("sold", foreground="#9a9a9a", background="#f0f0f0")
    tree.tag_configure("ignored", foreground="#a0a0a0")
    tree.tag_configure("outlier", foreground="#a0661a", background="#fbf3e4")
    tree.frame = frame
    enable_multiselect(tree)
    return tree


def enable_multiselect(tree) -> None:
    """Picking several rows at once, on top of Tk's own Ctrl+click / Shift+click:
    - Alt+click adds or removes a single row
    - Ctrl+Alt+drag adds every row the mouse passes over (scrolls at the edges)
    - Ctrl+A selects all, Esc clears"""
    st = {"anchor": None, "base": set()}
    tree.dragging = False

    def toggle(e):
        r = tree.identify_row(e.y)
        if r:
            tree.selection_toggle(r)
            tree.focus(r)
            st["anchor"], st["base"] = r, set(tree.selection())
        return "break"

    def range_start(e):
        r = tree.identify_row(e.y)
        if r:
            st["anchor"], st["base"] = r, set(tree.selection())
            tree.selection_add(r)
            tree.focus(r)
            tree.dragging = True
        return "break"

    def range_drag(e):
        a = st["anchor"]
        if a is None or not tree.exists(a):
            return "break"
        h = tree.winfo_height()
        if e.y < 30:
            tree.yview_scroll(-1, "units")
        elif e.y > h - 6:
            tree.yview_scroll(1, "units")
        r = tree.identify_row(min(max(e.y, 30), h - 4))
        if r:
            kids = tree.get_children("")
            i, j = sorted((kids.index(a), kids.index(r)))
            tree.selection_set(list(st["base"] | set(kids[i:j + 1])))
        return "break"

    def release(_e):
        tree.dragging = False

    # on a Mac, Option is Alt; Ctrl+click is right-click there, so Cmd+Option+drag too
    alt = ("Alt", "Option") if MAC else ("Alt",)
    ctrl = ("Control", "Command") if MAC else ("Control",)
    for a in alt:
        tree.bind(f"<{a}-Button-1>", toggle)
        tree.bind(f"<{a}-B1-Motion>", lambda e: range_drag(e) if tree.dragging else "break")
        for c in ctrl:
            tree.bind(f"<{c}-{a}-Button-1>", range_start)
            tree.bind(f"<{c}-{a}-B1-Motion>", range_drag)
    tree.bind("<B1-Motion>", lambda e: range_drag(e) if tree.dragging else None, add="+")
    tree.bind("<ButtonRelease-1>", release, add="+")
    for c in (("Control", "Command") if MAC else ("Control",)):
        tree.bind(f"<{c}-a>", lambda e: (tree.selection_set(tree.get_children("")), "break")[1])
        tree.bind(f"<{c}-A>", lambda e: (tree.selection_set(tree.get_children("")), "break")[1])
    tree.bind("<Escape>", lambda e: tree.selection_set(()))


def sort_tree(tree, col, reverse):
    rows = [(tree.set(k, col), k) for k in tree.get_children("")]
    key = (lambda r: num(r[0])) if col in NUMERIC else (lambda r: r[0].lower())
    rows.sort(key=key, reverse=reverse)
    for i, (_, k) in enumerate(rows):
        tree.move(k, "", i)
    tree.heading(col, command=lambda: sort_tree(tree, col, not reverse))


def tag_for(v: str) -> str:
    return {"DEAL": "deal", "HIGH": "high", "?": "unknown", "SOLD": "sold", "OUTLIER": "outlier"}.get(v, "")


def diff_text(pct) -> str:
    return "" if pct is None else f"{pct:+.0f}%"


class IconList:
    """A list of item names with their icons (Treeview), for search results."""

    def __init__(self, parent, app, height=8, width=360):
        self.app = app
        self.tree = ttk.Treeview(parent, show="tree", selectmode="browse", style="Search.Treeview",
                                 height=height)
        self.tree.column("#0", width=width)
        self.names: dict[str, str] = {}
        app.icon_lists.append(self)

    def set_names(self, names):
        t = self.tree
        t.delete(*t.get_children())
        self.names = {}
        for n in names:
            iid = t.insert("", "end", text="  " + n, image=self.app.icon_for(n, 26))
            self.names[iid] = n
        kids = t.get_children()
        if kids:
            t.selection_set(kids[0])
            t.see(kids[0])
        self.app.request_icons(list(names)[:8])

    def selected(self) -> str:
        sel = self.tree.selection()
        return self.names.get(sel[0], "") if sel else ""

    def refresh_images(self):
        try:
            for iid, n in self.names.items():
                self.tree.item(iid, image=self.app.icon_for(n, 26))
        except tk.TclError:
            pass

    def alive(self) -> bool:
        try:
            return bool(self.tree.winfo_exists())
        except tk.TclError:
            return False


class ItemSearch(ttk.Frame):
    """Text box that suggests meowdb item names, with their icons, as you type.
    Arrow keys / mouse pick a suggestion; Enter with nothing picked calls on_return."""

    def __init__(self, parent, app, width=42, on_return=None):
        super().__init__(parent)
        self.app, self.on_return = app, on_return
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x")
        self.popup = None
        self.list = None
        self._after = None
        self._quiet = False
        self._navigated = False
        self.var.trace_add("write", self._changed)
        self.entry.bind("<Down>", self._into_list)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<KP_Enter>", self._enter)
        self.entry.bind("<FocusOut>", lambda e: self.after(200, self._maybe_hide))

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str) -> None:
        self._quiet = True
        self.var.set(value)
        self._quiet = False
        self.hide()

    def focus(self):
        self.entry.focus_set()

    def _changed(self, *_):
        if self._quiet:
            return
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(120, self._update)

    def _update(self):
        self._after = None
        text = self.var.get().strip()
        if len(text) < 2:
            self.hide()
            return
        # the fuzzy search runs on a worker thread (the first lookup loads the item
        # dictionary, and odd text can mean comparing against every name); only the
        # answer to what's typed now is shown
        self._asked = getattr(self, "_asked", 0) + 1
        seq = self._asked

        def work():
            try:
                names = mfm.suggest_items(text, 12)
            except Exception:
                names = []
            self.app.events.put(("call", lambda: self._suggested(seq, text, names)))
        threading.Thread(target=work, daemon=True).start()

    def _suggested(self, seq, text, names):
        if seq != getattr(self, "_asked", 0):
            return       # typed more since
        try:
            if not self.winfo_exists():
                return
            if not names or (len(names) == 1 and names[0] == text):
                self.hide()
                return
            self._show(names)
        except tk.TclError:
            pass         # the window was closed meanwhile

    def _show(self, names):
        if self.popup is None:
            self.popup = tk.Toplevel(self)
            self.popup.wm_overrideredirect(True)
            self.popup.transient(self.winfo_toplevel())
            self.list = IconList(self.popup, self.app, height=8, width=max(self.entry.winfo_width(), 360))
            t = self.list.tree
            t.pack(fill="both", expand=True)
            t.bind("<ButtonRelease-1>", lambda e: self._pick())
            t.bind("<Return>", lambda e: self._pick())
            t.bind("<KP_Enter>", lambda e: self._pick())
            t.bind("<Escape>", lambda e: (self.hide(), self.entry.focus_set()))
            t.bind("<Up>", self._up)
            t.bind("<FocusOut>", lambda e: self.after(200, self._maybe_hide))
        self._navigated = False
        self.list.set_names(names)
        self.list.tree.configure(height=min(8, len(names)))
        x, y = self.entry.winfo_rootx(), self.entry.winfo_rooty() + self.entry.winfo_height()
        w = max(self.entry.winfo_width(), 360)
        self.list.tree.column("#0", width=w - 4)
        self.popup.geometry(f"{w}x{min(8, len(names)) * 30 + 4}+{x}+{y}")
        self.popup.deiconify()
        self.popup.lift()

    def hide(self):
        if self.popup is not None:
            self.popup.withdraw()

    def visible(self) -> bool:
        return self.popup is not None and self.popup.winfo_viewable()

    def _maybe_hide(self):
        f = self.focus_get()
        if self.popup is not None and f is not None and str(f).startswith(str(self.popup)):
            return
        if f is not self.entry:
            self.hide()

    def _into_list(self, _e=None):
        if self.visible():
            self._navigated = True
            self.list.tree.focus_set()
            kids = self.list.tree.get_children()
            if kids:
                self.list.tree.focus(self.list.tree.selection()[0] if self.list.tree.selection() else kids[0])
        return "break"

    def _up(self, _e=None):
        t = self.list.tree
        kids = t.get_children()
        if kids and t.selection() and t.selection()[0] == kids[0]:
            self.entry.focus_set()
            return "break"
        return None

    def _pick(self):
        name = self.list.selected() if self.list else ""
        if name:
            self.set(name)
            self.entry.focus_set()
            self.entry.icursor("end")

    def _enter(self, _e=None):
        if self.visible() and self._navigated:
            self._pick()
            return "break"
        self.hide()
        if self.on_return:
            self.on_return()
        return "break"


# ------------------------------------------------------------------ overlay widget
# Drawn on one canvas (rounded panel and chips, icons from lines and polygons), in the
# look of the EXP tracker widget: neutral-900 panel, tinted chips, flat icon buttons.
OV_W = 268
OV_KEY = "#010203"                   # painted see-through (the panel's rounded corners)
OV_BG, OV_BORDER = "#171717", "#262626"
OV_TEXT, OV_DIM, OV_FAINT = "#ffffff", "#a3a3a3", "#737373"
OV_OK, OV_WARN, OV_ERR = "#4ade80", "#fbbf24", "#f87171"
OV_HOVER_BG, OV_CLOSE, OV_CLOSE_BG = "#2e2e2e", "#f43f5e", "#32161e"
# key: (title, label colour, chip fill, chip border)   — blue, fuchsia and green tints
OV_CHIPS = {"state": ("Monitoring", "#93c5fd", "#1b2230", "#243044"),
            "frames": ("Frames waiting", "#f0abfc", "#2a1c2d", "#3a2640"),
            "channel": ("Channel", "#86efac", "#18281e", "#22382a")}


class Overlay(tk.Toplevel):
    """A small card always on top of the game (the Widget box in the toolbar): the
    monitoring clock, Start / Stop, Next shop and Settings on its bar, a message line
    (what to do next, or a warning), and chips for Monitoring, Frames waiting and
    Channel (with chevrons to change it). Dragged from anywhere but the buttons; where
    it was left is kept in the settings. Updated twice a second. On Windows it never
    takes the focus from the game when clicked."""
    BAR, CHIP, GAP, PAD, ICON, BTN = 26, 36, 4, 5, 18, 26

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.overrideredirect(True)          # no title bar
        self.attributes("-topmost", True)
        self.configure(background=OV_KEY)
        try:
            self.attributes("-transparentcolor", OV_KEY)
        except tk.TclError:                  # not Windows: square corners
            self.configure(background=OV_BG)
        fam = getattr(app, "ui_font", None) or "TkDefaultFont"
        self.f_title = (fam, 11, "bold")
        self.f_text, self.f_small = (fam, 9), (fam, 8)
        self.c = tk.Canvas(self, width=OV_W, height=100, background=self.cget("background"),
                           highlightthickness=0, bd=0)
        self.c.pack()
        self._items: dict = {}
        self._shown: dict = {}
        self._status_h = 0
        self._drag = None
        self._hwnd = None
        self._bind_buttons()
        self._draw()
        pos = app.cfg.get("overlay_pos")
        x, y = (int(pos[0]), int(pos[1])) if pos else (40, 40)
        self.geometry(f"+{x}+{y}")
        self.update_idletasks()
        self._no_activate()
        self.tick()

    # -- buttons: tag → (command, hint); bound once, the items are redrawn freely
    BUTTONS = {
        "play": (lambda o: o.app.toggle(), "Start / stop monitoring."),
        "reset": (lambda o: o.app.next_shop(),
                  "Next shop: end the current visit (for when you hop shops without the window closing)."),
        "gear": (lambda o: o.app.settings(), "Settings."),
        "close": (lambda o: o.app.show_overlay(False),
                  "Hide the widget (the Widget box in the app's toolbar brings it back)."),
        "prev": (lambda o: o.app.step_channel(-1), "Previous channel."),
        "next": (lambda o: o.app.step_channel(+1), "Next channel."),
    }

    def _bind_buttons(self):
        c = self.c
        for name, (cmd, hint) in self.BUTTONS.items():
            tag = f"btn_{name}"
            c.tag_bind(tag, "<Button-1>", lambda e, f=cmd: f(self))
            c.tag_bind(tag, "<Enter>", lambda e, n=name: self._hover(n, True))
            c.tag_bind(tag, "<Leave>", lambda e, n=name: self._hover(n, False))
            CanvasTip(c, tag, hint)
        CanvasTip(c, "timer", "How long this monitoring run has been going.")
        c.bind("<ButtonPress-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", self._drag_end)

    def _hover(self, name, on):
        c = self.c
        close = name == "close"
        c.itemconfig(f"hb_{name}", state="normal" if on else "hidden",
                     fill=OV_CLOSE_BG if close else OV_HOVER_BG)
        self._recolor(f"ic_{name}", (OV_CLOSE if close else OV_TEXT) if on else OV_DIM)

    def _recolor(self, tag, color):
        c = self.c
        for item in c.find_withtag(tag):
            kind = c.type(item)
            if kind in ("oval", "arc") or (kind == "polygon" and not c.itemcget(item, "fill")):
                c.itemconfig(item, outline=color)
            else:
                c.itemconfig(item, fill=color)

    # -- drawing
    def _rrect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.c.create_polygon(pts, smooth=True, splinesteps=10, **kw)

    def _icon(self, kind, x, y, color, tags, size=ICON):
        """One of the widget's icons with its top left corner at (x, y), drawn on a 24-grid."""
        c, k = self.c, size / 24
        P = lambda *pts: [x + v * k if i % 2 == 0 else y + v * k for i, v in enumerate(pts)]
        w = max(1.5, 2 * k)
        line = dict(fill=color, width=w, capstyle="round", joinstyle="round", tags=tags)
        if kind == "play":
            c.create_polygon(P(7, 4, 20, 12, 7, 20), fill=color, outline="", tags=tags)
        elif kind == "pause":
            for a, b in ((5, 10), (14, 19)):
                c.create_rectangle(P(a, 4, b, 20), fill=color, outline="", tags=tags)
        elif kind == "reset":
            c.create_arc(P(4, 4, 20, 20), start=300, extent=-300, style="arc", outline=color, width=w, tags=tags)
            c.create_line(P(20, 3, 20, 9, 14, 9), **line)
        elif kind == "gear":                 # three sliders
            for yy, cx in ((6, 15), (12, 8), (18, 16)):
                c.create_line(P(4, yy, 20, yy), **line)
                c.create_oval(P(cx - 2.2, yy - 2.2, cx + 2.2, yy + 2.2), fill=OV_BG, outline=color, width=w, tags=tags)
        elif kind == "close":
            c.create_line(P(6, 6, 18, 18), **line)
            c.create_line(P(18, 6, 6, 18), **line)
        elif kind == "prev":
            c.create_line(P(15, 6, 9, 12, 15, 18), **line)
        elif kind == "next":
            c.create_line(P(9, 6, 15, 12, 9, 18), **line)
        elif kind == "warn":
            c.create_polygon(P(12, 3, 21.5, 19.5, 2.5, 19.5), fill="", outline=color, width=w,
                             joinstyle="round", tags=tags)
            c.create_line(P(12, 9, 12, 13.5), **line)
            c.create_line(P(12, 16.5, 12, 16.6), **line)
        elif kind == "trend":                # Monitoring
            c.create_line(P(3, 17, 9, 11, 13, 15, 21, 7), **line)
            c.create_line(P(14, 7, 21, 7, 21, 14), **line)
        elif kind == "hourglass":            # Frames waiting
            c.create_polygon(P(6, 4, 18, 4, 12, 11.5), fill="", outline=color, width=w, joinstyle="round", tags=tags)
            c.create_polygon(P(6, 20, 18, 20, 12, 12.5), fill="", outline=color, width=w, joinstyle="round", tags=tags)
        elif kind == "hash":                 # Channel
            c.create_text(x + size / 2, y + size / 2, text="#", fill=color, font=(self.f_title[0], 12, "bold"), tags=tags)

    def _button(self, name, kind, x, y):
        """A flat 26x26 icon button: its hover background (hidden) and icon."""
        tags = ("btn", f"btn_{name}")
        self._rrect(x, y, x + self.BTN, y + self.BTN, 6, fill=OV_HOVER_BG, outline="",
                    tags=tags + (f"hb_{name}",), state="hidden")
        self._icon(kind, x + 4, y + 4, OV_DIM, tags + (f"ic_{name}",))

    def _status_height(self, text, with_icon):
        if not text:
            return 0
        x = self.PAD + 8 + (self.ICON - 4 + 5 if with_icon else 0)
        t = self.c.create_text(0, 0, text=text, anchor="nw", width=OV_W - x - self.PAD - 8, font=self.f_text)
        h = self.c.bbox(t)[3] - self.c.bbox(t)[1]
        self.c.delete(t)
        return max(16, h) + 2 * 2 + 4

    def _draw(self):
        """Everything, from scratch (when the message line's height changes); tick()
        updates the texts and colours in place the rest of the time."""
        c, it = self.c, self._items
        c.delete("all")
        it.clear()
        for k in ("timer", "state", "frames", "channel", "alive"):
            self._shown.pop(k, None)         # new items: tick() fills them in again
        text, warn = self._shown.get("status", ("", False))
        self._status_h = sh = self._status_height(text, warn)
        H = self.PAD + self.BAR + self.GAP + sh + 3 * self.CHIP + 2 * self.GAP + self.PAD
        c.config(height=H)
        self._rrect(0.5, 0.5, OV_W - 0.5, H - 0.5, 8, fill=OV_BG, outline=OV_BORDER)
        # -- the bar: clock, play / pause, next shop ... settings, close
        x, y = self.PAD + 8, self.PAD
        it["timer"] = c.create_text(x, y + self.BAR / 2, text="00:00:00", anchor="w", fill=OV_TEXT,
                                    font=self.f_title, tags=("timer",))
        from tkinter import font as tkfont
        x += tkfont.Font(font=self.f_title).measure("00:00:00") + 8
        self._button("play", "play", x, y)
        self._icon("pause", x + 4, y + 4, OV_DIM, ("btn", "btn_play", "ic_play", "ic_pause"))
        c.itemconfig("ic_pause", state="hidden")
        self._button("reset", "reset", x + self.BTN, y)
        self._button("close", "close", OV_W - self.PAD - self.BTN, y)
        self._button("gear", "gear", OV_W - self.PAD - 2 * self.BTN, y)
        # -- the message line
        y += self.BAR + self.GAP
        if text:
            x = self.PAD + 8
            if warn:
                self._icon("warn", x, y + 2, OV_WARN, ("status_icon",), size=self.ICON - 4)
                x += self.ICON - 4 + 5
            it["status"] = c.create_text(x, y + 2, text=text, anchor="nw", width=OV_W - x - self.PAD - 8,
                                         fill=OV_WARN if warn else OV_DIM, font=self.f_text)
            y += sh
        # -- the chips
        for key, (title, color, fill, border) in OV_CHIPS.items():
            x0, x1 = self.PAD, OV_W - self.PAD
            self._rrect(x0, y, x1, y + self.CHIP, 6, fill=fill, outline=border)
            self._icon({"state": "trend", "frames": "hourglass", "channel": "hash"}[key],
                       x0 + 8, y + (self.CHIP - self.ICON) / 2, color, ("chip_icon",))
            c.create_text(x0 + 8 + self.ICON + 4, y + self.CHIP / 2, text=title, anchor="w", fill=color,
                          font=self.f_title)
            if key == "channel":
                self._button("next", "next", x1 - 8 - self.BTN, y + (self.CHIP - self.BTN) / 2)
                it["channel"] = c.create_text(x1 - 8 - self.BTN - 2, y + self.CHIP / 2, text="—", anchor="e",
                                              fill=OV_TEXT, font=self.f_title)
                it["prev_x"] = x1 - 8 - self.BTN - 2
            else:
                it[key] = c.create_text(x1 - 8, y + self.CHIP / 2, text="—", anchor="e", fill=OV_TEXT, font=self.f_title)
            y += self.CHIP + self.GAP
        c.tag_raise("btn")
        self._place_prev()

    def _place_prev(self):
        """The ‹ button sits just left of the channel number, wherever that ends."""
        c, it = self.c, self._items
        c.delete("btn_prev")
        bb = c.bbox(it["channel"])
        x = (bb[0] if bb else it["prev_x"]) - 2 - self.BTN
        y0 = c.coords(it["channel"])[1]
        self._button("prev", "prev", x, y0 - self.BTN / 2)

    # -- no focus for the game to lose: WS_EX_NOACTIVATE (and no taskbar button) on Windows
    def _no_activate(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.winfo_id())
            style = u.GetWindowLongW(hwnd, -20)                       # GWL_EXSTYLE
            u.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)   # NOACTIVATE | TOOLWINDOW
            self._hwnd = hwnd
        except Exception:
            self._hwnd = None

    def keep_on_top(self):
        """Windows can push a topmost window behind the game when you alt-tab: put it
        back, without activating it."""
        if self._hwnd:
            try:
                import ctypes
                # HWND_TOPMOST; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
                ctypes.windll.user32.SetWindowPos(self._hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
                return
            except Exception:
                pass
        self.attributes("-topmost", True)

    # -- dragging, from anywhere but a button
    def _drag_start(self, e):
        if "btn" in self.c.gettags("current"):
            return
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, e):
        if self._drag:
            self._drag = None
            self.app.cfg["overlay_pos"] = [self.winfo_x(), self.winfo_y()]
            mfm.save_config(self.app.cfg)

    # -- what it shows
    def _set(self, key, **kw):
        """itemconfig only when something changed (the canvas isn't touched otherwise)."""
        if self._shown.get(key) != kw:
            self._shown[key] = kw
            self.c.itemconfig(self._items[key], **kw)

    def tick(self):
        if not self.winfo_exists():
            return
        a = self.app
        alive = bool(a.worker and a.worker.is_alive())
        stopping = alive and a.stop_event is not None and a.stop_event.is_set()
        running = alive and not stopping
        secs = int((time.time() - a.run_started) if running and a.run_started else a.run_elapsed)
        ch = a.channel()
        if stopping:
            status, warn = f"Stopping: still reading {a.backlog} captured frames.", False
        elif not running:
            key = hotkey_cfg(a.cfg)["monitor"] if a.cfg.get("hotkeys_on", False) and a._hotkeys else ""
            status, warn = (f"Press {key} to start monitoring." if key
                            else "Press Start monitoring in the app."), False
        elif a.backlog >= 10:
            status, warn = f"Reading is {a.backlog} frames behind. Nothing is lost.", True
        elif not ch:
            status, warn = "Set your channel: it's saved with every listing.", True
        else:
            status, warn = "Open shops and scroll through them.", False
        if self._shown.get("status") != (status, warn):
            self._shown["status"] = (status, warn)
            if self._status_height(status, warn) != self._status_h or "status" not in self._items:
                self._draw()
            else:
                self.c.itemconfig(self._items["status"], text=status, fill=OV_WARN if warn else OV_DIM)
                if warn:
                    self._recolor("status_icon", OV_WARN)
        self._set("timer", text=f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}",
                  fill=OV_OK if running else OV_TEXT)
        if self._shown.get("alive") != alive:
            self._shown["alive"] = alive
            self.c.itemconfig("ic_pause", state="normal" if alive else "hidden")
            for item in self.c.find_withtag("ic_play"):
                if "ic_pause" not in self.c.gettags(item):
                    self.c.itemconfig(item, state="hidden" if alive else "normal")
        self._set("state", text="Running" if running else "Finishing…" if stopping else "Stopped",
                  fill=OV_OK if running else OV_WARN if stopping else OV_ERR)
        self._set("frames", text=str(a.backlog), fill=OV_WARN if a.backlog >= 10 else OV_TEXT)
        before = self.c.bbox(self._items["channel"])
        self._set("channel", text=f"CH {ch}" if ch else "—", fill=OV_TEXT if ch else OV_ERR)
        if self.c.bbox(self._items["channel"]) != before:
            self._place_prev()
        self.keep_on_top()
        self.after(500, self.tick)

class App:
    def __init__(self, root: tk.Tk):
        import sys
        # threads take turns at Python's lock more often (default 5 ms): the window's
        # many small Tk calls wait less while a background thread is busy
        sys.setswitchinterval(0.001)
        self.root = root
        self.cfg = mfm.load_config()
        mfm.CAPTURE_CFG = self.cfg              # how the screen is read (game window or whole screen)
        self.con = mfm.db()
        self.events: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.monitor: mfm.Monitor | None = None
        self.preview_img = None
        self.last_map = ""
        self.found_at = None
        self.icons: dict[str, ImageTk.PhotoImage] = {}
        self.scopes: dict[tuple, dict] = {}   # (table, row) -> which logged listing it is
        self.icon_lists: list = []           # open search lists, refreshed when icons arrive
        self._icon_queue: queue.Queue = queue.Queue()
        self._icon_asked: set[str] = set()
        self._icon_worker = None
        self._no_icon: set[str] = set()      # items known to have no icon yet: not looked up again
        # Deals / Items / Review are worked out by a background thread (see refresh_tables)
        self._tables_lock = threading.Lock()
        self._tables_want: dict | None = None   # the refresh asked for, until the thread takes it
        self._tables_busy = False
        self._tables_data: dict | None = None   # last result: re-filtered without recomputing
        self._refill_job = None
        self._stale: set[str] = set()           # tables whose rows are older than _tables_data
        self._deals_job = None                  # the Deals table is filled in slices (see _fill_deals)
        self.watch = mfm.load_watchlist()
        self.watched_listings = mfm.load_watched_listings()
        self._error_times: dict = {}
        root.report_callback_exception = self._tk_error
        self.ignored = set(mfm.load_ignorelist())
        self.shops_today = 0
        self.logged_today = 0
        self.backlog = 0        # captured frames waiting for OCR (0 = reading live)
        self._behind = False    # "the OCR is behind" is showing in the status bar
        self.run_started = None   # when this monitoring run began (the widget's clock)
        self.run_elapsed = 0.0    # how long the last run took, shown once it's stopped
        self.overlay = None       # the widget over the game (see show_overlay)
        self.chan_watch = mfm.ChannelWatch(cfg=self.cfg)   # change-channel dialog: seen, then gone
        self._own_rects: list = []   # where the app's windows are on screen, for the monitor (see own_windows)
        self.chan_popup = None

        root.title("Free Market Tracker")
        # wide enough for the Megaphones tab's Message column next to its preview
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1880, max(1000, sw - 40))}x{min(760, max(560, sh - 100))}")
        root.minsize(980, 500)
        self._build()
        self.refresh_tables()
        root.after(100, self.drain_events)
        if self.cfg.get("channel_prompt"):
            root.after(500, lambda: self.ask_channel(changed=False))
        threading.Thread(target=self._channel_idle_check, daemon=True).start()
        self._hotkeys = None
        self._grabbing_hotkey = False
        self._start_hotkeys()
        self.show_overlay(bool(self.cfg.get("overlay_on", True)))
        if not mfm.ITEMS_PATH.exists():
            root.after(800, self.offer_item_list)
        elif mfm.needs_type_refresh() or mfm.needs_map_refresh():
            root.after(800, self.offer_type_refresh)
        root.after(5000, self.periodic_refresh)
        root.after(1500, self.check_db_size)
        root.after(2000, lambda: self.run_retention(quiet=True))   # every launch
        root.after(6 * 3600 * 1000, self.retention_tick)           # and every 6 hours
        threading.Thread(target=mfm.cleanup_old_update, daemon=True).start()   # after an update
        root.after(6000, self.auto_update_check)                   # a new version?
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.shared = None
        self.shared_state = {"ok": False, "text": "Not connected"}
        if self.cfg.get("shared_on") and self.cfg.get("shared_server"):
            root.after(1500, self._start_shared)

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.ui_font = apply_fonts(self.root)
        style = ttk.Style()
        if "clam" in style.theme_names() and mfm.sys.platform != "win32":
            style.theme_use("clam")
        style.configure("TButton", padding=(10, 3), width=-6)   # size to the label (min 6 chars)
        style.configure("Treeview", rowheight=34)
        style.configure("Search.Treeview", rowheight=30)
        style.configure("Status.TLabel", foreground="#555")
        style.configure("Big.TLabel", font="MfmBig")
        style.configure("Bold.TLabel", font="MfmBold")
        style.configure("Treeview.Heading", font="TkHeadingFont")

        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")
        self.top_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Always on top", variable=self.top_var,
                        command=lambda: self.root.attributes("-topmost", self.top_var.get())
                        ).pack(side="right")
        self.overlay_var = tk.BooleanVar(value=bool(self.cfg.get("overlay_on", True)))
        cb = ttk.Checkbutton(bar, text="Widget", variable=self.overlay_var,
                             command=lambda: self.show_overlay(self.overlay_var.get(), save=True))
        cb.pack(side="right", padx=(0, 10))
        tip("A small card that stays on top of the game: monitoring time and state, frames "
            "waiting and the channel, with Start / Stop, Next shop and the channel chevrons. "
            "Drag it by its top bar.", cb)
        ttk.Button(bar, text="Settings", command=self.settings).pack(side="right", padx=6)
        ttk.Button(bar, text="Import prices", command=self.import_ref).pack(side="right")
        ttk.Button(bar, text="Export prices", command=self.export_prices).pack(side="right", padx=(0, 6))
        b = ttk.Button(bar, text="Shared Database", command=self.shared_dialog)
        b.pack(side="right", padx=(0, 6))
        tip("Share prices and megaphones with other players through a server one of you runs.", b)
        self.shared_dot = tk.Label(bar, text="●", font=("TkDefaultFont", 14), foreground="#c62828")
        self.shared_dot.pack(side="right", padx=(0, 2))
        tip("Green: connected to the Shared Database and syncing. Red: not connected.", self.shared_dot)
        # left of the button: a green arrow while monitoring, a red square when stopped
        # (drawn, not a text symbol, so it shows whatever fonts there are)
        self.run_mark = tk.Canvas(bar, width=18, height=18, highlightthickness=0, bd=0,
                                  background=ttk.Style().lookup("TFrame", "background") or None)
        self.run_mark.pack(side="left", padx=(0, 5))
        self._run_mark(False)
        tip("Green arrow: monitoring. Red square: stopped.", self.run_mark)
        self.start_btn = ttk.Button(bar, text="Start monitoring", command=self.toggle, width=16)
        self.start_btn.pack(side="left")
        ttk.Label(bar, text="Channel:").pack(side="left", padx=(14, 4))
        # stays filled in for every shop until you change it (saved with each listing)
        self.channel_var = tk.StringVar()
        self.channel_var.trace_add("write", self._channel_changed)
        # pick 1-100 from the list or type it; anything else can't be entered
        ok = (self.root.register(lambda p: p == "" or (p.isdigit() and p[0] != "0" and int(p) <= 100)), "%P")
        # the arrow (or Down / Alt+Down) opens the channel picker instead of a list
        self.channel_box = ttk.Combobox(bar, textvariable=self.channel_var, width=5,
                                        values=(), postcommand=self._channel_box_posted,
                                        validate="key", validatecommand=ok)
        self.channel_box.pack(side="left")
        self.channel_box.bind("<Button-1>", self._channel_box_click)
        for key in ("<Down>", "<Alt-Down>", "<F4>"):
            self.channel_box.bind(key, lambda e: (self.open_channel_picker(), "break")[1])
        # the mouse wheel over the box (and the Up key) steps the channel; the widget
        # over the game has ‹ › for it too
        self.channel_box.bind("<MouseWheel>", lambda e: self.step_channel(1 if e.delta > 0 else -1))
        self.channel_box.bind("<Button-4>", lambda e: self.step_channel(+1))    # X11 wheel
        self.channel_box.bind("<Button-5>", lambda e: self.step_channel(-1))
        self.channel_box.bind("<Up>", lambda e: (self.step_channel(+1), "break")[1])
        # the map you're on, read off the minimap while monitoring
        self.map_label = ttk.Label(bar, text="Map: —")
        self.map_label.pack(side="left", padx=(14, 0))
        tip("The map you're on, read from the game's minimap while monitoring.", self.map_label)
        # rarely needed now that shop changes are detected on every frame
        self.root.bind_all("<Control-n>", lambda e: self.next_shop())
        if MAC:
            self.root.bind_all("<Command-n>", lambda e: self.next_shop())

        # red warning when prices.db gets big (shown by check_db_size)
        self.size_warn = tk.Label(self.root, background="#c62828", foreground="white", anchor="w",
                                  padx=10, pady=5, cursor="hand2", font="MfmBold", justify="left")
        self.size_warn.bind("<Button-1>", lambda e: self.settings())
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10)

        # Live tab: current shop table + preview/stats panel
        live = ttk.Frame(self.nb, padding=6)
        self.nb.add(live, text="  Live  ")
        left = ttk.Frame(live)
        left.pack(side="left", fill="both", expand=True)
        self.shop_title = ttk.Label(left, text="Waiting for a shop…", style="Big.TLabel")
        self.shop_title.pack(anchor="w", pady=(0, 6))
        self.live = make_tree(left, COLS + ("notes", "saved"))
        self.live.tag_configure("dupe", foreground="#7a7a7a")
        self.live.frame.pack(fill="both", expand=True)
        self.live.bind("<Double-1>", lambda e: self.open_history(self.live))

        right = ttk.Frame(live, padding=(10, 0, 0, 0), width=300)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ttk.Label(right, text="What the OCR sees").pack(anchor="w")
        self.preview = ttk.Label(right, relief="solid", anchor="center", text="no capture yet")
        self.preview.pack(fill="x", pady=(2, 8), ipady=4)
        self.stats = ttk.Label(right, justify="left")
        self.stats.pack(anchor="w")
        ttk.Label(right, text="Recent shops", style="Big.TLabel").pack(anchor="w", pady=(14, 4))
        self.recent = tk.Listbox(right, height=10, activestyle="none", borderwidth=0)
        self.recent.pack(fill="both", expand=True)

        # Deals tab
        deals = ttk.Frame(self.nb, padding=6)
        self.nb.add(deals, text="  Deals  ")
        top = ttk.Frame(deals)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Listings from the last").pack(side="left")
        self.hours_var = tk.IntVar(value=24)
        ttk.Spinbox(top, from_=1, to=720, width=5, textvariable=self.hours_var,
                    command=self.refresh_tables).pack(side="left", padx=4)
        ttk.Label(top, text="hours, best deals first.").pack(side="left")
        # "Include sold" and the hours change what's queried; the other three only
        # filter the rows already computed, so they apply at once
        self.deals_watched_only = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(top, text="Watched items", variable=self.deals_watched_only,
                             command=self.refill_tables)
        cb.pack(side="right")
        tip("Shows only watched items (at or under their max price) and watched listings.", cb)
        self.only_deals = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Deals only", variable=self.only_deals,
                        command=self.refill_tables).pack(side="right", padx=(0, 10))
        self.show_sold = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include sold", variable=self.show_sold,
                        command=self.refresh_tables).pack(side="right", padx=10)
        self.show_ignored = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show ignored", variable=self.show_ignored,
                        command=self.refill_tables).pack(side="right")
        self.show_outliers = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show outliers", variable=self.show_outliers,
                        command=self.refill_tables).pack(side="right", padx=10)
        self.deals = make_tree(deals, COLS + ("status", "notes", "shop", "player", "map", "channel", "time"))
        self.deals.frame.pack(fill="both", expand=True)
        self.deals.bind("<Double-1>", lambda e: self.open_history(self.deals))

        # Items tab
        items = ttk.Frame(self.nb, padding=6)
        self.nb.add(items, text="  Items  ")
        ttk.Label(items, text="Every item you've seen: the cheapest listing for sale (last 24 hours) and "
                              "what it lists and sells for. Delete removes a bad read; "
                              + ("Option+click" if MAC else "Alt+click")
                              + " selects several rows."
                  ).pack(anchor="w")
        legend = ttk.Frame(items)
        legend.pack(anchor="w", pady=(2, 6))
        self.legend_icon = ImageTk.PhotoImage(self._legend_badge())
        ttk.Label(legend, image=self.legend_icon).pack(side="left")
        ttk.Label(legend, foreground="#666",
                  text=" = name not in the item list. Fix it in the Review tab."
                  ).pack(side="left")
        self._build_item_filter(items)
        self.items = make_tree(items, ("item", "best", "best_shop", "best_player", "best_map", "best_channel",
                                       "listed_med", "listed_n",
                                       "sold_med", "sold_n", "market", "msell", "mbuy", "npc_buy", "npc_sell",
                                       "source"))
        self.items.frame.pack(fill="both", expand=True)
        self.items.bind("<Double-1>", lambda e: self.open_history(self.items))
        self.items.bind("<Delete>", lambda e: self.forget_selected())
        self.items.bind("<BackSpace>", lambda e: self.forget_selected())
        for tree in (self.live, self.deals):
            tree.bind("<Delete>", lambda e, t=tree: self.delete_listing(t))
            tree.bind("<BackSpace>", lambda e, t=tree: self.delete_listing(t))
        for tree in (self.live, self.deals, self.items):
            self._add_menu(tree)

        self._build_megaphones_tab()
        self.live_tab, self.deals_tab, self.items_tab = live, deals, items
        self.nb.select(live)
        # a table is only filled while its tab is showing; the others catch up when opened
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._fill_stale(), add="+")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._mega_tab_shown(), add="+")
        self._build_review_tab()
        if not self.cfg.get("review_on", True):
            self.nb.hide(self.review_tab)     # Settings → General: Review tab off
        self._build_watched_tab()
        self._build_ignore_tab()
        self._build_inventory_tab()

        self.status = ttk.Label(self.root, style="Status.TLabel", padding=(10, 4))
        # packed ahead of the tabs, at the bottom: the tabs ask for more height than the
        # window has, and whatever is packed after them is squeezed out of sight
        self.status.pack(side="bottom", fill="x", before=self.nb)
        # red, above the status bar: something to change in the game so it can be read
        self.hints: dict[str, str] = {}
        self.hint_bar = tk.Label(self.root, background="#c62828", foreground="white", anchor="w",
                                 padx=10, pady=4, font="MfmBold", justify="left")
        self._set_status()
        self._update_stats(None, None)

    # ------------------------------------------------------------------ monitoring
    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop()
        else:
            self.start()

    def start(self):
        if not mfm.mac_screen_access(ask=True):
            messagebox.showwarning(
                "Screen Recording permission needed",
                "Allow Free Market Tracker in System Settings > Privacy & Security > "
                "Screen Recording, then quit and reopen the app.")
            return
        self.stop_event = threading.Event()
        self.backlog, self._behind = 0, False
        self.monitor = mfm.Monitor(self.cfg, lambda k, p: self.events.put((k, p)),
                                   self.channel())
        self.monitor.chan_watch = self.chan_watch
        self.monitor.own_windows = lambda: self._own_rects
        self.worker = threading.Thread(target=self.monitor.run, args=(self.stop_event,), daemon=True)
        self.worker.start()
        self.run_started = time.time()
        self.start_btn.config(text="Stop monitoring")
        self._run_mark(True)
        self._set_status("Monitoring — open shops and scroll through them normally.")
        self._mega_status_text(None)

    def _run_mark(self, running: bool) -> None:
        """The mark next to Start/Stop monitoring: green arrow, or red square."""
        c = self.run_mark
        c.delete("all")
        if running:
            c.create_polygon(4, 2, 16, 9, 4, 16, fill="#2e7d32", outline="#1b5e20")
        else:
            c.create_rectangle(3, 3, 15, 15, fill="#c62828", outline="#8e0000")

    def stop(self):
        aborting = False
        if self.run_started:
            self.run_elapsed, self.run_started = time.time() - self.run_started, None
        if self.stop_event:
            if self.stop_event.is_set() and self.monitor:
                self.monitor.abort = True   # pressed again while finishing: drop the queue
                aborting = True
            self.stop_event.set()
        self.start_btn.config(text="Start monitoring")
        self._run_mark(False)
        if not aborting and self.worker and self.worker.is_alive() and self.backlog:
            self._set_status(f"Stopped — still reading {self.backlog} captured frames "
                             "(press Stop again to discard them).")
        else:
            self._set_status("Stopped.")
        self.root.after(300, lambda: self._mega_status_text(None))

    def channel(self) -> str | None:
        """The channel in the box (1-100), as saved with listings; None when empty."""
        ch = mfm.parse_channel(self.channel_var.get())
        return ch if ch and ch.isdigit() else None

    def step_channel(self, delta: int):
        """The next (+1) or previous (-1) channel: the widget's ‹ ›, the hotkeys, the
        mouse wheel over the Channel box or Up. Wraps round at 1 and the channel count."""
        count = max(1, min(100, int(self.cfg.get("channel_count", 40) or 40)))
        cur = self.channel()
        if cur is None:
            ch = 1 if delta > 0 else count
        else:
            ch = (int(cur) - 1 + delta) % max(count, int(cur)) + 1
        self.channel_var.set(str(ch))
        # you're about to change channel in the game to match: don't ask which
        # channel it is when the game's Change Channel window closes
        self.chan_watch.quiet(15)
        self._set_status(f"Channel set to {mfm.channel_label(str(ch))}.")

    def next_shop(self):
        """Manually split visits, for when you hop shops without the window closing."""
        if self.monitor:
            self.monitor.request_split()
            self._set_status("Next shop: the current visit was ended (Ctrl+N).")

    # ------------------------------------------------------------------ hotkeys
    def _start_hotkeys(self):
        """Listens for the hotkeys (Settings → General → Hotkeys) in every window, the
        game's included. Nothing on Wayland, where apps can't see other windows' keys,
        or without pynput. Called again when Settings changes them."""
        self._stop_hotkeys()
        if not self.cfg.get("hotkeys_on", False) or (mfm.session_type() == "wayland"
                                                    and not os.environ.get("DISPLAY")):
            return       # (Wayland with XWayland: keys pressed in X11 windows, like the game's)
        try:
            from pynput import keyboard
        except ImportError:
            mfm.app_log("hotkeys: pynput isn't installed, so none")
            return
        combos = []
        for name, _ in HOTKEY_ACTIONS:
            spec = hotkey_spec(hotkey_cfg(self.cfg).get(name, ""))
            if not spec:
                continue
            try:
                combos.append(keyboard.HotKey(keyboard.HotKey.parse(spec),
                                              lambda n=name: self.events.put(("hotkey", n))))
            except (ValueError, KeyError):
                mfm.app_log(f"hotkey {name}: {spec!r} isn't a key")
        if not combos:
            return

        # matched here rather than by pynput's GlobalHotKeys, which ignores keys another
        # program sends (a remapping tool, a macro), so those count too
        def on_press(key):
            k = listener.canonical(key)
            for h in combos:
                h.press(k)

        def on_release(key):
            k = listener.canonical(key)
            for h in combos:
                h.release(k)
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        try:
            listener.start()
        except Exception as e:
            mfm.app_log(f"hotkeys not started: {e}")
            return
        self._hotkeys = listener

    def _stop_hotkeys(self):
        listener, self._hotkeys = self._hotkeys, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def on_hotkey(self, name):
        """A hotkey was pressed, in whichever window had focus."""
        if self._grabbing_hotkey:
            return                    # Settings is waiting for a key to assign
        if name == "monitor":
            self.toggle()
        elif name == "channel_up":
            self.step_channel(+1)
        elif name == "channel_down":
            self.step_channel(-1)

    # ------------------------------------------------------------------ overlay widget
    def show_overlay(self, on: bool, save: bool = False):
        """Show or hide the widget over the game (the Widget box in the toolbar, its ✕)."""
        if on and self.overlay is None:
            self.overlay = Overlay(self)
        elif not on and self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        self.overlay_var.set(on)
        if save or self.cfg.get("overlay_on", True) != on:
            self.cfg["overlay_on"] = on
            mfm.save_config(self.cfg)

    # ------------------------------------------------------------------ channel prompt
    def _channel_idle_check(self):
        """Background thread: while not monitoring, look for the game's
        change-channel dialog about once a second (monitoring does the same
        check on its own screenshots)."""
        import time

        while True:
            time.sleep(self.chan_watch.every)
            try:
                if not self.cfg.get("channel_prompt") or (self.worker and self.worker.is_alive()):
                    continue
                # on Wayland only when the game's window can be read directly (a
                # screenshot tool is too slow to run this often); macOS needs Screen
                # Recording permission first
                if mfm.session_type() == "wayland":
                    shot = mfm.game_window_grab(self.cfg) if self.cfg.get("capture_window", True) else None
                    if shot is None:
                        continue
                elif not mfm.mac_screen_access():
                    continue
                else:
                    shot = mfm.grab_full_screen()
                if self.chan_watch.feed(shot):
                    self.events.put(("channel_change", self.chan_watch.channel))
                elif self.chan_watch.take_cancelled():
                    self.events.put(("channel_cancel", None))
            except Exception:
                time.sleep(5)

    def on_channel_cancel(self, _=None):
        """The game's Change Channel window closed but the game didn't change channel
        (no fade to black: CANCEL, Esc, or a change the game refused): the channel stays."""
        self._set_status(f"Change Channel window closed without changing channel: still channel "
                         f"{self.channel() or '(not set)'}.")

    def on_channel_change(self, channel=None):
        """The game's Change Channel window was closed. The channel highlighted in it
        (read off the screen) is the one you changed to; only if it couldn't be read
        does the app ask."""
        if channel and mfm.parse_channel(channel):
            if self.chan_popup is not None and self.chan_popup.winfo_exists() and self.chan_popup.mode == "prompt":
                self.chan_popup.destroy()
            if self.channel() != str(channel):
                self.channel_var.set(str(channel))
            self._set_status(f"Channel {channel}: read from the game's Change Channel window.")
            return
        if self.chan_popup is not None and self.chan_popup.winfo_exists():
            return                    # the channel picker is already open
        self.ask_channel(changed=True)

    def _channel_box_click(self, e):
        """Clicking the Channel box's arrow opens the picker; clicking the text
        still lets you type."""
        el = str(self.channel_box.identify(e.x, e.y))
        if "arrow" in el or e.x >= self.channel_box.winfo_width() - 22:
            self.open_channel_picker()
            return "break"
        return None

    def _channel_box_posted(self):
        """Fallback if the theme posts the (empty) list anyway: close it, open the picker."""
        self.root.after_idle(lambda: (self.channel_box.tk.call("ttk::combobox::Unpost", self.channel_box),
                                      self.open_channel_picker()))

    def open_channel_picker(self):
        """The channel picker as the Channel box's drop-down."""
        if self.chan_popup is not None and self.chan_popup.winfo_exists():
            if self.chan_popup.mode == "dropdown":
                self.chan_popup.destroy()          # a second click on the arrow closes it
                return
        self.ask_channel(changed=False, dropdown=True)

    def ask_channel(self, changed: bool = True, dropdown: bool = False):
        """Channel picker: a prompt at launch and after the game's change-channel
        dialog has come and gone, or the Channel box's drop-down. Picking a
        channel fills in the Channel box."""
        head = ("Channel change detected. Which channel are you on now?" if changed
                else "Which channel are you on?")
        win = self.chan_popup
        if win is not None and win.winfo_exists():
            if win.mode == "prompt":
                if not dropdown:                 # another change while it's open
                    win.head.config(text=head)
                    self.root.bell()
                win.deiconify()
                win.lift()
                win.focus_force()
                return
            win.destroy()                         # an open drop-down gives way to the prompt
        cur = self.channel()
        count = max(1, min(100, int(self.cfg.get("channel_count", 40) or 40)))
        win = tk.Toplevel(self.root)
        self.chan_popup = win
        win.mode = "dropdown" if dropdown else "prompt"
        # no channel-change detection while you're picking a channel
        self.chan_watch.pause()
        launch = not changed and not dropdown and not getattr(self, "_launch_asked", False)
        if launch:
            self._launch_asked = True

        def closed(ev):
            if ev.widget is not win:
                return
            self.chan_watch.resume()
            if launch:
                # you may have opened the game's Change Channel window to check your
                # channel: don't ask again when it closes
                self.chan_watch.quiet(15)
        win.bind("<Destroy>", closed, add="+")
        win.resizable(False, False)
        if dropdown:
            win.overrideredirect(True)            # a plain panel under the box, like a list
            win.configure(background="#888")
            body = ttk.Frame(win, padding=(8, 8, 8, 6))
            body.pack(padx=1, pady=1)
        else:
            win.title("Channel")
            win.attributes("-topmost", True)      # over the game, even with this window behind it
            body = ttk.Frame(win)
            body.pack()
            win.head = ttk.Label(body, text=head, style="Bold.TLabel", padding=(14, 12, 14, 8))
            win.head.pack(anchor="w")

        def pick(ch):
            ch = mfm.parse_channel(ch)
            if not ch or not ch.isdigit():
                self.root.bell()
                return
            self.channel_var.set(ch)
            self._set_status(f"Channel set to {mfm.channel_label(ch)}.")
            win.destroy()

        cols = 8
        grid = ttk.Frame(body, padding=(0, 0, 0, 4) if dropdown else (14, 0, 14, 4))
        grid.pack()
        buttons = []
        for n in range(1, count + 1):
            b = tk.Button(grid, text=str(n), width=5, padx=2, pady=2, relief="groove",
                          highlightthickness=2, highlightcolor="#1e88e5",
                          command=lambda n=n: pick(str(n)))
            b.normal_bg = b.cget("background")
            if cur == str(n):
                b.config(background="#1e88e5", foreground="white", activebackground="#1565c0",
                         activeforeground="white")
                b.normal_bg = "#1e88e5"
            b.grid(row=(n - 1) // cols, column=(n - 1) % cols, padx=1, pady=1)
            buttons.append(b)
        bottom = ttk.Frame(body, padding=(0, 6, 0, 0) if dropdown else (14, 8, 14, 12))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Or type it:").pack(side="left")
        typed = tk.StringVar()
        ok = (win.register(lambda p: p == "" or (p.isdigit() and p[0] != "0" and int(p) <= 100)), "%P")
        e = ttk.Entry(bottom, textvariable=typed, width=5, validate="key", validatecommand=ok)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: pick(typed.get()))
        e.bind("<KP_Enter>", lambda ev: pick(typed.get()))

        # arrow keys move between the channel buttons, Enter picks one; typing a
        # number goes to the box
        def move(i, key):
            n = len(buttons)
            if key == "Left":
                j = i - 1 if i > 0 else n - 1
            elif key == "Right":
                j = i + 1 if i < n - 1 else 0
            elif key == "Up":
                j = i - cols if i - cols >= 0 else None
            else:
                j = i + cols if i + cols < n else None
            if j is None:
                if key == "Down":
                    e.focus_set()
                    e.icursor("end")
                return "break"
            buttons[j].focus_set()
            return "break"

        def type_digit(ev):
            if ev.char.isdigit():
                e.focus_set()
                typed.set(typed.get() + ev.char if typed.get() else ev.char)
                e.icursor("end")
                return "break"
            return None

        def focus_in(b):
            if b.normal_bg != "#1e88e5":
                b.config(background="#cfe3fa")

        for i, b in enumerate(buttons):
            for key in ("Left", "Right", "Up", "Down"):
                b.bind(f"<{key}>", lambda ev, i=i, key=key: move(i, key))
            b.bind("<Return>", lambda ev, b=b: (b.invoke(), "break")[1])
            b.bind("<KP_Enter>", lambda ev, b=b: (b.invoke(), "break")[1])
            b.bind("<Key>", type_digit)
            b.bind("<FocusIn>", lambda ev, b=b: focus_in(b))
            b.bind("<FocusOut>", lambda ev, b=b: b.config(background=b.normal_bg))

        def entry_up(ev):
            if buttons:
                last_row = (len(buttons) - 1) // cols
                buttons[min(len(buttons) - 1, last_row * cols)].focus_set()
            return "break"
        e.bind("<Up>", entry_up)
        start = buttons[int(cur) - 1] if cur and cur.isdigit() and 1 <= int(cur) <= len(buttons) else \
            (buttons[0] if buttons else e)
        ttk.Button(bottom, text="OK", command=lambda: pick(typed.get())).pack(side="left")
        if dropdown:
            clear = ttk.Button(bottom, text="Clear", command=lambda: (self.channel_var.set(""), win.destroy()))
            clear.pack(side="right")
            tip("Leave the channel empty (listings are saved without one).", clear)
        else:
            skip = ttk.Button(bottom, text=f"Keep {mfm.channel_label(cur)}" if cur else "Skip",
                              command=win.destroy)
            skip.pack(side="right")
            tip("Close without changing the channel (turn these pop-ups off in Settings).", skip)
        win.bind("<Escape>", lambda ev: win.destroy())
        win.update_idletasks()
        if dropdown:
            # right under the Channel box, kept on screen
            x = self.channel_box.winfo_rootx()
            y = self.channel_box.winfo_rooty() + self.channel_box.winfo_height() + 2
            x = max(0, min(x, win.winfo_screenwidth() - win.winfo_reqwidth()))
            win.geometry(f"+{x}+{y}")

            def outside(ev):
                # like a drop-down list: a click anywhere else closes it
                x0, y0 = win.winfo_rootx(), win.winfo_rooty()
                if not (x0 <= ev.x_root < x0 + win.winfo_width() and y0 <= ev.y_root < y0 + win.winfo_height()):
                    win.destroy()
            win.bind("<ButtonPress>", outside)
            try:
                win.wait_visibility()
                win.grab_set()
            except tk.TclError:
                pass
            win.lift()
            start.focus_set()
            return
        # centred over the main window
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.lift()
        win.focus_force()
        start.focus_set()
        self.root.bell()

    def _channel_changed(self, *_):
        if self.monitor:
            self.monitor.channel = self.channel()

    def drain_events(self):
        try:
            while True:
                kind, p = self.events.get_nowait()
                getattr(self, f"on_{kind.lstrip('_')}")(p)
        except queue.Empty:
            pass
        self._own_rects = self.own_windows()
        self.root.after(100, self.drain_events)

    def own_windows(self) -> list:
        """Where this app's windows are on screen, (left, top, width, height) each:
        the monitor paints them over before looking for a store, or a shop photo
        open here (a store window at its real size) is read as a shop in the game."""
        rects = []

        def walk(w):
            try:
                if w.winfo_viewable():
                    rects.append((w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()))
                for c in w.winfo_children():
                    if isinstance(c, tk.Toplevel):
                        walk(c)
            except tk.TclError:
                pass
        walk(self.root)
        return rects

    def on_session(self, p):
        self.live.delete(*self.live.get_children())
        self.scopes = {k: v for k, v in self.scopes.items() if k[0] != str(self.live)}
        shop = (" · ".join(x for x in (p.get("title") and f"Shop: {p['title']}",
                                        p.get("seller") and f"Player: {p['seller']}") if x)
                or p["shop"])
        channel = p.get("channel") or self.channel()
        self.shop_title.config(text="  ·  ".join(x for x in (
            shop, p["map"] and f"Map: {p['map']}",
            channel and f"Channel: {mfm.channel_label(channel)}",
            f"opened {p['started']:%H:%M:%S}") if x))
        self.last_map = p["map"] or self.last_map
        self._show_map()
        self._update_stats(None, None)
        self.shops_today += 1

    def on_new(self, s: mfm.SeenListing):
        verdict = s.verdict
        if s.status == "sold":
            verdict = "SOLD"   # was listed on an earlier visit, sold since
        hit = None if s.sold else mfm.watch_match(self.watch, s.item, s.each)
        if hit:
            self.alert(s, hit)
        wl = mfm.watched_listing_match(self.watched_listings, s.item, s.title, s.seller, s.price)
        if wl:
            self.listing_seen(s, wl)
        self._forget_icon(s.item)  # an icon may have just been saved/upgraded
        ign = s.item in self.ignored
        found = bool(hit) or (bool(wl) and not s.sold)
        iid = self.live.insert("", "end", image=self.icon_for(s.item, found=found),
                         tags=(("ignored",) if ign and verdict != "SOLD" else (tag_for(verdict),))
                         + (("watch",) if found else ()), values=(
            s.item, mfm.fmt(s.each), mfm.fmt(s.market),
            diff_text(s.pct), verdict, s.notes,
            {"seen": "already logged", "sold": "marked sold"}.get(s.status, "new")))
        if s.status == "seen":   # same shop, same price, logged in the last 24 h: not added again
            self.live.item(iid, tags=tuple(self.live.item(iid, "tags")) + ("dupe",))
        self.scopes[(str(self.live), iid)] = {"price": s.price, "qty": s.qty, "label": s.shop,
                                              "title": s.title, "seller": s.seller, "map": s.map,
                                              "channel": s.channel, "id": s.id, "item": s.item}
        self.live.yview_moveto(1.0)
        if s.status != "seen":
            self.logged_today += 1
        # deals only make a sound for items on your alert list (a watched item under
        # its max price already pinged above)
        if verdict == "DEAL" and not hit and not ign and any(w["item"] == s.item and not mfm.watch_is_word(w) for w in self.watch):
            self.root.bell()

    def _ping(self):
        """The alert: a sound (once per burst of matches) and this window to the front."""
        import time as _t
        if self.cfg.get("alert_sound_on", True) and _t.monotonic() - getattr(self, "_last_ping", 0) > 2:
            self._last_ping = _t.monotonic()
            if not mfm.play_sound(self.cfg.get("alert_sound") or None):
                self.root.bell()
        if self.cfg.get("alert_raise", True):
            try:
                self.root.deiconify()
                self.root.lift()
            except tk.TclError:
                pass

    def alert(self, s, w):
        """A watched item is for sale here: alert, say where, remember it."""
        where = " · ".join(x for x in (s.shop, s.map, mfm.channel_label(s.channel)) if x)
        self._set_status(f"Alert: {s.item} for {mfm.fmt(s.each)} at {where}")
        title, player = (s.title, s.seller) if (s.title or s.seller) else split_shop(s.shop)
        w["last_found"] = {"each": s.each, "shop": title, "player": player, "map": s.map,
                           "channel": s.channel, "time": s.first_seen}
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self._ping()

    def listing_seen(self, s, w):
        """A watched listing was seen again: alert if it's still for sale, and keep
        its latest price, place and time."""
        title, player = (s.title, s.seller) if (s.title or s.seller) else split_shop(s.shop)
        w["last"] = {"price": s.price, "shop": title, "seller": player, "map": s.map,
                     "channel": s.channel, "time": s.first_seen, "sold": bool(s.sold)}
        mfm.save_watched_listings(self.watched_listings)
        self.refresh_watched_listings()
        name = s.item + (f" ({w['notes']})" if w.get("notes") else "")
        if s.sold:
            self._set_status(f"Watched listing sold: {name}")
            return
        where = " · ".join(x for x in (s.shop, s.map, mfm.channel_label(s.channel)) if x)
        self._set_status(f"Alert: watched listing {name} for {mfm.fmt(s.price)} at {where}")
        self._ping()

    def on_closed(self, p):
        n = p["count"]
        deals = sum(1 for k in self.live.get_children() if "deal" in self.live.item(k, "tags"))
        label = " · ".join(x for x in (p.get("title"), p.get("seller")) if x) or p["shop"]
        self.recent.insert(0, f"{datetime.now():%H:%M}  {label}"
                              + (f" @ {p['map']}" if p["map"] else "") + f"  — {n} items"
                              + (f", {p['sold']} sold" if p.get("sold") else "")
                              + (f", {deals} deal{'s' if deals != 1 else ''}" if deals else ""))
        if deals:
            self.recent.itemconfig(0, foreground="#145c2c")
        self.shop_title.config(text=self.shop_title.cget("text") + "  ·  closed")
        self.refresh_tables()
        self.fetch_icons_async([self.live.set(k, "item") for k in self.live.get_children()])

    def on_frame(self, p):
        img = p["image"].copy()
        img.thumbnail((280, 200))
        self.preview_img = ImageTk.PhotoImage(img)
        self.preview.config(image=self.preview_img, text="")
        n = len(p["listings"])
        self._update_stats(f"{n}  (+{p['hidden']} under cursor)" if p.get("hidden") else n,
                           p["ocr_ms"])

    def on_map(self, name):
        self.last_map = name or self.last_map
        self._show_map()
        self._update_stats(None, None)

    def _show_map(self):
        self.map_label.config(text=f"Map: {self.last_map or '—'}")

    def _hint(self, key: str, text: str | None):
        """Show (or, with None, take away) a red line in the bar above the status bar."""
        if text:
            self.hints[key] = text
        else:
            self.hints.pop(key, None)
        if self.hints:
            self.hint_bar.config(text="\n".join(self.hints.values()))
            if not self.hint_bar.winfo_ismapped():
                self.hint_bar.pack(side="bottom", fill="x", after=self.status)   # just above it
        else:
            self.hint_bar.pack_forget()

    def on_chat_hint(self, no_time):
        self._hint("chat_time", "Chat timestamps are off, so megaphones get the time they were read, not sent. "
                                "Turn them on in the game's settings (each line then starts like [13:45])." if no_time else None)

    def on_map_hint(self, no_name):
        self._hint("map_name", "The map name can't be read: switch the game's minimap to the view that shows "
                               "the map name (the street and map name above the map)." if no_name else None)

    def on_layout(self, layout):
        self.found_at = layout
        self._update_stats(None, None)

    def on_backlog(self, p):
        """How far the OCR is behind the captures. Scrolling faster than the OCR
        queues frames instead of losing rows; they're read moments later."""
        self.backlog = p["frames"]
        if self.stop_event is not None and self.stop_event.is_set():
            if self.backlog and not (self.monitor and self.monitor.abort):
                self._set_status(f"Stopped — still reading {self.backlog} captured frames "
                                 "(press Stop again to discard them).")
        elif self.backlog >= 10:
            self._behind = True
            self._set_status(f"Monitoring — reading is {self.backlog} frames behind the "
                             "screen; nothing is lost, results show in a moment.")
        elif self._behind and not self.backlog:
            self._behind = False
            self._set_status("Monitoring — open shops and scroll through them normally.")

    def on_stopped(self, _p=None):
        """The monitor finished, including any frames still queued when Stop was pressed."""
        self.backlog = 0
        self._hint("chat_time", None)          # (checked again on the next start)
        self._hint("map_name", None)
        if self.stop_event is not None and self.stop_event.is_set():
            self._set_status("Stopped.")

    def on_error(self, msg):
        self.report_error("Monitoring error", msg)

    def report_error(self, title: str, msg: str):
        """Errors go to the status bar and to an error window you can't miss. One
        window collects them (no pile of dialogs), and the same error repeating
        (e.g. on every frame) reopens it at most every 2 minutes."""
        import time as _t
        msg = str(msg).strip() or "Unknown error"
        self._set_status(f"Error: {title}: {msg}")
        hint = ""
        low = msg.lower()
        if "readonly" in low or "read-only" in low or "permission denied" in low:
            hint = "Prices can't be saved. Check that you can write to the data folder:\n" + str(mfm.DATA)
        elif "disk" in low and ("full" in low or "space" in low):
            hint = "The disk is full. Free some space."
        elif "database is locked" in low:
            hint = "Another copy of the app may be open. Close it."
        elif "certificate" in low:
            hint = "Secure connection failed (certificate). Please report this, with the details above."
        elif any(k in low for k in ("urlopen", "timed out", "name resolution", "connection", "http error")):
            hint = "Check your internet connection, then try again from Settings."
        key = (title, msg)
        now = _t.monotonic()
        last = self._error_times.get(key)
        self._error_times[key] = now
        if last is not None and now - last < 120:
            return
        line = f"{datetime.now():%H:%M:%S}  {title}\n{msg}" + (f"\n→ {hint}" if hint else "")
        win = getattr(self, "_error_win", None)
        if win is None or not win.winfo_exists():
            win = self.window()
            win.title("Free Market Tracker: error")
            win.transient(self.root)
            win.geometry("620x300")
            head = ttk.Frame(win, padding=(14, 12, 14, 6))
            head.pack(fill="x")
            ttk.Label(head, text="⚠", foreground="#c8352b", font=("TkDefaultFont", 22)).pack(side="left")
            ttk.Label(head, text="  Something went wrong", style="Big.TLabel").pack(side="left")
            box = tk.Text(win, wrap="word", height=8, relief="flat", padx=10, pady=8,
                          background="#fbefee", font="TkDefaultFont")
            box.pack(fill="both", expand=True, padx=14)
            btns = ttk.Frame(win, padding=(14, 8, 14, 12))
            btns.pack(fill="x")
            ttk.Button(btns, text="OK", command=win.destroy).pack(side="right")
            ttk.Button(btns, text="Copy details", command=lambda: (
                self.root.clipboard_clear(), self.root.clipboard_append(box.get("1.0", "end")))
            ).pack(side="right", padx=6)
            ttk.Button(btns, text="Open data folder", command=self.open_data_folder).pack(side="left")
            win._box = box
            self._error_win = win
        box = win._box
        box.configure(state="normal")
        if box.get("1.0", "end").strip():
            box.insert("end", "\n\n")
        box.insert("end", line)
        box.see("end")
        box.configure(state="disabled")
        win.deiconify()
        win.lift()

    def _tk_error(self, exc, val, tb):
        """Any failing button / menu action (e.g. deleting from a write-protected
        database) is reported instead of failing silently."""
        import traceback
        detail = "".join(traceback.format_exception_only(exc, val)).strip()
        try:
            with open(mfm.DATA / "app.log", "a", encoding="utf-8") as f:
                f.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass
        self.report_error("Action failed", detail)

    def _update_stats(self, rows, ocr_ms):
        on_screen = "—" if rows is None else rows
        speed = "—" if ocr_ms is None else f"{ocr_ms} ms"
        region, mregion = mfm.drawn_area(self.cfg, "region"), mfm.drawn_area(self.cfg, "map_region")

        def area(r, auto_r):
            # a saved area covering most of the screen is ignored (auto is used)
            screen = tuple(mfm.LAST_SCREEN_SIZE) or (self.root.winfo_screenwidth(),
                                                     self.root.winfo_screenheight())
            if r and not mfm.is_auto(r, screen):
                return f"manual {r[2]}×{r[3]}"
            if auto_r:
                return f"auto, found at {auto_r[0]},{auto_r[1]}"
            return "auto (open a shop)"
        lay = self.found_at
        self.stats.config(text=(
            f"Rows read this frame:  {on_screen}\n"
            f"OCR time:  {speed}\n"
            f"Frames waiting:  {self.backlog}\n"
            f"Shops visited:  {self.shops_today}\n"
            f"Listings logged:  {self.logged_today}\n"
            f"Shop area:  {area(region, lay.store if lay else None)}\n"
            f"Map area:  {area(mregion, lay.map if lay else None)}\n"
            f"Current map:  {self.last_map or '—'}"))

    def _set_status(self, text=None):
        base = text or "Idle. Press Start, then play normally."
        self.status.config(text=f"{base}    ·    v{mfm.VERSION}    ·    data: {mfm.DATA}")

    # ------------------------------------------------------------------ tables
    def check_db_size(self, repeat: bool = True):
        try:
            size = mfm.db_size()
        except OSError:
            size = 0
        if size > mfm.DB_WARN_BYTES:
            self.size_warn.config(text=(
                f"⚠  Your price database is {size / 1048576:,.0f} MB. Click here to archive or "
                "delete old data."))
            if not self.size_warn.winfo_ismapped():
                self.size_warn.pack(fill="x", padx=10, pady=(0, 6), before=self.nb)
        elif self.size_warn.winfo_ismapped():
            self.size_warn.pack_forget()
        if repeat:
            self.root.after(60000, self.check_db_size)

    def retention_tick(self):
        """Every 6 hours while the app is open. Skipped while monitoring (cleaning up
        briefly locks the database) and tried again 10 minutes later."""
        if self.worker and self.worker.is_alive():
            self.root.after(10 * 60 * 1000, self.retention_tick)
            return
        self.run_retention(quiet=True)
        self.root.after(6 * 3600 * 1000, self.retention_tick)

    def run_retention(self, quiet=False, cfg=None, done=None):
        cfg = cfg or self.cfg
        if cfg.get("retention", "archive") not in ("archive", "delete") or getattr(self, "_pruning", False):
            return
        self._pruning = True

        def work():
            try:
                con = mfm.db()
                n, path = mfm.apply_retention(con, cfg)
                con.close()
                try:
                    mfm.purge_shots(float(cfg.get("shot_days", 7)))
                except Exception as e:
                    self.events.put(("error", f"couldn't clean up old shop photos: {e}"))
                if n or not quiet:
                    self.events.put(("status", (
                        f"Removed {mfm.plural(n, 'listing')} not seen for "
                        f"{mfm.plural(int(cfg.get('keep_days', 30)), 'day')}"
                        + (f"; archived to {path}" if path else "") + ".")
                        if n else "No listings old enough to remove."))
                if n:
                    self.events.put(("call", lambda: (self.refresh_tables(), self.check_db_size(False))))
            except Exception as e:
                msg = f"Cleaning up old listings failed: {e}"
                self.events.put(("call", lambda: self.report_error("Cleanup failed", msg)))
            finally:
                self._pruning = False
                if done:
                    self.events.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    def periodic_refresh(self):
        if self.nb.select() != str(self.live_tab):
            self.refresh_tables()
        self.refresh_megaphones()
        self.root.after(5000, self.periodic_refresh)

    def _keep_selection(self, tree, key):
        """Remember which rows are selected (and the scroll position) so a refresh
        doesn't throw away a multi-selection; returns a function that restores it."""
        chosen = {key(tree, k) for k in tree.selection()}
        top = tree.yview()[0]

        def restore():
            if chosen:   # (nothing was selected: don't read every row's columns for nothing)
                again = [k for k in tree.get_children("") if key(tree, k) in chosen]
                if again:
                    tree.selection_set(again)
            tree.yview_moveto(top)
        return restore

    def refresh_tables(self):
        """Bring every table up to date. Alerts, Ignored and My Inventory are
        rebuilt right here (a few rows). Deals, Items and Review mean thousands
        of queries and the fuzzy name search for every unmatched name, so they're
        worked out by a background thread with its own database connection
        (request_tables) and only filled in here once that's done (on_tables);
        the window keeps responding meanwhile. Toggling a filter re-filters the
        last result instead (refill_tables), without going back to the database."""
        self.refresh_alerts()
        self.refresh_ignored()
        self.refresh_inventory()
        self.request_tables()

    def request_tables(self):
        """Ask the background thread for fresh Deals / Items / Review data. Asking
        again while it's busy just replaces what it does next; the result arrives
        as a "tables" event."""
        try:
            hours = max(1, int(self.hours_var.get()))
        except (tk.TclError, ValueError):
            hours = 24
        want = {"hours": hours, "show_sold": bool(self.show_sold.get()), "cfg": dict(self.cfg),
                "show_kept": bool(hasattr(self, "review_show_kept") and self.review_show_kept.get()),
                # My Inventory's stats come with the data (see refresh_inventory)
                "inventory": sorted({r["item"] for r in mfm.load_inventory(self.inv_con)})
                if hasattr(self, "inv_con") else []}
        with self._tables_lock:
            self._tables_want = want
            if self._tables_busy:
                return
            self._tables_busy = True
        threading.Thread(target=self._tables_work, daemon=True, name="mfm-tables").start()

    def _tables_work(self):
        con = None
        last = None     # (what was asked, the database's version, when) of the last answer
        while True:
            with self._tables_lock:
                want, self._tables_want = self._tables_want, None
                if want is None:
                    self._tables_busy = False
                    return
            try:
                if con is None:
                    con = mfm.db()
                # Asked every 5 seconds (periodic_refresh). The same question, nothing
                # written to the database since (by any connection: PRAGMA data_version)
                # and the last answer under a minute old (listings do age out of the
                # window) has the same answer: skip the seconds of queries, which kept
                # this thread busy, and the window slow, with nothing new to show.
                version = con.execute("PRAGMA data_version").fetchone()[0]
                if last and last[0] == want and last[1] == version and time.monotonic() - last[2] < 60:
                    continue
                data = mfm.table_data(con, want["cfg"], hours=want["hours"],
                                      show_sold=want["show_sold"], show_kept=want["show_kept"],
                                      review=bool(want["cfg"].get("review_on", True)))
                data["inventory"] = {}
                for item in want.get("inventory") or ():
                    mfm.ui_pause()
                    data["inventory"][item] = mfm.inventory_stats(con, item, want["cfg"])
                last = (want, version, time.monotonic())
            except Exception as e:
                self.events.put(("tables_error", f"{type(e).__name__}: {e}"))
                continue
            self.events.put(("tables", data))

    def on_tables(self, data):
        if data == self._tables_data:
            return        # nothing changed since the last fill: leave the tables alone
        self._tables_data = data
        self.refresh_inventory()      # its stats came with the data
        self.refill_tables()

    def on_tables_error(self, msg):
        self.report_error("Updating the tables failed", msg)

    def refill_tables(self):
        """Show the last computed data in the Deals, Items and Review tables, as the
        current checkboxes and filter say. Only the table on screen is filled now
        (thousands of rows take a moment even without the database); the others
        are marked stale and filled when their tab is opened. Waits while you're
        drag-selecting rows."""
        data = self._tables_data
        if data is None:
            return
        for job in ("_refill_job", "_deals_job"):
            if getattr(self, job):
                self.root.after_cancel(getattr(self, job))
                setattr(self, job, None)
        trees = [self.deals, self.items] + ([self.review] if hasattr(self, "review") else [])
        if any(getattr(t, "dragging", False) for t in trees):
            self._refill_job = self.root.after(400, self.refill_tables)
            return
        self._stale = {"deals", "items", "review"}
        if hasattr(self, "review_tab"):
            self._shop_review_n = sum(1 for r in data["review"] if not r["kept"])
            self._review_title()
        self._fill_stale()

    def _review_title(self):
        """The Review tab's count: shop names plus megaphone phrases waiting."""
        a, b = getattr(self, "_shop_review_n", 0), getattr(self, "_mega_review_n", 0)
        self.nb.tab(self.review_tab, text=f"  Review ({a + b})  " if a + b else "  Review  ")
        if hasattr(self, "review_nb"):
            self.review_nb.tab(self.review_shop_tab, text=f"  Shop listings ({a})  " if a else "  Shop listings  ")
            self.review_nb.tab(self.review_mega_tab, text=f"  Megaphones ({b})  " if b else "  Megaphones  ")

    def _fill_stale(self):
        data = self._tables_data
        if data is None or not self._stale:
            return
        showing = self.nb.select()
        tabs = (("deals", "deals_tab", self._fill_deals), ("items", "items_tab", self._fill_items),
                ("review", "review_tab", self._fill_review))
        for name, tab, fill in tabs:
            if name in self._stale and hasattr(self, tab) and showing == str(getattr(self, tab)):
                self._stale.discard(name)
                with self._ui_busy():
                    fill(data[name])

    def _fill_deals(self, scored):
        """The Deals table can run to thousands of rows (every listing seen in the
        window), so they go in 1,500 at a time, with the window free in between."""
        if self._deals_job:
            self.root.after_cancel(self._deals_job)
            self._deals_job = None
        row_key = lambda t, k: tuple(t.set(k, c) for c in ("item", "each", "status", "shop", "player", "map",
                                                           "channel"))
        restore = self._keep_selection(self.deals, row_key)
        show_outliers, only_deals, show_ignored = (self.show_outliers.get(), self.only_deals.get(),
                                                   self.show_ignored.get())
        rows = [r for r in scored
                if (show_outliers or r[6] != "OUTLIER")
                and not (only_deals and (r[6] != "DEAL" or r[0]))
                and (show_ignored or r[2] not in self.ignored)]
        if self.deals_watched_only.get():
            # sold, pct, item, qty, each, mkt, v, where, ts, price, shop, seller, notes
            rows = [r for r in rows if not r[0] and (
                mfm.watch_match(self.watch, r[2], r[4])
                or mfm.watched_listing_match(self.watched_listings, r[2], r[10] or "", r[11] or "", r[9]))]
        self.deals.delete(*self.deals.get_children())
        self.scopes = {k: v for k, v in self.scopes.items() if k[0] != str(self.deals)}
        tree_id = str(self.deals)

        def put(start):
            self._deals_job = None
            end = min(start + 1500, len(rows))
            for sold, pct, item, qty, each, mkt, v, where, ts, price, shop, seller, notes in rows[start:end]:
                found = not sold and (bool(mfm.watch_match(self.watch, item, each)) or bool(
                    mfm.watched_listing_match(self.watched_listings, item, shop or "", seller or "", price)))
                tag = "sold" if sold else ("ignored" if item in self.ignored else tag_for(v))
                iid = self.deals.insert("", "end", image=self.icon_for(item, found=found),
                                        tags=(tag,) + (("watch",) if found else ()), values=(
                    item, mfm.fmt(each), mfm.fmt(mkt),
                    diff_text(None if pct == 999 else pct),
                    v, "sold" if sold else "for sale", notes, *where, mfm.local_time(ts)))
                self.scopes[(tree_id, iid)] = {"price": price, "qty": qty, "shop": shop, "seller": seller,
                                               "title": shop or "", "map": where[2], "channel": where[3],
                                               "item": item, "ts": ts}
            if end < len(rows):
                self._deals_job = self.root.after(1, lambda: put(end))
            else:
                restore()
        put(0)

    def _fill_items(self, rows):
        restore = self._keep_selection(self.items, lambda t, k: t.set(k, "item"))
        self.items.delete(*self.items.get_children())
        filt = self.item_filter() if hasattr(self, "f_name") else {}
        threshold = self.cfg["deal_threshold_pct"]
        shown = total = 0
        for item, mkt, src, st, b, npc_buy, npc_sell, mg in rows:
            total += 1
            if filt and not mfm.item_matches(item, filt):
                continue
            shown += 1
            tag = ""
            if item in self.ignored:
                tag = "ignored"
            elif b:
                v, _ = mfm.verdict(b[0], mkt, threshold)
                tag = tag_for(v) if v == "DEAL" else ""
            found = bool(b) and bool(mfm.watch_match(self.watch, item, b[0]))
            self.items.insert("", "end", image=self.icon_for(item, found=found),
                              tags=(tag,) + (("watch",) if found else ()), values=(
                item, mfm.fmt(b[0]) if b else "-", (b[1] or "") if b else "", (b[2] or "") if b else "",
                (b[3] or "") if b else "", (b[4] or "") if b else "",
                mfm.fmt(st["listed_median"]), st["listed_n"],
                mfm.fmt(st["sold_median"]), st["sold_n"], mfm.fmt(mkt),
                mfm.fmt(mg["sell"]) if mg.get("sell") else "-", mfm.fmt(mg["buy"]) if mg.get("buy") else "-",
                mfm.fmt(npc_buy) if npc_buy else "-", mfm.fmt(npc_sell) if npc_sell else "-", src))
        if hasattr(self, "f_count"):
            self.f_count.config(text=f"{shown:,} of {total:,} items" if shown != total else
                                f"{total:,} items")
        restore()

    # ------------------------------------------------------------------ items filter
    ANY = "Any"

    def _build_item_filter(self, parent):
        """Filter bar for the Items tab, modelled on NiaMeowDB's item database: name,
        category and subtype, class (with or without all-class items), level range and
        sold by NPCs. Remembered between sessions."""
        saved = self.cfg.get("items_filter") or {}
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 6))
        self.f_name = tk.StringVar(value=saved.get("name", ""))
        self.f_type = tk.StringVar(value=saved.get("type") or self.ANY)
        self.f_sub = tk.StringVar(value=saved.get("subtype") or self.ANY)
        self.f_job = tk.StringVar(value=saved.get("job") or self.ANY)
        self.f_allclass = tk.BooleanVar(value=saved.get("all_class", True))
        self.f_lmin = tk.StringVar(value="" if saved.get("level_min") is None else str(saved["level_min"]))
        self.f_lmax = tk.StringVar(value="" if saved.get("level_max") is None else str(saved["level_max"]))
        self.f_npc = tk.BooleanVar(value=saved.get("npc", False))

        def lab(text, **kw):
            ttk.Label(bar, text=text).pack(side="left", **kw)
        lab("Filter:")
        e = ttk.Entry(bar, textvariable=self.f_name, width=16)
        e.pack(side="left", padx=(4, 10))
        tip("Show items whose name contains this text.", e)
        lab("Category")
        self.f_type_cb = ttk.Combobox(bar, textvariable=self.f_type, state="readonly", width=7)
        self.f_type_cb.pack(side="left", padx=(4, 6))
        tip("Equip, Use, Etc, Setup or Cash.", self.f_type_cb)
        self.f_sub_cb = ttk.Combobox(bar, textvariable=self.f_sub, state="readonly", width=14)
        self.f_sub_cb.pack(side="left", padx=(0, 10))
        tip("The kind of item (Shoes, Scroll, Ore, ...).", self.f_sub_cb)
        lab("Class")
        self.f_job_cb = ttk.Combobox(bar, textvariable=self.f_job, state="readonly", width=9)
        self.f_job_cb.pack(side="left", padx=(4, 2))
        tip("Items that need this class.", self.f_job_cb)
        cb = ttk.Checkbutton(bar, text="+ all-class", variable=self.f_allclass)
        cb.pack(side="left", padx=(0, 10))
        tip("Also show items every class can use.", cb)
        lab("Level")
        ok = (self.root.register(lambda v: v == "" or (v.isdigit() and int(v) <= 250)), "%P")
        for var, t in ((self.f_lmin, "Lowest required level"), (self.f_lmax, "Highest required level")):
            sp = ttk.Spinbox(bar, from_=0, to=200, increment=10, width=4, textvariable=var,
                             validate="key", validatecommand=ok)
            sp.pack(side="left", padx=(4, 0))
            tip(t + " (empty = no limit).", sp)
            if var is self.f_lmin:
                lab("–", padx=(3, 0))
        cb = ttk.Checkbutton(bar, text="Sold by NPCs", variable=self.f_npc)
        cb.pack(side="left", padx=(10, 0))
        tip("Only items an NPC shop sells.", cb)
        b = ttk.Button(bar, text="Clear", command=self.clear_item_filter)
        b.pack(side="left", padx=(10, 0))
        self.f_count = ttk.Label(bar, foreground="#666")
        self.f_count.pack(side="left", padx=(10, 0))
        self._filter_job = None

        def changed(*_):
            if self.f_type.get() == self.ANY and self.f_sub.get() != self.ANY:
                self.f_sub.set(self.ANY)
            self._update_filter_choices()
            if self._filter_job:
                self.root.after_cancel(self._filter_job)
            self._filter_job = self.root.after(250, self._apply_item_filter)   # after typing pauses
        for v in (self.f_name, self.f_type, self.f_sub, self.f_job, self.f_allclass,
                  self.f_lmin, self.f_lmax, self.f_npc):
            v.trace_add("write", changed)
        self._update_filter_choices()

    def item_filter(self) -> dict:
        def num(v):
            return int(v) if v.strip().isdigit() else None
        any_ = self.ANY
        return {"name": self.f_name.get().strip(),
                "type": None if self.f_type.get() == any_ else self.f_type.get(),
                "subtype": None if self.f_sub.get() == any_ else self.f_sub.get(),
                "job": None if self.f_job.get() == any_ else self.f_job.get(),
                "all_class": self.f_allclass.get(),
                "level_min": num(self.f_lmin.get()), "level_max": num(self.f_lmax.get()),
                "npc": self.f_npc.get()}

    def _update_filter_choices(self):
        if self._tables_data is not None:
            names = [r[0] for r in self._tables_data["items"]]
        else:
            names = [r[0] for r in self.con.execute("SELECT DISTINCT item FROM listings")]
        opts = mfm.item_filter_options(names)
        self.f_type_cb["values"] = [self.ANY] + opts["types"]
        t = self.f_type.get()
        subs = opts["subtypes"].get(t, []) if t != self.ANY else []
        self.f_sub_cb["values"] = [self.ANY] + subs
        self.f_sub_cb.state(["!disabled" if subs else "disabled"])
        self.f_job_cb["values"] = [self.ANY] + opts["jobs"]

    def _apply_item_filter(self):
        self._filter_job = None
        f = self.item_filter()
        self.cfg["items_filter"] = f
        mfm.save_config(self.cfg)
        if self._tables_data is not None:
            self._stale.discard("items")
            self._fill_items(self._tables_data["items"])   # the same data, filtered: instant
        else:
            self.request_tables()

    def clear_item_filter(self):
        for v in (self.f_name, self.f_lmin, self.f_lmax):
            v.set("")
        for v in (self.f_type, self.f_sub, self.f_job):
            v.set(self.ANY)
        self.f_allclass.set(True)
        self.f_npc.set(False)

    def _build_watched_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Watched  ")
        # sound options for every alert, under both halves
        bottom = ttk.Frame(tab)
        bottom.pack(side="bottom", fill="x", pady=(6, 0))
        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True)
        top_half = ttk.Frame(panes)
        low_half = ttk.Frame(panes, padding=(0, 8, 0, 0))
        panes.add(top_half, weight=1)
        panes.add(low_half, weight=1)

        # ---- top: watched items
        head = ttk.Frame(top_half)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Watched items", style="Bold.TLabel").pack(side="left")
        ttk.Label(head, foreground="#555", text="  Get an alert when one of these items is for sale. "
                  "Leave the max price empty for any price. Words: megaphones that say them are "
                  "highlighted.").pack(side="left")
        add = ttk.Frame(top_half)
        add.pack(fill="x")
        # Item or Word, like the Type in Review → Megaphones: a word is for something that
        # can't be matched to an item; megaphones whose message has it are highlighted
        self.alert_words = False
        self.alert_type_btn = ttk.Button(add, text="Type: Item", width=12, command=self._alert_toggle_type)
        self.alert_type_btn.pack(side="left", padx=(0, 6))
        tip("What Watch adds. Item: an item, found in shops and megaphones. Word: words found "
            "in megaphone messages, for things that can't be matched to an item. Click to switch.",
            self.alert_type_btn)
        self.alert_item_lbl = ttk.Label(add, text="Item:")
        self.alert_item_lbl.pack(side="left")
        self.alert_entry = ItemSearch(add, self, width=42, on_return=lambda: self.alert_max_entry.focus_set())
        self.alert_entry.pack(side="left", padx=4)
        self.alert_item = self.alert_entry.var
        self.alert_word = tk.StringVar()
        self.alert_word_entry = ttk.Entry(add, textvariable=self.alert_word, width=44)
        self.alert_word_entry.bind("<Return>", lambda e: self.add_alert_from_form())
        self.alert_max_lbl = ttk.Label(add, text="Max price:")
        self.alert_max_lbl.pack(side="left", padx=(10, 0))
        self.alert_max = tk.StringVar()
        self.alert_max_entry = ttk.Entry(add, textvariable=self.alert_max, width=12)
        self.alert_max_entry.pack(side="left", padx=4)
        self.alert_max_entry.bind("<Return>", lambda e: self.add_alert_from_form())
        self.alert_watch_btn = ttk.Button(add, text="Watch", command=self.add_alert_from_form)
        self.alert_watch_btn.pack(side="left", padx=4)
        ttk.Button(add, text="Remove selected", command=self.remove_alert).pack(side="right")

        self.alerts = make_tree(top_half, ("item", "max_each", "last_price", "shop", "player", "map",
                                           "channel", "time"))
        self.alerts.frame.pack(fill="both", expand=True, pady=(6, 0))
        # (a watched word has no item history)
        item_row = lambda: all(k.startswith("i\x1f") for k in self.alerts.selection())
        self.alerts.bind("<Double-1>", lambda e: item_row() and self.open_history(self.alerts))
        self._list_menu(self.alerts, [
            ("Show history", lambda t: item_row() and self.open_history(t), True),
            ("Change max price", lambda t: self.change_alert_max(), False),
            None,
            ("Remove item", lambda t: self.remove_alert(), False)])
        self.alerts.bind("<Delete>", lambda e: self.remove_alert())
        self.alerts.bind("<BackSpace>", lambda e: self.remove_alert())

        # ---- bottom: watched listings
        head = ttk.Frame(low_half)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Watched listings", style="Bold.TLabel").pack(side="left")
        ttk.Label(head, foreground="#555", text="  Get an alert when one of these listings is for "
                  "sale again. Right-click a listing and choose Watch this listing.").pack(side="left")
        ttk.Button(head, text="Remove selected", command=self.remove_watched_listing).pack(side="right")
        self.wlist = make_tree(low_half, ("item", "notes", "each", "status", "shop", "player", "map",
                                          "channel", "time"))
        self.wlist.column("status", width=110)
        self.wlist.frame.pack(fill="both", expand=True)
        self.wlist.bind("<Double-1>", lambda e: self.open_history(self.wlist))
        self._list_menu(self.wlist, [
            ("Show history", self.open_history, True),
            ("Edit note", lambda t: self.edit_watched_note(), True),
            None,
            ("Remove listing", lambda t: self.remove_watched_listing(), False)])
        self.wlist.bind("<Delete>", lambda e: self.remove_watched_listing())
        self.wlist.bind("<BackSpace>", lambda e: self.remove_watched_listing())

        ttk.Label(bottom, text="Alerts:").pack(side="left")
        self.sound_on = tk.BooleanVar(value=self.cfg.get("alert_sound_on", True))
        ttk.Checkbutton(bottom, text="Play sound", variable=self.sound_on,
                        command=self._save_sound_prefs).pack(side="left", padx=(8, 0))
        self.raise_on = tk.BooleanVar(value=self.cfg.get("alert_raise", True))
        ttk.Checkbutton(bottom, text="Bring this window to front", variable=self.raise_on,
                        command=self._save_sound_prefs).pack(side="left", padx=(10, 0))
        ttk.Button(bottom, text="Test sound", command=self.test_sound).pack(side="left", padx=(10, 0))
        ttk.Button(bottom, text="Choose sound", command=self.choose_sound).pack(side="left", padx=4)
        self.sound_lbl = ttk.Label(bottom, foreground="#666")
        self.sound_lbl.pack(side="left", padx=6)
        self.refresh_alerts()
        self.refresh_watched_listings()

    # ------------------------------------------------------------------ shared database
    def _start_shared(self):
        import mfm_shared

        login = mfm_shared.load_login()
        url = self.cfg.get("shared_server", "")
        if self.shared:
            self.shared.stop()
            self.shared = None
        if not login.get("token") or login.get("server") != mfm_shared.normalize_url(url):
            self.on_shared({"ok": False, "text": "Not signed in"})
            return
        try:
            self.shared = mfm_shared.SharedSync(self.cfg, url, login["token"],
                                                on_status=lambda st: self.events.put(("shared", st)),
                                                on_new_rows=lambda n: self.events.put(("shared_rows", n)),
                                                on_megaphones=lambda n: self.events.put(("megaphones", n)))
        except mfm_shared.SharedError as e:
            self.on_shared({"ok": False, "text": str(e)})
            return
        self.on_shared({"ok": False, "text": "Connecting…"})
        self.shared.start()

    def _stop_shared(self):
        if self.shared:
            self.shared.stop()
            self.shared = None
        self.on_shared({"ok": False, "text": "Not connected"})

    def on_shared(self, st):
        if st.get("signed_out") and self.shared:
            self.shared.stop()            # the server ended the sign-in: ask for the password
            self.shared = None
        self.shared_state = st
        self.shared_dot.config(foreground="#2e7d32" if st.get("ok") else "#c62828")
        dlg = getattr(self, "shared_win", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.refresh()

    def on_shared_rows(self, n):
        self.refresh_tables()

    def shared_dialog(self):
        import mfm_shared

        old = getattr(self, "shared_win", None)
        if old is not None and old.winfo_exists():
            old.deiconify()
            old.lift()
            return
        win = tk.Toplevel(self.root)
        self.shared_win = win
        win.title("Shared Database")
        win.resizable(False, False)
        win.transient(self.root)
        win.geometry(f"+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 90}")
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, wraplength=560, justify="left", text=(
            "Share prices and megaphones with other players through a Free Market Tracker server that one of "
            "you runs. Everyone connected sees each other's prices here as they're logged. Only items from the "
            "item list are shared (names in Review are shared once you match them).")).pack(anchor="w")
        login = mfm_shared.load_login()
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x", pady=(12, 0))
        addr = tk.StringVar(value=self.cfg.get("shared_server", "") or login.get("server", ""))
        user = tk.StringVar(value=login.get("username", ""))
        pw = tk.StringVar()
        ttk.Label(form, text="Server address:").grid(row=0, column=0, sticky="w")
        e1 = ttk.Entry(form, textvariable=addr, width=44)
        e1.grid(row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(form, text="e.g. 203.0.113.5:8765", foreground="#777").grid(row=0, column=2, sticky="w")
        ttk.Label(form, text="User name:").grid(row=1, column=0, sticky="w")
        e2 = ttk.Entry(form, textvariable=user, width=44)
        e2.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(form, text="Password:").grid(row=2, column=0, sticky="w")
        e3 = ttk.Entry(form, textvariable=pw, width=44, show="•")
        e3.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        pw_note = ttk.Label(form, foreground="#777")
        pw_note.grid(row=2, column=2, sticky="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(10, 0))
        dot = tk.Label(row, text="●", font=("TkDefaultFont", 14))
        dot.pack(side="left")
        status = ttk.Label(row, wraplength=520, justify="left")
        status.pack(side="left", padx=6)
        bottom = ttk.Frame(body)
        bottom.pack(fill="x", pady=(14, 0))
        go = ttk.Button(bottom, text="Connect")
        go.pack(side="left")
        out_btn = ttk.Button(bottom, text="Sign out")
        out_btn.pack(side="left", padx=6)
        chg_btn = ttk.Button(bottom, text="Change password")
        chg_btn.pack(side="left")
        ttk.Button(bottom, text="Close", command=win.destroy).pack(side="right")
        busy = {"on": False}

        def refresh():
            st = self.shared_state
            running = self.shared is not None
            saved = mfm_shared.load_login()
            dot.config(foreground="#2e7d32" if st.get("ok") else "#c62828")
            text = st.get("text", "")
            if running and st.get("ok"):
                text = (f"Connected to {st.get('server') or 'the server'}"
                        + (f" (v{st['version']})" if st.get("version") else "")
                        + (f" as {st['you']}" if st.get("you") else "") + ".\n"
                        f"Prices sent: {st.get('sent', 0):,} · received: {st.get('received', 0):,}"
                        f" · purged: {(st.get('purged') or {}).get('prices', 0):,}\n"
                        f"Megaphones sent: {st.get('mega_sent', 0):,} · received: {st.get('mega_received', 0):,}"
                        f" · purged: {(st.get('purged') or {}).get('megaphones', 0):,}\n"
                        f"Aliases sent: {st.get('alias_sent', 0):,} · received: {st.get('alias_received', 0):,}"
                        f" · purged: {(st.get('purged') or {}).get('aliases', 0):,}\n"
                        "(since the app started)")
            status.config(text=text)
            signed_in = bool(saved.get("token"))
            pw_note.config(text="(saved: no need to enter it)" if signed_in else "")
            go.config(text="Stop sharing" if running else "Connect", state="disabled" if busy["on"] else "normal")
            out_btn.config(state="normal" if signed_in else "disabled")
            chg_btn.config(state="normal" if signed_in else "disabled")
        win.refresh = refresh

        def run(task, then):
            """Talk to the server in the background, then `then(result, error)`."""
            busy["on"] = True
            refresh()

            def work():
                try:
                    res, err = task(), None
                except mfm_shared.SharedError as e:
                    res, err = None, str(e)
                except Exception as e:
                    res, err = None, f"{type(e).__name__}: {e}"
                self.events.put(("call", lambda: (busy.update(on=False), then(res, err))))
            threading.Thread(target=work, daemon=True).start()

        def connect():
            if self.shared is not None:
                self.cfg["shared_on"] = False
                mfm.save_config(self.cfg)
                self._stop_shared()
                return
            url, name, password = mfm_shared.normalize_url(addr.get()), user.get().strip(), pw.get()
            saved = mfm_shared.load_login()
            self.on_shared({"ok": False, "text": "Connecting…"})
            if password or not (saved.get("token") and saved.get("server") == url
                                and saved.get("username", "").lower() == name.lower()):
                task = lambda: mfm_shared.sign_in(url, name, password)
            else:
                task = lambda: mfm_shared.check_saved(url)

            def done(info, error):
                pw.set("")
                if error:
                    self.on_shared({"ok": False, "text": error})
                    return
                addr.set(url)
                self.cfg.update(shared_server=url, shared_on=True)
                self.cfg.pop("shared_key", None)
                mfm.save_config(self.cfg)
                self._start_shared()
                self._set_status(f"Shared Database: connected to {info.get('name') or url}.")
            run(task, done)

        def sign_out():
            saved = mfm_shared.load_login()
            self.cfg["shared_on"] = False
            mfm.save_config(self.cfg)
            self._stop_shared()

            def task():
                try:
                    mfm_shared.Server(saved.get("server", ""), saved.get("token")).logout()
                finally:
                    mfm_shared.forget_login()
            run(task, lambda res, err: self.on_shared({"ok": False, "text": "Signed out."}))

        def change_password():
            saved = mfm_shared.load_login()
            old = ask_value(win, "Change password", "Your current password:", "", secret=True)
            if not old:
                return
            new = ask_value(win, "Change password", "A new password (at least 8 characters):", "",
                            check=lambda v: len(v) >= 8, secret=True)
            if not new:
                return
            again = ask_value(win, "Change password", "The new password again:", "", secret=True)
            if again != new:
                messagebox.showerror("Change password", "The two new passwords aren't the same.", parent=win)
                return
            run(lambda: mfm_shared.Server(saved.get("server", ""), saved.get("token")).change_password(old, new),
                lambda res, err: messagebox.showerror("Change password", err, parent=win) if err else
                messagebox.showinfo("Change password", "Password changed. Your other devices need to sign in "
                                    "again.", parent=win))
        go.config(command=connect)
        out_btn.config(command=sign_out)
        chg_btn.config(command=change_password)
        e3.bind("<Return>", lambda e: connect())
        refresh()
        (e1 if not addr.get() else e2 if not user.get() else e3).focus_set()

    # ------------------------------------------------------------------ megaphones
    def _build_megaphones_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.insert(0, tab, text="  Megaphones  ")
        self.mega_tab = tab
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Keep megaphones for").pack(side="left")
        self.mega_hours = tk.IntVar(value=int(self.cfg.get("megaphone_hours", 72) or 72))
        sp = ttk.Spinbox(top, from_=1, to=720, width=5, textvariable=self.mega_hours,
                         command=self._mega_hours_changed)
        sp.pack(side="left", padx=4)
        sp.bind("<Return>", lambda e: self._mega_hours_changed())
        sp.bind("<FocusOut>", lambda e: self._mega_hours_changed())
        tip("Megaphones older than this are deleted.", sp)
        ttk.Label(top, text="hours. Newest first, read from the chat window while monitoring.").pack(side="left")
        self._alias_buttons(top).pack(side="left", padx=(12, 0))
        # filter: only megaphones whose message has every word typed here
        ttk.Label(top, text="Filter:").pack(side="left", padx=(16, 4))
        self.mega_filter = tk.StringVar()
        fe = ttk.Entry(top, textvariable=self.mega_filter, width=26)
        fe.pack(side="left")
        tip("Shows only megaphones whose message has every word you type here "
            "(any order, upper or lower case). Empty shows them all.", fe)
        fe.bind("<Escape>", lambda e: self.mega_filter.set(""))
        ttk.Button(top, text="Clear", width=6, command=lambda: self.mega_filter.set("")).pack(side="left", padx=(4, 0))
        self.mega_watched_only = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(top, text="Watched items", variable=self.mega_watched_only,
                             command=self._refilter_megaphones)
        cb.pack(side="left", padx=(12, 0))
        tip("Shows only megaphones with a watched item (at or under its max price when selling) "
            "or a watched word (Watched tab).", cb)
        self._mega_filter_job = None

        def filter_changed(*_):
            if self._mega_filter_job:
                self.root.after_cancel(self._mega_filter_job)
            self._mega_filter_job = self.root.after(250, self._refilter_megaphones)
        self.mega_filter.trace_add("write", filter_changed)
        self.mega_status = ttk.Label(top, foreground="#666")
        self.mega_status.pack(side="right")
        # right: what the OCR sees (the chat window, megaphones outlined)
        side = ttk.Frame(tab, padding=(10, 0, 0, 0), width=300)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        ttk.Label(side, text="What the OCR sees").pack(anchor="w")
        self.mega_preview = ttk.Label(side, relief="solid", anchor="center", text="no capture yet")
        self.mega_preview.pack(fill="x", pady=(2, 8), ipady=4)
        self.mega_seen = ttk.Label(side, justify="left", foreground="#555", wraplength=280)
        self.mega_seen.pack(anchor="w")
        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True)
        self.mega_trees = {}
        for kind, title in (("sell", "Selling"), ("buy", "Buying")):
            f = ttk.Frame(panes)
            ttk.Label(f, text=title, style="Bold.TLabel").pack(anchor="w", pady=(4, 2))
            t = make_tree(f, ("time", "item", "mprice", "market", "player", "channel", "message"))
            t.column("message", stretch=True)
            t.column("item", width=300, stretch=False)
            t.column("player", width=120)
            t.column("time", width=140)
            t.frame.pack(fill="both", expand=True)
            self._list_menu(t, [("Match to an item", lambda tr: self.match_megaphone_item(tr), True),
                                ("Not an item", lambda tr: self.megaphone_not_item(tr), False),
                                ("Show item history", lambda tr: self._megaphone_history_of(tr), True),
                                None,
                                ("Copy message", lambda tr: self._copy_megaphone(tr, "message"), False),
                                ("Copy player name", lambda tr: self._copy_megaphone(tr, "player"), False),
                                None,
                                ("Delete", lambda tr: self.delete_megaphones(tr), False)])
            t.bind("<Delete>", lambda e, tr=t: self.delete_megaphones(tr))
            t.bind("<Double-1>", lambda e, tr=t: self.open_history(tr) if tr.set(tr.focus(), "item")
                   and tr.focus() in getattr(tr, "matched", set()) else self._copy_megaphone(tr, "message"))
            panes.add(f, weight=1)
            self.mega_trees[kind] = t
        self._mega_status_text(None)
        self.refresh_megaphones()

    def _mega_status_text(self, found):
        running = bool(self.worker and self.worker.is_alive())
        manual = mfm.drawn_area(self.cfg, "chat_region")
        self.mega_status.config(text=(
            "Start monitoring to read megaphones." if not running else
            "Reading the chat area you drew." if manual and found else
            "Reading the chat window." if found else
            ("Chat window not found in " + ("the game's window" if mfm.GRAB_MODE[0] == "window" else "the screen")
             + ": open it in the game (or draw it in Settings → Reading → Window detection).") if found is False else
            "Looking for the chat window…"))

    def on_chat(self, p):
        self._mega_status_text(p.get("found"))
        if p.get("found") is False:
            mfm.app_log(f"chat window not found (reading {'the game window' if p.get('mode') == 'window' else 'the whole screen'}, "
                        f"{p.get('size')})")

    def on_megaphones(self, n):
        self.refresh_megaphones()
        self.refresh_megaphone_review()
        if mfm.watched_words(self.watch):
            self.refresh_alerts()             # a watched word's newest megaphone

    def on_chat_frame(self, p):
        from PIL import ImageDraw

        img = p["image"].convert("RGB").copy()
        d = ImageDraw.Draw(img)
        for box, color in p["marks"]:
            d.rectangle((box[0] + 1, box[1], box[2] - 1, box[3] - 1), outline="#e53935", width=2)
        img.thumbnail((280, 400))
        self.mega_preview_img = ImageTk.PhotoImage(img)
        self.mega_preview.config(image=self.mega_preview_img, text="")
        n = len(p["marks"])
        self.mega_seen.config(text=f"Megaphones on screen: {n}\nOutlined in red: the lines being read.")

    def _mega_hours_changed(self):
        try:
            hours = max(1, min(720, int(self.mega_hours.get())))
        except (tk.TclError, ValueError):
            return
        if hours != self.cfg.get("megaphone_hours"):
            more = hours > int(self.cfg.get("megaphone_hours", 72) or 72)
            self.cfg["megaphone_hours"] = hours
            mfm.save_config(self.cfg)
            self._mega_purged = -1e9          # purge now for the new hours
            if more and self.cfg.get("shared_server"):
                # get the older megaphones from the Shared Database too
                import mfm_shared
                mfm_shared.reset_megaphones(self.con, self.cfg["shared_server"])
                if self.shared:
                    self.shared.wake.set()
        self.refresh_megaphones()

    REVIEW_SHOWN = 500        # Review → Megaphones shows the most-seen phrases, at most this many

    def refresh_megaphones(self, delay: int = 400):
        """Bring the Megaphones tab and Review → Megaphones up to date. Asks are
        gathered (several megaphones arriving at once make one refresh), the data is
        read on a background thread, and only the tables on screen are redrawn: with
        thousands of megaphones from the Shared Database this kept the window busy."""
        self._mega_dirty = True
        if getattr(self, "_mega_job", None) is None:
            self._mega_job = self.root.after(delay, self._mega_start)

    refresh_megaphone_review = refresh_megaphones

    def _mega_visible(self) -> tuple[bool, bool]:
        try:
            showing = self.nb.select()
            mega = hasattr(self, "mega_tab") and showing == str(self.mega_tab)
            review = (hasattr(self, "review_nb") and showing == str(self.review_tab)
                      and self.review_nb.select() == str(self.review_mega_tab))
            return mega, review
        except tk.TclError:
            return False, False

    def _mega_start(self):
        self._mega_job = None
        if not hasattr(self, "mega_trees") or not getattr(self, "_mega_dirty", False):
            return
        if getattr(self, "_mega_running", False):
            return                                # it runs again when this one is done
        mega, review = self._mega_visible()
        self._mega_dirty = False
        self._mega_running = True
        hours = max(1, int(self.cfg.get("megaphone_hours", 72) or 72))
        purge = time.monotonic() - getattr(self, "_mega_purged", -1e9) > 60
        cache = self.__dict__.setdefault("_mkt_cache", {})
        asked = self.__dict__.setdefault("_mega_icon_asked", set())
        cfg = dict(self.cfg)

        def work():
            data = {"mega": mega, "review": review}
            con = mfm.db()
            try:
                if purge:
                    try:
                        mfm.purge_megaphones(con, hours)
                        data["purged"] = True
                    except sqlite3.OperationalError:
                        con.rollback()
                data["count"] = mfm.count_unmatched_offers(con, hours)
                if mega:
                    rows = con.execute(
                        "SELECT m.id, m.ts, m.player, m.channel, m.message, m.kind, m.color, o.idx, o.side, o.item, "
                        "o.phrase, o.price, o.note FROM megaphones m LEFT JOIN megaphone_offers o "
                        "ON o.megaphone_id = m.id WHERE m.ts >= ? AND m.kind IS NOT NULL "
                        "ORDER BY m.ts DESC, m.id DESC, o.idx", (mfm.utc_ago(hours=hours),)).fetchall()
                    now = time.monotonic()
                    markets = {}
                    for item in {r[9] for r in rows if r[9]}:
                        hit = cache.get(item)
                        if hit is None or now - hit[1] > 120:      # market prices: once every 2 minutes
                            hit = cache[item] = (mfm.market_rate(con, item, cfg)[0], now)
                        markets[item] = hit[0]
                    data["rows"], data["markets"] = rows, markets
                    data["missing"] = [i for i in markets if i not in asked and mfm.load_icon(con, i) is None]
                if review:
                    data["review_rows"] = mfm.unmatched_offers(con, hours, limit=self.REVIEW_SHOWN)
            except Exception as e:
                data["error"] = f"{type(e).__name__}: {e}"
            finally:
                con.close()
            self.events.put(("call", lambda: self._mega_apply(data)))
        threading.Thread(target=work, daemon=True).start()

    def _mega_apply(self, data):
        self._mega_running = False
        if data.get("purged"):
            self._mega_purged = time.monotonic()
        if "error" in data:
            self._set_status("Megaphones: " + data["error"])
        with self._ui_busy():
            if "rows" in data:
                self._fill_megaphones(data["rows"], data["markets"])
            if "review_rows" in data:
                self._fill_megaphone_review(data["review_rows"], data["count"])
        self._mega_review_n = data.get("count", getattr(self, "_mega_review_n", 0))
        if hasattr(self, "review_tab"):
            self._review_title()
        missing = data.get("missing") or []
        if missing:
            self._mega_icon_asked.update(missing)
            self.fetch_icons_async(missing)
        # what isn't on screen now is drawn when its tab is opened
        mega, review = self._mega_visible()
        if (mega and not data["mega"]) or (review and not data["review"]):
            self._mega_dirty = True
        if self._mega_dirty:
            self.refresh_megaphones(delay=1000)

    def _mega_tab_shown(self):
        mega, review = self._mega_visible()
        if mega or review:
            self._mega_dirty = True
            self.refresh_megaphones(delay=10)

    def _ui_busy(self):
        """While redrawing a big table: background threads pause (see mfm.ui_pause)."""
        import contextlib

        @contextlib.contextmanager
        def cm():
            mfm.UI_FREE.clear()
            try:
                yield
            finally:
                mfm.UI_FREE.set()
        return cm()

    def _refilter_megaphones(self):
        self._mega_filter_job = None
        last = getattr(self, "_mega_last", None)
        if last:
            with self._ui_busy():
                self._fill_megaphones(*last)

    def _fill_megaphones(self, rows, markets):
        """One row per offer in a megaphone (an item it sells or buys), with the item's
        icon, name and price once it's matched to an item. With words in the Filter
        box, only megaphones whose message has all of them."""
        self._mega_last = (rows, markets)
        words = [w.lower() for w in (self.mega_filter.get() if hasattr(self, "mega_filter") else "").split()]
        if words:
            rows = [r for r in rows if all(w in (r[4] or "").lower() for w in words)]
        words_watched = mfm.watched_words(self.watch)
        watched_only = hasattr(self, "mega_watched_only") and self.mega_watched_only.get()
        for kind, t in self.mega_trees.items():
            restore = self._keep_selection(t, lambda tr, k: k)
            t.delete(*t.get_children())
            t.matched = set()
            t.offer_of = {}
            t.info = {}                           # row -> (message, player)
            seen_mega = set()
            last_mid = None
            lines = {}                            # how many lines each megaphone has here
            watched = set()                       # megaphones with a watched item: highlighted
            for r in rows:
                if r[7] is not None and r[8] == kind:
                    lines[r[0]] = lines.get(r[0], 0) + 1
                    if r[9] and self._mega_watched(r[9], r[11], r[8]):
                        watched.add(r[0])
                if words_watched and r[0] not in watched and mfm.message_has_word(r[4], words_watched):
                    watched.add(r[0])                 # ... or one that says a watched word
            for mid, ts, player, ch, msg, k, color, idx, side, item, phrase, price, note in rows:
                if watched_only and mid not in watched:
                    continue
                if idx is None:                       # no offers read from it: show it by its kind
                    if k not in (kind, "both"):
                        continue
                elif side != kind:
                    continue
                iid = f"{mid}:{idx if idx is not None else -1}"
                if iid in seen_mega:
                    continue
                seen_mega.add(iid)
                tags = ()
                if color in mfm.MEGAPHONE_COLORS:     # a colour name instead of #rrggbb
                    color = "#%02x%02x%02x" % mfm.MEGAPHONE_COLORS[color]
                if color and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    tag = "mc" + color[1:].lower()
                    if tag not in getattr(t, "color_tags", set()):
                        r_, g_, b_ = (int(color[i:i + 2], 16) for i in (1, 3, 5))
                        dark = 0.299 * r_ + 0.587 * g_ + 0.114 * b_ < 140
                        t.tag_configure(tag, background=color, foreground="white" if dark else "black")
                        t.color_tags = getattr(t, "color_tags", set()) | {tag}
                    tags = (tag,)
                if mid in watched:
                    tags = ("watch",) + tags
                mkt = None
                if item:
                    mkt = markets.get(item)
                    t.matched.add(iid)
                if item:
                    price_txt = mfm.fmt(price) if price else ("offer" if note and "offer" in note else "")
                    if price and note and "c/o" in note:
                        price_txt = "c/o " + price_txt
                    shown = item
                elif lines.get(mid, 0) > 1:           # one of several: what it says, and its price
                    price_txt, shown = (mfm.fmt(price) if price else ""), f"\"{phrase}\""
                else:                                 # not matched yet: the message as it was read
                    price_txt, shown = "", msg
                # several items in one megaphone: one line each; the time, player, channel
                # and message only on its first line
                first = mid != last_mid
                last_mid = mid
                t.insert("", "end", iid=iid, tags=tags, image=self.icon_for(item) if item else "",
                         values=(mfm.local_time(ts) if first else "", shown, price_txt,
                                 mfm.fmt(mkt) if mkt else "", player if first else "",
                                 (ch or "") if first else "",
                                 msg if first and (item or lines.get(mid, 0) > 1) else ""))
                t.offer_of[iid] = (mid, idx, phrase)
                t.info[iid] = (msg, player)
            restore()

    def _mega_watched(self, item, price, side) -> bool:
        """Is this megaphone offer for a watched item? Selling at or under your price
        cap (or with no price given); buying, at any price."""
        for w in self.watch:
            if w.get("item") == item:
                cap = w.get("max_each")
                return side == "buy" or not price or cap in (None, "") or price <= float(cap)
        return False

    def _offer_phrases(self, tree) -> list[str]:
        return [tree.offer_of[k][2] for k in tree.selection() if tree.offer_of.get(k, (0, None, None))[2]]

    def _aliases_changed(self, note: str = ""):
        """Aliases were taught or changed: read every megaphone again (in the
        background), then show the result."""
        if note:
            self._set_status(note)

        def work():
            con = mfm.db()
            try:
                mfm.update_offers(con)
            finally:
                con.close()
            self.events.put(("megaphones", 0))
            self.events.put(("call", self.refresh_megaphone_review))
        threading.Thread(target=work, daemon=True).start()
        if self.shared:
            self.shared.wake.set()

    def match_megaphone_item(self, tree):
        phrases = self._offer_phrases(tree)
        if not phrases:
            return
        name = self._choose_item(self.root, f"Which item is \"{phrases[0]}\"?", phrases[0].title())
        if name:
            mfm.set_alias(self.con, phrases[0], name)
            self._aliases_changed(f"\"{phrases[0]}\" is now an alias for {name}.")

    def megaphone_not_item(self, tree):
        phrases = self._offer_phrases(tree)
        for ph in phrases:
            mfm.set_alias(self.con, ph, None)
        if phrases:
            self._aliases_changed(f"{mfm.plural(len(phrases), 'phrase')} marked as not an item.")

    def _megaphone_history_of(self, tree):
        if any(k in getattr(tree, "matched", set()) for k in tree.selection()):
            self.open_history(tree)
        else:
            self._set_status("This megaphone isn't matched to an item yet: match it first.")

    def _copy_megaphone(self, tree, col):
        info = getattr(tree, "info", {})
        vals = list(dict.fromkeys(info[k][0 if col == "message" else 1] if k in info else tree.set(k, col)
                                  for k in tree.selection()))
        if vals:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(vals))
            self._set_status(f"Copied {mfm.plural(len(vals), 'line')}.")

    def delete_megaphones(self, tree):
        ids = sorted({int(str(k).split(":")[0]) for k in tree.selection()})
        if not ids:
            return
        mfm.delete_megaphones(self.con, ids)
        self.refresh_megaphones()

    # ------------------------------------------------------------------ my inventory
    INV_EDITABLE = ("qty", "list_price", "notes")

    def _build_inventory_tab(self):
        # My Inventory sits at the far right of the tab row: a one-tab notebook placed
        # there, whose page is shown over the main notebook's page
        style = ttk.Style()
        normal_bg = style.lookup("TNotebook.Tab", "background") or "#bab5ab"
        for name in ("Dim.TNotebook", "DimInv.TNotebook"):
            style.configure(name, **{k: style.lookup("TNotebook", k) for k in ("background",)
                                     if style.lookup("TNotebook", k)})
            style.map(name + ".Tab", background=[("selected", normal_bg), ("!selected", normal_bg)],
                      lightcolor=[("selected", "#cfcdc8"), ("!selected", "#cfcdc8")],
                      expand=[("selected", (0, 0, 0, 0))])
        # the lone My Inventory tab, when not shown: the height of an unselected tab
        style.map("DimInv.TNotebook.Tab", padding=[("selected", style.lookup("TNotebook.Tab", "padding") or 2)])
        self.inv_tabbar = ttk.Notebook(self.root, style="DimInv.TNotebook")
        self.inv_tabbar.add(ttk.Frame(self.inv_tabbar, height=1), text="  My Inventory  ")
        self.inv_tabbar.bind("<Button-1>", lambda e: self.show_inventory())
        self.inv_underline = tk.Canvas(self.root, height=2, highlightthickness=0, borderwidth=0)
        self.inv_tabtop = tk.Canvas(self.root, height=2, highlightthickness=0, borderwidth=0)
        self.inv_tabedge = tk.Frame(self.root, width=1, height=2, borderwidth=0)
        self.inv_tabcorner = tk.Frame(self.root, width=1, height=1, borderwidth=0)
        self.root.after(50, self._place_inv_tabbar)
        self.nb.bind("<Configure>", lambda e: self._place_inv_tabbar_later(), add="+")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.hide_inventory(), add="+")
        self.nb.bind("<ButtonRelease-1>", lambda e: self.hide_inventory()
                     if self.nb.identify(e.x, e.y) else None, add="+")
        self.inv_con = mfm.inventory_db()

        tab = ttk.Frame(self.root, padding=6)
        self.inv_page = tab
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        b = ttk.Button(top, text="Auto Detect (beta)", command=self.open_inventory_scan)
        b.pack(side="left")
        tip("Reads your Item Inventory window in the game and matches the items.", b)
        b = ttk.Button(top, text="Add by name", command=self.add_inventory_by_name)
        b.pack(side="left", padx=(6, 0))
        tip("Add an item by typing its name.", b)
        self.inv_sold_btn = ttk.Button(top, text="Sold", command=self.toggle_inventory_sold, width=8)
        self.inv_sold_btn.pack(side="left", padx=(6, 0))
        tip("Mark the selected items as sold (or, for sold items, unsell them).", self.inv_sold_btn)
        ttk.Button(top, text="Remove selected", command=self.remove_inventory).pack(side="left", padx=6)
        b = ttk.Button(top, text="Clear My Inventory", command=self.clear_inventory)
        b.pack(side="right")
        tip("Delete everything in My Inventory (asks first, keeps a backup).", b)
        b = ttk.Button(top, text="Export", command=self.export_inventory)
        b.pack(side="right", padx=6)
        tip("Save My Inventory as a CSV file.", b)
        b = ttk.Button(top, text="Import", command=self.import_inventory)
        b.pack(side="right")
        tip("Load a My Inventory CSV file.", b)

        t = make_tree(tab, ("item", "qty", "list_price", "notes", "market", "range", "points", "sold24",
                            "last3", "status"))
        self.inv_tree = t
        t.column("status", width=130)
        t.frame.pack(fill="both", expand=True)
        t.bind("<<TreeviewSelect>>", lambda e: self._update_sold_button(), add="+")
        t.bind("<Delete>", lambda e: self.remove_inventory())
        t.bind("<BackSpace>", lambda e: self.remove_inventory())
        self._inline_editing(t, self.INV_EDITABLE, self._inv_edit_done)
        t.bind("<Double-1>", lambda e: (self._cancel_inline(t), self.open_history(t)), add="+")
        menu = tk.Menu(t, tearoff=False)

        def popup(e):
            row = t.identify_row(e.y)
            if not row:
                return
            if row not in t.selection():
                t.selection_set(row)
            sel = t.selection()
            n = len(sel)
            sold = [k for k in sel if "sold" in t.item(k, "tags")]
            menu.delete(0, "end")
            menu.add_command(label="Show history", state="normal" if n == 1 else "disabled",
                             command=lambda: self.open_history(t))
            menu.add_separator()
            for col, label in (("notes", "Edit note"), ("qty", "Change quantity"),
                               ("list_price", "Set list price")):
                menu.add_command(label=label if n == 1 else f"{label} ({n} items)",
                                 command=lambda c=col: self._inv_edit_dialog(c))
            menu.add_separator()
            if sold and len(sold) == n:
                menu.add_command(label="Unmark as sold", command=self.unsell_inventory)
            else:
                menu.add_command(label="Mark as sold", command=self.sell_inventory)
            menu.add_separator()
            menu.add_command(label="Remove" if n == 1 else f"Remove ({n} items)", command=self.remove_inventory)
            menu.tk_popup(e.x_root, e.y_root)
        bind_right_click(t, popup)

        worth = ttk.LabelFrame(tab, text=" Net worth  ·  3% meso tax included ", padding=(10, 4, 10, 6))
        worth.pack(fill="x", pady=(6, 0))
        left, right = ttk.Frame(worth), ttk.Frame(worth)
        left.pack(side="left", anchor="n")
        right.pack(side="left", anchor="n", padx=(60, 0))
        self.worth_range = ttk.Label(left, style="Bold.TLabel")
        self.worth_market = ttk.Label(left, style="Bold.TLabel")
        self.worth_sales = ttk.Label(left, style="Bold.TLabel")
        self.worth_listed = ttk.Label(right, style="Bold.TLabel")
        self.worth_profit = ttk.Label(right, style="Bold.TLabel")
        for w in (self.worth_range, self.worth_market, self.worth_sales):
            w.pack(anchor="w")
        for w in (self.worth_listed, self.worth_profit):
            w.pack(anchor="w")
        self.worth_note = ttk.Label(worth, foreground="#666")
        self.worth_note.pack(side="right", anchor="s")
        tip("The lowest and highest prices seen for the items you own, added up, less 3% tax.",
            self.worth_range)
        tip("The market price of the items you own, added up, less 3% tax.", self.worth_market)
        tip("The average of each item's last 3 sales, added up, less 3% tax.", self.worth_sales)
        tip("The list prices of the items you own, added up, less 3% tax.", self.worth_listed)
        tip("What the items you marked as sold sold for, added up, less 3% tax.", self.worth_profit)
        self.refresh_inventory()

    @staticmethod
    def _page_edge(nb) -> tuple[int, int] | None:
        """(top of a notebook's page border, top of its page), in the notebook."""
        try:
            page = nb.nametowidget(nb.select())
        except Exception:
            return None
        y, bw = page.winfo_y(), page.winfo_x()
        return (y - bw, y) if y > 0 else None

    def _place_inv_tabbar_later(self):
        if not getattr(self, "_inv_tabbar_job", None):
            self._inv_tabbar_job = self.root.after(30, self._place_inv_tabbar)

    def _place_inv_tabbar(self):
        """Put the My Inventory tab at the right end of the tab row, cut off just above
        its own page border, so it looks like one of the notebook's tabs: selected, it
        runs into the page; not selected, it sits on the page's top border."""
        self._inv_tabbar_job = None
        tb, nb = self.inv_tabbar, self.nb
        if not tb.winfo_ismapped():
            tb.place(in_=nb, relx=1.0, x=0, y=0, anchor="ne")
            nb.update_idletasks()
        tb.place_configure(height="", width="")
        tb.update_idletasks()
        own, main = self._page_edge(tb), self._page_edge(nb)
        if not own or not main:
            return
        shown = getattr(self, "inventory_shown", False)
        # the tab without the page border below it; a selected tab covers that border
        cut = own[1] if shown else own[0]
        # line the tab's right edge up with the notebook's right edge
        last = None
        for x in range(tb.winfo_width() - 1, -1, -1):
            try:
                if tb.index(f"@{x},{cut // 2}") == 0:
                    last = x
                    break
            except tk.TclError:
                pass
        width = last + 1 if last is not None else ""
        tb.place_configure(y=(main[1] if shown else main[0]) - cut, height=cut, width=width)
        # while My Inventory is shown, make the main notebook's selected tab look like
        # the other, unselected ones: lower its top and draw the page border under it
        line, top, edge = self.inv_underline, self.inv_tabtop, self.inv_tabedge
        style = ttk.Style()
        if style.theme_use() != "clam":
            for w in (line, top, edge):
                w.place_forget()
            return
        # keep the right edge unbroken where the My Inventory tab meets the page: the
        # page's rounded top-right corner would leave a notch there
        edge.configure(background=style.lookup("TNotebook", "bordercolor") or "#9e9a91")
        edge.place(in_=nb, relx=1.0, x=-1, y=main[0], width=1,
                   height=main[1] - main[0] if shown else 1)
        tk.Misc.lift(edge)
        corner = self.inv_tabcorner
        if not shown:
            for w in (line, top, corner):
                w.place_forget()
            return
        # a selected tab's left border stops at the page's top border line
        if last is not None and main[1] - main[0] > 1:
            corner.configure(background=style.lookup("TNotebook", "lightcolor") or "#eeebe7")
            corner.place(in_=nb, relx=1.0, x=-(last + 1), y=main[0] + 1, width=1,
                         height=main[1] - main[0] - 1)
            tk.Misc.lift(corner)
        cur, xs = nb.index("current"), []
        for x in range(0, nb.winfo_width()):
            try:
                if nb.index(f"@{x},{main[0] // 2}") == cur:
                    xs.append(x)
            except tk.TclError:
                if xs:
                    break
        if not xs:
            line.place_forget()
            top.place_forget()
            return
        border = style.lookup("TNotebook", "bordercolor") or "#9e9a91"
        light = style.lookup("TNotebook", "lightcolor") or "#eeebe7"
        dim_light = style.lookup("TNotebook.Tab", "lightcolor") or "#cfcdc8"
        bg = style.lookup("TNotebook", "background") or "#dcdad5"
        x0, x1 = xs[0], xs[-1]
        # the page border, from just inside the notebook's left edge
        lx = max(x0, 1)
        w, bw = x1 - lx + 2, main[1] - main[0]
        line.delete("all")
        for i in range(bw):
            line.create_line(0, i, w, i, fill=border if i == 0 else light)
        line.place(in_=nb, x=lx, y=main[0], width=w, height=bw)
        tk.Misc.lift(line)
        # a selected tab is taller by the difference in top padding
        try:
            pads = [str(style.lookup("TNotebook.Tab", "padding")).split(),
                    str(dict(style.map("TNotebook.Tab")).get("padding", [("", "")])[0][-1]).split()]
            dy = round(nb.winfo_fpixels(pads[1][1]) - nb.winfo_fpixels(pads[0][1]))
        except Exception:
            dy = 2
        if dy <= 0:
            top.place_forget()
            return
        w = x1 - x0 + 1
        top.delete("all")
        top.create_rectangle(0, 0, w + 1, dy, fill=bg, width=0)   # + the tab's right border
        top.create_line(1, dy, w - 1, dy, fill=border)
        top.create_line(1, dy + 1, w - 1, dy + 1, fill=dim_light)
        top.create_line(0, dy + 1, 0, dy + 2, fill=border)
        top.create_line(w - 1, dy + 1, w - 1, dy + 2, fill=border)
        top.place(in_=nb, x=x0, y=0, width=w + 1, height=dy + 2)
        tk.Misc.lift(top)

    def show_inventory(self):
        """Bring My Inventory to the front (its tab is at the far right of the tab row)."""
        page = self.nb.nametowidget(self.nb.select())
        self.inv_page.place(in_=page, x=0, y=0, relwidth=1, relheight=1)
        self.inv_page.lift()
        self.inv_tabbar.lift()
        self.nb.configure(style="Dim.TNotebook")
        self.inv_tabbar.configure(style="TNotebook")
        self.inventory_shown = True
        self._place_inv_tabbar()
        self._place_inv_tabbar_later()   # again, once the new styles are laid out
        self.refresh_inventory()

    def hide_inventory(self):
        if not getattr(self, "inventory_shown", False):
            return
        self.inv_page.place_forget()
        self.nb.configure(style="TNotebook")
        self.inv_tabbar.configure(style="DimInv.TNotebook")
        self.inventory_shown = False
        self._place_inv_tabbar()
        self._place_inv_tabbar_later()

    # -- inline editing: click a cell of a highlighted row to type in it
    def _inline_editing(self, tree, cols, on_done):
        state = {"before": (), "job": None}

        def press(e):
            state["before"] = tree.selection()

        def release(e):
            row, col = tree.identify_row(e.y), self._clicked_column(tree, e)
            if not row or col not in cols or row not in state["before"] or len(tree.selection()) != 1:
                return
            if e.state & 0x000C:          # Ctrl / Alt / Shift: selecting, not editing
                return
            state["job"] = tree.after(320, lambda: self._inline_edit(tree, row, col, on_done))
        tree.bind("<ButtonPress-1>", press, add="+")
        tree.bind("<ButtonRelease-1>", release, add="+")
        tree.inline_state = state

    def _cancel_inline(self, tree):
        st = getattr(tree, "inline_state", None)
        if st and st["job"]:
            tree.after_cancel(st["job"])
            st["job"] = None

    def _inline_edit(self, tree, row, col, on_done):
        tree.inline_state["job"] = None
        if not tree.exists(row):
            return
        box = tree.bbox(row, col)
        if not box:
            return
        x, y, w, h = box
        var = tk.StringVar(value=tree.set(row, col))
        e = ttk.Entry(tree, textvariable=var)
        e.place(x=x, y=y, width=max(w, 80), height=h)
        e.focus_set()
        e.select_range(0, "end")
        done = {"v": False}

        def finish(save):
            if done["v"]:
                return
            done["v"] = True
            val = var.get()
            e.destroy()
            if save:
                on_done(tree, row, col, val)
        e.bind("<Return>", lambda ev: finish(True))
        e.bind("<KP_Enter>", lambda ev: finish(True))
        e.bind("<Escape>", lambda ev: finish(False))
        e.bind("<FocusOut>", lambda ev: finish(True))

    def _inv_value(self, col, raw):
        """A typed value for an inventory column, or raise ValueError."""
        raw = (raw or "").strip()
        if col == "qty":
            if not raw.isdigit() or int(raw) < 1:
                raise ValueError("Quantity: a whole number, 1 or more.")
            return int(raw)
        if col == "list_price":
            if not raw or raw == "-":
                return None
            v = mfm.parse_price(raw)
            if not v:
                raise ValueError("List price: a number like 45000, 45,000 or 45k.")
            return v
        return raw or None

    def _inv_edit_done(self, tree, row, col, raw):
        try:
            val = self._inv_value(col, raw)
        except ValueError as e:
            self._set_status(str(e))
            self.root.bell()
            return
        mfm.update_inventory(self.inv_con, [int(row)], **{col: val})
        self.refresh_inventory()

    def _inv_edit_dialog(self, col):
        sel = self.inv_tree.selection()
        if not sel:
            return
        cur = self.inv_tree.set(sel[0], col) if len(sel) == 1 else ""
        label = {"qty": "How many do you have?", "list_price": "List price (each; e.g. 45000 or 45k, "
                 "empty = none):", "notes": "Note (e.g. +5 ATK, 7 slots):"}[col]
        raw = ask_value(self.root, {"qty": "Quantity", "list_price": "List price", "notes": "Note"}[col],
                        label, "" if cur == "-" else cur)
        if raw is None:
            return
        try:
            val = self._inv_value(col, raw)
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e))
            return
        mfm.update_inventory(self.inv_con, [int(k) for k in sel], **{col: val})
        self.refresh_inventory()

    def refresh_inventory(self):
        if not hasattr(self, "inv_tree"):
            return
        t = self.inv_tree
        restore = self._keep_selection(t, lambda tr, k: k)
        t.delete(*t.get_children())
        lo = hi = mkt = sales = listed = profit = 0
        missing = owned_n = 0
        # The stats are worked out by the tables thread (request_tables) and come with
        # the Deals and Items data: eight queries an item, done here on a big database,
        # froze the window for a second every refresh (twenty while the item names
        # were being matched for Review at launch). Until the first data arrives the
        # rows show "-"; after that an item not in it yet (just added) is done here, once.
        data = getattr(self, "_tables_data", None)
        stats = dict((data or {}).get("inventory") or {})
        pending = {"market": None, "low": None, "high": None, "points": "", "sold24": "",
                   "last3": None, "last3_n": 0}
        rows = mfm.load_inventory(self.inv_con)
        for r in rows:
            item, qty = r["item"], int(r["qty"] or 1)
            st = stats.get(item) or stats.setdefault(
                item, mfm.inventory_stats(self.con, item, self.cfg) if data is not None else pending)
            lp = r["list_price"]
            if r["sold"]:
                profit += (lp or 0) * qty
            else:
                owned_n += qty
                if st["low"] is not None:
                    lo += st["low"] * qty
                    hi += st["high"] * qty
                if st["market"]:
                    mkt += st["market"] * qty
                if st["last3"]:
                    sales += st["last3"] * qty
                if lp:
                    listed += lp * qty
                if st["low"] is None and not st["market"] and st is not pending:
                    missing += 1
            rng = (f"{mfm.fmt(st['low'])} – {mfm.fmt(st['high'])}" if st["low"] is not None
                   and st["low"] != st["high"] else mfm.fmt(st["low"]))
            status = f"sold {mfm.local_time(r['sold_at'])[:10]}" if r["sold"] and r["sold_at"] else \
                ("sold" if r["sold"] else "")
            t.insert("", "end", iid=str(r["id"]), image=self.icon_for(item), tags=("sold",) if r["sold"] else (),
                     values=(item, qty, mfm.fmt(lp), r["notes"] or "", mfm.fmt(st["market"]), rng, st["points"],
                             st["sold24"], mfm.fmt(st["last3"]) + (f" ({st['last3_n']})" if st["last3"]
                                                                    and st["last3_n"] < 3 else ""), status))
        restore()
        keep = 1 - mfm.MESO_TAX
        self.worth_range.config(text=f"Possible range:  {mfm.fmt(lo * keep)} – {mfm.fmt(hi * keep)}")
        self.worth_market.config(text=f"Estimated value (market prices):  {mfm.fmt(mkt * keep)}")
        self.worth_sales.config(text=f"Estimated value (last 3 sales):  {mfm.fmt(sales * keep)}")
        self.worth_listed.config(text=f"Listed items value:  {mfm.fmt(listed * keep)}")
        self.worth_profit.config(text=f"Realized profit:  {mfm.fmt(profit * keep)}")
        self.worth_note.config(text=f"{owned_n} owned" + (f"  ·  {missing} without price data" if missing else ""))
        self._update_sold_button()

    def _update_sold_button(self):
        t = self.inv_tree
        sel = t.selection()
        all_sold = bool(sel) and all("sold" in t.item(k, "tags") for k in sel)
        self.inv_sold_btn.config(text="Unsell" if all_sold else "Sold")

    def toggle_inventory_sold(self):
        if self.inv_sold_btn.cget("text") == "Unsell":
            self.unsell_inventory()
        else:
            self.sell_inventory()

    def sell_inventory(self):
        """Mark the selected items as sold: each sale goes into the price database."""
        t = self.inv_tree
        ids = [k for k in t.selection() if "sold" not in t.item(k, "tags")]
        if not ids:
            return
        n = None
        if len(ids) == 1:
            qty = int(t.set(ids[0], "qty") or 1)
            if qty > 1:
                raw = ask_value(self.root, "Mark as sold", f"How many of '{t.set(ids[0], 'item')}' were sold? "
                                f"(1–{qty})", str(qty), check=lambda s: s.strip().isdigit() and 1 <= int(s) <= qty)
                if raw is None:
                    return
                n = int(raw)
        done = 0
        for k in ids:
            item, lp = t.set(k, "item"), mfm.parse_price(t.set(k, "list_price")) if t.set(k, "list_price") not in ("", "-") else None
            price = lp
            if not price:
                mkt = t.set(k, "market")
                raw = ask_value(self.root, "Sold for", f"What did '{item}' sell for (each)?",
                                "" if mkt == "-" else mkt, check=lambda s: (mfm.parse_price(s) or 0) > 0)
                if raw is None:
                    continue
                price = mfm.parse_price(raw)
            mfm.mark_sold(self.inv_con, self.con, int(k), n if len(ids) == 1 else None, price)
            done += 1
        self.refresh_inventory()
        self.refresh_tables()
        if done:
            self._set_status(f"Marked {mfm.plural(done, 'item')} as sold.")

    def unsell_inventory(self):
        t = self.inv_tree
        ids = [k for k in t.selection() if "sold" in t.item(k, "tags")]
        if not ids:
            return
        removed = sum(mfm.unmark_sold(self.inv_con, self.con, int(k)) for k in ids)
        self.refresh_inventory()
        self.refresh_tables()
        self._set_status(f"Unmarked {mfm.plural(len(ids), 'item')} as sold"
                         + (f"; {mfm.plural(removed, 'sale')} removed from the prices." if removed else "."))

    def add_inventory_by_name(self):
        name = self._choose_item(self.root, "Which item?", "")
        if not name:
            return
        raw = ask_value(self.root, "Quantity", f"How many {name}?", "1",
                        check=lambda s: s.strip().isdigit() and int(s) > 0)
        if raw is None:
            return
        self.add_to_inventory([(name, int(raw))])

    def add_to_inventory(self, items: list[tuple[str, int]]):
        for item, qty in items:
            mfm.add_inventory(self.inv_con, item, qty)
        self.refresh_inventory()
        self._set_status(f"Added {mfm.plural(len(items), 'item')} to My Inventory.")

    def remove_inventory(self):
        ids = [int(k) for k in self.inv_tree.selection()]
        if not ids:
            return
        mfm.delete_inventory(self.inv_con, ids)
        self.refresh_inventory()

    def clear_inventory(self):
        n = len(mfm.load_inventory(self.inv_con))
        if not n:
            return
        if not messagebox.askyesno("Clear My Inventory", f"Delete all {mfm.plural(n, 'row')} in My Inventory?\n\n"
                                   "A backup is saved in the data folder's backups folder.",
                                   icon="warning", default="no"):
            return
        backup = mfm.clear_inventory(self.inv_con)
        self.refresh_inventory()
        self._set_status("My Inventory cleared." + (f" Backup: {backup}" if backup else ""))

    def export_inventory(self):
        path = filedialog.asksaveasfilename(title="Export My Inventory", defaultextension=".csv",
                                            initialfile=mfm.stamped("mfm-tracker my inventory.csv"),
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            n = mfm.export_inventory(self.inv_con, path)
        except Exception as e:
            self.report_error("Exporting My Inventory failed", f"{path}\n\n{e}")
            return
        self._set_status(f"Exported {mfm.plural(n, 'row')} to {path}")

    def import_inventory(self):
        path = filedialog.askopenfilename(title="Import My Inventory", filetypes=[("CSV", "*.csv"), ("All files", "*")])
        if not path:
            return
        replace = False
        if mfm.load_inventory(self.inv_con):
            ans = messagebox.askyesnocancel("Import My Inventory", "Replace your current list?\n\n"
                                            "Yes = replace it, No = add to it.")
            if ans is None:
                return
            replace = ans
        try:
            n = mfm.import_inventory(self.inv_con, path, replace=replace)
        except Exception as e:
            messagebox.showerror("Import failed", str(e))
            return
        self.refresh_inventory()
        self._set_status(f"Imported {mfm.plural(n, 'row')} into My Inventory.")

    # ------------------------------------------------------------------ add items window
    def open_inventory_scan(self):
        w = getattr(self, "inv_scan", None)
        if w is not None and w.winfo_exists():
            w.deiconify()
            w.lift()
            return
        if not mfm.mac_screen_access(ask=True):
            messagebox.showwarning("Screen Recording permission needed",
                                   "Allow Free Market Tracker in System Settings > Privacy & Security > "
                                   "Screen Recording, then quit and reopen the app.")
            return
        win = self.window()
        self.inv_scan = win
        win.title("Auto Detect (beta)")
        win.geometry("920x660")
        win.minsize(760, 560)
        top = ttk.Frame(win, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Open your Item Inventory in the game. Its items show here; select the "
                  "ones you want and click Add.",
                  wraplength=560, justify="left").pack(side="left")
        ttk.Label(top, text="Not found? Settings → Reading → Window detection.", foreground="#666").pack(side="right")
        body = ttk.Frame(win, padding=(12, 4, 12, 4))
        body.pack(fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(10, 0))
        ttk.Label(right, text="What's being read").pack(anchor="w")
        win.preview = ttk.Label(right, text="—", relief="solid", anchor="center", width=24)
        win.preview.pack(anchor="n", pady=(2, 6))
        win.status = ttk.Label(right, foreground="#555", wraplength=220, justify="left",
                               text="Loading item icons…")
        win.status.pack(anchor="w")
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, foreground="#555", wraplength=560, justify="left", text=(
            "Icons that couldn't be matched are highlighted. Enter = accept the suggestion · "
            "M = pick an item · Delete = remove. Your choice is remembered for next time."
            )).pack(anchor="w", pady=(0, 4))
        tree = make_tree(left, ("item", "qty", "tab", "match"))
        tree.column("item", width=240)
        tree.tag_configure("review", background="#fff1dc")
        tree.frame.pack(fill="both", expand=True)
        win.tree = tree
        win.rows = {}             # (tab, slot) -> scan result
        win.chosen = {}           # (tab, slot) -> item picked by hand
        win.qty = {}              # (tab, slot) -> quantity set by hand
        win.photos = {}
        win.state = {"matcher": None, "memo": {}}   # shared with the scanning thread
        win.hidden = set()

        def keys_of(sel):
            return [tuple(json.loads(k)) for k in sel]

        def learn(name, keys):
            """Use this item for these slots, and remember their icons."""
            for key in keys:
                r = win.rows.get(key)
                if not r:
                    continue
                win.chosen[key] = name
                try:
                    mfm.learn_icon(self.con, name, r["icon"])
                    if win.state["matcher"] is not None:
                        win.state["matcher"].add(name, r["icon"])
                except Exception:
                    pass
            win.state["memo"].clear()           # read every slot again with what was learned
            self._inv_scan_show(win)

        def pick_item():
            sel = tree.selection()
            if not sel:
                return
            cur = tree.set(sel[0], "item")
            guess = win.rows.get(keys_of(sel)[0], {}).get("guess") or ""
            name = self._choose_item(win, "Which item is this?", guess if cur == "?" else cur)
            if name:
                learn(name, keys_of(sel))

        def accept():
            done = 0
            for key in keys_of(tree.selection()):
                r = win.rows.get(key)
                if r and not r["item"] and key not in win.chosen and r.get("guess"):
                    learn(r["guess"], [key])
                    done += 1
            if not done:
                self.root.bell()

        def remove():
            for key in keys_of(tree.selection()):
                win.hidden.add(key)
            self._inv_scan_show(win)

        def set_qty():
            sel = tree.selection()
            if not sel:
                return
            raw = ask_value(win, "Quantity", "How many?", tree.set(sel[0], "qty"),
                            check=lambda s: s.strip().isdigit() and int(s) > 0)
            if raw is None:
                return
            for k in sel:
                win.qty[tuple(json.loads(k))] = int(raw)
            self._inv_scan_show(win)
        self._list_menu(tree, [("Accept suggestion  (Enter)", lambda t: accept(), False),
                               ("Pick an item  (M)", lambda t: pick_item(), False),
                               ("Change quantity", lambda t: set_qty(), False),
                               None,
                               ("Remove  (Delete)", lambda t: remove(), False)])
        tree.bind("<Double-1>", lambda e: set_qty() if self._clicked_column(tree, e) == "qty" else pick_item())
        tree.bind("<Return>", lambda e: accept())
        tree.bind("<KeyPress-m>", lambda e: pick_item())
        tree.bind("<KeyPress-M>", lambda e: pick_item())
        tree.bind("<Delete>", lambda e: remove())
        tree.bind("<BackSpace>", lambda e: remove())
        win.pick_item = pick_item

        def add():
            sel = tree.selection() or ()
            items, unknown = [], 0
            for k in sel:
                name = tree.set(k, "item")
                if name == "?":
                    unknown += 1
                    continue
                items.append((name, int(tree.set(k, "qty") or 1)))
            if not items:
                messagebox.showinfo("Add items", "Select the items to add first."
                                    + (" Items shown as ? need an item chosen (double-click)." if unknown else ""),
                                    parent=win)
                return
            merged: dict[str, int] = {}
            for name, q in items:
                merged[name] = merged.get(name, 0) + q
            self.add_to_inventory(list(merged.items()))
            win.status.config(text=f"Added {mfm.plural(len(merged), 'item')} to My Inventory."
                              + (f" {unknown} unknown skipped." if unknown else ""))
        bottom = ttk.Frame(win, padding=(12, 4, 12, 12))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Select all", command=lambda: tree.selection_set(tree.get_children())
                   ).pack(side="left")
        bq = ttk.Button(bottom, text="Quantity", command=set_qty)
        bq.pack(side="left", padx=6)
        tip("Change how many of the selected items to add (or double-click a Qty).", bq)
        ttk.Button(bottom, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(bottom, text="Add", command=add).pack(side="right", padx=6)
        win.stop = threading.Event()
        win.bind("<Destroy>", lambda e: e.widget is win and win.stop.set())
        threading.Thread(target=self._inventory_scan_loop, args=(win.stop, win.state), daemon=True).start()

    @staticmethod
    def _clicked_column(tree, e) -> str | None:
        """The name of the column a mouse event is in."""
        col = tree.identify_column(e.x)
        if not col or col == "#0":
            return None
        try:
            return tree["columns"][int(col[1:]) - 1]
        except (ValueError, IndexError):
            return None

    def _choose_item(self, parent, title, cur=""):
        """A small dialog to pick an item by name (with suggestions). Returns the name or None."""
        dlg = self.window(parent)
        dlg.title(title)
        dlg.transient(parent)
        dlg.resizable(False, False)
        ttk.Label(dlg, text=title, padding=(12, 10, 12, 4)).pack(anchor="w")
        res = {}
        box = ItemSearch(dlg, self, width=40, on_return=lambda: ok())
        box.pack(padx=12, pady=4)
        box.set(cur)

        def ok():
            name = box.var.get().strip()
            if name:
                if mfm.has_item_list() and not mfm.is_known_item(name):
                    sugg = mfm.suggest_items(name, 1)
                    if sugg:
                        name = sugg[0]
                res["name"] = name
            dlg.destroy()
        bf = ttk.Frame(dlg, padding=(12, 6, 12, 12))
        bf.pack(fill="x")
        ttk.Button(bf, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(bf, text="OK", command=ok).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        box.entry.focus_set()
        dlg.grab_set()
        dlg.wait_window()
        return res.get("name")

    def _inventory_scan_loop(self, stop, state):
        """Background: read the item inventory window about once a second while the
        Add items window is open."""
        import hashlib
        try:
            con = mfm.db()
            matcher = mfm.IconMatcher(con)
            state["matcher"] = matcher
        except Exception as e:
            self.events.put(("inv_scan", {"error": f"Couldn't load item icons: {e}"}))
            return
        n_off = sum(1 for r in matcher.refs if r[2])
        self.events.put(("inv_scan", {"status": "Looking for your Item Inventory…",
                                      "icons": len(matcher.refs), "official": n_off}))
        memo = state["memo"]
        region = None
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                full = mfm.grab_full_screen()
                manual = mfm.drawn_area(self.cfg, "inventory_region")
                if manual and not mfm.is_auto(manual, full.size):
                    region, how = manual, "manual area"
                else:
                    region = mfm.find_inventory(full)
                    how = f"auto, found at {region[0]},{region[1]}" if region else ""
                if not region:
                    self.events.put(("inv_scan", {"status": "Item Inventory not found. Open it in the game "
                                                            "(or draw it in Settings → Reading → Window detection)."}))
                else:
                    win = mfm.crop(full, region)
                    tab = mfm.inventory_tab(win)
                    found = []
                    for i, (x0, y0, x1, y1) in enumerate(mfm.inventory_slots(win)):
                        slot = win.crop((x0, y0, x1, y1))
                        key = hashlib.md5(slot.resize((24, 24)).tobytes()).hexdigest()
                        if key not in memo:
                            best = matcher.match_slot(slot)
                            if not best:
                                memo[key] = None
                            else:
                                memo[key] = {**mfm.decide_match(best),
                                             "qty": mfm.read_slot_count(slot, self.cfg), "icon": slot}
                        if memo[key]:
                            found.append((i, memo[key]))
                    self.events.put(("inv_scan", {"tab": tab, "slots": found, "preview": win,
                                                  "status": f"Inventory: {how}."}))
            except SystemExit as e:
                self.events.put(("inv_scan", {"error": str(e)}))
                return
            except Exception as e:
                self.events.put(("inv_scan", {"status": f"Read failed: {type(e).__name__}: {e}"}))
            stop.wait(max(0.2, 1.0 - (time.monotonic() - t0)))

    def on_inv_scan(self, p):
        win = getattr(self, "inv_scan", None)
        if win is None or not win.winfo_exists():
            return
        if "error" in p:
            win.status.config(text=p["error"])
            return
        if "icons" in p:
            win.icons_note = ("" if p["official"] else
                              "\n\nFew item icons are downloaded, so matching is limited: "
                              "Settings → Game data → Update downloads them.")
        if p.get("status"):
            win.status.config(text=p["status"] + getattr(win, "icons_note", ""))
        if "slots" in p:
            tab = p["tab"] if p["tab"] is not None else -1
            for i, r in p["slots"]:
                win.rows[(tab, i)] = r
            prev = p["preview"].copy()
            prev.thumbnail((220, 300))
            win.preview_photo = ImageTk.PhotoImage(prev)
            win.preview.config(image=win.preview_photo, text="")
            self._inv_scan_show(win)

    INV_TABS = ("Equip", "Use", "Set Up", "Etc", "Cash", "Deco")

    def _inv_scan_show(self, win):
        tree = win.tree
        keep = set(tree.selection())
        tree.delete(*tree.get_children())
        for (tab, i), r in sorted(win.rows.items()):
            if (tab, i) in win.hidden:
                continue
            iid = json.dumps([tab, i])
            name = win.chosen.get((tab, i)) or r["item"] or "?"
            if (tab, i) not in win.photos:
                ic = r["icon"].copy()
                ic.thumbnail((28, 28))   # same size as the other tables' icons
                win.photos[(tab, i)] = ImageTk.PhotoImage(ic)
            review = not r["item"] and (tab, i) not in win.chosen
            match = ("chosen" if (tab, i) in win.chosen else f"{r['score']:.0%}" if r["item"]
                     else f"review: {r['guess']}? ({r['score']:.0%})" if r.get("guess") else "review: no match")
            tree.insert("", "end", iid=iid, image=win.photos[(tab, i)], tags=("review",) if review else (),
                        values=(
                name, win.qty.get((tab, i), r["qty"]),
                self.INV_TABS[tab] if 0 <= tab < len(self.INV_TABS) else "",
                match))
        tree.selection_set([k for k in keep if tree.exists(k)])

    # ------------------------------------------------------------------ watched listings
    def refresh_watched_listings(self):
        if not hasattr(self, "wlist"):
            return
        restore = self._keep_selection(self.wlist, lambda t, k: k)
        self.wlist.delete(*self.wlist.get_children())
        for i, w in enumerate(self.watched_listings):
            last = w.get("last") or {}
            now = {**w, **{k: v for k, v in last.items() if v not in (None, "")}}
            status = ("sold" if last.get("sold") else "for sale") if last else "not seen yet"
            self.wlist.insert("", "end", iid=str(i), image=self.icon_for(w["item"], found=bool(last) and not
                                                                          last.get("sold")),
                              tags=(("sold",) if last.get("sold") else ()), values=(
                w["item"], w.get("notes") or "", mfm.fmt(now.get("price")), status,
                now.get("shop") or "", now.get("seller") or "", now.get("map") or "",
                now.get("channel") or "", mfm.local_time(last["time"]) if last.get("time") else ""))
        restore()

    def _listing_from_table(self, tree):
        """(item, details, scope) of the one selected Live / Deals row."""
        sel = tree.selection()
        if len(sel) != 1:
            return None
        scope = self.scopes.get((str(tree), sel[0]))
        if not scope:
            return None
        item = tree.set(sel[0], "item")
        title, seller = scope.get("title"), scope.get("seller")
        if title is None and seller is None:
            title, seller = split_shop(scope.get("label") or "")
        details = {"price": scope["price"], "shop": title or "", "seller": seller or "",
                   "map": scope.get("map") or "", "channel": scope.get("channel"),
                   "notes": tree.set(sel[0], "notes")}
        return item, details, scope

    def _ask_note(self, parent, title, item, cur):
        return ask_value(parent, title, f"Note for this listing of '{item}' (e.g. +5 ATK, 7 slots):", cur)

    def note_from_table(self, tree):
        got = self._listing_from_table(tree)
        if not got:
            return
        item, d, scope = got
        note = self._ask_note(self.root, "Note", item, d["notes"])
        if note is None:
            return
        mfm.set_listing_notes(self.con, item, note, scope=scope)
        if tree is self.live:
            tree.set(tree.selection()[0], "notes", note.strip())
        self._note_to_watched(item, d["shop"], d["seller"], d["price"], note)
        self.refresh_tables()
        self._set_status(f"Note saved for '{item}'." if note.strip() else f"Note removed for '{item}'.")

    def _note_to_watched(self, item, shop, seller, price, note):
        """A note set on a listing also goes on its watched listing, if it's watched."""
        w = mfm.watched_listing_match(self.watched_listings, item, shop, seller, price)
        if w:
            w["notes"] = note.strip()
            mfm.save_watched_listings(self.watched_listings)
            self.refresh_watched_listings()

    def watch_listing_from_table(self, tree):
        got = self._listing_from_table(tree)
        if got:
            item, d, scope = got
            self.watch_listing(item, d, scope=scope)

    def watch_listing(self, item, d, scope=None, ids=None, parent=None):
        """Add one listing to the watched listings, after asking for its note."""
        note = self._ask_note(parent or self.root, "Watch this listing", item, d.get("notes") or "")
        if note is None:
            return
        note = note.strip()
        mfm.set_listing_notes(self.con, item, note, scope=scope, ids=ids)
        w = mfm.watched_listing_match(self.watched_listings, item, d["shop"], d["seller"], d["price"])
        if w is None:
            w = {"item": item, "added": mfm.utc_now(), "last": None}
            self.watched_listings.append(w)
        w.update({"price": d["price"], "shop": d["shop"], "seller": d["seller"], "map": d["map"],
                  "channel": d.get("channel"), "notes": note})
        mfm.save_watched_listings(self.watched_listings)
        self.refresh_watched_listings()
        self.refresh_tables()
        for k in self.live.get_children():        # the same listing in the Live tab
            sc = self.scopes.get((str(self.live), k))
            if sc and self.live.set(k, "item") == item and sc["price"] == d["price"] \
                    and (sc.get("seller") or "") == d["seller"]:
                self.live.set(k, "notes", note)
        self._set_status(f"Watching this listing: {item}" + (f" ({note})" if note else "") + ".")

    def edit_watched_note(self):
        sel = self.wlist.selection()
        if len(sel) != 1:
            return
        w = self.watched_listings[int(sel[0])]
        note = self._ask_note(self.root, "Note", w["item"], w.get("notes") or "")
        if note is None:
            return
        w["notes"] = note.strip()
        mfm.save_watched_listings(self.watched_listings)
        # the same note on its logged sightings
        who, val = ("seller", w.get("seller")) if w.get("seller") else ("shop", w.get("shop"))
        if val:
            ids = [r[0] for r in self.con.execute(
                f"SELECT id FROM listings WHERE item = ? AND {who} = ?", (w["item"], val))]
            mfm.set_listing_notes(self.con, w["item"], note, ids=ids)
        self.refresh_watched_listings()
        self.refresh_tables()

    def remove_watched_listing(self):
        sel = {int(k) for k in self.wlist.selection()}
        if not sel:
            return
        self.watched_listings = [w for i, w in enumerate(self.watched_listings) if i not in sel]
        mfm.save_watched_listings(self.watched_listings)
        self.refresh_watched_listings()
        self.refresh_tables()

    def refresh_alerts(self):
        if not hasattr(self, "alerts"):
            return
        restore = self._keep_selection(self.alerts, lambda t, k: k)
        self.alerts.delete(*self.alerts.get_children())
        for w in self.watch:
            if mfm.watch_is_word(w):
                # the newest megaphone that says it
                last = None
                try:
                    last = self.con.execute(
                        "SELECT ts, player, channel, message FROM megaphones WHERE LOWER(message) LIKE ? "
                        "ORDER BY ts DESC LIMIT 1", (f"%{w['item'].lower()}%",)).fetchone()
                except sqlite3.Error:
                    pass
                self.alerts.insert("", "end", iid="w\x1f" + w["item"], tags=("watch",) if last else (), values=(
                    f"\u201c{w['item']}\u201d  (word)", "", "megaphone" if last else "not seen yet",
                    (last[3] if last else ""), (last[1] or "") if last else "", "",
                    (last[2] or "") if last else "", mfm.local_time(last[0]) if last else ""))
                continue
            lf = w.get("last_found") or {}
            if lf and "where" in lf:        # saved by versions before 2610.5: "shop · map"
                parts = lf["where"].split(" · ")
                lf = {"each": lf.get("each"), "shop": parts[0] if parts else "",
                      "map": parts[1] if len(parts) > 1 else "", "channel": None, "time": lf.get("when")}
            self.alerts.insert("", "end", iid="i\x1f" + w["item"], image=self.icon_for(w["item"], found=bool(lf)), values=(
                w["item"], mfm.fmt(w["max_each"]) if w.get("max_each") not in (None, "") else "any",
                mfm.fmt(lf["each"]) if lf else "not seen yet",
                *((lf.get("shop") or "", lf.get("player") or "") if "player" in lf
                  else split_shop(lf.get("shop") or "")),
                (lf.get("map") if ": " in (lf.get("map") or "") else mfm.snap_map(lf.get("map") or "")),
                lf.get("channel") or "",
                mfm.local_time(lf["time"]) if lf.get("time") else ""))
        restore()
        snd = self.cfg.get("alert_sound")
        self.sound_lbl.config(text=f"Sound: {Path(snd).name if snd else 'default ping'}")

    def _alert_toggle_type(self):
        """Item ↔ Word for what Watch adds (the item search box, or a plain text box)."""
        self.alert_words = not self.alert_words
        self.alert_type_btn.config(text="Type: Word" if self.alert_words else "Type: Item")
        self.alert_item_lbl.config(text="Words:" if self.alert_words else "Item:")
        if self.alert_words:
            self.alert_entry.pack_forget()
            for w in (self.alert_max_lbl, self.alert_max_entry):
                w.pack_forget()
            self.alert_word_entry.pack(side="left", padx=4, before=self.alert_watch_btn)
            self.alert_word_entry.focus_set()
        else:
            self.alert_word_entry.pack_forget()
            self.alert_entry.pack(side="left", padx=4, before=self.alert_watch_btn)
            self.alert_max_lbl.pack(side="left", padx=(10, 0), before=self.alert_watch_btn)
            self.alert_max_entry.pack(side="left", padx=4, before=self.alert_watch_btn)

    def _alert_keys(self, sel) -> list[tuple[str, str]]:
        """The watched rows selected: ("i", item) or ("w", words)."""
        return [tuple(k.split("\x1f", 1)) for k in sel if "\x1f" in k]

    def add_alert(self, item, max_each=None, word=False):
        item = " ".join(item.split()) if word else item.strip()
        if not item:
            return
        if word:
            self.watch = [w for w in self.watch
                          if not (mfm.watch_is_word(w) and w["item"].lower() == item.lower())]
            self.watch.append({"item": item, "type": "word", "max_each": None, "last_found": None})
            mfm.save_watchlist(self.watch)
            self.refresh_alerts()
            self.refresh_megaphones()
            self._set_status(f"Watching megaphones that say \u201c{item}\u201d.")
            return
        self.watch = [w for w in self.watch if mfm.watch_is_word(w) or w["item"] != item]
        self.watch.append({"item": item, "max_each": max_each, "last_found": None})
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self.refresh_tables()
        self.refresh_megaphones()             # watched items are highlighted there too
        self._set_status(f"Watching {item}" + (f" at {mfm.fmt(max_each)} or less" if max_each else "") + ".")

    def add_alert_from_form(self):
        if self.alert_words:
            self.add_alert(self.alert_word.get(), word=True)
            self.alert_word.set("")
            return
        raw = self.alert_max.get().strip()
        try:
            max_each = mfm.parse_price(raw) if raw else None
        except Exception:
            max_each = None
        name = self.alert_item.get()
        if mfm.has_item_list() and not mfm.is_known_item(name):
            sugg = mfm.suggest_items(name, 1)
            if sugg and messagebox.askyesno("Item name", f"'{name}' isn't in the item database.\n\n"
                                            f"Use '{sugg[0]}' instead?"):
                name = sugg[0]
        self.add_alert(name, max_each)
        self.alert_entry.set("")
        self.alert_max.set("")

    def _list_menu(self, tree, entries):
        """Right-click menu for a list of items. entries: (label, fn(tree), single_only)
        or None for a separator; labels get a count when several rows are selected."""
        menu = tk.Menu(tree, tearoff=False)

        def popup(e):
            row = tree.identify_row(e.y)
            if not row:
                return
            if row not in tree.selection():   # right-clicking inside a selection keeps it
                tree.selection_set(row)
                tree.focus(row)
            n = len(tree.selection())
            menu.delete(0, "end")
            for ent in entries:
                if ent is None:
                    menu.add_separator()
                    continue
                label, fn, single = ent
                if single:
                    menu.add_command(label=label, command=lambda fn=fn: fn(tree),
                                     state="normal" if n == 1 else "disabled")
                else:
                    menu.add_command(label=label if n == 1 else f"{label} ({n} items)",
                                     command=lambda fn=fn: fn(tree))
            menu.tk_popup(e.x_root, e.y_root)
        bind_right_click(tree, popup)

    def change_alert_max(self):
        items = [k for t, k in self._alert_keys(self.alerts.selection()) if t == "i"]
        if not items:
            return
        cur = ""
        if len(items) == 1:
            w = next((w for w in self.watch if not mfm.watch_is_word(w) and w["item"] == items[0]), None)
            cur = mfm.fmt(w["max_each"]) if w and w.get("max_each") not in (None, "") else ""
        raw = ask_value(self.root, "Max price",
                        (f"Alert when '{items[0]}' is for sale at or below:" if len(items) == 1 else
                         f"Max price for these {len(items)} items:")
                        + "\n(e.g. 45000, 45,000 or 45k; empty = any price)", cur,
                        check=lambda t: not t.strip() or (mfm.parse_price(t) or 0) > 0)
        if raw is None:
            return
        cap = mfm.parse_price(raw) if raw.strip() else None
        for w in self.watch:
            if not mfm.watch_is_word(w) and w["item"] in items:
                w["max_each"] = cap
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self.refresh_tables()
        self.refresh_megaphones()             # watched items are highlighted there too
        self._set_status(f"Max price {'set to ' + mfm.fmt(cap) if cap else 'removed (any price)'} for "
                         + (f"'{items[0]}'." if len(items) == 1 else f"{len(items)} items."))

    def remove_alert(self):
        sel = self.alerts.selection()
        if not sel:
            return
        gone = set(self._alert_keys(sel))
        self.watch = [w for w in self.watch if ("w" if mfm.watch_is_word(w) else "i", w["item"]) not in gone]
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self.refresh_tables()
        self.refresh_megaphones()             # watched items are highlighted there too

    def alert_from_table(self, tree):
        items = self._selected_items(tree)
        if not items:
            return
        from tkinter import simpledialog
        what = f"'{items[0]}'" if len(items) == 1 else f"any of these {len(items)} items"
        raw = simpledialog.askstring("Watch item", f"Get an alert when {what} is for sale.\n\n"
                                     "Max price (leave empty for any price"
                                     + (", applies to each" if len(items) > 1 else "") + "):",
                                     parent=self.root)
        if raw is None:
            return
        cap = mfm.parse_price(raw) if raw.strip() else None
        for item in items:
            self.add_alert(item, cap)
        if len(items) > 1:
            self._set_status(f"Watching {len(items)} items"
                             + (f" at {mfm.fmt(cap)} or less each" if cap else "") + ".")

    # ------------------------------------------------------------------ review unmatched
    # ------------------------------------------------------------------ review: megaphones
    def _build_megaphone_review(self):
        tab = ttk.Frame(self.review_nb, padding=6)
        self.review_mega_tab = tab
        self.review_nb.add(tab, text="  Megaphones  ")
        ttk.Label(tab, wraplength=1040, justify="left", text=(
            "Things megaphones sell or buy that aren't matched to an item yet. Matching one teaches the "
            "app an alias, used for every megaphone from then on (and shared through the Shared "
            "Database). Enter = accept the suggestion · M = pick an item · Delete = not an item.")
                  ).pack(anchor="w", pady=(0, 6))
        self._alias_buttons(tab).pack(fill="x", pady=(0, 6))
        t = make_tree(tab, ("phrase", "side", "mprice", "suggest", "score", "count", "message"))
        t.heading("count", text="Seen")
        t.column("message", stretch=True)
        t.frame.pack(fill="both", expand=True)
        t.tag_configure("good", foreground="#145c2c")
        t.tag_configure("weak", foreground="#8a6d1a")
        self.mreview = t
        t.bind("<Return>", lambda e: self.mreview_accept())
        t.bind("<KP_Enter>", lambda e: self.mreview_accept())
        t.bind("<Delete>", lambda e: self.mreview_not_item())
        t.bind("<BackSpace>", lambda e: self.mreview_not_item())
        for k in ("m", "M"):
            t.bind(f"<KeyPress-{k}>", lambda e: self.mreview_match())
        t.bind("<Double-1>", lambda e: self.mreview_match())
        self._list_menu(t, [("Accept suggestion  (Enter)", lambda tr: self.mreview_accept(), False),
                            ("Pick an item  (M)", lambda tr: self.mreview_match(), True),
                            ("Not an item  (Delete)", lambda tr: self.mreview_not_item(), False)])
        bottom = ttk.Frame(tab)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="Accept suggestion", command=self.mreview_accept).pack(side="left")
        ttk.Button(bottom, text="Match manually", command=self.mreview_match).pack(side="left", padx=4)
        # Item or Words: what Accept and Match manually teach, like the Type in Edit aliases
        self.mreview_words = False
        self.mreview_type_btn = ttk.Button(bottom, text="Type: Item", width=12, command=self._mreview_toggle_type)
        self.mreview_type_btn.pack(side="left", padx=(0, 4))
        tip("What Accept suggestion and Match manually teach. Item: the phrase is an item. Words: the "
            "phrase stands for words, used in any item's name (\"att\" = Attack). Click to switch.",
            self.mreview_type_btn)
        ttk.Button(bottom, text="Not an item", command=self.mreview_not_item).pack(side="left")
        b = ttk.Button(bottom, text="Delete megaphone", command=self.mreview_delete)
        b.pack(side="left", padx=4)
        tip("Delete the megaphones these rows came from (a misread line, spam). Nothing is "
            "added to the aliases.", b)
        b = ttk.Button(bottom, text="Remove all megaphones", command=self.mreview_delete_all)
        b.pack(side="left", padx=(12, 0))
        tip("Deletes every megaphone with something in this list (all of them, not only the "
            "ones shown). Nothing is added to the aliases.", b)
        self.mreview_more = ttk.Label(bottom, foreground="#666")
        self.mreview_more.pack(side="right")
        self.refresh_megaphone_review()
        # common shorthand ("att" = attack, "ohs" = one handed sword) as word aliases
        try:
            mfm.seed_default_words(self.con)
        except sqlite3.Error:
            pass
        # read items and prices from megaphones saved before (or by an older version)
        self.root.after(800, self._aliases_changed)

    def _fill_megaphone_review(self, rows, total):
        if not hasattr(self, "mreview"):
            return
        t = self.mreview
        restore = self._keep_selection(t, lambda tr, k: k)
        t.delete(*t.get_children())
        self.mreview_rows = {}
        for r in rows:
            sides = r["sides"]
            side = "Buying and selling" if len(sides) > 1 else ("Buying" if "buy" in sides else "Selling")
            score = r["score"] or 0
            tag = "good" if score >= 0.85 else "weak" if r["suggest"] else ""
            t.insert("", "end", iid=r["key"], tags=(tag,) if tag else (),
                     image=self.icon_for(r["suggest"]) if r["suggest"] else "",
                     values=(r["phrase"], side, mfm.fmt(r["price"]) if r["price"] else "", r["suggest"] or "",
                             f"{score:.0%}" if r["suggest"] else "", r["count"], r["example"]))
            self.mreview_rows[r["key"]] = r
        restore()
        if hasattr(self, "mreview_more"):
            self.mreview_more.config(text=f"Showing the {len(rows):,} most seen of {total:,}." if total > len(rows)
                                     else "")

    def _mreview_toggle_type(self):
        self.mreview_words = not self.mreview_words
        self.mreview_type_btn.config(text="Type: Words" if self.mreview_words else "Type: Item")

    def mreview_accept(self):
        done = 0
        for key in self.mreview.selection():
            r = self.mreview_rows.get(key)
            if r and r["suggest"]:
                # Words: the suggested item's name as the words (like Item -> Words in Edit aliases)
                mfm.set_alias(self.con, r["phrase"], (mfm.WORDS_PREFIX if self.mreview_words else "") + r["suggest"])
                done += 1
        if done:
            self._aliases_changed(f"Learned {mfm.plural(done, 'alias')}.")
        else:
            self.root.bell()

    def mreview_match(self):
        sel = self.mreview.selection()
        if not sel:
            return
        r = self.mreview_rows.get(sel[0])
        if not r:
            return
        if self.mreview_words:
            from tkinter import simpledialog
            words = simpledialog.askstring(
                "Words", f"What words does \"{r['phrase']}\" stand for?\n(e.g. \"att\" = Attack, "
                "\"60%\" = Intermediate)", initialvalue=r["phrase"], parent=self.root)
            words = " ".join((words or "").split())
            if not words:
                return
            for key in sel:
                rr = self.mreview_rows.get(key)
                if rr:
                    mfm.set_alias(self.con, rr["phrase"], mfm.WORDS_PREFIX + words)
            self._aliases_changed(f"\"{r['phrase']}\" now stands for the words \"{words}\".")
            return
        name = self._choose_item(self.root, f"Which item is \"{r['phrase']}\"?", r["suggest"] or r["phrase"].title())
        if name:
            for key in sel:                       # the same item for every selected phrase
                rr = self.mreview_rows.get(key)
                if rr:
                    mfm.set_alias(self.con, rr["phrase"], name)
            self._aliases_changed(f"\"{r['phrase']}\" is now an alias for {name}.")

    def mreview_not_item(self):
        sel = list(self.mreview.selection())
        for key in sel:
            r = self.mreview_rows.get(key)
            if r:
                mfm.set_alias(self.con, r["phrase"], None)
        if sel:
            self._aliases_changed(f"{mfm.plural(len(sel), 'phrase')} marked as not an item.")

    def _alias_buttons(self, parent):
        bar = ttk.Frame(parent)
        b = ttk.Button(bar, text="Edit aliases", command=self.aliases_window)
        b.pack(side="left")
        tip("A sheet of every alias: what megaphones call an item, and the item it is. "
            "Add, change or remove them.", b)
        b = ttk.Button(bar, text="Refresh", command=self.reapply_aliases)
        b.pack(side="left", padx=(6, 0))
        tip("Match every megaphone again with the current aliases. Phrases that now match "
            "leave Review.", b)
        return bar

    def reapply_aliases(self):
        """Read every megaphone again with the current aliases (in the background)."""
        if getattr(self, "_reapplying", False):
            return
        self._reapplying = True
        self._set_status("Matching megaphones with the aliases…")
        hours = max(1, float(self.cfg.get("megaphone_hours", 72) or 72))

        def work():
            con = mfm.db()
            try:
                before = {r["key"] for r in mfm.unmatched_offers(con, hours)}
                mfm.update_offers(con, force=True)
                after = {r["key"] for r in mfm.unmatched_offers(con, hours)}
                n = len(before - after)
                msg = (f"Refreshed: {mfm.plural(n, 'phrase')} matched and removed from Review." if n
                       else "Refreshed: no new matches.")
            except Exception as e:
                msg = f"Refresh failed: {e}"
            finally:
                con.close()

            def done():
                self._reapplying = False
                self.refresh_megaphones()
                self.refresh_megaphone_review()
                self._set_status(msg)
            self.events.put(("call", done))
        threading.Thread(target=work, daemon=True).start()

    def _item_named(self, parent, text):
        """The item for what was typed: the exact name (any capitals), else ask."""
        text = " ".join((text or "").split())
        if not text:
            return None
        hit = mfm._item_index().get(mfm.alias_key(text))   # the name as the item list spells it
        if hit:
            return hit
        if mfm.is_known_item(text):
            return text
        return self._choose_item(parent, f"Which item is \"{text}\"?", text)

    NOT_ITEM = "(not an item)"
    NOT_ITEM_WORDS = ("", "-", "(not an item)", "not an item")

    def mreview_delete(self):
        """Delete the megaphones the selected Review rows came from."""
        keys = [k for k in self.mreview.selection() if k in getattr(self, "mreview_rows", {})]
        if not keys:
            return
        hours = max(1, int(self.cfg.get("megaphone_hours", 72) or 72))
        q = ",".join("?" * len(keys))
        ids = [r[0] for r in self.con.execute(
            f"SELECT DISTINCT o.megaphone_id FROM megaphone_offers o JOIN megaphones m ON m.id = o.megaphone_id "
            f"WHERE o.key IN ({q}) AND o.item IS NULL AND m.ts >= ?", keys + [mfm.utc_ago(hours=hours)])]
        if not ids:
            return
        if not messagebox.askyesno("Delete megaphones",
                                   f"Delete {mfm.plural(len(ids), 'megaphone')} with "
                                   + (f"\"{self.mreview_rows[keys[0]]['phrase']}\"" if len(keys) == 1
                                      else f"these {len(keys)} phrases") + "?", parent=self.root):
            return
        nxt = self.mreview.next(keys[-1]) or self.mreview.prev(keys[0])
        mfm.delete_megaphones(self.con, ids)
        self._set_status(f"Deleted {mfm.plural(len(ids), 'megaphone')}.")
        self.refresh_megaphones(delay=10)
        if nxt and nxt not in keys:
            self.root.after(600, lambda: self.mreview.exists(nxt) and (self.mreview.selection_set(nxt),
                                                                       self.mreview.see(nxt)))

    def mreview_delete_all(self):
        """Delete every megaphone that has a phrase waiting in Review → Megaphones."""
        hours = max(1, int(self.cfg.get("megaphone_hours", 72) or 72))
        ids = [r[0] for r in self.con.execute(
            "SELECT DISTINCT o.megaphone_id FROM megaphone_offers o JOIN megaphones m ON m.id = o.megaphone_id "
            "WHERE o.item IS NULL AND COALESCE(o.how, '') != 'ignored' AND m.ts >= ?",
            (mfm.utc_ago(hours=hours),))]
        if not ids:
            self.root.bell()
            return
        if not messagebox.askyesno("Remove all megaphones",
                                   f"Delete all {mfm.plural(len(ids), 'megaphone')} with something not "
                                   "matched to an item yet?\n\nNothing is added to the aliases.",
                                   parent=self.root):
            return
        mfm.delete_megaphones(self.con, ids)
        self._set_status(f"Deleted {mfm.plural(len(ids), 'megaphone')}.")
        self.refresh_megaphones(delay=10)
        self.refresh_megaphone_review()

    def aliases_window(self):
        """An editable sheet of aliases: "Megaphone term" (what players write),
        "Alias" (what it means), and "Type": Item (the phrase is that item) or Words (the
        phrase stands for other words, used to read any item's name: "60%" =
        "Intermediate", "att" = "Attack"). Click a cell to type in it."""
        if getattr(self, "_alias_win", None) and self._alias_win.winfo_exists():
            self._alias_win.lift()
            return
        win = self.window()
        self._alias_win = win
        win.title("Aliases")
        win.geometry("860x560")
        win.transient(self.root)
        ttk.Label(win, padding=(10, 8), wraplength=820, justify="left", text=(
            "What players write in megaphones, and what it means. Click a cell to type in it (Enter "
            "saves, Esc cancels); the last row adds a new one; Delete removes the selected ones.\n"
            "Type \"Item\": the phrase is that item (\"kumbis\" = Kumbi Throwing-Stars). Leave the item "
            "empty for \"not an item\".\n"
            "Type \"Words\": the phrase stands for other words, for any item (\"60%\" = Intermediate, "
            "\"att\" = Attack): \"60% gloves att\" then matches Gloves Attack Scroll: Intermediate. "
            "Click the Type cell to switch.")).pack(anchor="w")
        t = make_tree(win, ("aterm", "avalue", "atype"))
        t.column("avalue", stretch=True)
        t.column("atype", anchor="center")
        t.tag_configure("new", foreground="#888")
        t.tag_configure("ignored", foreground="#888")
        t.tag_configure("words", foreground="#1f4e9a")
        t.frame.pack(fill="both", expand=True, padx=10)
        NEW = "__new__"
        WP = mfm.WORDS_PREFIX
        pending = {}                     # rows with an alias typed but nothing else yet

        def fill(select=None):
            t.delete(*t.get_children())
            rows = sorted(mfm.load_aliases(self.con).items(), key=lambda kv: (kv[1].startswith(WP), kv[0]))
            for key, val in rows:
                if val.startswith(WP):
                    t.insert("", "end", iid="k:" + key, tags=("words",), values=(key, val[len(WP):], "Words"))
                else:
                    t.insert("", "end", iid="k:" + key, image=self.icon_for(val) if val else "",
                             tags=() if val else ("ignored",), values=(key, val or self.NOT_ITEM, "Item"))
            for pid, p in pending.items():
                t.insert("", "end", iid=pid, values=(p["phrase"], "", "Words" if p["words"] else "Item"))
            t.insert("", "end", iid=NEW, tags=("new",), values=("(click to add an alias)", "", ""))
            if select and t.exists(select):
                t.selection_set(select)
                t.focus(select)
                t.see(select)

        def saved(key):
            fill("k:" + key)
            self._aliases_changed()

        def value_for(raw, words):
            """What to store for what was typed (None = cancelled)."""
            if words:
                return WP + raw
            if raw.lower() in self.NOT_ITEM_WORDS:
                return ""
            return self._item_named(win, raw)

        def on_done(tree, row, col, raw):
            raw = " ".join((raw or "").split())
            if row == NEW:
                if col != "aterm" or not mfm.alias_key(raw):
                    return
                pid = f"p:{len(pending) + 1}:{time.monotonic()}"
                pending[pid] = {"phrase": raw, "words": False}
                fill(pid)
                win.after(50, lambda: start_edit(pid, "avalue"))   # straight on to its meaning
                return
            if row in pending:
                p = pending[row]
                if col == "aterm":
                    if mfm.alias_key(raw):
                        p["phrase"] = raw
                    fill(row)
                    return
                val = value_for(raw, p["words"])
                if val is None:
                    return                      # cancelled the pick
                pending.pop(row)
                saved(mfm.set_alias(self.con, p["phrase"], val or None))
                return
            key = row[2:]
            cur = mfm.load_aliases(self.con).get(key, "")
            if col == "aterm":
                new = mfm.alias_key(raw)
                if not new or new == key:
                    return
                mfm.delete_alias(self.con, key)
                saved(mfm.set_alias(self.con, new, cur or None))
            else:
                val = value_for(raw, cur.startswith(WP))
                if val is not None:
                    saved(mfm.set_alias(self.con, key, val or None))

        def toggle_type(row):
            if row in pending:
                pending[row]["words"] = not pending[row]["words"]
                fill(row)
                return
            if not row.startswith("k:"):
                return
            key = row[2:]
            cur = mfm.load_aliases(self.con).get(key, "")
            if cur.startswith(WP):              # Words -> Item: the words should name an item
                val = value_for(cur[len(WP):], False)
                if val is None:
                    return
            else:                               # Item -> Words: the item's name as the words
                val = WP + cur
            saved(mfm.set_alias(self.con, key, val or None))

        def start_edit(row, col):
            if t.exists(row):
                if row == NEW:
                    t.set(NEW, "aterm", "")
                t.see(row)
                t.update_idletasks()
                self._inline_edit(t, row, col, on_done)

        def on_double(e):
            row, col = t.identify_row(e.y), self._clicked_column(t, e)
            if row and col and col != "atype":
                self._cancel_inline(t)
                start_edit(row, col)

        def on_click(e):
            row, col = t.identify_row(e.y), self._clicked_column(t, e)
            if row == NEW:               # one click on the last row starts a new alias
                win.after(10, lambda: start_edit(NEW, "aterm"))
            elif row and col == "atype":
                win.after(10, lambda: toggle_type(row))

        def remove():
            keys = [k for k in t.selection() if k.startswith("k:")]
            for k in t.selection():
                pending.pop(k, None)
            for k in keys:
                mfm.delete_alias(self.con, k[2:])
            fill()
            if keys:
                self._aliases_changed(f"Removed {mfm.plural(len(keys), 'alias')}.")

        def edit_selected(col):
            sel = t.selection()
            if sel:
                start_edit(sel[0], col)

        def pick():
            sel = [k for k in t.selection() if k != NEW]
            if not sel:
                return
            cur = t.set(sel[0], "avalue")
            name = self._choose_item(win, f"Which item is \"{t.set(sel[0], 'aterm')}\"?",
                                     "" if cur == self.NOT_ITEM else cur)
            if not name:
                return
            row = sel[0]
            if row in pending:                  # a picked item makes it an Item alias
                pending[row]["words"] = False
                on_done(t, row, "avalue", name)
            elif row.startswith("k:"):
                saved(mfm.set_alias(self.con, row[2:], name))

        self._inline_editing(t, ("aterm", "avalue"), on_done)
        t.on_done = on_done
        t.bind("<Double-1>", on_double)
        t.bind("<ButtonRelease-1>", on_click, add="+")
        t.bind("<Return>", lambda e: edit_selected("avalue"))
        t.bind("<F2>", lambda e: edit_selected("aterm"))
        t.bind("<Delete>", lambda e: remove())
        t.bind("<BackSpace>", lambda e: remove())
        bar = ttk.Frame(win, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add an alias", command=lambda: start_edit(NEW, "aterm")).pack(side="left")
        ttk.Button(bar, text="Pick the item…", command=pick).pack(side="left", padx=6)
        ttk.Button(bar, text="Item / Words", command=lambda: [toggle_type(k) for k in t.selection()[:1]]
                   ).pack(side="left")
        ttk.Button(bar, text="Remove", command=remove).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        fill()

    # ------------------------------------------------------------------ review
    def _build_review_tab(self):
        outer = ttk.Frame(self.nb, padding=6)
        self.review_tab = outer
        self.nb.add(outer, text="  Review  ")
        # two kinds of names to review: from shops, and from megaphones
        self.review_nb = ttk.Notebook(outer)
        self.review_nb.pack(fill="both", expand=True)
        self.review_nb.bind("<<NotebookTabChanged>>", lambda e: self._mega_tab_shown(), add="+")
        tab = ttk.Frame(self.review_nb, padding=6)
        self.review_shop_tab = tab
        self.review_nb.add(tab, text="  Shop listings  ")
        self._build_megaphone_review()
        ttk.Label(tab, wraplength=1040, justify="left", text=(
            "Names not in the item list, with the closest item. Enter = accept · M = pick an "
            "item · K = keep the name · Delete = delete its listings.")).pack(anchor="w", pady=(0, 6))
        self.review = make_tree(tab, ("item", "suggest", "score", "each", "shop", "player", "channel",
                                      "count", "time", "raw"))
        self.review.column("time", width=140)
        self.review.column("shop", width=160)
        self.review.frame.pack(fill="both", expand=True)
        self.review.tag_configure("good", foreground="#145c2c")
        self.review.tag_configure("weak", foreground="#8a6d1a")
        self.review.tag_configure("kept", foreground="#9a9a9a")
        t = self.review
        t.bind("<Return>", lambda e: self.review_accept())
        t.bind("<KP_Enter>", lambda e: self.review_accept())
        t.bind("<Delete>", lambda e: self.review_delete())
        t.bind("<BackSpace>", lambda e: self.review_delete())
        for k in ("m", "M"):
            t.bind(f"<KeyPress-{k}>", lambda e: self.match_dialog(self.review))
        for k in ("k", "K"):
            t.bind(f"<KeyPress-{k}>", lambda e: self.review_keep())
        t.bind("<Double-1>", lambda e: self.open_history(self.review))
        menu = tk.Menu(self.root, tearoff=0)

        def popup(e):
            row = t.identify_row(e.y)
            if not row:
                return
            if row not in t.selection():
                t.selection_set(row)
            n = len(t.selection())
            menu.delete(0, "end")
            menu.add_command(label="Accept suggested match" + (f" ({n})" if n > 1 else "") + "   Enter",
                             command=self.review_accept)
            menu.add_command(label="Match by hand   M", state="normal" if n == 1 else "disabled",
                             command=lambda: self.match_dialog(t))
            menu.add_command(label="Keep name as is" + (f" ({n})" if n > 1 else "") + "   K",
                             command=self.review_keep)
            menu.add_command(label="Delete listings" + (f" ({n} items)" if n > 1 else "") + "   Del",
                             command=self.review_delete)
            menu.add_separator()
            menu.add_command(label="Show history", state="normal" if n == 1 else "disabled",
                             command=lambda: self.open_history(t))
            menu.add_command(label="Look up on meowdb", state="normal" if n == 1 else "disabled",
                             command=lambda: self.open_meowdb(t))
            menu.tk_popup(e.x_root, e.y_root)
        bind_right_click(t, popup)

        bottom = ttk.Frame(tab)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="Accept suggestion", command=self.review_accept).pack(side="left")
        ttk.Button(bottom, text="Match manually", command=lambda: self.match_dialog(self.review)).pack(side="left",
                                                                                               padx=4)
        ttk.Button(bottom, text="Keep as is", command=self.review_keep).pack(side="left")
        ttk.Button(bottom, text="Delete listings", command=self.review_delete).pack(side="left", padx=4)
        ttk.Button(bottom, text="Accept all 90%+ alike", command=self.review_accept_confident
                   ).pack(side="left", padx=(16, 0))
        b = ttk.Button(bottom, text="Remove all listings", command=self.review_delete_all)
        b.pack(side="left", padx=(16, 0))
        tip("Deletes the listings of every name in this list (kept names only when Show kept "
            "names is on).", b)
        self.review_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="Remember matches for future reads", variable=self.review_remember
                        ).pack(side="left", padx=(16, 0))
        self.review_show_kept = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Show kept names", variable=self.review_show_kept,
                        command=self.refresh_review).pack(side="right")
        self.refresh_review()

    def refresh_review(self):
        """Recompute the Review tab (in the background; see refresh_tables)."""
        if hasattr(self, "review"):
            self.request_tables()

    def _fill_review(self, rows):
        if not hasattr(self, "review"):
            return
        t = self.review
        restore = self._keep_selection(t, lambda tr, k: tr.set(k, "item"))
        t.delete(*t.get_children())
        self.review_rows = {}
        for r in rows:
            tag = "kept" if r["kept"] else "good" if r["score"] >= 0.9 else "weak" if r["suggest"] else ""
            iid = t.insert("", "end", image=self.icon_for(r["item"]), tags=(tag,), values=(
                r["item"], (r["suggest"] or "— no close match —") + ("   (kept as is)" if r["kept"] else ""),
                f"{r['score']:.0%}" if r["suggest"] else "",
                mfm.fmt(r["price"]) if r.get("price") else "", r.get("shop", ""), r.get("seller", ""),
                mfm.channel_label(r["channel"]) if r.get("channel") else "",
                r["count"], mfm.local_time(r["last"]), r["raw"]))
            self.review_rows[iid] = r
        restore()

    def _review_selected(self):
        t = self.review
        sel = list(t.selection())
        kids = list(t.get_children())
        after = kids.index(sel[-1]) + 1 if sel else 0
        nxt = next((k for k in kids[after:] if k not in sel), None) or \
            next((k for k in reversed(kids[:kids.index(sel[0])]) if k not in sel), None) if sel else None
        nxt_item = t.set(nxt, "item") if nxt else None
        return [self.review_rows[k] for k in sel if k in self.review_rows], nxt_item

    def _review_select(self, item):
        """After acting on a row, move on to the next one."""
        t = self.review
        for k in t.get_children():
            if t.set(k, "item") == item:
                t.selection_set(k)
                t.focus(k)
                t.see(k)
                break
        t.focus_set()

    def _apply_match(self, old, pick, remember) -> int:
        n = mfm.rename_item(self.con, old, pick)
        if remember:
            mfm.add_alias(old, pick, mfm.load_icon(self.con, pick) if old.endswith("…") else None)
        self._forget_icon(old)
        self._forget_icon(pick)
        for k in self.live.get_children():
            if self.live.set(k, "item") == old:
                self.live.set(k, "item", pick)
                self.live.item(k, image=self.icon_for(pick))
        return n

    def review_accept(self, rows=None):
        rows, nxt = (rows, None) if rows is not None else self._review_selected()
        done = [(r["item"], r["suggest"]) for r in rows if r["suggest"]]
        if not done:
            self._set_status("No suggestion to accept; use Match (M) to pick one by hand.")
            return
        n = sum(self._apply_match(old, pick, self.review_remember.get()) for old, pick in done)
        self.refresh_tables()
        self.fetch_icons_async([p for _, p in done])
        self._set_status(f"Matched {len(done)} name{'s' if len(done) != 1 else ''} ({n} listings)"
                         + (": " + ", ".join(f"{o} → {p}" for o, p in done[:3]) if len(done) <= 3 else "."))
        if nxt:
            self._review_select(nxt)

    def review_accept_confident(self):
        rows = [r for r in self.review_rows.values() if r["suggest"] and r["score"] >= 0.9 and not r["kept"]]
        if not rows:
            self._set_status("No suggestions are 90% alike or better.")
            return
        listing = "\n".join(f"{r['item']}  →  {r['suggest']}" for r in rows[:15]) + \
            (f"\n… and {len(rows) - 15} more" if len(rows) > 15 else "")
        if messagebox.askyesno("Accept matches", f"Match these {len(rows)} names?\n\n{listing}"):
            self.review_accept(rows)

    def review_keep(self):
        rows, nxt = self._review_selected()
        if not rows:
            return
        kept = set(mfm.load_kept())
        unkeep = all(r["kept"] for r in rows)
        for r in rows:
            (kept.discard if unkeep else kept.add)(r["item"])
            self._forget_icon(r["item"])
        mfm.save_kept(kept)
        self.refresh_tables()
        what = f"'{rows[0]['item']}'" if len(rows) == 1 else f"{len(rows)} names"
        self._set_status(f"{what} {'back in review' if unkeep else 'kept as is (no ! badge)'}.")
        if nxt:
            self._review_select(nxt)

    def review_delete(self):
        rows, nxt = self._review_selected()
        if not rows:
            return
        n_list = sum(r["count"] for r in rows)
        names = "\n".join(r["item"] for r in rows[:12]) + (f"\n… and {len(rows) - 12} more" if len(rows) > 12 else "")
        if not messagebox.askyesno("Delete listings", f"Delete all {n_list} listings of\n\n{names}?"):
            return
        for r in rows:
            mfm.forget_item(self.con, r["item"])
            self._forget_icon(r["item"])
        self.refresh_tables()
        self._set_status(f"Deleted {n_list} listings of {len(rows)} name{'s' if len(rows) != 1 else ''}.")
        if nxt:
            self._review_select(nxt)

    def review_delete_all(self):
        """Delete the listings of every name in Review → Shop listings."""
        rows = [r for r in (self._tables_data or {}).get("review", [])
                if self.review_show_kept.get() or not r["kept"]]
        if not rows:
            self.root.bell()
            return
        n_list = sum(r["count"] for r in rows)
        if not messagebox.askyesno("Remove all listings",
                                   f"Delete all {n_list:,} listings of the {len(rows):,} names in this "
                                   "list?", parent=self.root):
            return
        for r in rows:
            mfm.forget_item(self.con, r["item"])
            self._forget_icon(r["item"])
        self.refresh_tables()
        self._set_status(f"Deleted {n_list:,} listings of {mfm.plural(len(rows), 'name')}.")

    # ------------------------------------------------------------------ ignore list
    def _build_ignore_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="  Ignored  ")
        ttk.Label(tab, text="Items here are never shown as deals. Their prices are still logged, "
                            "and watched items still get alerts."
                  , wraplength=1000, justify="left").pack(anchor="w", pady=(0, 6))
        add = ttk.Frame(tab)
        add.pack(fill="x")
        ttk.Label(add, text="Item:").pack(side="left")
        box = ItemSearch(add, self, width=42, on_return=self.add_ignore_from_form)
        self.ignore_box = box
        self.ignore_item = box.var
        box.pack(side="left", padx=4)
        ttk.Button(add, text="Ignore", command=self.add_ignore_from_form).pack(side="left", padx=4)
        self.ignore_tree = make_tree(tab, ("item",))
        self.ignore_tree.frame.pack(fill="both", expand=True, pady=6)
        self.ignore_tree.bind("<Double-1>", lambda e: self.open_history(self.ignore_tree))
        self._list_menu(self.ignore_tree, [
            ("Show history", self.open_history, True),
            None,
            ("Remove item", lambda t: self.unignore_selected(), False)])
        self.ignore_tree.bind("<Delete>", lambda e: self.unignore_selected())
        self.ignore_tree.bind("<BackSpace>", lambda e: self.unignore_selected())
        bottom = ttk.Frame(tab)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Stop ignoring selected", command=self.unignore_selected).pack(side="left")
        self.refresh_ignored()

    def refresh_ignored(self):
        if not hasattr(self, "ignore_tree"):
            return
        t = self.ignore_tree
        restore = self._keep_selection(t, lambda tr, k: tr.set(k, "item"))
        t.delete(*t.get_children())
        for item in sorted(self.ignored, key=str.lower):
            t.insert("", "end", image=self.icon_for(item), values=(item,))
        restore()

    def set_ignored(self, items, on: bool):
        items = [i for i in items if i]
        if on:
            self.ignored.update(items)
        else:
            self.ignored.difference_update(items)
        mfm.save_ignorelist(self.ignored)
        for k in self.live.get_children():   # re-tag the current shop's rows
            if self.live.set(k, "item") in items:
                tags = [t for t in self.live.item(k, "tags") if t not in ("ignored", "deal", "high", "unknown")]
                if on and "sold" not in tags:
                    tags.append("ignored")
                elif not on:
                    tags.append(tag_for(self.live.set(k, "verdict")))
                self.live.item(k, tags=tags)
        self.refresh_tables()
        what = f"'{items[0]}'" if len(items) == 1 else f"{len(items)} items"
        self._set_status(f"{'Ignoring deals on' if on else 'No longer ignoring'} {what}.")

    def add_ignore_from_form(self):
        name = self.ignore_item.get().strip()
        if not name:
            return
        if mfm.has_item_list() and not mfm.is_known_item(name):
            sugg = mfm.suggest_items(name, 1)
            if sugg and messagebox.askyesno("Item name", f"'{name}' isn't in the item database.\n\n"
                                            f"Use '{sugg[0]}' instead?"):
                name = sugg[0]
        self.set_ignored([name], True)
        self.ignore_box.set("")

    def unignore_selected(self):
        items = self._selected_items(self.ignore_tree)
        if items:
            self.set_ignored(items, False)

    def _save_sound_prefs(self):
        self.cfg["alert_sound_on"] = self.sound_on.get()
        self.cfg["alert_raise"] = self.raise_on.get()
        mfm.save_config(self.cfg)

    def test_sound(self):
        if not mfm.play_sound(self.cfg.get("alert_sound") or None):
            self.root.bell()
            self._set_status("No sound player found; used the system bell instead.")

    def choose_sound(self):
        path = filedialog.askopenfilename(title="Alert sound (.wav)",
                                          filetypes=[("WAV audio", "*.wav"), ("All files", "*")])
        if path:
            self.cfg["alert_sound"] = path
        elif self.cfg.get("alert_sound") and messagebox.askyesno("Alert sound", "Go back to the default ping?"):
            self.cfg["alert_sound"] = None
        mfm.save_config(self.cfg)
        self.refresh_alerts()

    def _selected_items(self, tree) -> list[str]:
        """Distinct item names of the selected rows, in table order."""
        return list(dict.fromkeys(tree.set(k, "item") for k in tree.selection()))

    def _add_menu(self, tree):
        """Right-click menu for any item table; acts on every selected row."""
        menu = tk.Menu(self.root, tearoff=0)

        def build():
            menu.delete(0, "end")
            rows, items = tree.selection(), self._selected_items(tree)
            one = len(rows) == 1
            n_items = "this item" if len(items) == 1 else f"{len(items)} items"
            single = "normal" if one else "disabled"
            menu.add_command(label="Match to database item", state=single,
                             command=lambda: self.match_dialog(tree))
            menu.add_command(label="Show history", state=single, command=lambda: self.open_history(tree))
            menu.add_command(label="Show shop photo", command=lambda: self.show_shot_for(tree),
                             state="normal" if one and self._listing_of(tree, rows[0]) else "disabled")
            menu.add_command(label="Look up on meowdb", state=single, command=lambda: self.open_meowdb(tree))
            menu.add_separator()
            menu.add_command(label="Watch this item" if len(items) == 1 else f"Watch these {len(items)} items",
                             command=lambda: self.alert_from_table(tree))
            if tree is not self.items:
                has_note = one and bool(tree.set(rows[0], "notes"))
                menu.add_command(label="Edit note" if has_note else "Add note", state=single,
                                 command=lambda: self.note_from_table(tree))
                menu.add_command(label="Watch this listing", state=single,
                                 command=lambda: self.watch_listing_from_table(tree))
            if all(i in self.ignored for i in items):
                menu.add_command(label=f"Stop ignoring {n_items}",
                                 command=lambda: self.set_ignored(items, False))
            else:
                menu.add_command(label=f"Ignore deals on {n_items}",
                                 command=lambda: self.set_ignored(items, True))
            menu.add_separator()
            if tree is self.items:
                menu.add_command(label=f"Delete all listings of {n_items}", command=self.forget_selected)
            else:
                menu.add_command(label="Delete this listing (bad read)" if one
                                 else f"Delete {len(rows)} listings (bad reads)",
                                 command=lambda: self.delete_listing(tree))
            menu.add_separator()
            menu.add_command(label="Select all  (Ctrl+A)",
                             command=lambda: tree.selection_set(tree.get_children("")))

        def popup(e):
            row = tree.identify_row(e.y)
            if row:
                if row not in tree.selection():   # right-clicking inside a selection keeps it
                    tree.selection_set(row)
                build()
                menu.tk_popup(e.x_root, e.y_root)
        bind_right_click(tree, popup)

    def match_dialog(self, tree):
        """Pick the right meowdb item for a misread name. Renames its listings and
        remembers the match so future reads of the same text are corrected."""
        sel = tree.selection()
        if not sel:
            return
        old = tree.set(sel[0], "item")
        if not mfm.has_item_list():
            messagebox.showinfo("No item list", "Download the item list first "
                                "(Settings → Game data → Update).")
            return
        win = self.window()
        win.title("Match to database item")
        win.transient(self.root)
        win.geometry("540x520")
        ttk.Label(win, text="Read as:", padding=(12, 10, 12, 0)).pack(anchor="w")
        ttk.Label(win, text="  " + old, image=self.icon_for(old, 48), compound="left",
                  style="Big.TLabel", padding=(12, 2)).pack(anchor="w")
        ttk.Label(win, text="Search database items (type to filter):", padding=(12, 8, 12, 2)).pack(anchor="w")
        q = tk.StringVar(value=old.rstrip("…").rstrip(": "))
        entry = ttk.Entry(win, textvariable=q)
        entry.pack(fill="x", padx=12)
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12, pady=6)
        results = IconList(frame, self, height=8, width=470)
        lb = results.tree
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        scope = self.scopes.get((str(tree), sel[0]))
        # a real item name that was wrong once (cursor, smudge) should only fix this
        # listing and not be remembered, or every genuine "Topaz" would be renamed too
        one_off = bool(scope) and mfm.is_known_item(old)
        only_this = tk.BooleanVar(value=one_off)
        if scope:
            where = scope.get("label") or " ".join(x for x in (scope.get("shop"), f"({scope.get('seller')})"
                                                              if scope.get("seller") else "") if x)
            ttk.Checkbutton(win, text=f"Only this listing ({mfm.fmt(scope['price'])} at {where or 'this shop'}),"
                                      " not every listing of this item", variable=only_this
                            ).pack(anchor="w", padx=12)
        remember = tk.BooleanVar(value=not one_off)
        ttk.Checkbutton(win, text="Remember: always read this text as the chosen item",
                        variable=remember).pack(anchor="w", padx=12)

        def refill(*_):
            results.set_names(mfm.suggest_items(q.get() or old))
        q.trace_add("write", refill)
        refill()

        def apply(*_):
            pick = results.selected()
            if not pick:
                return
            n = mfm.rename_item(self.con, old, pick, scope if only_this.get() else None)
            if remember.get():
                mfm.add_alias(old, pick, mfm.load_icon(self.con, pick) if old.endswith("…") else None)
            for k in list(self.icons):
                if k.startswith(old + "@") or k.startswith(pick + "@"):
                    self.icons.pop(k, None)
            for t in (self.live,):
                for k in t.get_children():
                    if t.set(k, "item") == old and (not only_this.get() or t is not tree or k == sel[0]):
                        t.set(k, "item", pick)
                        t.item(k, image=self.icon_for(pick))
            self.refresh_tables()
            self.fetch_icons_async([pick])   # its official icon, if not downloaded yet
            self._set_status(f"Matched '{old}' to '{pick}' ({n} listings)"
                             + ("; future reads will match automatically." if remember.get() else "."))
            win.destroy()
        lb.bind("<Double-1>", apply)
        lb.bind("<Return>", apply)
        entry.bind("<Return>", apply)

        def move(d):
            kids = lb.get_children()
            if not kids:
                return "break"
            cur = lb.selection()[0] if lb.selection() else kids[0]
            i = max(0, min(len(kids) - 1, kids.index(cur) + d))
            lb.selection_set(kids[i])
            lb.see(kids[i])
            return "break"
        entry.bind("<Down>", lambda e: move(1))
        entry.bind("<Up>", lambda e: move(-1))
        btns = ttk.Frame(win, padding=(12, 4, 12, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Match", command=apply).pack(side="right", padx=6)
        entry.focus_set()
        entry.select_range(0, "end")

    def delete_listing(self, tree):
        """Remove the selected shops' listings (bad reads) without touching other
        listings of the same items."""
        rows = [(k, tree.set(k, "item"), self.scopes.get((str(tree), k))) for k in tree.selection()]
        rows = [r for r in rows if r[2]]
        if not rows:
            return
        if len(rows) == 1:
            k, item, scope = rows[0]
            q = f"Delete this listing of '{item}' ({mfm.fmt(scope['price'])})?"
        else:
            names = sorted({r[1] for r in rows})
            q = (f"Delete these {len(rows)} listings?\n\n" + "\n".join(names[:12])
                 + (f"\n… and {len(names) - 12} more" if len(names) > 12 else ""))
        if not messagebox.askyesno("Delete listings", q + "\n\nOther listings of the same items are kept."):
            return
        n = 0
        for k, item, scope in rows:
            n += mfm.forget_item(self.con, item, scope)
            if tree is self.live:
                tree.delete(k)
            self._forget_icon(item)
        self.refresh_tables()
        self._set_status(f"Deleted {n} listing{'s' if n != 1 else ''}"
                         + (f" of '{rows[0][1]}'." if len(rows) == 1 else "."))

    def open_meowdb(self, tree):
        import webbrowser

        sel = tree.selection()
        if sel:
            webbrowser.open(mfm.meowdb_url(tree.set(sel[0], "item")))

    def import_item_list(self, parent=None):
        return self._import_game_data("items", parent)

    GAME_DATA = {
        "items": ("item list", "mfm-tracker item list", mfm.import_items, mfm.export_items),
        "maps": ("map list", "mfm-tracker map list", mfm.import_maps, mfm.export_maps),
    }

    def _import_game_data(self, kind, parent=None):
        what, _, do_import, _ = self.GAME_DATA[kind]
        path = filedialog.askopenfilename(
            parent=parent, title=f"Import {what}",
            filetypes=[(what.capitalize() + " files",
                        "*.zip *.json *.csv *.txt" if kind == "items" else "*.json *.csv *.txt"),
                       ("All files", "*")])
        if not path:
            return None
        try:
            n = do_import(path)
        except Exception as e:
            self.report_error(f"Importing the {what} failed", f"{path}\n\n{e}")
            return None
        if kind == "items":
            mfm._Dictionary.cache = None
            self.on_icons_ready(None)
        self._set_status(f"Imported a {what} of {n:,} {'names' if kind == 'items' else 'maps'} "
                         f"from {Path(path).name}.")
        return n

    def _export_game_data(self, kind, parent=None):
        what, default, _, do_export = self.GAME_DATA[kind]
        types = [("JSON: the whole list, can be imported again", "*.json"),
                 ("CSV: the whole list, opens in a spreadsheet", "*.csv"),
                 ("Text: names only", "*.txt")]
        if kind == "items":
            types.insert(0, ("ZIP: the whole list plus every item icon", "*.zip"))
        ext = types[0][1][1:]
        path = filedialog.asksaveasfilename(
            parent=parent, title=f"Export {what}", initialfile=mfm.stamped(default + ext),
            defaultextension=ext, filetypes=types)
        if not path:
            return
        try:
            n = do_export(path)
        except Exception as e:
            self.report_error(f"Exporting the {what} failed", f"{path}\n\n{e}")
            return
        self._set_status(f"Exported the {what} ({n:,}) to {path}")

    def _game_data_frame(self, parent):
        """Settings section for the reference lists the app reads the game with."""
        box = ttk.LabelFrame(parent, text=" Game data ", padding=(10, 6))
        wrap = 850
        grey = {"foreground": "#666", "wraplength": wrap, "justify": "left"}

        def status_items():
            i = mfm.items_info()
            if not i["names"]:
                return "Not installed: item names won't be spell-checked. Use Update."
            have, total = mfm.icon_status(self.con)
            return (f"{i['names']:,} item names  ·  {i['source'] or 'imported list'}"
                    + (f"  ·  icons {have:,} / {total:,}" if total else ""))

        def status_maps():
            m = mfm.maps_info()
            return (f"{m['maps']:,} maps  ·  {m['source']}" if m["maps"] else
                    "Not installed: misread map names won't be corrected. Use Update.")

        def status_osms():
            o = mfm.osms_info()
            if not o["items"] and not o["maps"]:
                return "OSMS Data Explorer: not downloaded yet (Update gets it)"
            return (f"OSMS Data Explorer  ·  adds {mfm.plural(o['extra_items'], 'item')} and "
                    f"{mfm.plural(o['extra_maps'], 'map')} meowdb doesn't have"
                    + (f"  ·  checked {o['checked']}" if o["checked"] else ""))

        rows = {}

        def refresh():
            for key, fn in (("items", status_items), ("maps", status_maps), ("osms", status_osms)):
                try:
                    rows[key].config(text=fn())
                except tk.TclError:
                    pass       # window closed

        top = ttk.Frame(box)
        top.pack(fill="x")
        b = ttk.Button(top, text="Update",
                       command=lambda: self.download_meowdb(rows["items"], force=True, then=refresh))
        b.pack(side="left")
        tip("Downloads new items and maps.", b)
        b = ttk.Button(top, text="Full refresh",
                       command=lambda: self.download_meowdb(rows["items"], full=True, force=True, then=refresh))
        b.pack(side="left", padx=6)
        tip("Downloads the whole item and map lists again.", b)
        ttk.Label(top, foreground="#666", wraplength=wrap - 300, justify="left", text=(
            "Item and map lists from NiaMeowDB, with OSMS Data Explorer as a backup.")
                  ).pack(side="left", padx=(10, 0))
        search_icons = tk.BooleanVar(value=self.cfg.get("search_icons", True))

        def toggle_search_icons():
            self.cfg["search_icons"] = search_icons.get()
            mfm.save_config(self.cfg)
        all_icons = tk.BooleanVar(value=self.cfg.get("download_all_icons", True))

        def toggle_icons():
            self.cfg["download_all_icons"] = all_icons.get()
            mfm.save_config(self.cfg)
        cb = ttk.Checkbutton(box, variable=all_icons, command=toggle_icons, text=(
            "Update also downloads every item icon (about 10 minutes the first time)"))
        cb.pack(anchor="w", pady=(2, 0))
        tip("When off, icons are downloaded only for items you see.", cb)
        sections = [
            ("items", "Item list", status_items,
             "Corrects misread item names and gives each item its icon, category, level, class "
             "and NPC prices.",
             "Files in cache/: items.txt, meowdb_items.json, item_ids.json, item_types.json, "
             "item_icons.json, npc_shops.json, icons.db"),
            ("maps", "Map list", status_maps,
             "Corrects misread map names.",
             "File in cache/: maps.json"),
        ]
        for i, (kind, title, fn, purpose, files) in enumerate(sections):
            f = ttk.Frame(box)
            f.pack(fill="x", pady=(10, 0))
            top = ttk.Frame(f)
            top.pack(fill="x")
            ttk.Label(top, text=title, style="Bold.TLabel").pack(side="left")
            rows[kind] = ttk.Label(top, text=fn(), foreground="#1f6f3a")
            rows[kind].pack(side="left", padx=(10, 0))
            b = ttk.Frame(top)
            b.pack(side="right")
            ttk.Label(f, text=purpose, wraplength=wrap, justify="left").pack(anchor="w", pady=(2, 0))
            ttk.Label(f, text=files, **grey).pack(anchor="w")
            if kind == "items":
                cb = ttk.Checkbutton(f, text="Show icons in item search", variable=search_icons,
                                     command=toggle_search_icons)
                cb.pack(anchor="w", pady=(2, 0))
                tip("Shows item pictures in item search.", cb)

            def imp(kind=kind):
                if self._import_game_data(kind, parent):
                    refresh()
            bi = ttk.Button(b, text="Import", command=imp)
            bi.pack(side="left")
            be = ttk.Button(b, text="Export", command=lambda kind=kind: self._export_game_data(kind, parent))
            be.pack(side="left", padx=(6, 0))
            noun = "item list" if kind == "items" else "map list"
            tip(f"Loads the {noun} from a file.", bi)
            tip(f"Saves the {noun} to a file"
                + (" (.zip includes every icon)." if kind == "items" else "."), be)
        bk = ttk.Frame(box)
        bk.pack(fill="x", pady=(10, 0))
        bk_title = ttk.Label(bk, text="Backup source", style="Bold.TLabel")
        bk_title.pack(side="left")
        rows["osms"] = ttk.Label(bk, text=status_osms(), foreground="#1f6f3a")
        rows["osms"].pack(side="left", padx=(10, 0))
        tip("Items, maps and icons that NiaMeowDB doesn't have. Files in cache/: "
            "osms_items.json, osms_maps.json.", bk_title, rows["osms"])
        ttk.Label(box, **grey, text=(
            "Export as .json or .csv for a full copy, .zip to include icons, .txt for names only."
            )).pack(anchor="w", pady=(10, 0))
        return box

    def offer_item_list(self):
        if messagebox.askyesno(
                "Item names",
                "Download the MapleStory Classic item and map lists?\n\n"
                "They correct misread names and add item icons. You can also do this later "
                "in Settings."):
            self.download_meowdb()

    def offer_type_refresh(self):
        if messagebox.askyesno(
                "Update item list",
                "Download the item and map lists again? This version needs them for the Items "
                "filters and NPC prices."):
            self.download_meowdb()

    def download_meowdb(self, label=None, full=False, force=False, then=None):
        """Fetch the item list in the background so the window stays responsive."""
        def say(text):
            self.events.put(("status", text))
            if label is not None:
                self.events.put(("label", (label, text)))

        def work():
            meow_error, meow_msg = None, ""
            try:
                r = mfm.update_items_meowdb(lambda k: say(f"Updating items from meowdb… {k:,}"),
                                            full=full, force=force)
                meow_msg = r.message + (" Map list updated." if r.maps_updated else "")
                say(meow_msg)
            except Exception as e:
                meow_error = e
            try:      # the backup source, also (especially) when meowdb failed
                say("Checking the backup source (OSMS Data Explorer)…")
                osms_msg = mfm.update_osms(force=force or full or meow_error is not None)
            except Exception as e:
                osms_msg = f"Backup source not updated: {e}"
            if meow_error is None:
                say(meow_msg + "   " + osms_msg)
            if then:
                self.events.put(("call", then))
            if meow_error is not None:
                say(f"meowdb download failed: {meow_error}   {osms_msg}")
                self.events.put(("download_error", str(meow_error) + (
                    "\n\nThe backup lists were updated." if not osms_msg.startswith("Backup source not") else "")))
                return
            try:
                if self.cfg.get("download_all_icons", True):
                    self.download_all_icons(label, then)
                con = mfm.db()
                seen = [r[0] for r in con.execute("SELECT DISTINCT item FROM listings")]
                if seen and mfm.fetch_official_icons(con, seen):
                    self.events.put(("icons_ready", seen))
            except Exception as e:
                say(f"meowdb download failed: {e}")
                self.events.put(("download_error", str(e)))
        say("Checking meowdb for updates…")
        threading.Thread(target=work, daemon=True).start()

    def download_all_icons(self, label=None, then=None):
        """Every meowdb icon not downloaded yet, ~2 per second, in the background.
        Resumes where it stopped (closing the app stops it)."""
        if getattr(self, "_icons_busy", False):
            return
        self._icons_busy = True
        self._icons_stop = threading.Event()

        def say(text):
            self.events.put(("status", text))
            if label is not None:
                self.events.put(("label", (label, text)))

        def work():
            con = mfm.db()
            try:
                have, total = mfm.icon_status(con)
                if have >= total:
                    return

                full_rate = float(self.cfg.get("icon_rate", 10))

                def progress(i, n, rate):
                    if i % 20 == 0 or i == n:
                        left = (n - i) / max(rate, 0.1) / 60
                        say(f"Downloading item icons from meowdb… {have + i:,} / {total:,}"
                            + (f", about {left:.0f} min left" if left >= 1 else "")
                            + (f" (meowdb asked to slow down: {rate:.1f}/s)" if rate < full_rate * 0.95 else ""))
                    if i % 500 == 0:
                        self.events.put(("icons_ready", None))
                got, bad = mfm.download_all_icons(con, progress, self._icons_stop, rate=full_rate)
                if not self._icons_stop.is_set():
                    say(f"Item icons: downloaded {got:,}" + (f" ({bad} unavailable)" if bad else "")
                        + f"; {mfm.icon_status(con)[0]:,} of {total:,} here.")
            except Exception as e:
                say(f"Icon download stopped: {e}")
            finally:
                self._icons_busy = False
                self.events.put(("icons_ready", None))
                if then:
                    self.events.put(("call", then))
                con.close()
        threading.Thread(target=work, daemon=True).start()

    def on_download_error(self, msg):
        self.report_error("meowdb download failed", msg)

    def on_call(self, fn):
        fn()

    def on_status(self, text):
        self._set_status(text)

    def on_label(self, p):
        lbl, text = p
        try:
            lbl.config(text=text)
        except tk.TclError:
            pass  # settings window was closed

    def on_icons_ready(self, items):
        if items is None:           # many new icons: drop every cached thumbnail
            # (keep the old images alive: open windows may still be showing them)
            self._retired_icons = getattr(self, "_retired_icons", []) + list(self.icons.values())
            self.icons.clear()
            self._no_icon.clear()
        for item in items or ():
            self._forget_icon(item)
        self.icon_lists = [l for l in self.icon_lists if l.alive()]
        for l in self.icon_lists:
            l.refresh_images()
        for k in self.live.get_children():
            self.live.item(k, image=self.icon_for(self.live.set(k, "item")))
        self.refresh_tables()
        self.refresh_megaphones()
        self.refresh_megaphone_review()

    def request_icons(self, names):
        """Download meowdb icons for search suggestions that have none yet: one at a
        time, at most 2 per second, each item only once (and never again after)."""
        if not self.cfg.get("search_icons", True) or not mfm.ITEM_ICONS_PATH.exists():
            return
        for n in names:
            if n in self._icon_asked:
                continue
            self._icon_asked.add(n)
            if mfm.load_icon(self.con, n) is None:
                self._icon_queue.put(n)
        if self._icon_worker is None or not self._icon_worker.is_alive():
            def work():
                con = mfm.db()
                while True:
                    try:
                        n = self._icon_queue.get(timeout=5)
                    except queue.Empty:
                        return
                    try:
                        if mfm.fetch_official_icons(con, [n], delay=0.5):
                            self.events.put(("search_icon", n))
                    except Exception:
                        pass
            self._icon_worker = threading.Thread(target=work, daemon=True)
            self._icon_worker.start()

    def on_search_icon(self, item):
        self._forget_icon(item)
        self.icon_lists = [l for l in self.icon_lists if l.alive()]
        for l in self.icon_lists:
            l.refresh_images()

    def fetch_icons_async(self, items):
        if not mfm.ITEM_ICONS_PATH.exists() or not items:
            return

        def work():
            if mfm.fetch_official_icons(mfm.db(), items):
                self.events.put(("icons_ready", items))
        threading.Thread(target=work, daemon=True).start()

    def icon_for(self, item: str, size: int = 30, found: bool = False):
        """Thumbnail for a table row (cached). Items that don't match anything in the
        meowdb list get a small red "!" badge; '' when there's nothing to show.
        (Watched items found for sale are shown by highlighting the whole row: the
        "watch" tag; `found` is no longer drawn on the icon.)"""
        found = False
        warn = mfm.needs_review(item)
        key = f"{item}@{size}{'!' if warn else ''}{'g' if found else ''}"
        if key not in self.icons:
            if item in self._no_icon and not warn and not found:
                return ""   # looked up before, nothing saved: skip the queries (two per row)
            img = mfm.load_icon(self.con, item)
            if img is None:
                self._no_icon.add(item)
                if not warn and not found:
                    return ""
            else:
                self._no_icon.discard(item)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            if img is not None:
                img = img.convert("RGBA")
                img.thumbnail((size, size), Image.LANCZOS)
                canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
            if warn:
                self._draw_warning(canvas)
            if found:
                from PIL import ImageDraw
                d = ImageDraw.Draw(canvas)
                for i in range(2):   # 2 px green frame
                    d.rectangle((i, i, size - 1 - i, size - 1 - i), outline=(30, 190, 70, 255))
            photo = ImageTk.PhotoImage(canvas)
            if img is None:
                # No thumbnail saved yet (e.g. the alert fired before the icon was stored):
                # don't cache the empty frame, so the real icon shows on the next refresh.
                self._uncached = getattr(self, "_uncached", [])[-50:] + [photo]   # keep a ref for Tk
                return photo
            self.icons[key] = photo
        return self.icons[key]

    def _forget_icon(self, item):
        self._no_icon.discard(item)
        for k in list(self.icons):
            if k.startswith(item + "@"):
                self.icons.pop(k, None)

    @staticmethod
    def _draw_warning(canvas, r=None):
        """Red circle with a white "!" in the top-right corner."""
        from PIL import ImageDraw

        s = canvas.width
        r = r or max(6, s * 13 // 30)             # badge diameter
        x0, y0 = s - r, 0
        d = ImageDraw.Draw(canvas)
        d.ellipse((x0, y0, x0 + r - 1, y0 + r - 1), fill=(214, 40, 40, 255), outline=(255, 255, 255, 255))
        cx = x0 + r / 2
        bar_w = max(1.0, r / 7)
        d.rectangle((cx - bar_w / 2, y0 + r * 0.2, cx + bar_w / 2, y0 + r * 0.58), fill=(255, 255, 255, 255))
        d.rectangle((cx - bar_w / 2, y0 + r * 0.68, cx + bar_w / 2, y0 + r * 0.8), fill=(255, 255, 255, 255))

    def _legend_badge(self):
        canvas = Image.new("RGBA", (15, 15), (0, 0, 0, 0))
        self._draw_warning(canvas, r=15)
        return canvas

    def forget_selected(self):
        items = self._selected_items(self.items)
        if not items:
            return
        shown = "\n".join(items[:12]) + (f"\n… and {len(items) - 12} more" if len(items) > 12 else "")
        if messagebox.askyesno("Delete items", f"Delete all logged listings of\n\n{shown}?"):
            n = sum(mfm.forget_item(self.con, item) for item in items)
            for item in items:
                self._forget_icon(item)
            self.refresh_tables()
            self._set_status(f"Deleted {n} listings of {len(items)} item{'s' if len(items) != 1 else ''}.")

    # ------------------------------------------------------------------ dialogs
    def calibrate(self, then_start=False, key="region", hide=(), on_set=None):
        """Draw a capture area on a screenshot. `hide`: other windows to hide meanwhile
        (e.g. Settings); `on_set()` runs once the area is saved."""
        was_running = bool(self.worker and self.worker.is_alive())
        hide = [w for w in hide if w is not None and w.winfo_exists()]
        for w in hide:
            w.withdraw()
        self.root.withdraw()
        self.root.update()

        def show():
            self.root.deiconify()
            for w in hide:
                if w.winfo_exists():
                    w.deiconify()
                    w.lift()

        def grab():
            try:
                shot = mfm.grab_full_screen()
                shot_mode = mfm.GRAB_MODE[0]   # (the monitor's next frame may change it)
            except SystemExit as e:  # missing Wayland screenshot tool
                show()
                messagebox.showerror("Can't capture screen", str(e))
                return

            def done(region):
                show()
                if region:
                    self.cfg[key] = region  # monitor shares this dict, picks it up next frame
                    # drawn on the game's window or the whole screen: only used on the same
                    self.cfg["area_modes"] = {**(self.cfg.get("area_modes") or {}), key: shot_mode}
                    self.cfg["area_sizes"] = {**(self.cfg.get("area_sizes") or {}), key: list(shot.size)}
                    mfm.save_config(self.cfg)
                    self._update_stats(None, None)
                    self._set_status(f"{AREA_NAMES.get(key, key)} set to {area_text(region)}.")
                    if on_set:
                        on_set()
                    if then_start and not was_running:
                        self.start()

            mfm.calibrate(parent=self.root, shot=shot, on_done=done, key=key)

        # give the window manager a moment to hide us so we're not in the screenshot
        self.root.after(600, grab)

    def import_ref(self):
        path = filedialog.askopenfilename(title="Reference prices CSV (item,price)",
                                          filetypes=[("CSV", "*.csv"), ("All files", "*")])
        if not path:
            return
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                ok = {"item", "price"} <= set(csv.DictReader(f).fieldnames or [])
            if not ok:
                raise ValueError("The CSV needs 'item' and 'price' column headers.")
            mfm.import_ref(path)
            self.refresh_tables()
            self._set_status(f"Imported reference prices from {path}")
        except Exception as e:
            messagebox.showerror("Import failed", str(e))

    def export_prices(self):
        """Save prices as CSV: one row per item (re-importable) or every sighting."""
        win = self.window()
        win.title("Export prices")
        win.transient(self.root)
        win.resizable(False, False)
        kind = tk.StringVar(value="market")
        ttk.Label(win, text="Export a CSV file (opens in any spreadsheet app):",
                  padding=(14, 12, 14, 6)).pack(anchor="w")
        ttk.Radiobutton(win, variable=kind, value="market", text=(
            "Market prices: one row per item. Can be loaded again with Import prices."
            )).pack(anchor="w", padx=14, pady=2)
        ttk.Radiobutton(win, variable=kind, value="listings", text=(
            "Every sighting: one row per listing seen."
        )).pack(anchor="w", padx=14, pady=2)

        def go():
            default = mfm.stamped(f"mfm-tracker {'market prices' if kind.get() == 'market' else 'sightings'}.csv")
            path = filedialog.asksaveasfilename(parent=win, title="Export prices", defaultextension=".csv",
                                                initialfile=default, filetypes=[("CSV", "*.csv")])
            if not path:
                return
            try:
                n = mfm.export_prices(self.con, path, self.cfg, kind.get())
            except Exception as e:
                messagebox.showerror("Export failed", str(e), parent=win)
                return
            win.destroy()
            self._set_status(f"Exported {n} rows to {path}")
        btns = ttk.Frame(win, padding=(14, 10, 14, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Export", command=go).pack(side="right", padx=6)

    def _confirm_twice(self, parent, title, first, second) -> bool:
        """Deleting anything takes two separate confirmations."""
        if not messagebox.askyesno(title, first, parent=parent, icon="warning", default="no"):
            return False
        return messagebox.askyesno(title, second, parent=parent, icon="warning", default="no")

    def clear_prices(self, parent):
        n = self.con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        r = self.con.execute("SELECT COUNT(*) FROM reference").fetchone()[0]
        if not self._confirm_twice(
                parent, "Clear pricing data",
                f"Delete all pricing data?\n\n• {n:,} listings\n• {r:,} imported prices\n\n"
                "Settings, watched items and icons are kept.",
                f"Are you sure? All {n + r:,} prices will be deleted.\n\n"
                "A backup is saved in the data folder's backups folder."):
            return
        if self.stop_event and self.worker and self.worker.is_alive():
            self.stop()
        backup = mfm.clear_prices(self.con)
        self.live.delete(*self.live.get_children())
        self.icons.clear()
        self.refresh_tables()
        self._set_status(f"Pricing data cleared. Backup: {backup}")
        messagebox.showinfo("Pricing data cleared", f"Done. A backup was saved to:\n{backup}", parent=parent)

    def reset_personal(self, parent):
        win = self.window(parent)
        win.title("Reset personal settings")
        win.transient(parent)
        win.resizable(False, False)
        ttk.Label(win, text="Reset these to how they were when the app was new:",
                  padding=(14, 12, 14, 6)).pack(anchor="w")
        picks = {}
        for key, (label, _) in mfm.SETTINGS_FILES.items():
            picks[key] = tk.BooleanVar(value=True)
            ttk.Checkbutton(win, text=label, variable=picks[key]).pack(anchor="w", padx=14, pady=1)
        ttk.Label(win, foreground="#777", padding=(14, 6, 14, 0), text=(
            "Pricing data and the item database are not affected.")).pack(anchor="w")

        def go():
            parts = [k for k, v in picks.items() if v.get()]
            if not parts:
                return
            names = "\n".join("• " + mfm.SETTINGS_FILES[k][0] for k in parts)
            if not self._confirm_twice(
                    win, "Reset personal settings", f"Reset and delete:\n\n{names}?",
                    "Are you sure?\n\nA backup is saved in the data folder's backups folder."):
                return
            backup = mfm.reset_settings(parts)
            self.cfg = mfm.load_config()
            mfm.CAPTURE_CFG = self.cfg
            self.watch = mfm.load_watchlist()
            self.watched_listings = mfm.load_watched_listings()
            self.refresh_watched_listings()
            self.refresh_inventory()
            self.ignored = set(mfm.load_ignorelist())
            self.sound_on.set(self.cfg.get("alert_sound_on", True))
            self.raise_on.set(self.cfg.get("alert_raise", True))
            if self.monitor:
                self.monitor.cfg = self.cfg
            self.found_at = None
            self.icons.clear()
            self._update_stats(None, None)
            self.refresh_tables()
            win.destroy()
            if isinstance(parent, tk.Toplevel):
                try:
                    parent.destroy()   # the open Settings window shows the old values
                except tk.TclError:
                    pass
            self._set_status(f"Personal settings reset. Backup: {backup}")
            messagebox.showinfo("Settings reset", f"Done. A backup was saved to:\n{backup}")
        btns = ttk.Frame(win, padding=(14, 10, 14, 12))
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Reset", command=go).pack(side="right", padx=6)

    # ------------------------------------------------------------------ OCR training lines
    def _ocr_training_frame(self, win):
        """Settings → Reading → OCR training: collect chat line images, send the checked ones to
        the Shared Database server, and check the ones the reads didn't agree on."""
        box = ttk.LabelFrame(win, text=" OCR training ", padding=(10, 2, 10, 4))
        collect = tk.BooleanVar(value=bool(self.cfg.get("ocr_collect", False)))
        stores = tk.BooleanVar(value=bool(self.cfg.get("ocr_collect_stores", False)))
        players = tk.BooleanVar(value=bool(self.cfg.get("ocr_collect_players", False)))
        share = tk.BooleanVar(value=bool(self.cfg.get("ocr_share", True)))
        ttk.Label(box, text="Collect for training the OCR:").pack(anchor="w")
        kinds = ttk.Frame(box)
        kinds.pack(anchor="w", padx=(12, 0))
        c1 = ttk.Checkbutton(kinds, text="Chat lines", variable=collect)
        c1.pack(side="left")
        tip("Keeps each megaphone line the OCR reads, as a picture with what was read. When several "
            "reads agree, that's its text; the rest wait for you in Check lines.", c1)
        c3 = ttk.Checkbutton(kinds, text="Store names", variable=stores)
        c3.pack(side="left", padx=(12, 0))
        tip("Keeps each shop's title (the white bar at the top of the shop window) as a picture with "
            "what was read, once per shop.", c3)
        c4 = ttk.Checkbutton(kinds, text="Player names", variable=players)
        c4.pack(side="left", padx=(12, 0))
        tip("Keeps each shop owner's name (the dark box under the title) as a picture with what was "
            "read, once per shop. A name also seen in a megaphone counts as agreed.", c4)
        c2 = ttk.Checkbutton(box, text="Send them to the Shared Database server", variable=share)
        c2.pack(anchor="w")
        tip("Lines with agreed or checked text go to your Shared Database server; the server's admin "
            "exports them (mfm-server export-ocr) for training.", c2)
        row = ttk.Frame(box)
        row.pack(anchor="w", fill="x", pady=(2, 0))
        counts = ttk.Label(row, foreground="#555")
        counts.pack(side="left")
        ttk.Button(row, text="Check lines", command=self.ocr_lines_dialog).pack(side="right")

        def show():
            try:
                n = mfm.settle_ocr_lines(self.con)
            except Exception:
                n = {}
            counts.config(text=f"{n.get('unsure', 0):,} to check · {n.get('auto', 0):,} agreed · "
                               f"{n.get('fixed', 0):,} checked")
        show()
        box.refresh_counts = show
        self._ocr_counts_label = show
        return box, (collect, share, stores, players)

    def ocr_lines_dialog(self):
        """One line at a time: its picture, and its text to fix. Enter saves it as
        checked and goes to the next; Skip leaves it out of training."""
        old = getattr(self, "ocr_win", None)
        if old is not None and old.winfo_exists():
            old.deiconify()
            old.lift()
            return
        mfm.settle_ocr_lines(self.con)
        win = tk.Toplevel(self.root)
        self.ocr_win = win
        win.title("OCR lines")
        win.transient(self.root)
        top = ttk.Frame(win, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        which = tk.StringVar(value="unsure")
        for val, text in (("unsure", "To check"), ("auto", "Agreed"), ("fixed", "Checked"), ("skip", "Skipped")):
            ttk.Radiobutton(top, text=text, value=val, variable=which, command=lambda: load()).pack(side="left",
                                                                                              padx=(0, 10))
        where = ttk.Label(top, foreground="#555")
        where.pack(side="right")
        kind_names = {"all": "All", "chat": "Chat lines", "store": "Store names", "player": "Player names"}
        kind = tk.StringVar(value=kind_names["all"])
        kc = ttk.Combobox(top, textvariable=kind, values=list(kind_names.values()), state="readonly", width=13)
        kc.pack(side="right", padx=(0, 12))
        kc.bind("<<ComboboxSelected>>", lambda e: load())
        ttk.Label(top, text="Show:").pack(side="right", padx=(0, 4))
        ttk.Label(win, foreground="#555", wraplength=900, justify="left", padding=(12, 0, 12, 4), text=(
            "Type exactly what the picture says (keep spaces, brackets and capitals as shown), then press "
            "Enter. Skip leaves a line out of training: use it when you can't tell what it says.")).pack(anchor="w")
        pic = ttk.Label(win, relief="solid", anchor="w", padding=4)
        pic.pack(fill="x", padx=12, pady=(4, 6))
        read_lbl = ttk.Label(win, foreground="#777", padding=(12, 0))
        read_lbl.pack(anchor="w")
        text = tk.StringVar()
        entry = ttk.Entry(win, textvariable=text, font=("TkFixedFont", 14))
        entry.pack(fill="x", padx=12, pady=(2, 8))
        bottom = ttk.Frame(win, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")
        state = {"grps": [], "i": 0, "img": None}

        def load(keep: int = 0):
            k = next((key for key, name in kind_names.items() if name == kind.get()), "all")
            state["grps"] = [g for (g,) in self.con.execute(
                "SELECT grp FROM ocr_lines WHERE status = ? AND (? = 'all' OR kind = ?) GROUP BY grp "
                "ORDER BY MIN(id)", (which.get(), k, k))]
            state["i"] = min(keep, max(0, len(state["grps"]) - 1))
            show()

        def show():
            grps = state["grps"]
            if not grps:
                where.config(text="none")
                pic.config(image="", text="Nothing here." if which.get() != "unsure" else
                           "Nothing to check. Lines show up here a couple of minutes after they're read.")
                text.set("")
                read_lbl.config(text="")
                return
            g = grps[state["i"]]
            rows = self.con.execute("SELECT png, read, text, kind FROM ocr_lines WHERE grp = ? ORDER BY id",
                                    (g,)).fetchall()
            import io
            img = Image.open(io.BytesIO(rows[0][0]))
            # the whole line (without the blank chat box to its right), as big as the
            # screen allows; the window grows to fit it
            from PIL import ImageOps
            box = ImageOps.invert(img.convert("L")).point(lambda v: 255 if v > 60 else 0).getbbox()
            if box:
                img = img.crop((0, 0, min(img.width, box[2] + 30), img.height))
            fit = max(600, win.winfo_screenwidth() - 80)
            if img.width > fit:
                img = img.resize((fit, max(1, img.height * fit // img.width)), Image.LANCZOS)
            state["img"] = ImageTk.PhotoImage(img)
            pic.config(image=state["img"], text="")
            want = max(img.width + 40, 980)
            if win.winfo_width() < want:
                win.geometry(f"{want}x{max(300, win.winfo_height())}")
                win.update_idletasks()
                x = max(0, min(win.winfo_x(), win.winfo_screenwidth() - want))
                win.geometry(f"+{x}+{max(0, win.winfo_y())}")
            reads = sorted({r for _, r, _, _ in rows if r})
            read_lbl.config(text="Read as: " + "  |  ".join(reads[:3]) if len(reads) > 1 else "")
            text.set(rows[0][2] or rows[0][1] or "")
            where.config(text=f"{kind_names.get(rows[0][3], 'Chat lines')[:-1]}  ·  {state['i'] + 1} of {len(grps)}")
            entry.focus_set()
            entry.icursor("end")

        def step(d):
            if state["grps"]:
                state["i"] = max(0, min(len(state["grps"]) - 1, state["i"] + d))
                show()

        def mark(status):
            if not state["grps"]:
                return
            g = state["grps"][state["i"]]
            t = text.get().strip()
            if status == "fixed" and not t:
                return
            mfm.set_ocr_line(self.con, g, t if status == "fixed" else None, status)
            if status == which.get():
                step(1)                       # (re-saved in its own list: just move on)
            else:
                load(keep=state["i"])
            if callable(getattr(self, "_ocr_counts_label", None)):
                try:
                    self._ocr_counts_label()
                except tk.TclError:
                    pass

        ttk.Button(bottom, text="◀ Previous", command=lambda: step(-1)).pack(side="left")
        ttk.Button(bottom, text="Next ▶", command=lambda: step(1)).pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="Skip", command=lambda: mark("skip")).pack(side="right")
        ttk.Button(bottom, text="Save (Enter)", command=lambda: mark("fixed")).pack(side="right", padx=(0, 6))
        entry.bind("<Return>", lambda e: mark("fixed"))
        entry.bind("<Prior>", lambda e: step(-1))
        entry.bind("<Next>", lambda e: step(1))
        win.geometry("980x300")
        load()

    def _hotkeys_frame(self, parent):
        """Settings → General → Hotkeys: a key for each action, set by pressing it.
        Returns the frame and a function giving (on, {action: key})."""
        box = ttk.LabelFrame(parent, text=" Hotkeys ", padding=(10, 2, 10, 6))
        on = tk.BooleanVar(value=bool(self.cfg.get("hotkeys_on", False)))
        keys = hotkey_cfg(self.cfg)
        cb = ttk.Checkbutton(box, text="Keys that work while the game has focus", variable=on)
        cb.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        tip("The keys below do their action in whichever window has focus, the game's "
            "included. The game gets them too: pick keys it doesn't use.", cb)
        wayland = mfm.session_type() == "wayland"
        xwayland = wayland and bool(os.environ.get("DISPLAY"))
        try:
            import pynput  # noqa: F401
            have = True
        except ImportError:
            have = False
        usable = have and (not wayland or xwayland)
        if wayland and not xwayland:
            note = "Not available on Wayland without XWayland: apps can't see keys pressed in other windows."
        elif not have:
            note = "Needs pynput: run  pip install pynput  and open the app again."
        elif xwayland:
            note = ("On Wayland, the keys work while an X11 window has focus, like the game's "
                    "(run through Wine or Proton).")
        else:
            note = ""
        if note:
            ttk.Label(box, foreground="#666", wraplength=330, justify="left", text=note
                      ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 4))
        if not usable:
            cb.state(["disabled"])
        buttons = {}

        def show(name):
            buttons[name].config(text=keys[name] or "(none)")

        def grab(name):
            b = buttons[name]
            b.config(text="Press a key…")
            b.focus_set()
            self._grabbing_hotkey = True

            def done():
                self._grabbing_hotkey = False
                b.unbind("<KeyPress>")
                b.unbind("<FocusOut>")
                show(name)

            def pressed(e):
                if e.keysym in _MODIFIER_KEYSYMS:
                    return "break"            # wait for the key that goes with it
                if e.keysym != "Escape":
                    key = hotkey_from_event(e)
                    if key is None or not hotkey_spec(key):
                        self.root.bell()
                        return "break"
                    keys[name] = key
                done()
                return "break"
            b.bind("<KeyPress>", pressed)
            b.bind("<FocusOut>", lambda e: done())

        def clear(name):
            keys[name] = ""
            show(name)

        for i, (name, label) in enumerate(HOTKEY_ACTIONS, start=2):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", pady=1)
            b = ttk.Button(box, width=14, command=lambda n=name: grab(n))
            b.grid(row=i, column=1, padx=(8, 4), pady=1)
            x = ttk.Button(box, text="×", width=2, command=lambda n=name: clear(n))
            x.grid(row=i, column=2, pady=1)
            tip("Click, then press the key (with Ctrl, Alt or Shift if you like). "
                "Esc keeps the one set.", b)
            tip("No key for this action.", x)
            if not usable:
                b.state(["disabled"])
                x.state(["disabled"])
            buttons[name] = b
            show(name)
        return box, lambda: (on.get(), dict(keys))

    def _window_detection_frame(self, win):
        """Settings → Reading → Window detection: each capture area, found automatically or drawn
        by hand (with its coordinates), a button to draw it and one to go back to automatic."""
        box = ttk.LabelFrame(win, text=" Window detection ", padding=(10, 4, 10, 6))
        ttk.Label(box, foreground="#666", wraplength=700, justify="left", text=(
            "Each area is found on the screen by itself. If one isn't found, draw it: the app then "
            "reads only that part (coordinates in pixels of what's read: the game window, or the whole screen).")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        labels = {}
        # what's read: the game's window on its own (read even when covered), or the screen
        cap = ttk.Frame(box)
        cap.grid(row=100, column=0, columnspan=4, sticky="we", pady=(8, 0))
        use_win = tk.BooleanVar(value=bool(self.cfg.get("capture_window", True)))
        title = tk.StringVar(value=self.cfg.get("game_window_title") or "MapleStory")
        cb = ttk.Checkbutton(cap, text="Read only the game window (still read when other windows cover it)",
                             variable=use_win)
        cb.pack(side="left")
        tip("Reads the game's window on its own, so it's read even behind other windows (not when "
            "it's minimized). Off: the whole screen is read, and the game must be visible.", cb)
        ttk.Label(cap, text="Window title:").pack(side="left", padx=(14, 4))
        te = ttk.Entry(cap, textvariable=title, width=16)
        te.pack(side="left")
        tip("The game's window is found by this in its title.", te)
        now = ttk.Label(box, foreground="#555")
        now.grid(row=101, column=0, columnspan=4, sticky="w", pady=(2, 0))

        def capture_changed(*_):
            self.cfg["capture_window"] = use_win.get()
            self.cfg["game_window_title"] = title.get().strip() or "MapleStory"
            mfm.save_config(self.cfg)
            refresh()
        use_win.trace_add("write", capture_changed)
        te.bind("<FocusOut>", capture_changed)
        te.bind("<Return>", capture_changed)

        def refresh():
            mode = mfm.GRAB_MODE[0]
            try:
                if not (self.worker and self.worker.is_alive()):
                    mfm.grab_full_screen()        # (not while monitoring: it's reading already)
                    mode = mfm.GRAB_MODE[0]
            except Exception:
                pass
            size = "×".join(str(v) for v in mfm.LAST_SCREEN_SIZE) if mfm.LAST_SCREEN_SIZE else ""
            now.config(text=("Reading now: the game window" + (f" ({size})" if size else "") if mode == "window"
                             else "Reading now: the whole screen" + (
                                 " (the game window wasn't found: is the game running, and not minimized?)"
                                 if self.cfg.get("capture_window", True) else "")))
            modes = self.cfg.get("area_modes") or {}
            for key, lab in labels.items():
                r = self.cfg.get(key)
                drawn_on = modes.get(key, "screen")
                if r and drawn_on != mode:
                    lab.config(text="Drawn on the " + ("game window" if drawn_on == "window" else "whole screen")
                               + ": not used now", foreground="#b26a00")
                else:
                    lab.config(text=("Manual: " + area_text(r)) if r else "Automatic",
                               foreground="#1565c0" if r else "#555")

        def auto(key=None):
            for k, _, _ in AREAS:
                if key in (None, k):
                    self.cfg[k] = None
                    (self.cfg.get("area_modes") or {}).pop(k, None)
            mfm.save_config(self.cfg)
            self.found_at = None
            self._update_stats(None, None)
            refresh()
            self._set_status("Every area will be found automatically." if key is None
                             else f"{AREA_NAMES[key]} will be found automatically.")

        for i, (key, name, what) in enumerate(AREAS, start=1):
            n = ttk.Label(box, text=name)
            n.grid(row=i, column=0, sticky="w", pady=2)
            lab = ttk.Label(box, width=40)
            lab.grid(row=i, column=1, sticky="w", padx=(10, 6))
            labels[key] = lab
            d = ttk.Button(box, text="Draw area",
                           command=lambda k=key: self.calibrate(key=k, hide=(win.winfo_toplevel(),),
                                                                on_set=refresh))
            d.grid(row=i, column=2, padx=(0, 6), pady=2)
            a = ttk.Button(box, text="Automatic", command=lambda k=key: auto(k))
            a.grid(row=i, column=3, pady=2)
            tip(f"{name}: {what}.", n, lab)
            tip(f"Draw a box around {what} on a screenshot.", d)
            tip("Forget the drawn area and find it by itself again.", a)
        b = ttk.Button(box, text="Auto Detection", command=auto)
        b.grid(row=len(AREAS) + 1, column=0, sticky="w", pady=(6, 0))
        tip("Forget every area you drew and find them all automatically.", b)
        ttk.Label(box, foreground="#666", text="Forgets every drawn area.").grid(
            row=len(AREAS) + 1, column=1, columnspan=3, sticky="w", padx=(10, 0), pady=(6, 0))
        refresh()
        return box

    def settings(self):
        win = self.window()
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        # in tabs, so the window fits on smaller screens
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        gen = ttk.Frame(nb, padding=(0, 8, 0, 4))
        rd = ttk.Frame(nb, padding=(12, 10, 12, 8))
        gd = ttk.Frame(nb, padding=(12, 10, 12, 8))
        nb.add(gen, text="  General  ")
        nb.add(rd, text="  Reading  ")
        nb.add(gd, text="  Game data  ")
        try:
            nb.select(int(self.cfg.get("settings_tab", 0)))
        except (tk.TclError, ValueError, TypeError):
            pass
        nb.bind("<<NotebookTabChanged>>",
                lambda e: self.cfg.__setitem__("settings_tab", nb.index(nb.select())), add="+")
        fields = [
            ("interval_seconds", "Capture every (seconds)", 0.05, 10, 0.05),
            ("ocr_workers", "Frames read at once", 1, 8, 1),
            ("confirm_frames", "Frames to confirm a row", 1, 5, 1),
            ("unmatched_extra_frames", "Extra reads for names not in database", 0, 5, 1),
            ("close_frames", "Empty frames = shop closed", 1, 20, 1),
            ("history_days", "Market median window (days)", 1, 90, 1),
            ("upscale", "OCR upscale factor", 1, 6, 1),
            ("min_sightings", "Required sightings before judging", 1, 100, 1),
            ("deal_threshold_pct", "Flag DEAL / HIGH at ± %", 1, 90, 1),
            ("outlier_ceiling_pct", "Outliers: ceiling, % above median", 10, 10000, 10),
            ("outlier_floor_pct", "Outliers: floor, % below median", 5, 99, 5),
        ]
        tips = {
            "deal_threshold_pct": "How far below (DEAL) or above (HIGH) the market price a listing "
                                  "must be.",
            "interval_seconds": "How often the screen is captured. The OCR reads the captures "
                                "at its own pace, so nothing is lost when you scroll fast. "
                                "Higher uses less CPU.",
            "ocr_workers": "While you scroll, this many captured frames are read at the same time, "
                           "so the reading keeps up. Each one uses a processor core. 1 reads them one "
                           "by one.",
            "confirm_frames": "How many matching reads a listing needs before it's saved.",
            "unmatched_extra_frames": "Extra reads for names not in the item list, before they're "
                                      "saved.",
            "close_frames": "Reads without a shop before the shop counts as closed.",
            "history_days": "How many days of sightings make the market price.",
            "shot_days": "The shop window as it was on screen when a row was logged is kept this "
                         "long (right-click a listing → Show shop photo), to check a misread name. "
                         "About 20 KB each, a few per shop visit. 0 takes none.",
            "upscale": "Raise it if small text is misread. Higher is slower.",
            "min_sightings": "Sightings an item needs before it gets a market price, DEAL / HIGH "
                             "and outliers.",
            "outlier_ceiling_pct": "Prices this far above usual are left out of the market price.",
            "outlier_floor_pct": "Prices this far below usual are left out of the market price "
                                 "(still shown as deals).",
        }
        vars_ = {}
        for i, (key, label, lo, hi, step) in enumerate(fields):
            lbl = ttk.Label(gen, text=label)
            lbl.grid(row=i, column=0, sticky="w", padx=12, pady=4)
            v = tk.StringVar(value=str(self.cfg.get(key, mfm.DEFAULT_CONFIG[key])))
            spin = ttk.Spinbox(gen, from_=lo, to=hi, increment=step, width=8, textvariable=v)
            spin.grid(row=i, column=1, padx=12, pady=4)
            tip(tips[key], lbl, spin)
            vars_[key] = (v, type(mfm.DEFAULT_CONFIG[key]))
        # ---- data retention (top right)
        ret = ttk.LabelFrame(gen, text=" Data retention ", padding=(10, 2, 10, 4))
        ret.grid(row=0, column=2, rowspan=5, sticky="nwe", padx=(0, 12), pady=(4, 0))
        r1 = ttk.Frame(ret)
        r1.pack(anchor="w", pady=(0, 2))
        keep_var = tk.StringVar(value=str(self.cfg.get("keep_days", 30)))
        lk = ttk.Label(r1, text="Keep listings for")
        lk.pack(side="left")
        sk = ttk.Spinbox(r1, from_=1, to=3650, increment=1, width=6, textvariable=keep_var)
        sk.pack(side="left", padx=6)
        ttk.Label(r1, text="days").pack(side="left")
        tip("Listings not seen for this many days are cleaned up.", lk, sk)
        ttk.Label(r1, text="· shop photos").pack(side="left", padx=(10, 0))
        shots_var = tk.StringVar(value=str(self.cfg.get("shot_days", 7)))
        ss = ttk.Spinbox(r1, from_=0, to=90, increment=1, width=4, textvariable=shots_var)
        ss.pack(side="left", padx=4)
        ttk.Label(r1, text="days").pack(side="left")
        tip("The shop window as it was on screen when a row was logged is kept this long "
            "(right-click a listing → Show shop photo), to check a misread name. About 20 KB "
            "each, a few per shop visit. 0 takes none.", ss)
        mode_var = tk.StringVar(value=self.cfg.get("retention", "archive"))
        rm = ttk.Frame(ret)
        rm.pack(anchor="w", pady=(0, 2))
        ttk.Label(rm, text="Then:").pack(side="left")
        for val, text, t in (
                ("archive", "Archive, then delete",
                 "Moves old listings to the archive file."),
                ("delete", "Delete", "Removes old listings for good."),
                ("keep", "Keep everything", "Never removes anything.")):
            rb = ttk.Radiobutton(rm, text=text, value=val, variable=mode_var)
            rb.pack(side="left", padx=(8, 0))
            tip(t, rb)
        r2 = ttk.Frame(ret)
        r2.pack(fill="x")
        ttk.Label(r2, text="Archive to:").pack(side="left", padx=(0, 6))
        arch_var = tk.StringVar(value=str(mfm.archive_dir(self.cfg)))
        ea = ttk.Entry(r2, textvariable=arch_var, width=26)
        ea.pack(side="left", fill="x", expand=True)

        def browse():
            d = filedialog.askdirectory(parent=win, title="Archive folder",
                                        initialdir=arch_var.get() or str(mfm.DATA))
            if d:
                arch_var.set(d)
        bb = ttk.Button(r2, text="Browse", command=browse)
        bb.pack(side="left", padx=(6, 0))
        tip(f"The folder for {mfm.ARCHIVE_NAME}.", ea, bb)
        r3 = ttk.Frame(ret)
        r3.pack(fill="x", pady=(3, 0))
        size_lbl = ttk.Label(r3, foreground="#666")

        def ret_cfg():
            try:
                days = max(1, int(float(keep_var.get())))
            except ValueError:
                days = 30
            try:
                shots = max(0, min(90, int(float(shots_var.get()))))
            except ValueError:
                shots = 7
            return {"keep_days": days, "retention": mode_var.get(),
                    "archive_dir": arch_var.get().strip() or None, "shot_days": shots}

        def show_size():
            size = mfm.db_size()
            try:
                old = mfm.old_listing_count(self.con, ret_cfg()["keep_days"])
            except Exception:
                old = 0
            size_lbl.config(text=f"Database {size / 1048576:,.1f} MB  ·  {old:,} to clean up  ·  "
                                 f"shop photos {mfm.shots_size() / 1048576:,.0f} MB",
                            foreground="#c62828" if size > mfm.DB_WARN_BYTES else "#666")

        def clean_now():
            c = ret_cfg()
            if c["retention"] == "keep":
                c["retention"] = "archive"
            n = mfm.old_listing_count(self.con, c["keep_days"])
            if not n:
                messagebox.showinfo("Clean up now", f"No listings are older than {c['keep_days']} days.",
                                    parent=win)
                return
            where = (f"archived to {mfm.archive_dir(c) / mfm.ARCHIVE_NAME} and then deleted"
                     if c["retention"] == "archive" else "deleted for good")
            if messagebox.askyesno("Clean up now", f"{mfm.plural(n, 'listing')} not seen for "
                                   f"{c['keep_days']} days will be {where}.\n\nContinue?", parent=win):
                self.run_retention(cfg=c, done=show_size)
        bc = ttk.Button(r3, text="Clean up now", command=clean_now)
        bc.pack(side="left")
        tip("Cleans up old listings now.", bc)
        size_lbl.pack(side="left", padx=(8, 0))
        keep_var.trace_add("write", lambda *a: show_size())
        show_size()

        save_caps = tk.BooleanVar(value=self.cfg.get("save_captures", False))
        self._window_detection_frame(rd).pack(fill="x")
        ocr_box, ocr_vars = self._ocr_training_frame(rd)
        ocr_box.pack(fill="x", pady=(10, 0))
        cb = ttk.Checkbutton(rd, text="Save every capture as PNG (for debugging OCR)", variable=save_caps)
        cb.pack(anchor="w", pady=(10, 0))
        tip("Saves a picture of every screen read, for troubleshooting. Leave off normally.", cb)
        self._game_data_frame(gd).pack(fill="x")
        # ---- channel picker (under data retention)
        chan_prompt = tk.BooleanVar(value=self.cfg.get("channel_prompt", True))
        cb = ttk.Checkbutton(gen, text="Ask for the channel at launch and after a channel change",
                             variable=chan_prompt)
        cb.grid(row=5, column=2, sticky="w", padx=(0, 12), pady=4)
        tip("Asks for your channel when the app opens and after you change channel in the game.", cb)
        rc = ttk.Frame(gen)
        rc.grid(row=6, column=2, sticky="w", padx=(0, 12), pady=4)
        chan_count = tk.StringVar(value=str(self.cfg.get("channel_count", 40)))
        lc = ttk.Label(rc, text="Channels in the channel picker")
        lc.pack(side="left")
        sc = ttk.Spinbox(rc, from_=1, to=100, increment=1, width=6, textvariable=chan_count)
        sc.pack(side="left", padx=8)
        tip("How many channel buttons the picker shows.", lc, sc)
        outliers_on = tk.BooleanVar(value=self.cfg.get("outliers_on", True))
        cb = ttk.Checkbutton(gen, text="Ignore outliers in market rates",
                             variable=outliers_on)
        cb.grid(row=len(fields) - 3, column=2, sticky="w", padx=(0, 12))
        review_on = tk.BooleanVar(value=self.cfg.get("review_on", True))
        cbr = ttk.Checkbutton(gen, text="Show the Review tab (names to match to items)", variable=review_on)
        cbr.grid(row=len(fields) - 4, column=2, sticky="w", padx=(0, 12))
        tip("Off hides the Review tab, and the app stops working out the names to review. "
            "Names are still matched by the aliases you've taught.", cbr)
        tip("Leaves troll prices and misreads out of the market price. "
            "200% ceiling = more than 3x the usual price; 75% floor = less than 1/4 of it.", cb)
        # ---- hotkeys (under the numbers)
        hot_box, hotkeys = self._hotkeys_frame(gen)
        hot_box.grid(row=len(fields), column=0, columnspan=2, sticky="nwe", padx=12, pady=(8, 0))
        # ---- the widget over the game (next to the hotkeys)
        ov_box = ttk.LabelFrame(gen, text=" Widget ", padding=(10, 2, 10, 6))
        ov_box.grid(row=len(fields), column=2, sticky="nwe", padx=(0, 12), pady=(8, 0))
        overlay_on = tk.BooleanVar(value=bool(self.cfg.get("overlay_on", True)))
        cb = ttk.Checkbutton(ov_box, text="Show the widget over the game", variable=overlay_on)
        cb.pack(anchor="w")
        tip("A small card that stays on top of the game: monitoring time and state, frames "
            "waiting and the channel, with Start / Stop, Next shop and the channel arrows. "
            "The Widget box in the toolbar and the widget's ✕ turn it on and off too.", cb)

        # ---- updates (under the channel picker)
        upd = ttk.LabelFrame(gen, text=" Updates ", padding=(10, 2, 10, 6))
        upd.grid(row=len(fields) - 2, column=2, rowspan=2, sticky="nwe", padx=(0, 12), pady=(6, 0))
        update_check = tk.BooleanVar(value=self.cfg.get("update_check", True))
        cb = ttk.Checkbutton(upd, text="Check for updates when the app opens", variable=update_check)
        cb.pack(anchor="w")
        tip("Looks for a new version at launch (and once a day) and asks before installing it.", cb)
        ru = ttk.Frame(upd)
        ru.pack(anchor="w", pady=(4, 0))
        bu = ttk.Button(ru, text="Check for updates", command=lambda: self.check_for_updates(True, win))
        bu.pack(side="left")
        tip("Looks for a new version now.", bu)
        ttk.Label(ru, text=f"You have version {mfm.VERSION}", foreground="#666").pack(side="left", padx=(8, 0))

        def save():
            try:
                for key, (v, typ) in vars_.items():
                    self.cfg[key] = typ(float(v.get())) if typ is int else float(v.get())
            except ValueError:
                messagebox.showerror("Invalid value", "Please enter numbers only.", parent=win)
                return
            c = ret_cfg()
            if c["retention"] != "keep" and c["keep_days"] < int(self.cfg.get("history_days", 14)):
                if not messagebox.askyesno(
                        "Data retention", f"Keeping listings for {c['keep_days']} days is shorter than "
                        f"the market price window ({self.cfg.get('history_days', 14)} days).\n\n"
                        "Save anyway?", parent=win):
                    return
            self.cfg.update(c)
            self.cfg["save_captures"] = save_caps.get()
            self.cfg["ocr_collect"], self.cfg["ocr_share"] = ocr_vars[0].get(), ocr_vars[1].get()
            self.cfg["ocr_collect_stores"], self.cfg["ocr_collect_players"] = ocr_vars[2].get(), ocr_vars[3].get()
            mfm.OCR_COLLECT.set_from(self.cfg)
            self.cfg["channel_prompt"] = chan_prompt.get()
            try:
                self.cfg["channel_count"] = max(1, min(100, int(float(chan_count.get()))))
            except ValueError:
                messagebox.showerror("Invalid value", "Channels in the channel picker: a number from 1 to 100.",
                                     parent=win)
                return
            self.cfg["outliers_on"] = outliers_on.get()
            hot_on, hot = hotkeys()
            used = [k for k in hot.values() if k]
            if len(used) != len(set(used)):
                messagebox.showerror("Hotkeys", "Two actions have the same key.", parent=win)
                return
            self.cfg["hotkeys_on"], self.cfg["hotkeys"] = hot_on, hot
            self.cfg["update_check"] = update_check.get()
            self.cfg["review_on"] = review_on.get()
            if review_on.get():
                self.nb.add(self.review_tab)  # (back where it was)
            else:
                self.nb.hide(self.review_tab)
            mfm.save_config(self.cfg)
            self._start_hotkeys()
            self.show_overlay(overlay_on.get(), save=True)
            self.refresh_tables()
            win.destroy()

        bottom = ttk.Frame(win)
        bottom.pack(fill="x", padx=12, pady=(10, 12))
        danger = ttk.LabelFrame(bottom, text=" Your data (prices and personal settings) ", padding=(10, 6))
        danger.pack(side="left")
        b1 = ttk.Button(danger, text="Clear pricing data", command=lambda: self.clear_prices(win))
        b1.pack(side="left")
        b2 = ttk.Button(danger, text="Reset personal settings", command=lambda: self.reset_personal(win))
        b2.pack(side="left", padx=6)
        b3 = ttk.Button(danger, text="Open data folder", command=self.open_data_folder)
        b3.pack(side="left")
        tip("Deletes all your prices (a backup is kept).", b1)
        tip("Choose settings, watched items or other lists to reset. Prices are kept.", b2)
        tip("Opens the folder with your prices and settings.", b3)
        b6 = ttk.Button(bottom, text="About", command=lambda: self.about(win))
        tip("Version and credits.", b6)
        b4 = ttk.Button(bottom, text="Cancel", command=win.destroy)
        b4.pack(side="right", anchor="s")
        b5 = ttk.Button(bottom, text="Save", command=save)
        b5.pack(side="right", anchor="s", padx=6)
        b6.pack(side="right", anchor="s", padx=(0, 12))
        tip("Saves your settings.", b5)

    # ------------------------------------------------------------------ updates
    def auto_update_check(self):
        """At launch and once a day: a newer version gets a prompt (unless skipped)."""
        self.root.after(int(mfm.UPDATE_CHECK_EVERY.total_seconds() * 1000), self.auto_update_check)
        if self.cfg.get("update_check", True) and mfm.FROZEN:
            self.check_for_updates(manual=False)

    def check_for_updates(self, manual=True, parent=None):
        """Ask GitHub for the newest release in the background; manual=True also says
        when you're up to date, or when the check failed."""
        if getattr(self, "_update_busy", False):
            return
        self._update_busy = True
        box: dict = {}

        def work():
            try:
                box["info"] = mfm.latest_release()
            except Exception as e:
                box["error"] = f"{type(e).__name__}: {e}"

        def wait():
            if t.is_alive():
                self.root.after(200, wait)
                return
            self._update_busy = False
            info = box.get("info")
            parent_ = parent if parent is not None and parent.winfo_exists() else self.root
            if "error" in box:
                if manual:
                    why = ("GitHub is limiting checks from your connection right now. Try again "
                           "in an hour." if "rate limit" in box["error"].lower() or "403" in box["error"]
                           else box["error"])
                    messagebox.showerror("Check for updates", "Couldn't check for updates:\n\n"
                                         + why, parent=parent_)
                return
            if not mfm.update_newer(info):
                if manual:
                    messagebox.showinfo("Check for updates", f"You have the latest version "
                                        f"({mfm.VERSION}).", parent=parent_)
                return
            if not manual and info["version"] == self.cfg.get("update_skip"):
                return
            self.update_prompt(info, parent_)

        t = threading.Thread(target=work, daemon=True, name="mfm-update-check")
        t.start()
        self.root.after(200, wait)

    def update_prompt(self, info, parent=None):
        import webbrowser

        win = self.window(parent)
        win.title("Update available")
        win.transient(parent or self.root)
        f = ttk.Frame(win, padding=(16, 14, 16, 12))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"Version {info['version']} is available", style="Big.TLabel").pack(anchor="w")
        ttk.Label(f, text=f"You have version {mfm.VERSION}.", foreground="#555").pack(anchor="w", pady=(2, 8))
        if info.get("notes"):
            ttk.Label(f, text="What's new:").pack(anchor="w")
            tf = ttk.Frame(f)
            tf.pack(fill="both", expand=True, pady=(2, 8))
            txt = tk.Text(tf, wrap="word", width=78, height=14, font="TkDefaultFont",
                          padx=6, pady=4, relief="solid", borderwidth=1)
            sb = ttk.Scrollbar(tf, orient="vertical", command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)
            txt.insert("1.0", info["notes"])
            txt.configure(state="disabled")
        blocker = mfm.update_blocker() if info.get("asset_url") else \
            f"This release has no download for {mfm.update_platform()}."
        if blocker:
            ttk.Label(f, text=blocker, wraplength=560, foreground="#c62828").pack(anchor="w", pady=(0, 8))
        btns = ttk.Frame(f)
        btns.pack(fill="x")

        def update_now():
            win.destroy()
            self.run_update(info)

        def skip():
            self.cfg["update_skip"] = info["version"]
            mfm.save_config(self.cfg)
            win.destroy()

        if blocker:
            b = ttk.Button(btns, text="Open download page",
                           command=lambda: (webbrowser.open(info["page"]), win.destroy()))
        else:
            b = ttk.Button(btns, text="Update now", command=update_now)
            tip("Downloads the new version, puts it in place of this one and restarts the app. "
                "Your prices and settings are kept.", b)
        b.pack(side="left")
        bl = ttk.Button(btns, text="Later", command=win.destroy)
        bl.pack(side="left", padx=6)
        tip("Asks again next time the app opens.", bl)
        bs = ttk.Button(btns, text="Skip this version", command=skip)
        bs.pack(side="left")
        tip("Doesn't ask about this version again. Settings → Check for updates still finds it.", bs)
        b.focus_set()

    def run_update(self, info):
        """Download and install, with a progress window; then restart into the new app."""
        win = self.window()
        win.title("Updating")
        win.transient(self.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=(16, 14, 16, 12))
        f.pack(fill="both", expand=True)
        msg = ttk.Label(f, text=f"Downloading version {info['version']}…", width=52)
        msg.pack(anchor="w")
        bar = ttk.Progressbar(f, length=380, mode="determinate", maximum=100)
        bar.pack(fill="x", pady=8)
        cancelled = threading.Event()
        bc = ttk.Button(f, text="Cancel", command=cancelled.set)
        bc.pack(anchor="e")
        win.protocol("WM_DELETE_WINDOW", cancelled.set)
        state: dict = {"done": 0, "total": info.get("size") or 0, "stage": "download"}

        def work():
            try:
                path = mfm.download_update(info, progress=lambda d, t: state.update(done=d, total=t),
                                           cancel=cancelled.is_set)
                state["stage"] = "install"
                state["cmd"] = mfm.install_update(path)
            except InterruptedError:
                state["error"] = None
            except Exception as e:
                state["error"] = f"{type(e).__name__}: {e}"
            finally:
                state["finished"] = True

        def tick():
            if not win.winfo_exists():
                return
            if state["total"]:
                bar["value"] = 100 * state["done"] / state["total"]
                msg.config(text=f"Downloading version {info['version']}…  "
                                f"{state['done'] / 1048576:,.0f} of {state['total'] / 1048576:,.0f} MB")
            if state["stage"] == "install":
                msg.config(text="Installing…")
                bc.state(["disabled"])
            if not state.get("finished"):
                self.root.after(150, tick)
                return
            if "cmd" in state:
                msg.config(text="Done. Restarting…")
                try:
                    mfm.start_new_app(state["cmd"])
                except Exception as e:
                    win.destroy()
                    messagebox.showinfo("Update installed", f"Version {info['version']} is installed. "
                                        f"Close and reopen the app to use it.\n\n({e})", parent=self.root)
                    return
                self.root.after(300, self.on_close)
                return
            win.destroy()
            if state.get("error"):
                messagebox.showerror("Update failed", "The update couldn't be installed; this version "
                                     "keeps running.\n\n" + state["error"], parent=self.root)

        threading.Thread(target=work, daemon=True, name="mfm-update").start()
        tick()

    def about(self, parent=None):
        import webbrowser

        win = self.window(parent)
        win.title("About Free Market Tracker")
        win.transient(parent or self.root)
        win.geometry("700x620")
        head = ttk.Frame(win, padding=(16, 14, 16, 4))
        head.pack(fill="x")
        try:
            for p in (mfm.BUNDLE / "icon.png", Path(__file__).resolve().parent / "packaging" / "icon.png"):
                if p.exists():
                    win._logo = ImageTk.PhotoImage(Image.open(p).resize((48, 48)))
                    ttk.Label(head, image=win._logo).pack(side="left", padx=(0, 12))
                    break
        except Exception:
            pass
        info = ttk.Frame(head)
        info.pack(side="left", fill="x")
        ttk.Label(info, text="Free Market Tracker", style="Big.TLabel").pack(anchor="w")
        ttk.Label(info, text=f"Version {mfm.VERSION}", foreground="#555").pack(anchor="w")
        ttk.Label(info, foreground="#555", text="Free Market price tracker for MapleStory Classic World"
                  ).pack(anchor="w")
        btns = ttk.Frame(win, padding=(16, 8, 16, 12))
        btns.pack(side="bottom", fill="x")          # packed first so it always keeps its room
        frame = ttk.Frame(win, padding=(16, 8, 16, 0))
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, wrap="word", relief="flat", borderwidth=0, highlightthickness=0,
                      background=win.cget("background"), padx=2, pady=2, cursor="arrow",
                      font="TkDefaultFont", spacing1=1, spacing3=1)
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h", font="MfmBold", spacing1=10, spacing3=4)
        txt.tag_configure("name", font="MfmBold")
        txt.tag_configure("note", foreground="#555")
        txt.tag_configure("link", foreground="#1a5fb4", underline=True)
        txt.tag_bind("link", "<Enter>", lambda e: txt.config(cursor="hand2"))
        txt.tag_bind("link", "<Leave>", lambda e: txt.config(cursor="arrow"))
        for i, (section, entries) in enumerate(CREDITS):
            txt.insert("end", section + "\n", ("h",))
            for name, what, url in entries:
                tag = f"url{id(url)}{name[:6]}"
                txt.insert("end", name, ("name",))
                txt.insert("end", "\n" + (what + "\n" if what else ""), ("note",))
                if url:
                    txt.insert("end", url, ("link", tag))
                    txt.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u))
                    txt.insert("end", "\n")
                txt.insert("end", "\n")
        txt.insert("end", "MapleStory is a trademark of NEXON. This is an unofficial fan tool, not "
                          "affiliated with or endorsed by NEXON, NiaMeowDB or OSMS Data Explorer.",
                   ("note",))
        txt.config(state="disabled")
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Copy credits", command=lambda: (
            win.clipboard_clear(), win.clipboard_append(
                f"Free Market Tracker {mfm.VERSION}\n\n" + "\n\n".join(
                    sec + "\n" + "\n".join(f"- {n}" + (f": {w}" if w else "") + (f" ({u})" if u else "") for n, w, u in ents)
                    for sec, ents in CREDITS)))).pack(side="right", padx=6)
        win.bind("<Escape>", lambda e: win.destroy())

    def open_data_folder(self):
        import subprocess
        path = str(mfm.DATA)
        try:
            if mfm.sys.platform == "win32":
                mfm.os.startfile(path)
            elif MAC:
                subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            messagebox.showinfo("Data folder", path)

    def _megaphone_offers_table(self, parent, item):
        """A list of the megaphones that sell or buy `item`; returns (frame, how many)."""
        t = make_tree(parent, ("time", "side", "mprice", "player", "channel", "message"))
        t.column("#0", width=0, minwidth=0)
        t.column("message", stretch=True, width=520)
        t.column("time", width=150)
        t.heading("side", text="Buying / Selling")
        hours = max(1, float(self.cfg.get("megaphone_hours", 72) or 72))
        rows = self.con.execute(
            "SELECT m.ts, o.side, o.price, o.note, m.player, m.channel, m.message FROM megaphone_offers o "
            "JOIN megaphones m ON m.id = o.megaphone_id WHERE o.item = ? AND m.ts >= ? ORDER BY m.ts DESC",
            (item, mfm.utc_ago(hours=hours))).fetchall()
        for ts, side, price, note, player, channel, msg in rows:
            p = mfm.fmt(price) if price else ""
            if note:
                p = f"{note} {p}".strip()
            t.insert("", "end", values=(mfm.local_time(ts), "Buying" if side == "buy" else "Selling", p,
                                        player or "", mfm.channel_label(channel) if channel else "", msg))

        def copy(_=None):
            vals = [t.set(k, "message") for k in t.selection()]
            if vals:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(vals))
        t.bind("<Double-1>", copy)
        return t.frame, len(rows)

    def _megaphone_history(self, item):
        """An item only seen in megaphones (no shop listings yet)."""
        win = self.window()
        win.title(item)
        win.geometry("1000x480")
        head = ttk.Frame(win, padding=(12, 10, 12, 0))
        head.pack(fill="x")
        ttk.Label(head, text="  " + item, image=self.icon_for(item, 48), compound="left",
                  style="Big.TLabel").pack(side="left")
        ttk.Label(win, padding=(12, 4), text="Not seen in any shop yet. Megaphones that mention it "
                  "(double-click one to copy it):").pack(anchor="w")
        frame, _ = self._megaphone_offers_table(win, item)
        frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    def window(self, parent=None) -> tk.Toplevel:
        """A new window over its parent, on the same screen: with two monitors the
        system would open it on the main one, wherever the app is. It's placed
        beside the parent's corner at once (so it never appears elsewhere) and
        centred on the parent once its size is known, kept within that monitor."""
        parent = parent or self.root
        win = tk.Toplevel(parent)
        win.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 60}")

        def centre():
            try:
                win.update_idletasks()
                w, h = win.winfo_width(), win.winfo_height()
                if w <= 1 or h <= 1:
                    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
                x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
                y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
                # kept on the monitor (its title bar above all): Windows knows each
                # monitor's area; elsewhere the screen is used
                area = monitor_work_area(parent) or (0, 0, parent.winfo_screenwidth(), parent.winfo_screenheight())
                x = max(area[0], min(x, area[2] - w))
                y = max(area[1], min(y, area[3] - h))
                win.geometry(f"+{x}+{y}")
            except tk.TclError:
                pass                 # closed before it was placed
        win.after_idle(centre)
        return win

    def _listing_of(self, tree, iid) -> int | None:
        """The price-database row behind a Live or Deals table row, if it can be found."""
        sc = self.scopes.get((str(tree), iid))
        if not sc:
            return None
        if sc.get("id"):
            return sc["id"]
        if "item" not in sc:
            return None
        r = self.con.execute(
            "SELECT id FROM listings WHERE item = ? AND price = ? AND COALESCE(shop, '') = ? "
            "AND COALESCE(seller, '') = ? ORDER BY COALESCE(sold_ts, last_seen, ts) DESC LIMIT 1",
            (sc["item"], sc["price"], sc.get("title") or sc.get("shop") or "", sc.get("seller") or "")).fetchone()
        return r[0] if r else None

    def show_shot_for(self, tree):
        sel = tree.selection()
        lid = self._listing_of(tree, sel[0]) if len(sel) == 1 else None
        if lid:
            self.show_shot(lid)

    def show_shot(self, lid: int, parent=None):
        """The shop window as it was on screen when this listing was logged: to check
        a shop, player or item name the OCR may have got wrong."""
        import io

        parent = parent or self.root
        r = self.con.execute("SELECT session, ts, last_seen, sold_ts, item, price, qty, shop, seller, map, "
                             "channel FROM listings WHERE id = ?", (lid,)).fetchone()
        if not r:
            return
        session, ts, last_seen, sold_ts, item, price, qty, shop, seller, mp, channel = r
        data = mfm.load_shot(session, ts, last_seen, sold_ts)
        if not data:
            days = self.cfg.get("shot_days", 7)
            messagebox.showinfo("Shop photo", "There's no photo of this sighting.\n\n"
                                + (f"Shop photos are kept for {mfm.plural(days, 'day')} (Settings)."
                                   if days else "Shop photos are off: set Keep shop photos in Settings."),
                                parent=parent)
            return
        img = Image.open(io.BytesIO(data))
        win = self.window(parent)
        win.title(f"{item} — {shop or 'shop'} ({seller or 'player'})")
        win.transient(parent)
        ttk.Label(win, padding=(12, 10, 12, 4), style="Bold.TLabel", text=(
            f"{item}   ·   {mfm.fmt(price)}" + (f" × {qty}" if qty and qty > 1 else ""))).pack(anchor="w")
        # what the OCR read, as "Label: value" pairs with the labels in bold
        row = ttk.Frame(win, padding=(12, 0, 12, 6))
        row.pack(anchor="w")
        for label, value in (("Shop:", shop or "—"), ("Player:", seller or "—"), ("Map:", mp or "—"),
                             ("Channel:", mfm.channel_label(channel) if channel else "—"),
                             ("Time:", mfm.local_time(ts))):
            ttk.Label(row, text=label, style="Bold.TLabel").pack(side="left", padx=(0, 4))
            ttk.Label(row, text=value, foreground="#555").pack(side="left", padx=(0, 14))
        ttk.Label(win, padding=(12, 0, 12, 6), foreground="#777", text=(
            "What the OCR read is above; the screen it read from is below. Click the picture to zoom.")
            ).pack(anchor="w")
        pic = ttk.Label(win, cursor="hand2")
        pic.pack(padx=12, pady=(0, 12))
        zoom = [1]

        def show():
            w, h = img.size
            scaled = img if zoom[0] == 1 else img.resize((w * zoom[0], h * zoom[0]), Image.NEAREST)
            pic.photo = ImageTk.PhotoImage(scaled)
            pic.config(image=pic.photo)
        pic.bind("<Button-1>", lambda e: (zoom.__setitem__(0, 1 if zoom[0] == 2 else 2), show()))
        win.bind("<Escape>", lambda e: win.destroy())
        show()

    def open_history(self, tree):
        sel = tree.selection()
        if not sel:
            return
        item = tree.set(sel[0], "item")
        if not self.con.execute("SELECT 1 FROM listings WHERE item = ?", (item,)).fetchone():
            if self.con.execute("SELECT 1 FROM megaphone_offers WHERE item = ?", (item,)).fetchone():
                return self._megaphone_history(item)
            messagebox.showinfo(item, f"'{item}' hasn't been seen in any shop yet.", parent=self.root)
            return
        win = self.window()
        win.title(item)
        win.geometry("1160x660")
        head = ttk.Frame(win, padding=(12, 10, 12, 0))
        head.pack(fill="x")
        ttk.Label(head, text="  " + item, image=self.icon_for(item, 48), compound="left",
                  style="Big.TLabel").pack(side="left")

        def lookup():
            import webbrowser
            webbrowser.open(mfm.meowdb_url(item))
        ttk.Button(head, text="Look up on meowdb", command=lookup).pack(side="right")
        summary = ttk.Label(win, padding=(12, 0))
        summary.pack(anchor="w")
        legend = ttk.Label(win, padding=(12, 0), foreground="#777")
        legend.pack(anchor="w")
        c = tk.Canvas(win, background="white", highlightthickness=0, height=260)
        c.pack(fill="both", expand=True, padx=12, pady=10)
        hint = ttk.Label(win, padding=(12, 0), justify="left", text=(
            "Right-click a sighting to see the shop as it was on screen, change its price or channel, "
            "add a note, watch it, or delete it. "
            + ("Option+click" if MAC else "Alt+click") + " selects several."))
        hint.pack(anchor="w", fill="x")
        hint.bind("<Configure>", lambda e: hint.config(wraplength=max(e.width - 24, 200)))
        tabs = ttk.Notebook(win)
        tabs.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        table = make_tree(tabs, ("each", "status", "notes", "shop", "player", "map", "channel", "time"))
        table.column("#0", width=0, minwidth=0)   # one item: no per-row icon needed
        table.column("status", width=130)
        tabs.add(table.frame, text="  Shop listings  ")
        n_mega = self._megaphone_offers_table(tabs, item)
        tabs.add(n_mega[0], text=f"  Megaphones ({n_mega[1]})  " if n_mega[1] else "  Megaphones  ")
        st = {"rows": [], "out": [], "mkt": None, "bounds": None}

        def load():
            """(Re)read the item's listings and refresh every part of the window."""
            rows = self.con.execute(
                "SELECT COALESCE(sold_ts, ts), price, shop, seller, map, channel, qty, "
                "COALESCE(sold, 0), id, notes FROM listings WHERE item = ? ORDER BY COALESCE(sold_ts, ts)",
                (item,)).fetchall()
            if not rows:
                win.destroy()
                return
            mkt, src = mfm.market_rate(self.con, item, self.cfg)
            bounds = mfm.outlier_bounds(self.con, item, self.cfg)
            out = [mfm.is_outlier(r[1], bounds) for r in rows]
            st.update(rows=rows, out=out, mkt=mkt, bounds=bounds)
            ps = mfm.price_stats(self.con, item, self.cfg, bounds)
            summary.config(text=(
                f"Market {mfm.fmt(mkt)} ({src})   ·   listed median {mfm.fmt(ps['listed_median'])} "
                f"({ps['listed_n']})   ·   sold median {mfm.fmt(ps['sold_median'])} ({ps['sold_n']})"))
            legend.config(text="dots = for sale    grey squares = sold    dashed line = market"
                          + ("    hollow orange = outlier (ignored)" if any(out) else "")
                          + (f"\nnormal range {mfm.fmt(bounds[0])} – {mfm.fmt(bounds[1])}; "
                             "prices outside it are left out of the market price" if bounds else ""))
            keep = set(table.selection())
            table.delete(*table.get_children())
            for ts, each, shop, seller, mp, channel, qty, sold, lid, notes in sorted(rows, key=lambda r: (r[7], r[1])):
                v, _ = mfm.verdict(each, mkt, self.cfg["deal_threshold_pct"], bounds)
                tag = "outlier" if v == "OUTLIER" else "sold" if sold else (tag_for(v) if v != "?" else "")
                low = mfm.is_outlier(each, bounds) and bounds and each < bounds[0]
                table.insert("", "end", iid=str(lid), tags=(tag,), values=(
                    mfm.fmt(each), "outlier" if v == "OUTLIER" else ("sold" if sold else "for sale")
                    + (" (very low)" if low else ""), notes or "",
                    shop or "", seller or "", mp or "", channel or "", mfm.local_time(ts)))
            table.selection_set([k for k in keep if table.exists(k)])
            draw()

        def picked():
            return [int(k) for k in table.selection()]

        def changed(msg):
            load()
            self.refresh_tables()
            self._set_status(msg)

        def change_price():
            ids = picked()
            if not ids:
                return
            cur = table.set(str(ids[0]), "each") if len(ids) == 1 else ""
            raw = ask_value(win, "Change price",
                            ("New price for this listing" if len(ids) == 1 else
                             f"New price for these {len(ids)} listings")
                            + " (e.g. 45000, 45,000 or 45k):", cur,
                            check=lambda t: (mfm.parse_price(t) or 0) > 0)
            if raw is None:
                return
            each = mfm.parse_price(raw)
            n = mfm.set_listing_price(self.con, ids, each)
            changed(f"Changed the price of {n} listing{'s' if n != 1 else ''} of '{item}' to {mfm.fmt(each)}.")

        def change_channel():
            ids = picked()
            if not ids:
                return
            cur = table.set(str(ids[0]), "channel") if len(ids) == 1 else ""
            raw = ask_value(win, "Change channel",
                            ("Channel for this listing" if len(ids) == 1 else
                             f"Channel for these {len(ids)} listings") + " (1–100, empty = unknown):",
                            cur, choices=[str(n) for n in range(1, 101)],
                            check=lambda t: not t.strip() or (t.strip().isdigit() and 1 <= int(t) <= 100))
            if raw is None:
                return
            n = mfm.set_listing_channel(self.con, ids, raw.strip())
            changed(f"Set channel of {n} listing{'s' if n != 1 else ''} to "
                    + (mfm.channel_label(raw.strip()) if raw.strip() else "unknown") + ".")

        def edit_note():
            ids = picked()
            if not ids:
                return
            cur = table.set(str(ids[0]), "notes") if len(ids) == 1 else ""
            note = ask_value(win, "Note", ("Note for this listing" if len(ids) == 1 else
                                           f"Note for these {len(ids)} listings")
                             + " (e.g. +5 ATK, 7 slots):", cur)
            if note is None:
                return
            mfm.set_listing_notes(self.con, item, note, ids=ids)
            for lid in ids:
                r = table.set(str(lid))
                self._note_to_watched(item, r["shop"], r["player"], mfm.parse_price(r["each"]), note)
            changed(f"Note saved for {len(ids)} listing{'s' if len(ids) != 1 else ''} of '{item}'."
                    if note.strip() else "Note removed.")

        def watch_this():
            ids = picked()
            if len(ids) != 1:
                return
            r = table.set(str(ids[0]))
            self.watch_listing(item, {"price": mfm.parse_price(r["each"]), "shop": r["shop"],
                                      "seller": r["player"], "map": r["map"],
                                      "channel": r["channel"] or None, "notes": r["notes"]},
                               ids=ids, parent=win)
            load()

        def delete():
            ids = picked()
            if not ids:
                return
            what = (f"this {table.set(str(ids[0]), 'each')} listing" if len(ids) == 1
                    else f"these {len(ids)} listings")
            if not messagebox.askyesno("Delete listings", f"Delete {what} of '{item}'?\n\n"
                                       "Other listings of the item are kept.", parent=win):
                return
            n = mfm.delete_listings(self.con, ids)
            if not self.con.execute("SELECT 1 FROM listings WHERE item = ?", (item,)).fetchone():
                self._forget_icon(item)
            changed(f"Deleted {n} listing{'s' if n != 1 else ''} of '{item}'.")

        menu = tk.Menu(win, tearoff=False)

        def popup(e):
            row = table.identify_row(e.y)
            if not row:
                return
            if row not in table.selection():   # right-clicking inside a selection keeps it
                table.selection_set(row)
            n = len(table.selection())
            menu.delete(0, "end")
            menu.add_command(label="Change price" if n == 1 else f"Change price of {n} listings",
                             command=change_price)
            menu.add_command(label="Change channel" if n == 1 else f"Change channel of {n} listings",
                             command=change_channel)
            has_note = n == 1 and bool(table.set(table.selection()[0], "notes"))
            menu.add_command(label="Edit note" if has_note else ("Add note" if n == 1 else
                                                                   f"Note for {n} listings"),
                             command=edit_note)
            menu.add_command(label="Watch this listing", command=watch_this,
                             state="normal" if n == 1 else "disabled")
            menu.add_command(label="Show shop photo", command=lambda: self.show_shot(picked()[0], parent=win),
                             state="normal" if n == 1 else "disabled")
            menu.add_separator()
            menu.add_command(label="Delete listing" if n == 1 else f"Delete {n} listings", command=delete)
            menu.tk_popup(e.x_root, e.y_root)
        bind_right_click(table, popup)
        table.bind("<Delete>", lambda e: delete())
        table.bind("<BackSpace>", lambda e: delete())
        table.bind("<Double-1>", lambda e: change_price() if table.identify_row(e.y) else None)

        def draw(_=None):
            rows, out, mkt, bounds = st["rows"], st["out"], st["mkt"], st["bounds"]
            c.delete("all")
            if not rows:
                return
            w, h = c.winfo_width(), c.winfo_height()
            pad_l, pad_r, pad_t, pad_b = 80, 16, 14, 28
            # scale to the normal prices; outliers are pinned to the top/bottom edge
            vals = [r[1] for r, o in zip(rows, out) if not o] or [r[1] for r in rows]
            vals += [mkt] if mkt else []
            lo, hi = min(vals), max(vals)
            if hi == lo:
                lo, hi = lo * 0.9, hi * 1.1 + 1
            span = hi - lo

            def y(v):
                v = min(max(v, lo), hi)
                return pad_t + (hi - v) / span * (h - pad_t - pad_b)

            def x(i):
                return pad_l + i * (w - pad_l - pad_r) / max(len(rows) - 1, 1)

            for frac in (0, 0.5, 1):
                v = lo + span * frac
                c.create_line(pad_l, y(v), w - pad_r, y(v), fill="#eeeeee")
                c.create_text(pad_l - 8, y(v), text=mfm.fmt(v), anchor="e", fill="#777")
            if mkt:
                c.create_line(pad_l, y(mkt), w - pad_r, y(mkt), fill="#999", dash=(4, 3))
                c.create_text(w - pad_r, y(mkt) - 8, text="market", anchor="e", fill="#999")
            pts = [(x(i), y(r[1])) for i, r in enumerate(rows)]
            line = [p for p, o in zip(pts, out) if not o]
            if len(line) > 1:
                c.create_line(*[v for p in line for v in p], fill="#3b6fd1", width=2)
            for (px, py), r, o in zip(pts, rows, out):
                if o:     # outlier: hollow orange ring at the edge, labelled with its price
                    c.create_oval(px - 5, py - 5, px + 5, py + 5, outline="#d9822b", width=2)
                    c.create_text(px + 8, py + (10 if py < h / 2 else -10), text=mfm.fmt(r[1]),
                                  anchor="w", fill="#d9822b")
                    continue
                if r[7]:  # sold: grey square
                    c.create_rectangle(px - 4, py - 4, px + 4, py + 4, fill="#7a7a7a", outline="")
                    continue
                v, _ = mfm.verdict(r[1], mkt, self.cfg["deal_threshold_pct"], bounds)
                col = {"DEAL": "#1f9a4a", "HIGH": "#c8352b"}.get(v, "#3b6fd1")
                c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=col, outline="")
            c.create_text(pad_l, h - 10, text=mfm.local_time(rows[0][0]), anchor="w", fill="#777")
            c.create_text(w - pad_r, h - 10, text=mfm.local_time(rows[-1][0]), anchor="e", fill="#777")

        c.bind("<Configure>", draw)
        load()

    def on_close(self):
        self.stop()
        self._stop_hotkeys()
        if self.shared:
            self.shared.stop()
        if getattr(self, "_icons_stop", None):
            self._icons_stop.set()
        self.root.after(200, self.root.destroy)


def set_window_icon(root) -> None:
    for p in (mfm.BUNDLE / "icon.png", Path(__file__).resolve().parent / "packaging" / "icon.png"):
        if p.exists():
            try:
                root._icon = ImageTk.PhotoImage(Image.open(p))
                root.iconphoto(True, root._icon)
            except Exception:
                pass
            return


def main():
    root = tk.Tk(className="free-market-tracker")
    set_window_icon(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
