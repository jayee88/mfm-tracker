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
import re
from pathlib import Path
import queue
import threading
import time
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
        "suggest": "Suggested item", "score": "Alike", "count": "Listings", "raw": "What OCR read"}
WIDTH = {"item": 155, "each": 82, "market": 82, "diff": 52, "verdict": 62,
         "shop": 140, "player": 100, "map": 265, "channel": 72,
         "best_shop": 140, "best_player": 100, "best_map": 265, "best_channel": 72,
         "time": 126, "low": 95, "high": 95, "seen": 50, "source": 110,
         "best": 90, "best_where": 230, "status": 66, "listed_med": 95, "sold_med": 90,
         "listed_n": 60, "sold_n": 45, "total": 100, "max_each": 110, "last_price": 100, "npc_buy": 85, "npc_sell": 85,
         "saved": 110, "notes": 170, "qty": 55, "range": 170, "points": 90, "sold24": 75,
         "last3": 130, "tab": 70, "match": 230, "list_price": 95, "message": 600, "suggest": 260, "score": 60, "count": 65, "raw": 300}
NUMERIC = {"each", "market", "diff", "low", "high", "seen", "best", "listed_med",
           "sold_med", "listed_n", "sold_n", "total", "score", "count", "channel", "best_channel",
           "max_each", "last_price", "npc_buy", "npc_sell", "qty", "range", "points", "sold24", "last3", "list_price"}
CENTER = {"channel", "best_channel"}


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
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
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

    def _hide(self, _=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None
        if Tooltip.active is self:
            Tooltip.active = None


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
        ("pynput", "global hotkey for the command-line `hotkey` mode (not in the packaged app) · LGPL-3.0",
         "https://github.com/moses-palmer/pynput"),
    ]),
]


def tip(text: str, *widgets) -> None:
    for w in widgets:
        Tooltip(w, text)


def ask_value(parent, title, prompt, initial="", choices=None, check=None) -> str | None:
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
           else ttk.Entry(frm, textvariable=var, width=18))
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
    tree.tag_configure("deal", background="#d5f2dc", foreground="#145c2c")
    tree.tag_configure("high", background="#fadad7", foreground="#8c1d16")
    tree.tag_configure("unknown", foreground="#8a8a8a")
    tree.tag_configure("sold", foreground="#9a9a9a", background="#f0f0f0")
    tree.tag_configure("watch", font="MfmBold")
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
        names = mfm.suggest_items(text, 12) if len(text) >= 2 else []
        if not names or (len(names) == 1 and names[0] == text):
            self.hide()
            return
        self._show(names)

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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = mfm.load_config()
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
        self.watch = mfm.load_watchlist()
        self.watched_listings = mfm.load_watched_listings()
        self._error_times: dict = {}
        root.report_callback_exception = self._tk_error
        self.ignored = set(mfm.load_ignorelist())
        self.shops_today = 0
        self.logged_today = 0
        self.backlog = 0        # captured frames waiting for OCR (0 = reading live)
        self._behind = False    # "the OCR is behind" is showing in the status bar
        self.chan_watch = mfm.ChannelWatch()   # change-channel dialog: seen, then gone
        self.chan_popup = None

        root.title("Free Market Tracker")
        root.geometry("1300x700")
        root.minsize(980, 500)
        self._build()
        self.refresh_tables()
        root.after(100, self.drain_events)
        if self.cfg.get("channel_prompt"):
            root.after(500, lambda: self.ask_channel(changed=False))
        threading.Thread(target=self._channel_idle_check, daemon=True).start()
        if not mfm.ITEMS_PATH.exists():
            root.after(800, self.offer_item_list)
        elif mfm.needs_type_refresh() or mfm.needs_map_refresh():
            root.after(800, self.offer_type_refresh)
        root.after(5000, self.periodic_refresh)
        root.after(1500, self.check_db_size)
        root.after(2000, lambda: self.run_retention(quiet=True))   # every launch
        root.after(6 * 3600 * 1000, self.retention_tick)           # and every 6 hours
        root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        ttk.Button(bar, text="Settings", command=self.settings).pack(side="right", padx=6)
        ttk.Button(bar, text="Import prices", command=self.import_ref).pack(side="right")
        ttk.Button(bar, text="Export prices", command=self.export_prices).pack(side="right", padx=(0, 6))
        self.start_btn = ttk.Button(bar, text="▶  Start monitoring", command=self.toggle, width=17)
        self.start_btn.pack(side="left")
        ttk.Button(bar, text="Shop area", command=self.calibrate).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Map area",
                   command=lambda: self.calibrate(key="map_region")).pack(side="left", padx=(6, 0))
        b = ttk.Button(bar, text="Chat area", command=lambda: self.calibrate(key="chat_region"))
        b.pack(side="left", padx=(6, 0))
        tip("Draw a box around the chat messages, if the chat window isn't found by itself. "
            "Settings → Auto Detection goes back to finding it.", b)
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
        self.only_deals = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Deals only", variable=self.only_deals,
                        command=self.refresh_tables).pack(side="right")
        self.show_sold = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include sold", variable=self.show_sold,
                        command=self.refresh_tables).pack(side="right", padx=10)
        self.show_ignored = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show ignored", variable=self.show_ignored,
                        command=self.refresh_tables).pack(side="right")
        self.show_outliers = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show outliers", variable=self.show_outliers,
                        command=self.refresh_tables).pack(side="right", padx=10)
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
                                       "sold_med", "sold_n", "market", "npc_buy", "npc_sell", "source"))
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
        self.live_tab = live
        self.nb.select(live)
        self._build_review_tab()
        self._build_watched_tab()
        self._build_ignore_tab()
        self._build_inventory_tab()

        self.status = ttk.Label(self.root, style="Status.TLabel", padding=(10, 4))
        self.status.pack(fill="x")
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
        self.worker = threading.Thread(target=self.monitor.run, args=(self.stop_event,), daemon=True)
        self.worker.start()
        self.start_btn.config(text="■  Stop monitoring")
        self._set_status("Monitoring — open shops and scroll through them normally.")
        self._mega_status_text(None)

    def stop(self):
        aborting = False
        if self.stop_event:
            if self.stop_event.is_set() and self.monitor:
                self.monitor.abort = True   # pressed again while finishing: drop the queue
                aborting = True
            self.stop_event.set()
        self.start_btn.config(text="▶  Start monitoring")
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

    def next_shop(self):
        """Manually split visits, for when you hop shops without the window closing."""
        if self.monitor:
            self.monitor.request_split()
            self._set_status("Next shop: the current visit was ended (Ctrl+N).")

    # ------------------------------------------------------------------ channel prompt
    def _channel_idle_check(self):
        """Background thread: while not monitoring, look for the game's
        change-channel dialog about once a second (monitoring does the same
        check on its own screenshots)."""
        import time

        while True:
            time.sleep(1.0)
            try:
                if not self.cfg.get("channel_prompt") or (self.worker and self.worker.is_alive()):
                    continue
                # Wayland screenshots go through a screenshot tool (too heavy to run
                # every second); macOS needs Screen Recording permission first
                if mfm.session_type() not in ("windows", "x11", "macos") or not mfm.mac_screen_access():
                    continue
                if self.chan_watch.feed(mfm.grab_full_screen()):
                    self.events.put(("channel_change", None))
            except Exception:
                time.sleep(5)

    def on_channel_change(self, _p=None):
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
        count = max(1, min(100, int(self.cfg.get("channel_count", 20) or 20)))
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
        self.root.after(100, self.drain_events)

    def on_session(self, p):
        self.live.delete(*self.live.get_children())
        self.scopes = {k: v for k, v in self.scopes.items() if k[0] != str(self.live)}
        shop = (" · ".join(x for x in (p.get("title") and f"Shop: {p['title']}",
                                        p.get("seller") and f"Player: {p['seller']}") if x)
                or p["shop"])
        self.shop_title.config(text="  ·  ".join(x for x in (
            shop, p["map"], f"opened {p['started']:%H:%M:%S}") if x))
        self.last_map = p["map"] or self.last_map
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
                                              "channel": s.channel}
        self.live.yview_moveto(1.0)
        if s.status != "seen":
            self.logged_today += 1
        # deals only make a sound for items on your alert list (a watched item under
        # its max price already pinged above)
        if verdict == "DEAL" and not hit and not ign and any(w["item"] == s.item for w in self.watch):
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
            win = tk.Toplevel(self.root)
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
        region, mregion = self.cfg.get("region"), self.cfg.get("map_region")

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
            again = [k for k in tree.get_children("") if key(tree, k) in chosen]
            if again:
                tree.selection_set(again)
            tree.yview_moveto(top)
        return restore

    def refresh_tables(self):
        cfg, con = self.cfg, self.con
        if any(getattr(t, "dragging", False) for t in (self.deals, self.items)):
            return   # mid-selection; the next refresh will catch up
        self.refresh_alerts()
        self.refresh_ignored()
        self.refresh_review()
        self.refresh_inventory()
        row_key = lambda t, k: tuple(t.set(k, c) for c in ("item", "each", "status", "shop", "player", "map",
                                                           "channel"))
        restore_deals = self._keep_selection(self.deals, row_key)
        restore_items = self._keep_selection(self.items, lambda t, k: t.set(k, "item"))
        try:
            hours = max(1, int(self.hours_var.get()))
        except (tk.TclError, ValueError):
            hours = 24
        since = mfm.utc_ago(hours=hours)
        sold_filter = "" if self.show_sold.get() else "AND COALESCE(sold, 0) = 0"
        rows = con.execute(
            "SELECT item, price, qty, shop, seller, map, channel, COALESCE(sold, 0), "
            "MAX(COALESCE(last_seen, ts)), MAX(COALESCE(notes, '')) FROM listings "
            "WHERE COALESCE(last_seen, ts) >= ? "
            f"{sold_filter} GROUP BY item, price, qty, shop, seller, map, sold",
            (since,)).fetchall()
        scored = []
        bounds: dict[str, object] = {}
        for item, price, qty, shop, seller, mp, channel, sold, ts, notes in rows:
            each = price
            mkt, _ = mfm.market_rate(con, item, cfg)
            if item not in bounds:
                bounds[item] = mfm.outlier_bounds(con, item, cfg)
            v, pct = mfm.verdict(each, mkt, cfg["deal_threshold_pct"], bounds[item])
            if v == "OUTLIER" and not self.show_outliers.get():
                continue
            if self.only_deals.get() and (v != "DEAL" or sold):
                continue
            if item in self.ignored and not self.show_ignored.get():
                continue
            scored.append((sold, pct if pct is not None else 999, item, qty, each, mkt, v,
                           (shop or "", seller or "", mp or "", channel or ""), ts, price, shop, seller,
                           notes))
        scored.sort()
        self.deals.delete(*self.deals.get_children())
        self.scopes = {k: v for k, v in self.scopes.items() if k[0] != str(self.deals)}
        for sold, pct, item, qty, each, mkt, v, where, ts, price, shop, seller, notes in scored:
            found = not sold and (bool(mfm.watch_match(self.watch, item, each)) or bool(
                mfm.watched_listing_match(self.watched_listings, item, shop or "", seller or "", price)))
            tag = "sold" if sold else ("ignored" if item in self.ignored else tag_for(v))
            iid = self.deals.insert("", "end", image=self.icon_for(item, found=found),
                              tags=(tag,) + (("watch",) if found else ()), values=(
                item, mfm.fmt(each), mfm.fmt(mkt),
                diff_text(None if pct == 999 else pct),
                v, "sold" if sold else "for sale", notes, *where, mfm.local_time(ts)))
            self.scopes[(str(self.deals), iid)] = {"price": price, "qty": qty, "shop": shop, "seller": seller,
                                                   "title": shop or "", "map": where[2],
                                                   "channel": where[3]}

        best = mfm.cheapest(con, 24, cfg)
        self.items.delete(*self.items.get_children())
        filt = self.item_filter() if hasattr(self, "f_name") else {}
        shown = total = 0
        for (item,) in con.execute(
                "SELECT DISTINCT item FROM listings ORDER BY item COLLATE NOCASE"):
            total += 1
            if filt and not mfm.item_matches(item, filt):
                continue
            shown += 1
            mkt, src = mfm.market_rate(con, item, cfg)
            info = mfm.item_info(item)
            st = mfm.price_stats(con, item, cfg, bounds.get(item, "auto"))
            b = best.get(item)
            tag = ""
            if item in self.ignored:
                tag = "ignored"
            elif b:
                v, _ = mfm.verdict(b[0], mkt, cfg["deal_threshold_pct"])
                tag = tag_for(v) if v == "DEAL" else ""
            found = bool(b) and bool(mfm.watch_match(self.watch, item, b[0]))
            self.items.insert("", "end", image=self.icon_for(item, found=found),
                              tags=(tag,) + (("watch",) if found else ()), values=(
                item, mfm.fmt(b[0]) if b else "-", (b[1] or "") if b else "", (b[2] or "") if b else "",
                (b[3] or "") if b else "", (b[4] or "") if b else "",
                mfm.fmt(st["listed_median"]), st["listed_n"],
                mfm.fmt(st["sold_median"]), st["sold_n"], mfm.fmt(mkt),
                mfm.fmt(info.get("npcBuy")) if info and info.get("npcBuy") else "-",
                mfm.fmt(info.get("npcSell")) if info and info.get("npcSell") else "-", src))
        if hasattr(self, "f_count"):
            self.f_count.config(text=f"{shown:,} of {total:,} items" if shown != total else
                                f"{total:,} items")
        restore_deals()
        restore_items()

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
        self.refresh_tables()

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
                  "Leave the max price empty for any price.").pack(side="left")
        add = ttk.Frame(top_half)
        add.pack(fill="x")
        ttk.Label(add, text="Item:").pack(side="left")
        self.alert_entry = ItemSearch(add, self, width=42, on_return=lambda: self.alert_max_entry.focus_set())
        self.alert_entry.pack(side="left", padx=4)
        self.alert_item = self.alert_entry.var
        ttk.Label(add, text="Max price:").pack(side="left", padx=(10, 0))
        self.alert_max = tk.StringVar()
        self.alert_max_entry = ttk.Entry(add, textvariable=self.alert_max, width=12)
        self.alert_max_entry.pack(side="left", padx=4)
        self.alert_max_entry.bind("<Return>", lambda e: self.add_alert_from_form())
        ttk.Button(add, text="Watch", command=self.add_alert_from_form).pack(side="left", padx=4)
        ttk.Button(add, text="Remove selected", command=self.remove_alert).pack(side="right")

        self.alerts = make_tree(top_half, ("item", "max_each", "last_price", "shop", "player", "map",
                                           "channel", "time"))
        self.alerts.frame.pack(fill="both", expand=True, pady=(6, 0))
        self.alerts.bind("<Double-1>", lambda e: self.open_history(self.alerts))
        self._list_menu(self.alerts, [
            ("Show history", self.open_history, True),
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

    # ------------------------------------------------------------------ megaphones
    def _build_megaphones_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.insert(0, tab, text="  Megaphones  ")
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Keep megaphones for").pack(side="left")
        self.mega_hours = tk.IntVar(value=int(self.cfg.get("megaphone_hours", 24) or 24))
        sp = ttk.Spinbox(top, from_=1, to=720, width=5, textvariable=self.mega_hours,
                         command=self._mega_hours_changed)
        sp.pack(side="left", padx=4)
        sp.bind("<Return>", lambda e: self._mega_hours_changed())
        sp.bind("<FocusOut>", lambda e: self._mega_hours_changed())
        tip("Megaphones older than this are deleted.", sp)
        ttk.Label(top, text="hours. Newest first, read from the chat window while monitoring.").pack(side="left")
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
            t = make_tree(f, ("time", "player", "channel", "message"))
            t.configure(show="headings")
            t.column("message", stretch=True)
            t.column("player", width=130)
            t.column("time", width=140)
            t.frame.pack(fill="both", expand=True)
            self._list_menu(t, [("Copy message", lambda tr: self._copy_megaphone(tr, "message"), False),
                                ("Copy player name", lambda tr: self._copy_megaphone(tr, "player"), False),
                                None,
                                ("Delete", lambda tr: self.delete_megaphones(tr), False)])
            t.bind("<Delete>", lambda e, tr=t: self.delete_megaphones(tr))
            t.bind("<Double-1>", lambda e, tr=t: self._copy_megaphone(tr, "message"))
            panes.add(f, weight=1)
            self.mega_trees[kind] = t
        self._mega_status_text(None)
        self.refresh_megaphones()

    def _mega_status_text(self, found):
        running = bool(self.worker and self.worker.is_alive())
        manual = self.cfg.get("chat_region")
        self.mega_status.config(text=(
            "Start monitoring to read megaphones." if not running else
            "Reading the chat area you drew." if manual and found else
            "Reading the chat window." if found else
            "Chat window not found: open it in the game (or set the Chat area)." if found is False else
            "Looking for the chat window…"))

    def on_chat(self, p):
        self._mega_status_text(p.get("found"))

    def on_megaphones(self, n):
        self.refresh_megaphones()

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
            self.cfg["megaphone_hours"] = hours
            mfm.save_config(self.cfg)
        self.refresh_megaphones()

    def refresh_megaphones(self):
        if not hasattr(self, "mega_trees"):
            return
        hours = max(1, int(self.cfg.get("megaphone_hours", 24) or 24))
        mfm.purge_megaphones(self.con, hours)
        rows = self.con.execute(
            "SELECT id, ts, player, channel, message, kind, color FROM megaphones WHERE ts >= ? "
            "AND kind IS NOT NULL ORDER BY ts DESC, id DESC", (mfm.utc_ago(hours=hours),)).fetchall()
        for kind, t in self.mega_trees.items():
            restore = self._keep_selection(t, lambda tr, k: k)
            t.delete(*t.get_children())
            for mid, ts, player, ch, msg, k, color in rows:
                if k not in (kind, "both"):
                    continue
                tags = ()
                if color and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    tag = "mc" + color[1:].lower()
                    if tag not in getattr(t, "color_tags", set()):
                        r_, g_, b_ = (int(color[i:i + 2], 16) for i in (1, 3, 5))
                        dark = 0.299 * r_ + 0.587 * g_ + 0.114 * b_ < 140
                        t.tag_configure(tag, background=color, foreground="white" if dark else "black")
                        t.color_tags = getattr(t, "color_tags", set()) | {tag}
                    tags = (tag,)
                t.insert("", "end", iid=str(mid), tags=tags, values=(mfm.local_time(ts), player, ch or "", msg))
            restore()

    def _copy_megaphone(self, tree, col):
        vals = [tree.set(k, col) for k in tree.selection()]
        if vals:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(vals))
            self._set_status(f"Copied {mfm.plural(len(vals), 'line')}.")

    def delete_megaphones(self, tree):
        ids = [int(k) for k in tree.selection()]
        if not ids:
            return
        self.con.executemany("DELETE FROM megaphones WHERE id = ?", [(i,) for i in ids])
        self.con.commit()
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
        stats = {}
        rows = mfm.load_inventory(self.inv_con)
        for r in rows:
            item, qty = r["item"], int(r["qty"] or 1)
            st = stats.get(item) or stats.setdefault(item, mfm.inventory_stats(self.con, item, self.cfg))
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
                if st["low"] is None and not st["market"]:
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
        win = tk.Toplevel(self.root)
        self.inv_scan = win
        win.title("Auto Detect (beta)")
        win.geometry("920x660")
        win.minsize(760, 560)
        top = ttk.Frame(win, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Open your Item Inventory in the game. Its items show here; select the "
                  "ones you want and click Add.",
                  wraplength=560, justify="left").pack(side="left")
        b = ttk.Button(top, text="Inventory area", command=lambda: self.calibrate(key="inventory_region"))
        b.pack(side="right")
        tip("Draw a box around your Item Inventory window, if it isn't found by itself. "
            "Settings → Auto Detection goes back to finding it.", b)
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
        dlg = tk.Toplevel(parent)
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
                manual = self.cfg.get("inventory_region")
                if manual and not mfm.is_auto(manual, full.size):
                    region, how = manual, "manual area"
                else:
                    region = mfm.find_inventory(full)
                    how = f"auto, found at {region[0]},{region[1]}" if region else ""
                if not region:
                    self.events.put(("inv_scan", {"status": "Item Inventory not found. Open it in the game "
                                                            "(or set the Inventory area)."}))
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
        restore = self._keep_selection(self.alerts, lambda t, k: t.set(k, "item"))
        self.alerts.delete(*self.alerts.get_children())
        for w in self.watch:
            lf = w.get("last_found") or {}
            if lf and "where" in lf:        # saved by versions before 2610.5: "shop · map"
                parts = lf["where"].split(" · ")
                lf = {"each": lf.get("each"), "shop": parts[0] if parts else "",
                      "map": parts[1] if len(parts) > 1 else "", "channel": None, "time": lf.get("when")}
            self.alerts.insert("", "end", image=self.icon_for(w["item"], found=bool(lf)), values=(
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

    def add_alert(self, item, max_each=None):
        item = item.strip()
        if not item:
            return
        self.watch = [w for w in self.watch if w["item"] != item]
        self.watch.append({"item": item, "max_each": max_each, "last_found": None})
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self.refresh_tables()
        self._set_status(f"Watching {item}" + (f" at {mfm.fmt(max_each)} or less" if max_each else "") + ".")

    def add_alert_from_form(self):
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
        items = [self.alerts.set(k, "item") for k in self.alerts.selection()]
        if not items:
            return
        cur = ""
        if len(items) == 1:
            w = next((w for w in self.watch if w["item"] == items[0]), None)
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
            if w["item"] in items:
                w["max_each"] = cap
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self.refresh_tables()
        self._set_status(f"Max price {'set to ' + mfm.fmt(cap) if cap else 'removed (any price)'} for "
                         + (f"'{items[0]}'." if len(items) == 1 else f"{len(items)} items."))

    def remove_alert(self):
        sel = self.alerts.selection()
        if not sel:
            return
        gone = {self.alerts.set(k, "item") for k in sel}
        self.watch = [w for w in self.watch if w["item"] not in gone]
        mfm.save_watchlist(self.watch)
        self.refresh_alerts()
        self.refresh_tables()

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
    def _build_review_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.review_tab = tab
        self.nb.add(tab, text="  Review  ")
        ttk.Label(tab, wraplength=1040, justify="left", text=(
            "Names not in the item list, with the closest item. Enter = accept · M = pick an "
            "item · K = keep the name · Delete = delete its listings.")).pack(anchor="w", pady=(0, 6))
        self.review = make_tree(tab, ("item", "suggest", "score", "count", "time", "raw"))
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
        ttk.Button(bottom, text="Match", command=lambda: self.match_dialog(self.review)).pack(side="left", padx=4)
        ttk.Button(bottom, text="Keep as is", command=self.review_keep).pack(side="left")
        ttk.Button(bottom, text="Delete listings", command=self.review_delete).pack(side="left", padx=4)
        ttk.Button(bottom, text="Accept all 90%+ alike", command=self.review_accept_confident
                   ).pack(side="left", padx=(16, 0))
        self.review_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="Remember matches for future reads", variable=self.review_remember
                        ).pack(side="left", padx=(16, 0))
        self.review_show_kept = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Show kept names", variable=self.review_show_kept,
                        command=self.refresh_review).pack(side="right")
        self.refresh_review()

    def refresh_review(self):
        if not hasattr(self, "review"):
            return
        t = self.review
        if getattr(t, "dragging", False):
            return
        restore = self._keep_selection(t, lambda tr, k: tr.set(k, "item"))
        rows = mfm.unmatched_items(self.con, include_kept=self.review_show_kept.get())
        t.delete(*t.get_children())
        self.review_rows = {}
        for r in rows:
            tag = "kept" if r["kept"] else "good" if r["score"] >= 0.9 else "weak" if r["suggest"] else ""
            iid = t.insert("", "end", image=self.icon_for(r["item"]), tags=(tag,), values=(
                r["item"], (r["suggest"] or "— no close match —") + ("   (kept as is)" if r["kept"] else ""),
                f"{r['score']:.0%}" if r["suggest"] else "", r["count"],
                mfm.local_time(r["last"]), r["raw"]))
            self.review_rows[iid] = r
        restore()
        n = sum(1 for r in rows if not r["kept"])
        self.nb.tab(self.review_tab, text=f"  Review ({n})  " if n else "  Review  ")

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
        win = tk.Toplevel(self.root)
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
        for item in items or ():
            self._forget_icon(item)
        self.icon_lists = [l for l in self.icon_lists if l.alive()]
        for l in self.icon_lists:
            l.refresh_images()
        for k in self.live.get_children():
            self.live.item(k, image=self.icon_for(self.live.set(k, "item")))
        self.refresh_tables()

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
        meowdb list get a small red "!" badge; watched items found for sale get a
        green outline; '' when there's nothing to show."""
        warn = mfm.needs_review(item)
        key = f"{item}@{size}{'!' if warn else ''}{'g' if found else ''}"
        if key not in self.icons:
            img = mfm.load_icon(self.con, item)
            if img is None and not warn and not found:
                return ""
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
    def calibrate(self, then_start=False, key="region"):
        was_running = bool(self.worker and self.worker.is_alive())
        self.root.withdraw()
        self.root.update()

        def grab():
            try:
                shot = mfm.grab_full_screen()
            except SystemExit as e:  # missing Wayland screenshot tool
                self.root.deiconify()
                messagebox.showerror("Can't capture screen", str(e))
                return

            def done(region):
                self.root.deiconify()
                if region:
                    self.cfg[key] = region  # monitor shares this dict, picks it up next frame
                    self._update_stats(None, None)
                    label = {"map_region": "Map area", "inventory_region": "Inventory area",
                             "chat_region": "Chat area"}.get(key, "Shop area")
                    self._set_status(f"{label} set to {region}.")
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
        win = tk.Toplevel(self.root)
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
        win = tk.Toplevel(parent)
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

    def settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        fields = [
            ("interval_seconds", "Capture every (seconds)", 0.05, 10, 0.05),
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
            "confirm_frames": "How many matching reads a listing needs before it's saved.",
            "unmatched_extra_frames": "Extra reads for names not in the item list, before they're "
                                      "saved.",
            "close_frames": "Reads without a shop before the shop counts as closed.",
            "history_days": "How many days of sightings make the market price.",
            "upscale": "Raise it if small text is misread. Higher is slower.",
            "min_sightings": "Sightings an item needs before it gets a market price, DEAL / HIGH "
                             "and outliers.",
            "outlier_ceiling_pct": "Prices this far above usual are left out of the market price.",
            "outlier_floor_pct": "Prices this far below usual are left out of the market price "
                                 "(still shown as deals).",
        }
        vars_ = {}
        for i, (key, label, lo, hi, step) in enumerate(fields):
            lbl = ttk.Label(win, text=label)
            lbl.grid(row=i, column=0, sticky="w", padx=12, pady=4)
            v = tk.StringVar(value=str(self.cfg.get(key, mfm.DEFAULT_CONFIG[key])))
            spin = ttk.Spinbox(win, from_=lo, to=hi, increment=step, width=8, textvariable=v)
            spin.grid(row=i, column=1, padx=12, pady=4)
            tip(tips[key], lbl, spin)
            vars_[key] = (v, type(mfm.DEFAULT_CONFIG[key]))
        # ---- data retention (top right)
        ret = ttk.LabelFrame(win, text=" Data retention ", padding=(10, 2, 10, 4))
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
            return {"keep_days": days, "retention": mode_var.get(),
                    "archive_dir": arch_var.get().strip() or None}

        def show_size():
            size = mfm.db_size()
            try:
                old = mfm.old_listing_count(self.con, ret_cfg()["keep_days"])
            except Exception:
                old = 0
            size_lbl.config(text=f"Database {size / 1048576:,.1f} MB  ·  {old:,} to clean up",
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
        cb = ttk.Checkbutton(win, text="Save every capture as PNG (for debugging OCR)", variable=save_caps)
        cb.grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=12, pady=4)
        tip("Saves a picture of every screen read, for troubleshooting. Leave off normally.", cb)
        # ---- channel picker (under data retention)
        chan_prompt = tk.BooleanVar(value=self.cfg.get("channel_prompt", True))
        cb = ttk.Checkbutton(win, text="Ask for the channel at launch and after a channel change",
                             variable=chan_prompt)
        cb.grid(row=5, column=2, sticky="w", padx=(0, 12), pady=4)
        tip("Asks for your channel when the app opens and after you change channel in the game.", cb)
        rc = ttk.Frame(win)
        rc.grid(row=6, column=2, sticky="w", padx=(0, 12), pady=4)
        chan_count = tk.StringVar(value=str(self.cfg.get("channel_count", 20)))
        lc = ttk.Label(rc, text="Channels in the channel picker")
        lc.pack(side="left")
        sc = ttk.Spinbox(rc, from_=1, to=100, increment=1, width=6, textvariable=chan_count)
        sc.pack(side="left", padx=8)
        tip("How many channel buttons the picker shows.", lc, sc)
        outliers_on = tk.BooleanVar(value=self.cfg.get("outliers_on", True))
        cb = ttk.Checkbutton(win, text="Ignore outliers in market rates",
                             variable=outliers_on)
        cb.grid(row=len(fields) - 3, column=2, sticky="w", padx=(0, 12))
        tip("Leaves troll prices and misreads out of the market price.", cb)
        ttk.Label(win, foreground="#777", justify="left", text=(
            "200% ceiling = more than 3x the usual price.\n"
            "75% floor = less than 1/4 of it.")).grid(row=len(fields) - 2, column=2, rowspan=2,
                                                       sticky="nw", padx=(0, 12))

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
            self.cfg["channel_prompt"] = chan_prompt.get()
            try:
                self.cfg["channel_count"] = max(1, min(100, int(float(chan_count.get()))))
            except ValueError:
                messagebox.showerror("Invalid value", "Channels in the channel picker: a number from 1 to 100.",
                                     parent=win)
                return
            self.cfg["outliers_on"] = outliers_on.get()
            mfm.save_config(self.cfg)
            self.refresh_tables()
            win.destroy()

        def reset_auto():
            self.cfg["region"] = self.cfg["map_region"] = self.cfg["inventory_region"] = None
            self.cfg["chat_region"] = None
            mfm.save_config(self.cfg)
            self.found_at = None
            self._update_stats(None, None)
            self._set_status("The shop, map, inventory and chat will be found automatically.")
        b = ttk.Button(win, text="Auto Detection", command=reset_auto)
        b.grid(row=len(fields) + 1, column=0, sticky="w", padx=12, pady=(8, 0))
        tip("Find the shop, minimap, inventory and chat automatically again.", b)
        ttk.Label(win, foreground="#666", wraplength=620, justify="left", text=(
            "Forgets the shop, map, inventory and chat areas you drew and finds them automatically."
            )).grid(row=len(fields) + 1, column=1, columnspan=2, sticky="w", padx=(0, 12), pady=(8, 0))
        self._game_data_frame(win).grid(row=len(fields) + 2, column=0, columnspan=3, sticky="we",
                                        padx=12, pady=(12, 0))
        bottom = ttk.Frame(win)
        bottom.grid(row=len(fields) + 3, column=0, columnspan=3, sticky="we", padx=12, pady=(12, 12))
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

    def about(self, parent=None):
        import webbrowser

        win = tk.Toplevel(parent or self.root)
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

    def open_history(self, tree):
        sel = tree.selection()
        if not sel:
            return
        item = tree.set(sel[0], "item")
        if not self.con.execute("SELECT 1 FROM listings WHERE item = ?", (item,)).fetchone():
            messagebox.showinfo(item, f"'{item}' hasn't been seen in any shop yet.", parent=self.root)
            return
        win = tk.Toplevel(self.root)
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
            "Right-click a sighting to change its price or channel, add a note, watch it, or delete it. "
            + ("Option+click" if MAC else "Alt+click") + " selects several."))
        hint.pack(anchor="w", fill="x")
        hint.bind("<Configure>", lambda e: hint.config(wraplength=max(e.width - 24, 200)))
        table = make_tree(win, ("each", "status", "notes", "shop", "player", "map", "channel", "time"))
        table.column("#0", width=0, minwidth=0)   # one item: no per-row icon needed
        table.column("status", width=130)
        table.frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))
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
