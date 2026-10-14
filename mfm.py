#!/usr/bin/env python3
"""
mfm-tracker - Free Market Tracker: Free Market price tracker for MapleStory Classic World.

Captures a region of your screen (the shop window), OCRs item names and prices,
logs them to a local SQLite database, and flags listings that are above or
below market rate.

Works on Windows, macOS and Linux (X11 and Wayland).

Commands:
  mfm-tracker calibrate [--map]         drag a box around the store window (or the minimap name)
  mfm-tracker capture [--channel N]     grab the store window once, OCR it, log prices
  mfm-tracker gui                       live window: monitors while you play and scroll shops
  mfm-tracker watch [--channel N]       same continuous monitoring, in the terminal
  mfm-tracker hotkey                    press F8 in-game to capture (Windows / X11 only)
  mfm-tracker scan IMAGE...             OCR existing screenshots instead of the live screen
  mfm-tracker deals                     listings from the last day vs. market rate
  mfm-tracker item NAME                 price history for one item
  mfm-tracker report                    write an HTML report ("report YYYY-MM-DD HH-MM-SS.html")
  mfm-tracker import-ref FILE.csv       load reference prices (item,price) e.g. from meso.watch
  mfm-tracker forget NAME               delete all logged listings of one item (bad OCR reads)
  mfm-tracker purge-old                 delete everything logged by the first (buggy) parser version
  mfm-tracker meowdb                    download Classic item names/ids from NiaMeowDB (meowdb.com)
  mfm-tracker fetch-icons               official meowdb icons for every item you've logged
  mfm-tracker osms [--force]            update the backup item + map lists (OSMS Data Explorer)
  mfm-tracker prune [--days N] [--delete | --archive DIR]  archive/delete old listings
  mfm-tracker import-items FILE         use your own item list (txt / csv / json)
  mfm-tracker export-items FILE         save the item list in use (json / csv keep ids, categories, icons)
  mfm-tracker import-maps FILE          use a map list for correcting map names
  mfm-tracker export-maps FILE          save the map list in use
  mfm-tracker review [--accept]         names not in the item database, with suggested matches
  mfm-tracker export FILE.csv [--all]   market prices per item (re-importable), or every sighting
Every export adds the local date and time to its file name: prices.csv is saved
as "prices 2026-10-13 08-11-00.csv".
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps

# --------------------------------------------------------------------------- paths

# Running as the packaged "Free Market Tracker" app (PyInstaller): Python and
# Tesseract are unpacked to a temporary folder (sys._MEIPASS).
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if FROZEN and sys.platform.startswith("linux"):
    # The app's own libraries were found via LD_LIBRARY_PATH when it started
    # (glibc reads it once). Programs we launch (grim, spectacle, pw-play, the web
    # browser) must use the system's libraries, so hand them the original value.
    _orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if _orig is not None:
        os.environ["LD_LIBRARY_PATH"] = _orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)



def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin" and "XDG_DATA_HOME" not in os.environ \
            and not (Path.home() / ".local" / "share" / "mfm-tracker").exists():
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "mfm-tracker"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Shown to you: YYMM.minor.hotfix. The codename is for development notes only.
VERSION = "2610.15.004"
CODENAME = "npcprices"

DATA = data_dir()
# Three kinds of data, kept apart so each can be exported, backed up or cleared
# on its own:
#   prices/    what you've seen in shops (listings) and imported reference prices
#   settings/  your own setup: settings + capture areas, alerts, ignore list,
#              manual matches, names kept as they are
#   cache/     downloaded / regenerable: meowdb item + map lists, item icons, sounds
PRICES_DIR = DATA / "prices"
SETTINGS_DIR = DATA / "settings"
CACHE_DIR = DATA / "cache"
BACKUP_DIR = DATA / "backups"
for _d in (PRICES_DIR, SETTINGS_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
DB_PATH = PRICES_DIR / "prices.db"
ICONS_DB_PATH = CACHE_DIR / "icons.db"
CONFIG_PATH = SETTINGS_DIR / "config.json"
SETTINGS_FILES = {   # what "reset personal settings" can clear
    "settings": ("Settings and capture areas", ["config.json"]),
    "alerts": ("Watched items and listings", ["watchlist.json", "watched_listings.json"]),
    "ignored": ("Ignore list", ["ignorelist.json"]),
    "matches": ("Manual matches (remembered corrections)", ["aliases.json"]),
    "kept": ("Names kept as they are (Review tab)", ["kept_names.json"]),
}


def _migrate_layout() -> None:
    """Versions before 2026.10.12-split kept everything in one folder: move each
    file to where it belongs now (once; nothing is deleted)."""
    moves = {"prices.db": PRICES_DIR,
             "config.json": SETTINGS_DIR, "watchlist.json": SETTINGS_DIR,
             "ignorelist.json": SETTINGS_DIR, "aliases.json": SETTINGS_DIR,
             "kept_names.json": SETTINGS_DIR,
             "items.txt": CACHE_DIR, "item_ids.json": CACHE_DIR, "item_icons.json": CACHE_DIR,
             "item_types.json": CACHE_DIR, "meowdb_items.json": CACHE_DIR,
             "meowdb_sync.json": CACHE_DIR, "items_source.txt": CACHE_DIR, "maps.json": CACHE_DIR,
             "ping.wav": CACHE_DIR, "captures": CACHE_DIR,
             "calibration_region.png": CACHE_DIR, "calibration_map_region.png": CACHE_DIR}
    for name, dest in moves.items():
        old, new = DATA / name, dest / name
        if old.exists() and not new.exists():
            try:
                shutil.move(str(old), str(new))
            except OSError:
                pass


_migrate_layout()

DEFAULT_CONFIG = {
    "region": None,            # store window [l, t, w, h]; None = find it automatically
    "map_region": None,        # minimap name area; None = estimate from the store position
    "interval_seconds": 0.1,   # how often the screen is captured (OCR runs at its own pace)
    "confirm_frames": 2,       # identical reads needed before a row is logged
    "unmatched_extra_frames": 2,  # extra reads for a name not in the meowdb list first
    "close_frames": 3,         # empty frames in a row = shop closed
    "hotkey": "f8",
    "deal_threshold_pct": 15,  # flag listings this % below/above market
    "history_days": 14,        # window for computing market rate from your logs
    "outliers_on": True,       # ignore absurd prices once an item has enough sightings
    "min_sightings": 5,        # sightings an item needs before it's judged (market price,
                               # DEAL / HIGH, outliers)
    "outlier_ceiling_pct": 200,  # more than this % ABOVE the median is an outlier (200 = 3x)
    "outlier_floor_pct": 75,     # more than this % BELOW the median is an outlier (75 = 1/4)
    "tesseract_cmd": None,     # set if tesseract isn't on PATH (Windows)
    "upscale": 3,              # game fonts are tiny; upscaling helps OCR a lot
    "channel_prompt": True,    # ask for the channel at launch and after a channel change
    "channel_count": 20,       # channels shown in the channel picker (1-100)
    "inventory_region": None,  # the item inventory window; None = find it automatically
    "chat_region": None,       # the chat messages (for megaphones); None = find it automatically
    "megaphone_hours": 24,     # megaphones are kept this long, then deleted
    "save_captures": False,    # keep a PNG of every capture for debugging
    "alert_sound": None,       # custom .wav for alerts (None = built-in ping)
    "alert_sound_on": True,
    "alert_raise": True,       # bring the window to the front on an alert
    "search_icons": True,      # item search shows meowdb icons (each downloaded once)
    "download_all_icons": True,  # Update from meowdb also fetches every icon not here yet
    "keep_days": 30,             # listings older than this are archived/deleted (see retention)
    "retention": "archive",      # "archive" (copy to the archive, then delete), "delete" or "keep"
    "archive_dir": None,         # where archives go (None = the data folder's archives/)
    "icon_rate": 10,             # icon downloads per second (slows down by itself if meowdb asks)
}

FRAME_QUEUE_MAX = 150   # captured frames waiting for OCR (~15-45 s of scrolling); above
                        # this the oldest give way, so memory stays modest


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text())
        if "confirm_frames" not in saved and saved.get("interval_seconds") == 3:
            saved.pop("interval_seconds")  # old default was too slow for scrolling
        if saved.get("channel_count") == 18:
            saved.pop("channel_count")     # the previous default: use the new one
        if saved.get("interval_seconds") in (0.75, 0.15):
            saved.pop("interval_seconds")  # previous defaults: use the new one
        if "outlier_min_n" in saved:       # renamed: it now applies to DEAL / HIGH too
            saved.setdefault("min_sightings", saved["outlier_min_n"])
            saved.pop("outlier_min_n")
        cfg.update(saved)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# --------------------------------------------------------------------------- time
# Times are saved in UTC ("2026-10-12T20:41:05Z"), so a database moved to another
# computer or time zone stays correct, and shown in this computer's local time.

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def utc_ago(**delta) -> str:
    """UTC timestamp `delta` ago, for "seen in the last N hours/days" queries."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime(UTC_FMT)


def to_utc(ts: str | None) -> str | None:
    """A stored time in UTC form; times saved by older versions had no zone and
    were the computer's local time."""
    if not ts or ts.endswith("Z"):
        return ts
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive = this computer's local time
    return dt.astimezone(timezone.utc).strftime(UTC_FMT)


def local_time(ts: str | None) -> str:
    """A stored time as this computer's local time: YYYY-MM-DD HH:MM."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return str(ts)[:16].replace("T", " ")
    if dt.tzinfo is not None:
        dt = dt.astimezone()          # to local
    return dt.strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- db


def db() -> sqlite3.Connection:
    """Prices database, with the icon cache attached: queries can use `icons` as
    if it were part of it, but it lives in cache/icons.db."""
    con = sqlite3.connect(DB_PATH)
    con.execute("ATTACH DATABASE ? AS cache", (str(ICONS_DB_PATH),))
    con.execute("""CREATE TABLE IF NOT EXISTS cache.icons (
            item TEXT PRIMARY KEY, png BLOB NOT NULL, faded INTEGER NOT NULL DEFAULT 0,
            updated TEXT, source TEXT)""")
    # official meowdb icons, one per icon key (several item names can share one)
    con.execute("""CREATE TABLE IF NOT EXISTS cache.meowdb_icons (
            icon_key TEXT PRIMARY KEY, png BLOB NOT NULL, fetched TEXT)""")
    # inventory icons you matched by hand: recognised automatically next time
    con.execute("""CREATE TABLE IF NOT EXISTS cache.learned_icons (
            id INTEGER PRIMARY KEY, item TEXT NOT NULL, png BLOB NOT NULL, added TEXT)""")
    # icons meowdb doesn't have (404): not asked for again for a month
    con.execute("""CREATE TABLE IF NOT EXISTS cache.meowdb_icons_missing (
            icon_key TEXT PRIMARY KEY, checked TEXT)""")
    if con.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='icons'").fetchone():
        # older versions kept icons inside prices.db: move them to the cache
        mcols = {r[1] for r in con.execute("PRAGMA main.table_info(icons)")}
        src = "source" if "source" in mcols else "NULL"
        con.execute(f"INSERT OR IGNORE INTO cache.icons (item, png, faded, updated, source) "
                    f"SELECT item, png, faded, updated, {src} FROM main.icons")
        con.execute("DROP TABLE main.icons")
        con.commit()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            item TEXT NOT NULL,
            price INTEGER NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            shop TEXT,
            raw TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_item_ts ON listings(item, ts);
        CREATE TABLE IF NOT EXISTS megaphones (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            seen TEXT,
            player TEXT,
            channel TEXT,
            message TEXT,
            kind TEXT,
            color TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_megaphones_ts ON megaphones(ts);
        CREATE TABLE IF NOT EXISTS reference (
            item TEXT PRIMARY KEY,
            price INTEGER NOT NULL,
            source TEXT,
            updated TEXT
        );
        """
    )
    mcols = {r[1] for r in con.execute("PRAGMA table_info(megaphones)")}
    for col in ("color", "votes"):
        if col not in mcols:
            con.execute(f"ALTER TABLE megaphones ADD COLUMN {col} TEXT")
    cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
    if con.execute("PRAGMA main.user_version").fetchone()[0] < 1 and "ts" in cols:
        # versions before 2610.1 saved local times without a zone: convert to UTC once
        has = [c for c in ("ts", "last_seen", "sold_ts") if c in cols]
        rows = con.execute(f"SELECT id, {', '.join(has)} FROM listings").fetchall()
        for row in rows:
            new = [to_utc(v) for v in row[1:]]
            if new != list(row[1:]):
                con.execute(f"UPDATE listings SET {', '.join(c + ' = ?' for c in has)} WHERE id = ?",
                            new + [row[0]])
        for item, upd in con.execute("SELECT item, updated FROM reference").fetchall():
            if upd and not upd.endswith("Z"):
                con.execute("UPDATE reference SET updated = ? WHERE item = ?", (to_utc(upd), item))
        con.execute("PRAGMA main.user_version = 1")
        con.commit()
    if "note" in cols and "channel" not in cols:
        # older versions called the channel a free-text "note" ("ch2"): rename the
        # column and keep just the number
        con.execute("ALTER TABLE listings RENAME COLUMN note TO channel")
        for (old,) in con.execute("SELECT DISTINCT channel FROM listings WHERE channel IS NOT NULL").fetchall():
            new = parse_channel(old)
            if new != old:
                con.execute("UPDATE listings SET channel = ? WHERE channel = ?", (new, old))
        con.commit()
        cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
    for col, typ in (("session", "TEXT"), ("seller", "TEXT"), ("map", "TEXT"), ("channel", "TEXT"),
                     ("sold", "INTEGER DEFAULT 0"), ("last_seen", "TEXT"), ("sold_ts", "TEXT"),
                     ("notes", "TEXT")):
        if col not in cols:  # upgrade older databases in place
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")
    if con.execute("SELECT 1 FROM listings WHERE qty != 1 LIMIT 1").fetchone():
        # 2610.9: no more quantities: a listing's price is the meso amount shown
        con.execute("UPDATE listings SET qty = 1 WHERE qty != 1")
        con.commit()
    if con.execute("PRAGMA main.user_version").fetchone()[0] < 3:
        # 2610.8: map names are "Area: Zone" ("Hidden Street: Free Market <2>"); give the
        # zone-only names saved before their area, once a map list is available to look it up.
        # 2610.10.001: drop the map count meowdb's index added to some areas ("( 8 )").
        try:
            if _map_names():
                for (mp,) in con.execute("SELECT DISTINCT map FROM listings WHERE map LIKE '%(%)%:%'").fetchall():
                    street, zone = _split_map(mp)
                    fixed = full_map_name(clean_street(street), zone)
                    if fixed != mp:
                        con.execute("UPDATE listings SET map = ? WHERE map = ?", (fixed, mp))
                fix_map_rows(con)
                con.execute("PRAGMA main.user_version = 3")
                con.commit()
        except Exception:
            pass
    return con


# --------------------------------------------------------------------------- capture


def session_type() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    return "x11"


LAST_SCREEN_SIZE: list[int] = []   # pixels of the latest screenshot (a Retina Mac has 2 per point)


def grab_full_screen() -> Image.Image:
    """Screenshot of the whole desktop as a PIL image."""
    st = session_type()
    if st in ("windows", "x11", "macos"):
        import mss  # imported lazily so `scan` works without it

        with mss.mss() as sct:
            mon = sct.monitors[0]  # virtual screen spanning all monitors
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            LAST_SCREEN_SIZE[:] = img.size
            return img

    # Wayland: X11 grabbers only see black, so shell out to a native tool.
    import threading
    tmp = Path(tempfile.gettempdir()) / f"mfm-tracker-grab-{threading.get_ident()}.png"
    candidates = [
        ["grim", str(tmp)],                                   # Hyprland, Sway, river...
        ["spectacle", "-b", "-n", "-f", "-o", str(tmp)],      # KDE Plasma
        ["gnome-screenshot", "-f", str(tmp)],                 # GNOME
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            img = Image.open(tmp).convert("RGB")
            img.load()
            LAST_SCREEN_SIZE[:] = img.size
            return img
    sys.exit(
        "No screenshot tool found. Install the one for your desktop:\n"
        "  grim (Hyprland, Sway), spectacle (KDE) or gnome-screenshot (GNOME)"
    )


def mac_screen_access(ask: bool = False) -> bool:
    """macOS only: has this app been allowed to record the screen (System Settings >
    Privacy & Security > Screen Recording)? Without it screenshots show only the
    desktop wallpaper, so no shop is ever found. ask=True shows Apple's prompt."""
    if sys.platform != "darwin":
        return True
    try:
        import ctypes
        cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        if cg.CGPreflightScreenCaptureAccess():
            return True
        if ask:
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGRequestScreenCaptureAccess())
        return False
    except Exception:          # macOS before 10.15 has no such permission
        return True


def grab_region(region) -> Image.Image:
    full = grab_full_screen()
    if not region:
        return full
    l, t, w, h = region
    return full.crop((l, t, l + w, t + h))


CALIBRATE_HINTS = {
    "region": "Drag a box around the whole store window: from the shop title bar down to "
              "the bottom of the item list. Esc cancels.",
    "map_region": "Drag a box around the minimap's street and map name "
                  "(e.g. 'Hidden Street / Free Market <2>'). Esc cancels.",
    "inventory_region": "Drag a box around your whole ITEM INVENTORY window. Esc cancels.",
    "chat_region": "Drag a box around the chat messages: not the scroll bar or the white chat "
                   "bar below them. Esc cancels.",
}


def calibrate(parent=None, shot=None, on_done=None, key="region") -> None:
    """Show a frozen screenshot full-screen and let the user drag a box.
    Standalone from the CLI, or as a Toplevel over the GUI when `parent` is given."""
    import tkinter as tk
    from PIL import ImageTk

    if shot is None:
        print("Switch to the game with a shop open. Capturing in 3 seconds...")
        time.sleep(3)
        shot = grab_full_screen()

    root = tk.Toplevel(parent) if parent else tk.Tk()
    root.attributes("-fullscreen", True)
    root.title("Free Market Tracker - drag around the shop item list, Esc to cancel")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = min(sw / shot.width, sh / shot.height, 1.0)
    disp = shot.resize((int(shot.width * scale), int(shot.height * scale)))
    photo = ImageTk.PhotoImage(disp)
    canvas = tk.Canvas(root, width=disp.width, height=disp.height, cursor="cross")
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)
    canvas.create_text(20, 20, anchor="nw", fill="yellow", font=("Arial", 16, "bold"),
                       text=CALIBRATE_HINTS[key])
    state = {"start": None, "rect": None, "result": None}

    def down(e):
        state["start"] = (e.x, e.y)
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def move(e):
        if state["rect"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)

    def up(e):
        x0, y0 = state["start"]
        x1, y1 = e.x, e.y
        l, t = min(x0, x1) / scale, min(y0, y1) / scale
        w, h = abs(x1 - x0) / scale, abs(y1 - y0) / scale
        if w > 10 and h > 10:
            state["result"] = [int(l), int(t), int(w), int(h)]
        root.destroy()

    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", up)
    root.bind("<Escape>", lambda e: root.destroy())
    root.focus_force()
    if parent:
        root.photo = photo  # keep a reference so Tk doesn't garbage-collect it
        root.wait_window()
    else:
        root.mainloop()

    if on_done:
        on_done(state["result"])
    if state["result"]:
        cfg = load_config()
        cfg[key] = state["result"]
        save_config(cfg)
        preview = CACHE_DIR / f"calibration_{key}.png"
        crop(shot, state["result"]).save(preview)
        print(f"Saved {key} {state['result']}. Preview: {preview}")
    else:
        print("Cancelled.")


# --------------------------------------------------------------------------- OCR + parsing
#
# A MapleStory Classic player-shop window looks like this:
#
#   [ 111                                 ]   <- shop title (white bar)
#   [avatar]            SELLER INFO / BUY ITEM / LEAVE STORE
#   [ liormm ]  (coin) [ 152,780 ]           <- seller name, seller's mesos
#   [icon] Cutlus
#          1 for 39,999 mesos                 <- every listing is two lines
#   [icon] Crossbow Attack Scroll: I...       <- long names are truncated
#          1 for 69,999 mesos
#
# Calibrate the capture area around the whole store window (title bar to the
# bottom of the item list) so the shop title and seller can be read too.


def bundled_tesseract() -> Path | None:
    """The Tesseract shipped inside the packaged app, if this is the app."""
    if not FROZEN:
        return None
    d = BUNDLE / "tesseract"
    if sys.platform == "win32":
        exe = d / "tesseract.exe"
        return exe if exe.exists() else None
    # Linux and macOS: a wrapper that points it at its own libs + tessdata
    run = d / "tesseract-run"          # wrapper: points it at its own libs + tessdata
    if run.exists():
        for f in (run, d / "tesseract.bin"):
            try:
                os.chmod(f, 0o755)
            except OSError:
                pass
        return run
    return None


def setup_tesseract(cfg: dict) -> None:
    import pytesseract

    if cfg.get("tesseract_cmd"):
        pytesseract.pytesseract.tesseract_cmd = cfg["tesseract_cmd"]
    elif bundled_tesseract():
        pytesseract.pytesseract.tesseract_cmd = str(bundled_tesseract())
        if sys.platform == "win32":
            os.environ.setdefault("TESSDATA_PREFIX", str(BUNDLE / "tesseract" / "tessdata"))
    elif sys.platform == "win32" and not shutil.which("tesseract"):
        default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if default.exists():
            pytesseract.pytesseract.tesseract_cmd = str(default)
    elif sys.platform == "darwin" and not shutil.which("tesseract"):
        for c in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):   # Homebrew
            if Path(c).exists():
                pytesseract.pytesseract.tesseract_cmd = c
                break
    elif not shutil.which("tesseract") and shutil.which("tesseract-ocr"):
        # Void Linux names the program tesseract-ocr
        pytesseract.pytesseract.tesseract_cmd = shutil.which("tesseract-ocr")


def preprocess(img: Image.Image, upscale: int, mode: str = "gray") -> Image.Image:
    """Upscale and tone-map for Tesseract.

    "gray": greyscale + contrast stretch (good for headers and normal rows).
    "blue": soft threshold on the blue channel. Item rows sit on bluish panels
            (blue >= 220) while text, including the faded grey text of sold
            items (~187), is lower in blue, so this separates both cleanly.
    """
    if mode == "blue":
        b = img.convert("RGB").split()[2]
        if upscale > 1:
            b = b.resize((b.width * upscale, b.height * upscale), Image.LANCZOS)
        return b.point(_BLUE_LUT)
    g = ImageOps.grayscale(img)
    if upscale > 1:
        g = g.resize((g.width * upscale, g.height * upscale), Image.LANCZOS)
    hist = g.histogram()
    if sum(i * n for i, n in enumerate(hist)) / max(sum(hist), 1) < 110:
        g = ImageOps.invert(g)  # mostly-dark capture: make text dark on light
    return ImageOps.autocontrast(g)


import math as _math
_BLUE_LUT = [int(255 / (1 + _math.exp(-(p - 200) / 18 * 4))) for p in range(256)]


@dataclass
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float


def ocr_words(img: Image.Image, cfg: dict, mode: str = "gray") -> list[list[Word]]:
    """OCR into lines of positioned words (coordinates in the original image)."""
    import pytesseract

    setup_tesseract(cfg)
    up = cfg.get("upscale", 3)
    d = pytesseract.image_to_data(preprocess(img, up, mode), config="--psm 6",
                                  output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[Word]] = {}
    for i, text in enumerate(d["text"]):
        if not text.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append(Word(
            text.strip(), d["left"][i] // up, d["top"][i] // up,
            d["width"][i] // up, d["height"][i] // up, float(d["conf"][i])))
    out = [sorted(ws, key=lambda w: w.left) for _, ws in sorted(lines.items())]
    out.sort(key=lambda ws: min(w.top for w in ws))
    return out


def ocr_lines(img: Image.Image, cfg: dict) -> list[str]:
    return [" ".join(w.text for w in ws) for ws in ocr_words(img, cfg)]


@dataclass
class Listing:
    item: str
    price: int
    qty: int
    raw: str
    sold: bool = False
    icon: "Image.Image | None" = None   # the item's icon, cropped from the store row


@dataclass
class ShopFrame:
    listings: list[Listing]
    title: str = ""
    seller: str = ""
    geom: dict | None = None     # learned text-column geometry (fractions of the image)
    hidden: int = 0              # rows skipped because the mouse cursor / a tooltip covered them

    @property
    def shop(self) -> str:
        if self.title and self.seller:
            return f"{self.title} ({self.seller})"
        return self.title or self.seller


# "1 for 39,999 mesos" -- the price is the number before "mesos"; whatever comes
# before "for" (a quantity) is ignored. Tolerant of OCR gluing words ("1for"),
# 0/o swaps, a garbled quantity, and a mangled or missing "mesos"
# The price must be followed by "mesos" (or a misread like "rnesos") or the end of
# the line. A faded "4,000" often OCRs as "4{HH}"; without this, "4" would pass.
PRICE_LINE_RE = re.compile(
    r"(?<![A-Za-z])([\dlILSsOoBZ|]{1,5})?\s*[il|'‘]?\s*f[o0ce]r\s*(\d[\d,.]*\d|\d)(?![\d,.]*\d)"
    r"(?=\s*(?:(?:m|rn)[a-z0-9]{2,6}\b|$))", re.I)


def qty_of(m) -> int:
    """Quantities aren't used: a listing's price is the meso amount shown, whatever
    "XX for" says. Kept as 1 so older code paths and databases stay compatible."""
    return 1
MESO_BOX_RE = re.compile(r"^\d{1,3}(?:[,.]\d{3})+$|^\d{4,}$")
JUNK_TOKEN_RE = re.compile(r"^[\W_]*\w?[\W_]+$|^[\W_]+$")
# short tokens with bracket-ish characters are icon / scrollbar crumbs: "[J", "&)", "(4)"
CRUMB_RE = re.compile(r"^(?=.{1,3}$).*[\[\](){}|&@]")


def is_junk(t: str) -> bool:
    return bool(JUNK_TOKEN_RE.match(t) or CRUMB_RE.match(t))


def parse_price(s: str) -> int | None:
    s = s.strip().lower().replace(" ", "")
    if s and s[-1] in "kmb":
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[s[-1]]
        try:
            return int(float(s[:-1]) * mult)
        except ValueError:
            return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def plausible_price(s: str) -> bool:
    """Maple prints prices with thousands separators; reject OCR garble like '1,00,0'
    and single-digit prices (a sign the rest of the number was lost)."""
    s = s.replace(".", ",")
    return bool(re.fullmatch(r"\d{1,3}(,\d{3})*", s)) and int(s.replace(",", "")) >= 10


def _line_text(ws: list[Word]) -> str:
    return " ".join(w.text for w in ws)


def _clean_name(words: list[Word]) -> str:
    toks = []
    for w in words:
        t = w.text
        if t.endswith("...") or t.endswith("…"):
            t = re.sub(r"^\|", "I", t)  # OCR reads a truncated "I..." as "|..."
        elif is_junk(t):
            continue  # icon/scrollbar crumbs like "&)", "[]", "(4)", "[J"
        toks.append(t)
    name = " ".join(toks)
    name = re.sub(r"^[^\w]+", "", name)            # leading icon crumbs
    name = re.sub(r"[\s|\[\](){}]+$", "", name)    # trailing scrollbar crumbs
    if re.search(r"(\.\.\.|…)$", name):
        name = re.sub(r"[\s.…]+$", "", name) + "…"  # mark truncated names
    elif name.endswith(":"):
        name += "…"   # "Scroll: L..." whose faded tail OCR dropped
    return name.strip()


@dataclass
class Row:
    name_words: list[Word]
    price_words: list[Word] | None
    qty: int | None = None
    price: int | None = None
    sold: bool = False


_DIGIT_FIX = str.maketrans({"S": "5", "s": "5", "O": "0", "o": "0", "D": "0", "l": "1",
                            "I": "1", "|": "1", "B": "8", "Z": "2", "z": "2"})


def fix_price_text(text: str) -> str:
    """Undo common OCR slips in the part of a price line after "for": letters read
    for digits ("S4 444" -> "54 444") and a thousands comma read as a space
    ("54 444" -> "54,444")."""
    m = re.search(r"f[o0ce]r\s*", text, re.I)
    if not m:
        return text
    head, tail = text[: m.end()], text[m.end():]
    mes = re.search(r"\s*(?:m|rn)[e3co]s", tail, re.I)
    num, rest = (tail[: mes.start()], tail[mes.start():]) if mes else (tail, "")
    num = num.translate(_DIGIT_FIX)
    num = re.sub(r"(?<=\d)[\s.'`]+(?=\d{3}\b)", ",", num)
    return head + num + rest


def _price_match(ws: list[Word] | str):
    text = ws if isinstance(ws, str) else _line_text(ws)
    m = PRICE_LINE_RE.search(fix_price_text(text))
    if m and plausible_price(m.group(2)):
        return m
    return None


def learn_geometry(lines: list[list[Word]], img_w: int, img_h: int,
                   img: Image.Image | None = None) -> dict | None:
    """Where the text column starts (x) and where the item list starts (y), from
    the quantity number on each price line. Cached so later frames can blank the
    icon column, whose artwork and quantity badges confuse OCR."""
    xs, tops = [], []
    for i, ws in enumerate(lines):
        m = _price_match(ws)
        if not m:
            continue
        q = next((w for w in ws if w.text.startswith(m.group(1))
                  or re.match(r"\d*[il|]?f[o0ce]r", w.text, re.I)), None)
        if q:
            xs.append(q.left)
        if i > 0:
            tops.append(min(w.top for w in lines[i - 1]))
    if len(xs) < 2:
        return None
    list_top = (min(tops) if tops else 0) / img_h
    box = find_seller_box(img) if img is not None else None
    if box:
        # the item list starts just under the seller name box; more reliable than
        # the first readable row, which may be a faded (sold) one we couldn't read
        list_top = (box[3] + 6) / img_h
    return {"col_x": statistics.median(xs) / img_w, "list_top": list_top}


def mask_icons(img: Image.Image, geom: dict) -> Image.Image:
    from PIL import ImageDraw

    out = img.copy()
    w, h = img.size
    col = int(geom["col_x"] * w) - 5
    top = max(0, int(geom["list_top"] * h) - 8)
    if col > 4:
        ImageDraw.Draw(out).rectangle((0, top, col, h), fill=(222, 232, 240))
    return out


def row_is_faded(img: Image.Image, top: int, bottom: int, col_x: int) -> bool:
    """Sold items are drawn in light grey. Normal rows contain near-black text
    pixels; faded rows don't (their darkest 2.5% stays ~170 vs ~0)."""
    g = ImageOps.grayscale(img).crop((max(0, col_x - 2), max(0, top), int(img.width * 0.86),
                                      min(img.height, bottom)))
    vals = sorted(g.tobytes())
    if len(vals) < 50:
        return False
    return vals[len(vals) // 40] > 100


def find_rows(lines: list[list[Word]], col_x: float, img: Image.Image | None = None) -> list[Row]:
    """Each listing is a name line followed by an 'N for X mesos' line."""
    rows = []
    price_h = [w.height for ws in lines if _price_match(ws) for w in ws
               if re.search(r"f[o0ce]r|m[e3co]s", w.text, re.I)]
    ref_h = statistics.median(price_h) if price_h else 0
    used = set()
    for i, ws in enumerate(lines):
        if i in used or _price_match(ws):
            continue
        name_words = [w for w in ws if w.left >= col_x - 8]
        real = [w for w in name_words if not is_junk(w.text)
                or w.text.endswith(("...", "…"))]
        if not real or len(re.findall(r"[A-Za-z]", _line_text(real))) < 3:
            continue
        if ref_h and max(w.height for w in real) < ref_h * 0.7:
            continue  # clipped by the list edge
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        top = min(w.top for w in ws) - 2
        bottom = (max(w.top + w.height for w in nxt) if nxt else
                  max(w.top + w.height for w in ws) + int((ref_h or 12) * 2)) + 2
        faded = img is not None and row_is_faded(img, top, bottom, int(col_x))
        # OCR is less sure of faded text, so sold rows get a lower bar (and a re-read)
        if statistics.mean(w.conf for w in real) < (20 if faded else 50):
            continue
        price_words, qty, price = None, None, None
        if nxt is not None:
            gap = min(w.top for w in nxt) - min(w.top for w in ws)
            m = _price_match(nxt)
            if m and gap <= (ref_h or 12) * 2.6:
                price_words, qty, price = nxt, qty_of(m), parse_price(m.group(2))
                used.add(i + 1)
            elif not m and gap <= (ref_h or 12) * 2.6:
                price_words = nxt  # a price line we couldn't read: retry it
        rows.append(Row(name_words, price_words, qty, price, faded))
    return rows


def is_faded(img: Image.Image, words: list[Word]) -> bool:
    """Sold items are drawn in light grey. Look at how dark the darkest text
    pixels in the row are: normal text reaches near-black, faded text doesn't."""
    vals = []
    g = ImageOps.grayscale(img)
    for w in words:
        if is_junk(w.text) or w.width < 3:
            continue
        box = g.crop((w.left, w.top, w.left + w.width, w.top + w.height))
        vals.extend(box.tobytes())
    if len(vals) < 20:
        return False
    vals.sort()
    return vals[max(0, len(vals) // 40)] > 115   # 2.5th percentile


def reread_row(img: Image.Image, row: Row, col_x: int, cfg: dict):
    """Second OCR pass on one row with contrast normalised for that row only.
    Used for faded (sold) rows and price lines the main pass garbled.
    Returns (qty, price, name_words) or None."""
    ws = row.name_words + (row.price_words or [])
    line_h = max(w.height for w in ws)
    top = min(w.top for w in ws) - 6
    bottom = max(w.top + w.height for w in ws) + max(8, line_h // 2)
    if not row.price_words:
        bottom = top + int(line_h * 4)
    right = int(img.width * 0.88)                   # leave out the scrollbar
    x0, y0 = max(0, col_x - 4), max(0, top)
    box = img.crop((x0, y0, right, min(img.height, bottom)))
    for mode in ("gray", "graybin", "graybin120", "gray4"):
        lines = _ocr_box(box, cfg, mode)
        for i, ln in enumerate(lines):
            m = _price_match(ln)
            if m:
                name = None
                if i > 0:
                    name = [Word(w.text, w.left + x0, w.top + y0, w.width, w.height, w.conf)
                            for w in lines[i - 1]]
                return qty_of(m), parse_price(m.group(2)), name
    return None


def _ocr_box(box: Image.Image, cfg: dict, mode: str) -> list[list[Word]]:
    import pytesseract

    up = 4 if mode == "gray4" else 3
    g = ImageOps.grayscale(box).resize((box.width * up, box.height * up), Image.LANCZOS)
    if mode in ("gray", "gray4"):
        g = ImageOps.autocontrast(g)
    else:
        cut = 120 if mode == "graybin120" else 150
        g = ImageOps.autocontrast(g, cutoff=1).point(lambda p, c=cut: 0 if p < c else 255)
    setup_tesseract(cfg)
    d = pytesseract.image_to_data(g, config="--psm 6", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[Word]] = {}
    for i, text in enumerate(d["text"]):
        if text.strip():
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(key, []).append(Word(text.strip(), d["left"][i] // up,
                                                  d["top"][i] // up, d["width"][i] // up,
                                                  d["height"][i] // up, float(d["conf"][i])))
    out = [sorted(ws, key=lambda w: w.left) for _, ws in sorted(lines.items())]
    out.sort(key=lambda ws: min(w.top for w in ws))
    return out


def find_seller_box(img: Image.Image):
    """The seller's name is white text in a dark navy box under their avatar."""
    w, h = img.size
    px = img.convert("RGB").load()

    def navy(x, y):
        r, g, b = px[x, y]
        return b > r + 25 and r < 70 and b < 140

    # sampled every 2nd pixel; letters cover part of the navy on text rows
    rows = [y for y in range(int(h * 0.2), int(h * 0.5))
            if sum(1 for x in range(int(w * 0.02), int(w * 0.40), 2) if navy(x, y)) > w * 0.06]
    if not rows:
        return None
    y0 = rows[0]
    y1 = y0
    for y in rows:
        if y - y1 > 4:
            break
        y1 = y
    mid = (y0 + y1) // 2
    # the box is the longest unbroken navy run on its middle row (not first..last
    # navy pixel: the window's own dark-blue border can sit a few pixels left of it)
    # (the letters of the name interrupt the navy for a few pixels: bridge gaps of
    # up to ~3% of the window; the border is ~6% away)
    max_gap = max(4, int(w * 0.03))
    best_run, run_start, last = (0, 0, -1), None, None
    for x in range(0, int(w * 0.5)):
        if navy(x, mid):
            if run_start is None or x - last > max_gap:
                run_start = x
            last = x
            if last - run_start > best_run[2] - best_run[1]:
                best_run = (0, run_start, last)
    _, xa, xb = best_run
    if xb - xa < w * 0.15 or y1 - y0 < 8:
        return None
    return (xa + 2, y0 + 1, xb - 1, y1)


def read_seller(img: Image.Image, cfg: dict) -> str:
    import pytesseract

    box = find_seller_box(img)
    if not box:
        return ""
    crop_ = img.crop(box)
    g = ImageOps.invert(ImageOps.grayscale(crop_)).resize((crop_.width * 3, crop_.height * 3),
                                                          Image.LANCZOS)
    g = ImageOps.expand(ImageOps.autocontrast(g), border=15, fill=255)
    setup_tesseract(cfg)
    text = pytesseract.image_to_string(g, config="--psm 7").strip()
    return re.sub(r"^[\W_]+|[\W_]+$", "", text.split()[0]) if text.split() else ""


def header_signature(img: Image.Image) -> bytes:
    """Fingerprint of the shop: its title bar and the seller's name box. Both stay
    pixel-identical while you scroll, and unlike the seller's avatar they never
    animate (glowing weapons, blinking), so only opening another shop changes it."""
    w, h = img.size
    g = ImageOps.grayscale(img)
    title = g.crop((int(w * 0.06), 0, w, int(h * 0.075))).resize((96, 8), Image.BOX)
    box = find_seller_box(img) or (int(w * 0.04), int(h * 0.33), int(w * 0.45), int(h * 0.37))
    seller = g.crop(box).resize((64, 8), Image.BOX)
    return title.tobytes() + seller.tobytes()


def header_distance(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)


def header_parts(img: Image.Image):
    """Title bar and seller name box as small greyscale strips."""
    w, h = img.size
    g = ImageOps.grayscale(img)
    title = g.crop((int(w * 0.06), 0, w, int(h * 0.075))).resize((96, 8), Image.BOX)
    box = find_seller_box(img) or (int(w * 0.04), int(h * 0.33), int(w * 0.45), int(h * 0.37))
    seller = g.crop(box).resize((64, 8), Image.BOX)
    return title, seller


def changed_fraction(a: Image.Image, b: Image.Image) -> float:
    """Share of columns that differ. A cursor over the header changes a narrow
    patch (<= ~11% of the seller box); a different seller's name changes >= ~30%."""
    pa, pb = a.load(), b.load()
    w, h = a.size
    return sum(1 for x in range(w)
               if sum(abs(pa[x, y] - pb[x, y]) for y in range(h)) / h > 25) / w


class _TitleWords:
    """English words (from the pyspellchecker package, if installed) plus item-name
    words, indexed by their confusable-letter fold."""
    cache = None

    def __init__(self):
        words: dict[str, int] = {}
        try:
            from spellchecker import SpellChecker
            words.update(SpellChecker().word_frequency.dictionary)
        except Exception:
            pass   # optional; item words alone still help
        d = _Dictionary.cache
        if d is not None:
            for w in d.vocab:
                words.setdefault(w, 1)
        for w in CLASSIC_WORDS + TRADE_WORDS:
            words.setdefault(w.lower(), 1)
        self.known = words
        self.by_fold: dict[str, str] = {}
        for w, n in sorted(words.items(), key=lambda kv: kv[1]):   # most frequent wins
            if len(w) >= 3 and w.isalpha():
                self.by_fold[fold(w)] = w

    @classmethod
    def get(cls):
        if cls.cache is None:
            cls.cache = cls()
        return cls.cache


TRADE_WORDS = """sell selling buy buying cheap cheapest offer offers scrolls ores leather equips
nx etc stars pots potions mesos shop store""".split()


def correct_title(title: str) -> str:
    """Fix OCR letter swaps in a shop title ("55 burpers" -> "55 burgers"). A word is
    only changed if it isn't a real word and undoing a confusable-letter swap turns
    it into one; anything else (odd spellings, slang, names) is kept as typed."""
    tw = _TitleWords.get()
    out = []
    for tok in title.split():
        m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", tok)
        # short words are slang/acronyms far more often than misreads ("OMG" is not
        # "AMP", "NX" stays "NX"), so only words of 5+ letters are corrected
        if not m or len(m.group(2)) < 5 or m.group(2).lower() in tw.known:
            out.append(tok)
            continue
        lead, core, trail = m.groups()
        hit = tw.by_fold.get(fold(core))
        if hit and len(hit) == len(core):
            hit = hit.upper() if core.isupper() else hit.capitalize() if core[0].isupper() else hit
            core = hit
        out.append(lead + core + trail)
    return " ".join(out)


def read_title(img: Image.Image, cfg: dict, below_y: int | None = None) -> str:
    """The shop title is typed into a white bar at the top of the store window."""
    import pytesseract

    w, h = img.size
    limit = min(h, below_y or h // 3)
    px = ImageOps.grayscale(img).load()
    # rows that are mostly white; a long title covers much of the bar, so the
    # threshold is low and short gaps (text rows) are bridged
    white = [sum(1 for x in range(0, w, 2) if px[x, y] > 235) / ((w + 1) // 2) > 0.3
             for y in range(limit)]
    best, y = None, 0
    while y < limit:
        if not white[y]:
            y += 1
            continue
        y0, last = y, y
        while y < limit and (white[y] or y - last <= 2):
            if white[y]:
                last = y
            y += 1
        if last - y0 + 1 >= 8:
            best = (y0, last + 1)
            break
    if not best:
        return ""
    y0, y1 = best
    bar = img.crop((int(w * 0.06), y0 + 1, w - 2, y1 - 1))  # skip the grip icon on the left
    up = 3
    g = ImageOps.autocontrast(ImageOps.grayscale(bar).resize((bar.width * up, bar.height * up),
                                                             Image.LANCZOS))
    setup_tesseract(cfg)
    text = pytesseract.image_to_string(g, config="--psm 7").strip()
    toks = [t for t in text.split() if not is_junk(t)]
    return correct_title(re.sub(r"^[^\w<>]+", "", " ".join(toks)).strip())


def _has_title_bar(img: Image.Image) -> bool:
    """A solid white strip (8+ px) across most of the top third of the image."""
    small = img.convert("RGB").resize((120, max(1, int(120 * img.height / img.width))))
    w, h = small.size
    px = small.load()
    run = best = 0
    for y in range(max(1, h // 3)):
        whites = sum(1 for x in range(w) if min(px[x, y]) > 235)
        run = run + 1 if whites > w * 0.6 else 0
        best = max(best, run)
    return best * img.height / h >= 8


def is_store_window(img: Image.Image) -> bool:
    """Strict check that an area really is a player-shop window, used when
    searching the screen for one. Sky, clouds and other windows can be white
    and light blue too, so the colours of the item panels aren't enough: this
    requires the white title bar AND the navy seller-name box, which in every
    shop sits ~33% down, is ~42% of the window wide and holds white text."""
    if img.width < 120 or img.height < 200 or not _has_title_bar(img):
        return False
    box = find_seller_box(img)
    if box is None:
        return False
    w, h = img.size
    x0, y0, x1, y1 = box
    if not (0.34 <= (x1 - x0) / w <= 0.48 and 0.26 <= y0 / h <= 0.40 and 0.015 <= (y1 - y0) / h <= 0.07
            and x0 / w <= 0.15):
        return False
    g = ImageOps.grayscale(img.crop(box))
    hist = g.histogram()
    white = sum(hist[201:]) / max(1, g.width * g.height)
    return 0.03 <= white <= 0.6          # the seller's name, not a blank or white area


def looks_like_shop(img: Image.Image) -> bool:
    """Cheap check (~10 ms) for an open store window, so we don't spend seconds
    OCR-ing the game world while you walk around. Looks for the light-blue item
    panels, plus the white title bar when the capture includes the header."""
    small = img.convert("RGB").resize((120, max(1, int(120 * img.height / img.width))))
    w, h = small.size
    px = small.load()
    panel = white_rows = run = 0
    for y in range(h):
        whites = 0
        for x in range(w):
            r, g, b = px[x, y]
            if b >= 190 and g >= 175 and b - 70 <= r <= b - 12:
                panel += 1
            elif r > 235 and g > 235 and b > 235:
                whites += 1
        if y < h // 3:
            run = run + 1 if whites > w * 0.6 else 0
            white_rows = max(white_rows, run)
    frac = panel / (w * h)
    title_bar = white_rows * img.height / h >= 8
    if (title_bar and frac > 0.18) or frac > 0.4:
        return True
    # a shop whose rows are mostly sold has few blue panels (faded rows are
    # whitish), but its title bar and navy seller box are still there
    return title_bar and find_seller_box(img) is not None


# ---- scroll grade from icon colour ------------------------------------------
SCROLL_GRADES = {"yellow": "Greater", "red": "Intermediate", "blue": "Lesser", "black": "Chaos"}


def scroll_color(icon: Image.Image | None, faded: bool = False) -> str | None:
    """Colour of a scroll icon: yellow / red / blue / black.

    The icon cell's background is sampled from its border and excluded, along
    with the dark outline, white highlights and the stock badge (bottom-left).
    The hue comes from the more saturated half of what's left, so a red scroll's
    body outweighs its small gold handles. Faded (sold) icons keep their hue but
    lose saturation, so brightness decides between "faded" and "black"."""
    import colorsys

    if icon is None:
        return None
    im = icon.convert("RGB")
    w, h = im.size
    px = im.load()
    border = [px[x, y] for x in range(w) for y in (0, 1, h - 2, h - 1)] + \
             [px[x, y] for y in range(h) for x in (0, 1, w - 2, w - 1)]
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    pts = []
    for y in range(2, h - 2):
        for x in range(2, w - 2):
            if x < w * 0.45 and y > h * 0.62:
                continue                                  # stock badge
            c = px[x, y]
            if sum((c[i] - bg[i]) ** 2 for i in range(3)) < (12 if faded else 30) ** 2:
                continue                                  # background (pale icons sit close to it)
            hh, sat, v = colorsys.rgb_to_hsv(*(ch / 255 for ch in c))
            if v < 0.25 or (sat < 0.06 and v > 0.9):
                continue                                  # outline / highlight
            pts.append((hh * 360, sat, v))
    if len(pts) < 20:
        return None
    mean_s = statistics.mean(p[1] for p in pts)
    mean_v = statistics.mean(p[2] for p in pts)
    tinted = [p for p in pts if p[1] > 0.04]
    if (mean_s < 0.25 and mean_v < 0.7) or len(tinted) < 0.3 * len(pts):
        return "black"
    cut = sorted(p[1] for p in tinted)[len(tinted) // 2]
    hues = sorted(p[0] for p in tinted if p[1] >= cut)
    med = hues[len(hues) // 2]
    if 180 <= med <= 260:
        return "blue"
    if med < 30 or med > 330:
        return "red"
    if 30 <= med < 70:
        return "yellow"
    return None


GRADES = ("Chaos", "Greater", "Intermediate", "Lesser")


def _scroll_family(name: str) -> tuple[list[str], str, bool]:
    """Graded scrolls ("X Scroll: Chaos/Greater/Intermediate/Lesser") that the visible
    text could be, the partly visible grade ("Inter"), and whether the whole
    "... Scroll" part was visible. Works whether or not OCR kept the "...":
    "Overall Armor DEF Scroll:...", "Overall Armor DEF Scroll", "Topwear DEF Scroll: Gre",
    "Two-handed Blunt Wea..." and small slips ("Cane Magic Def." for "Cape") all count."""
    d = _Dictionary.cache
    if not d or not d.graded_scrolls:
        return [], "", False
    text = re.sub(r"[\s.,:;…|'`~_-]+$", "", name.replace("…", "")).strip()
    m = re.match(r"^(.*?\bscro[l1i|]{1,2})\b\s*[:;.,]?\s*([A-Za-z]*)$", text, re.I)
    head, part = (m.group(1), m.group(2)) if m else (text, "")
    fh = fold(_norm(head))
    if len(fh) < 8:
        return [], part, bool(m)
    scored = []
    for n in d.graded_scrolls:
        fb = d.scroll_base_fold[n]
        # whole "... Scroll" visible: compare with the whole base name; cut off
        # earlier ("Two-handed Blunt Wea…"): with the same-length start of it
        ref = fb if m else fb[:len(fh)]
        r = 1.0 if ref == fh else difflib.SequenceMatcher(None, fh, ref).ratio()
        if r >= 0.9:
            scored.append((r, n))
    if not scored:
        return [], part, bool(m)
    top = max(r for r, _ in scored)
    return [n for r, n in scored if r == top], part, bool(m)


def _grades_from_text(part: str) -> list[str]:
    """Grades a partly visible grade word could be ("Inter" -> Intermediate). The
    four start with different letters, but I/l are confusable, so it's compared
    folded."""
    if not part:
        return []
    fp = fold(part)
    return [g for g in GRADES
            if difflib.SequenceMatcher(None, fp, fold(g)[:len(fp)]).ratio() >= 0.75]


def looks_like_partial_scroll(name: str) -> bool:
    """A cut-off / unresolved scroll name that the icon colour should decide."""
    if is_known_item(name):
        return False
    cands, _, _ = _scroll_family(name)
    return len({c.rsplit(":", 1)[0] for c in cands}) == 1 and len(cands) > 1


def resolve_scroll(name: str, icon, faded: bool = False) -> str:
    """A truncated scroll name ('Overall Armor LUK Scroll:…', 'One-Handed Axe
    Attack S…') can match several grades. Pick the one whose icon colour matches:
    yellow = Greater, red = Intermediate, blue = Lesser, black = Chaos.

    The decision doesn't depend on OCR seeing the "...": any name that isn't an
    exact item and matches one scroll family is graded. A partly visible grade
    ("Scroll: Gre") is used when the colour can't be read, and wins over the colour
    when 3+ letters of it are visible and they disagree."""
    d = _Dictionary.cache
    if d and d.names:
        if is_known_item(name):
            return name
        cands, part, _ = _scroll_family(name)
        bases = {c.rsplit(":", 1)[0] for c in cands}
        if len(bases) != 1:
            return name                       # not a scroll, or the family is ambiguous
        base = bases.pop()
        colour_grade = SCROLL_GRADES.get(scroll_color(icon, faded) or "")
        text_grades = _grades_from_text(part)
        if colour_grade and (not text_grades or colour_grade in text_grades):
            grade = colour_grade
        elif len(text_grades) == 1 and (not colour_grade or len(part) >= 3):
            grade = text_grades[0]
        elif colour_grade:
            grade = colour_grade
        else:
            return name                       # no colour, no readable grade: leave for review
        full = f"{base}: {grade}"
        return full if full in cands else name
    # no item list installed: complete "…Scroll:" / "…Scroll: Gre" names ourselves
    if not name.endswith("…") and "scroll" not in name.lower():
        return name
    grade = SCROLL_GRADES.get(scroll_color(icon, faded) or "")
    if not grade:
        return name
    m = re.match(r"^(.*\bScroll)\s*:?\s*([A-Za-z]*)$", name.rstrip("…").strip())
    if m and grade.lower().startswith(m.group(2).lower()):
        return f"{m.group(1)}: {grade}"
    return name


# ---- mouse cursor and item tooltips ------------------------------------------
#
# Hovering the list puts the game's glove cursor over the text ("Toga" -> "Topa")
# and pops up an item tooltip whose text OCR glues onto the rows below it
# ("Ripped Jeans" -> "Ripped Jeans *Orange"). Rows under either are skipped for
# that frame; they're read as soon as the mouse moves off them.
#
# The glove is a solid blob of white/cyan-white (green and blue both ~255). Text
# has a thin halo of similar colour and sold rows are whitish (242,245,248), but
# neither contains a solid 6x6 block of it. The tooltip is a large navy box: many
# consecutive pixel rows with lots of navy, where the window's navy border is 1-3
# pixels tall.

def obscured_spans(img: Image.Image, col_x: int, top: int = 0) -> list[tuple[int, int]]:
    """Vertical pixel ranges of the list covered by the cursor or a tooltip."""
    from PIL import ImageChops, ImageFilter

    w, h = img.size
    x1 = int(w * 0.9)                      # text area only: icons can be white, the scrollbar is grey
    if x1 - col_x < 20 or h - top < 20:
        return []
    area = img.crop((col_x, top, x1, h)).convert("RGB")
    r, g, b = area.split()
    spans = []
    # glove: g >= 253, b >= 253, r >= 200, eroded so only solid 6x6 blocks survive
    light = ImageChops.multiply(ImageChops.multiply(
        g.point(lambda v: 255 if v >= 253 else 0), b.point(lambda v: 255 if v >= 253 else 0)),
        r.point(lambda v: 255 if v >= 200 else 0))
    solid = light.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MinFilter(3))   # ~ 7x7 erosion
    bb = solid.getbbox()
    if bb:
        # the blob is the glove's palm; its finger tip reaches ~12 px higher
        spans.append((top + bb[1] - 14, top + bb[3] + 8))
    # tooltip: navy (b well above r) in at least 15 px of a pixel row, 6+ rows in a row
    navy = ImageChops.multiply(ImageChops.multiply(
        r.point(lambda v: 255 if 40 <= v <= 115 else 0), g.point(lambda v: 255 if 45 <= v <= 120 else 0)),
        ImageChops.multiply(b.point(lambda v: 255 if 95 <= v <= 175 else 0),
                            ImageChops.subtract(b, r).point(lambda v: 255 if v >= 35 else 0)))
    counts = list(navy.resize((1, navy.height), Image.BOX).tobytes())   # one byte per row (mode L)
    need = 15 * 255 / navy.width
    edge = 2.5 * 255 / navy.width     # the tooltip's navy frame alone, beside its picture
    run_start = None
    y = 0
    while y <= len(counts):
        c = counts[y] if y < len(counts) else 0
        if c >= need:
            run_start = y if run_start is None else run_start
        else:
            if run_start is not None and y - run_start >= 6:
                # a tooltip: follow it down past its item picture and text, where
                # only its navy frame remains, tolerating short gaps
                end, gap = y, 0
                while end < len(counts) and gap <= 8:
                    gap = 0 if counts[end] >= edge else gap + 1
                    end += 1
                spans.append((top + run_start - 4, top + end - gap + 4))
                y = end
            run_start = None
        y += 1
    return spans


def _covered(spans, y0: int, y1: int) -> bool:
    return any(a < y1 and y0 < b for a, b in spans)


def _name_conf(words: list[Word], col_x: int) -> float:
    real = [w for w in words if w.left >= col_x - 8 and not is_junk(w.text)]
    return statistics.mean(w.conf for w in real) if real else 0.0


def _icon_crop(img: Image.Image, row: Row, col_x: int) -> Image.Image | None:
    """The item's icon: the square left of the row's text."""
    ws = row.name_words + (row.price_words or [])
    top, bottom = min(w.top for w in ws), max(w.top + w.height for w in ws)
    cy = (top + bottom) // 2
    size = min(col_x - 8, int((bottom - top) * 1.5))
    if size < 12:
        return None
    x0 = max(0, (col_x - 6) - size)
    return img.crop((x0, max(0, cy - size // 2), x0 + size, min(img.height, cy + size // 2)))


def _recover_missing_rows(img, found, geom, col_x, cfg, known, skipped=(), blocked=()
                          ) -> list[tuple[int, Listing]]:
    """Safety net: list rows are evenly spaced, so any fully visible row slot that
    produced no listing (typically a faded one OCR lost) is re-read on its own.
    Rows skipped because the cursor covered them are not 'missing'."""
    if len(found) + len(skipped) < 2:
        return []
    tops = sorted([t for t, _ in found] + list(skipped))
    gaps = [b - a for a, b in zip(tops, tops[1:]) if b - a > 20]
    if not gaps:
        return []
    pitch = min(gaps)                                   # one row
    # refine with every gap that's a whole number of rows
    multiples = [g / round(g / pitch) for g in gaps if round(g / pitch) >= 1]
    pitch = statistics.median(multiples)
    if not 30 <= pitch <= 140:
        return []
    h = img.height
    list_top = int(geom["list_top"] * h)
    anchor = tops[0]
    slot = anchor
    while slot - pitch >= list_top - 4:
        slot -= pitch
    out = []
    while slot + pitch * 0.85 <= h:
        y = int(round(slot))
        if all(abs(y - t) > pitch * 0.4 for t in tops) and not _covered(blocked, y - 6, int(y + pitch * 0.85)):
            box = img.crop((max(0, col_x - 4), max(0, y - 6), int(img.width * 0.88),
                            min(h, int(y + pitch * 0.85))))
            if min(ImageOps.grayscale(box).tobytes() or b"\xff") < 215:   # has some text
                lst = _read_slot(box, img, y, pitch, col_x, cfg, known)
                if lst:
                    out.append((y, lst))
        slot += pitch
    return out


def _read_slot(box, img, y, pitch, col_x, cfg, known) -> Listing | None:
    for mode in ("gray", "graybin"):
        lines = _ocr_box(box, cfg, mode)
        for i, ln in enumerate(lines):
            m = _price_match(ln)
            if m and i > 0:
                name = _clean_name([w for w in lines[i - 1] if not is_junk(w.text)])
                if len(re.findall(r"[A-Za-z]", name)) < 3:
                    continue
                sold = row_is_faded(img, y - 2, int(y + pitch * 0.8), col_x)
                ws = [Word(w.text, w.left + col_x - 4, w.top + y - 6, w.width, w.height, w.conf)
                      for w in lines[i - 1] + ln]
                icon = _icon_crop(img, Row(ws, None), col_x)
                raw = _line_text(lines[i - 1]) + " / " + _line_text(ln) + " (slot re-read)"
                return Listing(normalize_name(name, known), parse_price(m.group(2)), qty_of(m),
                               raw, sold, icon)
    return None


def read_shop(img: Image.Image, cfg: dict, known: list[str], with_title=True,
              geom: dict | None = None) -> ShopFrame:
    """Read every listing in the store window, including faded (sold) ones.

    1. If the text-column geometry isn't known yet, a plain pass learns it.
    2. The icon column is blanked and the list OCR'd with the blue-channel
       tone map, which keeps faded text readable.
    3. Rows that are faded, or whose price line didn't parse, get a second
       OCR pass on just that row with locally normalised contrast.
    """
    w, h = img.size
    if geom is None:
        first = ocr_words(img, cfg, "gray")
        geom = learn_geometry(first, w, h, img)
        if geom is None:
            return ShopFrame([], geom=None)
    lines = ocr_words(mask_icons(img, geom), cfg, "blue")
    col_x = int(geom["col_x"] * w)
    listings = []
    found: list[tuple[int, Listing]] = []   # (name-line top, listing)
    rows = find_rows(lines, col_x, img)
    blocked = obscured_spans(img, col_x, int(geom.get("list_top", 0.3) * h))
    skipped: list[int] = []
    for row in rows:
        ws = row.name_words + (row.price_words or [])
        if blocked and _covered(blocked, min(x.top for x in ws) - 3,
                                max(x.top + x.height for x in ws) + 3):
            skipped.append(min(x.top for x in row.name_words))
            continue
        sold = row.sold
        qty, price, name_words = row.qty, row.price, row.name_words
        if row.price_words is not None and (sold or price is None):
            again = reread_row(img, row, col_x, cfg)
            if again:
                qty, price, re_name = again
                # faded names read better with the row's own contrast; keep the surer read
                if sold and re_name and _name_conf(re_name, col_x) > _name_conf(name_words, col_x):
                    name_words = [w for w in re_name if w.left >= col_x - 8]
        if not price or not qty:
            continue
        name = _clean_name(name_words)
        if len(re.findall(r"[A-Za-z]", name)) < 3:
            continue
        raw = _line_text(name_words) + " / " + _line_text(row.price_words or [])
        found.append((min(w.top for w in row.name_words),
                      Listing(normalize_name(name, known), price, qty, raw, sold,
                              _icon_crop(img, row, col_x))))
    found += _recover_missing_rows(img, found, geom, col_x, cfg, known, skipped, blocked)
    listings = [l for _, l in sorted(found, key=lambda t: t[0])]
    for l in listings:
        l.item = resolve_name(l.item, l.icon, l.sold)
    frame = ShopFrame(listings, geom=geom, hidden=len(skipped))
    if with_title and listings:
        frame.title = read_title(img, cfg)
        frame.seller = read_seller(img, cfg)
    return frame


# ---- automatic layout detection --------------------------------------------
#
# The store window's title bar is a solid white strip roughly 24x wider than it
# is tall, which nothing else on screen looks like (the trade chat box is white
# too, but nearly square). Every other part of the window is measured relative
# to that bar's width, so this works at any game resolution or window position.

# store window geometry, as fractions of the title bar width (measured in-game)
STORE_LEFT, STORE_TOP, STORE_W, STORE_H = -0.006, -0.011, 0.452, 0.797
# game window, relative to the store: the store is centred horizontally, its
# title bar sits ~20% down, and the bar is ~42% of the game width
GAME_W_PER_BAR, GAME_ASPECT, BAR_Y_FRAC = 1 / 0.421, 0.624, 0.203
# minimap street + map name, as fractions of the game window
MAP_W, MAP_H = 0.135, 0.08


@dataclass
class Layout:
    store: list            # [left, top, width, height] of the store window
    map: list              # where the minimap name should be
    bar: tuple             # title bar (left, top, right, bottom)


def _layout_for_store(full: Image.Image, store: list, bar=None) -> Layout:
    sw = store[2]
    bw = sw / STORE_W                        # title bar width
    gw = bw * GAME_W_PER_BAR
    gh = gw * GAME_ASPECT
    gl = max(0, int(store[0] + sw / 2 - gw / 2))
    gt = max(0, int(store[1] - BAR_Y_FRAC * gh))
    guess = [gl, gt, int(MAP_W * gw), int(MAP_H * gh)]
    # the minimap found by its own look; the guess (which assumes the store
    # window is where the game first opens it) only if it can't be seen
    mp = find_minimap(full, store) or guess
    if bar is None:
        bar = (int(store[0] - STORE_LEFT * bw), int(store[1] - STORE_TOP * bw),
               int(store[0] - STORE_LEFT * bw + bw), int(store[1] - STORE_TOP * bw + 0.04 * bw))
    return Layout(store, mp, bar)


def locate_store(full: Image.Image) -> Layout | None:
    """Find an open store window anywhere on screen (any monitor). It's found by
    its SELLER INFO / BUY ITEM / LEAVE STORE button stack, which nothing else
    has, and confirmed by its white title bar; the white-title-bar search
    remains as a fallback (e.g. a tooltip covering the buttons)."""
    from PIL import ImageChops

    scale = 2 if full.width > 1200 else 1
    small = full.convert("RGB").resize((full.width // scale, full.height // scale), Image.NEAREST)
    for store in find_store_by_buttons(small, scale):
        store = [max(0, store[0]), max(0, store[1]), store[2], store[3]]
        store[2] = min(store[2], full.width - store[0])
        store[3] = min(store[3], full.height - store[1])
        if store[2] >= 120 and store[3] >= 200 and _has_title_bar(crop(full, store)):
            return _layout_for_store(full, store)
    r, g, b = small.split()
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    mask = mn.point(lambda p: 255 if p >= 235 else 0)
    W, H = mask.size
    bars = []
    # white pixels per row, via a 1-px-wide box resize
    per_row = list(mask.resize((1, H), Image.BOX).tobytes())
    min_w = 220 // scale
    cand = [p * W / 255 >= min_w for p in per_row]
    y = 0
    while y < H:
        if not cand[y]:
            y += 1
            continue
        y0 = y
        while y < H and cand[y]:
            y += 1
        h = y - y0
        if h < 6 // scale or h > 120 // scale:
            continue
        block = mask.crop((0, y0, W, y))
        # widest horizontal span that is mostly white across the whole block
        # (text and the grip icon inside the bar leave small dips; bridge them)
        cols = list(block.resize((W, 1), Image.BOX).tobytes())
        max_gap = max(2, 8 // scale)
        bs = best_len = best_start = 0
        last_white, in_run = -10 ** 9, False
        for x, c in enumerate(cols):
            if c >= 100:
                if not in_run or x - last_white > max_gap:
                    bs, in_run = x, True
                last_white = x
                if x - bs + 1 > best_len:
                    best_len, best_start = x - bs + 1, bs
        if best_len >= min_w and best_len / h >= 10:
            bars.append((best_start * scale, y0 * scale, (best_start + best_len) * scale, y * scale))
    # every white bar is a candidate (a browser toolbar on another monitor can be
    # wider than the store's): take the widest one that really is a store window
    for bar in sorted(bars, key=lambda b: b[0] - b[2]):
        l, t, rgt, btm = bar
        bw = rgt - l
        store = [max(0, int(l + STORE_LEFT * bw)), max(0, int(t + STORE_TOP * bw)),
                 int(STORE_W * bw), int(STORE_H * bw)]
        store[2] = min(store[2], full.width - store[0])
        store[3] = min(store[3], full.height - store[1])
        if store[2] < 120 or store[3] < 200 or not is_store_window(crop(full, store)):
            continue
        return _layout_for_store(full, store, bar)
    return None


def _color_masks(img: Image.Image):
    """Masks of the shop's orange (BUY ITEM / LEAVE STORE, minimap WORLD) and lime
    green (SELLER INFO) button colours."""
    from PIL import ImageChops

    r, g, b = img.convert("RGB").split()
    mul = ImageChops.multiply

    def band(ch, lo, hi):
        return ch.point(lambda v: 255 if lo <= v <= hi else 0)
    orange = mul(mul(band(r, 200, 255), band(g, 100, 185)),
                 mul(band(b, 0, 110), ImageChops.subtract(r, b).point(lambda v: 255 if v >= 120 else 0)))
    green = mul(mul(band(g, 130, 245), band(b, 0, 80)),
                mul(ImageChops.subtract(g, b).point(lambda v: 255 if v >= 90 else 0),
                    ImageChops.subtract(r, g).point(lambda v: 0 if v > 5 else 255)))
    return orange, green


def _blobs(mask: Image.Image, min_w: int = 6, min_h: int = 3) -> list[tuple]:
    """Bounding boxes (x0, y0, x1, y1) of connected regions of a 0/255 mask
    (run-length labelling: fast enough on a half-size full-screen capture)."""
    W, H = mask.size
    data = mask.tobytes()
    parent: dict[int, int] = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    boxes: dict[int, tuple] = {}
    prev: list[tuple] = []
    nxt = 0
    for y in range(H):
        row = data[y * W:(y + 1) * W]
        runs = []
        x = row.find(b"\xff")
        while x != -1:
            e = row.find(b"\x00", x)
            e = W if e == -1 else e
            lab = None
            for px0, px1, pl in prev:
                if px0 <= e and x <= px1:
                    if lab is None:
                        lab = find(pl)
                    else:
                        a, c = find(lab), find(pl)
                        if a != c:
                            parent[c] = a
            if lab is None:
                lab = nxt
                parent[lab] = lab
                nxt += 1
            runs.append((x, e, lab))
            bx = boxes.get(lab)
            boxes[lab] = (x, y, e, y + 1) if bx is None else (min(bx[0], x), bx[1], max(bx[2], e), y + 1)
            x = row.find(b"\xff", e)
        prev = runs
    merged: dict[int, tuple] = {}
    for lab, bx in boxes.items():
        root = find(lab)
        m = merged.get(root)
        merged[root] = bx if m is None else (min(m[0], bx[0]), min(m[1], bx[1]),
                                             max(m[2], bx[2]), max(m[3], bx[3]))
    return [b for b in merged.values() if b[2] - b[0] >= min_w and b[3] - b[1] >= min_h]


# Every shop window has the same three buttons stacked on the right of its header:
# SELLER INFO (lime green) above BUY ITEM and LEAVE STORE (orange), all the same
# width. Measured on many shops, relative to the button width (bw):
#   store left = green.x0 - 1.925 bw, store top = green.y0 - 0.835 bw,
#   store width = 3.105 bw, store height = 1.763 x store width;
#   BUY ITEM starts 0.416 bw below SELLER INFO, LEAVE STORE 0.264 bw below that.
BTN_STORE_LEFT, BTN_STORE_TOP, BTN_STORE_W, STORE_H_PER_W = 1.925, 0.835, 3.105, 1.763


def find_store_by_buttons(img: Image.Image, scale: int) -> list[list]:
    """Store windows on screen, found by their SELLER INFO / BUY ITEM / LEAVE STORE
    buttons. Returns [left, top, width, height] for each (full-size coordinates)."""
    orange, green = _color_masks(img)
    ob = [b for b in _blobs(orange) if 3 <= (b[2] - b[0]) / (b[3] - b[1]) <= 9]
    gb = [b for b in _blobs(green) if 3 <= (b[2] - b[0]) / (b[3] - b[1]) <= 9]
    stores = []
    for g in gb:
        bw = g[2] - g[0]
        tol = max(2, 0.08 * bw)

        def under(prev, dy):
            for o in ob:
                if abs(o[0] - prev[0]) <= tol and abs((o[2] - o[0]) - bw) <= 2 * tol \
                        and abs((o[1] - prev[1]) - dy * bw) <= 2 * tol:
                    return o
            return None
        buy = under(g, 0.416)
        leave = under(buy, 0.264) if buy else None
        if not leave:
            continue
        bw_full = bw * scale
        sw = BTN_STORE_W * bw_full
        left = g[0] * scale - BTN_STORE_LEFT * bw_full
        top = g[1] * scale - BTN_STORE_TOP * bw_full
        stores.append([int(left), int(top), int(sw), int(sw * STORE_H_PER_W)])
    return stores


def find_minimap(full: Image.Image, store=None) -> list | None:
    """The minimap's title strip + street/map name panel, found by its own look:
    the white "MINI MAP" strip with the orange WORLD button at its right end, and
    the steel-blue name panel underneath. Its size changes with the map, so it's
    measured each time. It sits at the game window's top-left: with a store
    given, the nearest minimap up and to the left of the store wins (another
    game window or monitor can't steal it). Returns [left, top, width, height]."""
    img = full.convert("RGB")
    W, H = img.size
    sc = 2 if W > 1200 else 1
    small = img.resize((W // sc, H // sc), Image.NEAREST)
    orange, _ = _color_masks(small)
    px = img.load()
    found = []
    for b in _blobs(orange, min_w=max(4, 16 // sc), min_h=max(2, 5 // sc)):
        x0, y0, x1, y1 = (v * sc for v in b)
        bh, bwid = y1 - y0, x1 - x0
        if not (2.2 <= bwid / max(1, bh) <= 8 and 8 <= bh <= 60):
            continue
        if store and not (x0 < store[0] + store[2] and y0 < store[1] + store[3]):
            continue                      # must be up/left of the store's bottom-right
        if store and store[0] <= x0 <= store[0] + store[2] and store[1] <= y0 <= store[1] + store[3]:
            continue                      # the store's own orange buttons
        # white title strip: its top rows run unbroken above all three buttons,
        # from the window's left edge to past the WORLD button
        ys = None
        for dy in range(1, bh + 1):
            y = y0 - dy
            if y < 0:
                break
            if sum(1 for x in range(x0, x1) if min(px[x, y]) >= 235) >= 0.9 * (x1 - x0):
                ys = y
                break
        if ys is None:
            continue
        x, gap = x0 - 1, 0
        while x > 0 and gap <= 2:
            gap = 0 if min(px[x, ys]) >= 235 else gap + 1
            x -= 1
        left = x + gap + 1
        if x0 - left < 2.5 * bwid:
            continue
        # steel-blue name panel under the strip, measured across its width (one
        # column could run through the map name's letters)
        xs = range(left + (x1 - left) // 3, x1, 3)

        def panel_share(yy):
            return sum(1 for xx in xs if px[xx, yy][2] - px[xx, yy][0] >= 25 and px[xx, yy][1] >= 100) / len(xs)
        y = y1 + 2
        while y < H and y - y1 < bh * 2 and panel_share(y) < 0.3:
            y += 1
        panel_top, miss = y, 0
        while y < H and y - y1 < bh * 9:
            miss = 0 if panel_share(y) >= 0.3 else miss + 1
            if miss > 1:
                y -= 1
                break
            y += 1
        if y - panel_top < bh * 2.5:
            continue
        found.append([max(0, left - 3), max(0, ys - 2), x1 + 8 - max(0, left - 3), y - max(0, ys - 2)])
    if not found:
        return None
    if store:
        found.sort(key=lambda m: (store[0] - m[0]) ** 2 + (store[1] - m[1]) ** 2)
    else:
        found.sort(key=lambda m: m[0] + m[1])
    return found[0]


def _blue_button_mask(img: Image.Image) -> Image.Image:
    """The bright blue of the game's blue buttons (CHANGE CHANNEL)."""
    from PIL import ImageChops

    r, g, b = img.convert("RGB").split()
    mul = ImageChops.multiply

    def band(ch, lo, hi):
        return ch.point(lambda v: 255 if lo <= v <= hi else 0)
    return mul(mul(band(b, 180, 255), band(g, 95, 200)),
               ImageChops.subtract(b, r).point(lambda v: 255 if v >= 110 else 0))


def find_channel_dialog(full: Image.Image) -> list | None:
    """The game's change-channel dialog, found by its two buttons side by side:
    a blue CHANGE CHANNEL with an orange CANCEL just to its right, the same
    height, the blue one about twice as wide, both with white lettering.
    Returns [left, top, width, height] around the pair, or None."""
    img = full.convert("RGB")
    W, H = img.size
    sc = 2 if W > 1200 else 1
    small = img.resize((W // sc, H // sc), Image.NEAREST)
    orange, _ = _color_masks(small)
    oranges = [tuple(v * sc for v in b) for b in _blobs(orange, min_w=max(4, 20 // sc), min_h=max(3, 8 // sc))]
    oranges = [b for b in oranges if 12 <= b[3] - b[1] <= 80 and 2.2 <= (b[2] - b[0]) / (b[3] - b[1]) <= 6.5]
    if not oranges:
        return None
    blues = [tuple(v * sc for v in b)
             for b in _blobs(_blue_button_mask(small), min_w=max(6, 40 // sc), min_h=max(3, 8 // sc))]
    px = img.load()

    def white_share(b):
        n = w = 0
        for y in range(b[1], b[3], 2):
            for x in range(b[0], b[2], 2):
                n += 1
                w += min(px[x, y]) >= 225
        return w / max(1, n)

    for o in oranges:
        oh, ow = o[3] - o[1], o[2] - o[0]
        for bl in blues:
            bh, bw = bl[3] - bl[1], bl[2] - bl[0]
            gap = o[0] - bl[2]
            overlap = min(o[3], bl[3]) - max(o[1], bl[1])
            if not (-2 <= gap <= max(6, 0.8 * oh) and overlap >= 0.7 * min(oh, bh)
                    and 0.75 <= bh / oh <= 1.35 and 1.3 <= bw / ow <= 2.4):
                continue
            if not (0.04 <= white_share(bl) <= 0.45 and 0.03 <= white_share(o) <= 0.45):
                continue                  # both carry white lettering
            x0, y0 = min(bl[0], o[0]), min(bl[1], o[1])
            return [x0, y0, max(bl[2], o[2]) - x0, max(bl[3], o[3]) - y0]
    return None


class ChannelWatch:
    """Notices a channel change: the change-channel dialog (find_channel_dialog)
    was on screen and has gone away, i.e. you changed channel (or cancelled).
    It only counts as gone after `misses` checks in a row without it, so a
    mouse over a button or one bad frame doesn't end it early."""

    def __init__(self, misses: int = 2, every: float = 0.7):
        self.misses, self.every = misses, every
        self.seen = False
        self.gone = 0
        self.last = 0.0

    paused = False

    def pause(self) -> None:
        """Stop noticing channel changes (e.g. while the app's channel picker is open)."""
        self.paused = True
        self.seen, self.gone = False, 0

    def resume(self) -> None:
        self.paused = False
        self.seen, self.gone = False, 0

    quiet_until = 0.0

    def quiet(self, seconds: float) -> None:
        """Ignore channel changes for a while (e.g. right after you picked your
        channel at launch, while the game's Change Channel window may still be open)."""
        self.quiet_until = time.monotonic() + seconds
        self.seen, self.gone = False, 0

    def _off(self) -> bool:
        return self.paused or time.monotonic() < self.quiet_until

    def due(self) -> bool:
        return not self._off() and time.monotonic() - self.last >= self.every

    def feed(self, full: Image.Image) -> bool:
        """Check one screenshot; True once the dialog has come and gone."""
        self.last = time.monotonic()
        if self._off():
            self.seen, self.gone = False, 0
            return False
        if find_channel_dialog(full):
            self.seen, self.gone = True, 0
            return False
        if self.seen:
            self.gone += 1
            if self.gone >= self.misses:
                self.seen, self.gone = False, 0
                return True
        return False


# --------------------------------------------------------------------------- item inventory
# The game's ITEM INVENTORY window: two red buttons and a blue X at the top right of its
# title bar, a gold coin and MESOS at the bottom, and a grid of beige item slots. Its
# size follows the buttons, so it's found the same way at any game resolution.

INV_SLOT_BG = (225, 223, 213)


def _rgb_mask(img: Image.Image, lo: tuple, hi: tuple) -> Image.Image:
    from PIL import ImageChops

    r, g, b = img.convert("RGB").split()
    mul = ImageChops.multiply

    def band(ch, a, z):
        return ch.point(lambda v: 255 if a <= v <= z else 0)
    return mul(mul(band(r, lo[0], hi[0]), band(g, lo[1], hi[1])), band(b, lo[2], hi[2]))


def find_inventory(full: Image.Image) -> list | None:
    """The ITEM INVENTORY window: [left, top, width, height], or None."""
    img = full.convert("RGB")
    W, H = img.size
    sc = 2 if W > 1200 else 1
    small = img.resize((W // sc, H // sc), Image.NEAREST)
    reds = [tuple(v * sc for v in b) for b in _blobs(_rgb_mask(small, (170, 0, 0), (255, 120, 120)),
                                                     min_w=max(3, 6 // sc), min_h=max(3, 6 // sc))]
    reds = [b for b in reds if 6 <= b[3] - b[1] <= 60 and 0.7 <= (b[2] - b[0]) / (b[3] - b[1]) <= 1.4]
    px = img.load()
    gold_mask = None
    found = []
    for a in reds:
        h = a[3] - a[1]
        for b in reds:
            p = b[0] - a[0]
            if not (h * 1.05 <= p <= h * 1.8 and abs(b[1] - a[1]) <= h * 0.25):
                continue
            # the blue X button just right of them: mostly blue (its white X splits
            # it into pieces, so the spot is checked rather than one blue blob)
            c = (b[0] + p, b[1], b[0] + p + (b[2] - b[0]), b[3])
            pts = [(x, y) for y in range(max(0, c[1]), min(H, c[3]), max(1, h // 6))
                   for x in range(max(0, c[0]), min(W, c[2]), max(1, h // 6))]
            blue = sum(1 for x, y in pts if px[x, y][2] >= 160 and px[x, y][2] - px[x, y][0] >= 60)
            if not pts or blue < 0.3 * len(pts):
                continue
            if sc > 1:
                # measure the buttons again at full size (the half-size picture is off by a pixel)
                zx0, zy0 = max(0, a[0] - h), max(0, a[1] - h)
                zone_img = img.crop((zx0, zy0, min(W, c[2] + h), min(H, c[3] + h)))
                fr = sorted((bb for bb in _blobs(_rgb_mask(zone_img, (170, 0, 0), (255, 120, 120)), 3, 3)
                             if 0.7 <= (bb[2] - bb[0]) / max(1, bb[3] - bb[1]) <= 1.4
                             and bb[3] - bb[1] >= 0.6 * h and abs(bb[1] + zy0 - a[1]) <= 0.4 * h),
                            key=lambda t: t[0])
                if len(fr) >= 2:
                    a = (fr[0][0] + zx0, fr[0][1] + zy0, fr[0][2] + zx0, fr[0][3] + zy0)
                    b = (fr[1][0] + zx0, fr[1][1] + zy0, fr[1][2] + zx0, fr[1][3] + zy0)
                    p = b[0] - a[0]
                    c = (b[0] + p, b[1], b[0] + p + (b[2] - b[0]), b[3])
            left = int(a[0] - 12.85 * p)
            top = int(a[1] - 0.4 * p)
            right = int(c[2] + 0.55 * p)
            bottom = int(top + 22.25 * p)
            if left < -p or right > W + p:
                continue
            # the left edge, measured: the title bar's white strip runs from the
            # buttons to the window's border (just above the buttons there's no text)
            best = None
            for yy in range(max(0, a[1] - max(2, int(0.35 * p))), a[1]):
                x, gap = a[0] - 2, 0
                while x > 0 and gap <= 1:
                    gap = 0 if min(px[x, yy]) >= 200 else gap + 1
                    x -= 1
                run = a[0] - 2 - (x + gap + 1)
                if best is None or run > best[0]:
                    best = (run, x + gap + 1)
            if best and abs(best[1] - left) <= 2.5 * p:
                left = best[1] - int(round(0.28 * p))   # the frame left of the white strip
            # the gold coin at the bottom left (only the item inventory has the mesos bar)
            if gold_mask is None:
                gold_mask = _rgb_mask(img, (200, 150, 0), (255, 235, 100))
            zone = (max(0, left - p), max(0, bottom - int(4 * p)), max(1, min(W, left + int(4 * p))),
                    min(H, bottom + int(2 * p)))
            if zone[2] <= zone[0] or zone[3] <= zone[1]:
                continue
            # the coin sits about 1 button-width in from the left edge, near the bottom
            coins = [g for g in _blobs(gold_mask.crop(zone), min_w=max(3, p // 4), min_h=max(3, p // 4))
                     if 0.6 <= (g[2] - g[0]) / max(1, g[3] - g[1]) <= 1.6
                     and 0.3 * p <= (g[0] + g[2]) / 2 + zone[0] - left <= 2.2 * p
                     and 0.4 * p <= g[3] - g[1] <= 1.2 * p]
            if not coins:
                continue
            coin = min(coins, key=lambda g: abs(g[3] + zone[1] + 0.6 * p - bottom))
            bottom = zone[1] + coin[3] + int(0.6 * p)
            left, top = max(0, left), max(0, top)
            found.append([left, top, min(W, right) - left, min(H, bottom) - top])
    return found[0] if found else None


def inventory_tab(win: Image.Image) -> int | None:
    """Which tab is open (0 = Equip, 1 = Use, 2 = Set Up, 3 = Etc, 4 = Cash, ...): the
    red one in the tab row."""
    W, H = win.size
    row = win.convert("RGB").crop((0, int(H * 0.09), W, int(H * 0.125)))
    m = _rgb_mask(row, (170, 0, 0), (255, 140, 160))
    cols = [x for x in range(W) if any(m.getpixel((x, y)) for y in range(0, row.height, 2))]
    if not cols:
        return None
    mid = (cols[0] + cols[-1]) / 2
    return int(mid / (W / 6))


def inventory_slots(win: Image.Image) -> list[tuple]:
    """Item slots of the inventory window, in reading order: (left, top, right, bottom)."""
    W, H = win.size
    beige = _rgb_mask(win, tuple(v - 10 for v in INV_SLOT_BG), tuple(v + 10 for v in INV_SLOT_BG))
    blobs = [b for b in _blobs(beige, min_w=8, min_h=8) if b[1] > H * 0.12]
    if not blobs:
        return []
    sizes = sorted(max(b[2] - b[0], b[3] - b[1]) for b in blobs)
    size = sizes[len(sizes) * 3 // 4]
    full = [b for b in blobs if b[2] - b[0] >= size * 0.85 and b[3] - b[1] >= size * 0.85]

    def clusters(vals):
        out = []
        for v in sorted(vals):
            if out and v - out[-1][-1] <= size * 0.3:
                out[-1].append(v)
            else:
                out.append([v])
        return [sorted(c)[len(c) // 2] for c in out]
    xs, ys = clusters(b[0] for b in full), clusters(b[1] for b in full)
    if not xs or not ys:
        return []

    def fill(vals, limit):
        # columns/rows hidden behind icons everywhere: fill the gaps by the grid pitch
        if len(vals) < 2:
            return vals
        pitch = min(b - a for a, b in zip(vals, vals[1:]))
        out = [vals[0]]
        for v in vals[1:]:
            while v - out[-1] > pitch * 1.5:
                out.append(out[-1] + pitch)
            out.append(v)
        while out[-1] + pitch + size <= limit:
            out.append(out[-1] + pitch)
        return out
    xs, ys = fill(xs, W), fill(ys, H * 0.93)
    return [(x, y, x + size, y + size) for y in ys for x in xs]


def _is_shadow(p, bg) -> bool:
    ratios = [p[i] / max(1, bg[i]) for i in range(3)]
    return max(ratios) - min(ratios) < 0.1 and 0.3 < sum(ratios) / 3 < 0.97


def _icon_mask(img: Image.Image, bg=None, skip_count=False):
    """Foreground pixels of an icon: opaque ones for an icon with transparency, else
    the ones that are neither the background colour nor its shadow. With
    skip_count, the white stack-count digits at the bottom left are left out."""
    rgba = img.convert("RGBA")
    W, H = rgba.size
    px = rgba.load()
    has_alpha = any(px[x, y][3] < 250 for x, y in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)))
    if bg is None and not has_alpha:
        corners = [px[x, y][:3] for x, y in ((1, 1), (W - 2, 1), (1, H - 2), (W - 2, H - 2))]
        bg = max(set(corners), key=corners.count)
    mask = [[False] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            p = px[x, y]
            if has_alpha:
                on = p[3] >= 128
            else:
                # the drop shadow is a grey patch of the background under the icon
                on = sum(abs(p[i] - bg[i]) for i in range(3)) > 45 and not (
                    y >= H * 0.68 and _is_shadow(p, bg))
            mask[y][x] = on
    if skip_count:
        # the stack count at the bottom left: white digits with a dark outline. Their
        # white cores are found, and everything within the outline's width of them is
        # left out (the outline and its soft edge too).
        r = max(1, W // 22)
        zx, zy = int(W * 0.45), int(H * 0.5)
        core = [(x, y) for y in range(zy, H) for x in range(zx) if min(px[x, y][:3]) >= 225]
        for x, y in core:
            for yy in range(max(0, y - r), min(H, y + r + 1)):
                for xx in range(max(0, x - r), min(W, x + r + 1)):
                    mask[yy][xx] = False
    return rgba, mask


TM_GRID = 16


def _slot_info(img: Image.Image, bg=INV_SLOT_BG):
    """An item slot (inventory, or a thumbnail captured from a shop row): its icon
    mask and where the icon is. The stack count sits in the bottom-left corner,
    so the icon's top and right edges are found without that corner."""
    rgba, mask = _icon_mask(img, bg)
    W, H = rgba.size
    zx, zy = int(W * 0.42), int(H * 0.58)
    # border lines left at the edges of the crop (a slot's frame) aren't the icon
    for _ in range(max(2, W // 12)):
        cleared = False
        for x in (0, W - 1):
            col = [mask[y][x] for y in range(H)]
            if sum(col) > 0.6 * H:
                for y in range(H):
                    mask[y][x] = False
                cleared = True
        for y in (0, H - 1):
            if sum(mask[y]) > 0.6 * W:
                mask[y] = [False] * W
                cleared = True
        # the next line in, if the outer one was a line
        if not cleared:
            break
        rgba = rgba.crop((1, 1, W - 1, H - 1))
        mask = [row[1:W - 1] for row in mask[1:H - 1]]
        W, H = W - 2, H - 2
        zx, zy = int(W * 0.42), int(H * 0.58)

    def bbox(skip_zone):
        xs, ys = [], []
        for y in range(H):
            row = mask[y]
            for x in range(W):
                if row[x] and not (skip_zone and x < zx and y >= zy):
                    xs.append(x)
                    ys.append(y)
        return (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if xs else None
    out = bbox(True)
    full = bbox(False)
    if not out or not full:
        return None
    return {"rgba": rgba, "mask": mask, "W": W, "H": H, "zone": (zx, zy), "top": out[1],
            "right": out[2], "left": full[0], "bottom": full[3], "out_bottom": out[3], "out_left": out[0]}


def ref_grid(img: Image.Image):
    """An official icon (with transparency) as a 16x16 grid over its outline:
    (True, colour) where the icon is, (False, None) where it isn't."""
    rgba = img.convert("RGBA")
    a = rgba.split()[3].point(lambda v: 255 if v >= 128 else 0)
    box = a.getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = box
    px = rgba.load()
    n = TM_GRID
    cells = []
    tot = [0, 0, 0]
    k = 0
    for j in range(n):
        for i in range(n):
            x = min(x1 - 1, x0 + int((i + 0.5) * (x1 - x0) / n))
            y = min(y1 - 1, y0 + int((j + 0.5) * (y1 - y0) / n))
            p = px[x, y]
            if p[3] >= 128:
                cells.append((True, p[:3]))
                tot[0] += p[0]; tot[1] += p[1]; tot[2] += p[2]
                k += 1
            else:
                cells.append((False, None))
    if not k:
        return None
    # the overall colour from inside the icon (its dark outline left out), as for slots
    al = rgba.split()[3].load()
    it = [0, 0, 0]
    m = 0
    for y in range(y0 + 1, y1 - 1):
        for x in range(x0 + 1, x1 - 1):
            if al[x, y] >= 128 and al[x - 1, y] >= 128 and al[x + 1, y] >= 128 \
                    and al[x, y - 1] >= 128 and al[x, y + 1] >= 128:
                p = px[x, y]
                if max(p[:3]) > 60:            # not the outline
                    it[0] += p[0]; it[1] += p[1]; it[2] += p[2]
                    m += 1
    mean = (it[0] / m, it[1] / m, it[2] / m) if m else (tot[0] / k, tot[1] / k, tot[2] / k)
    return {"cells": cells, "aspect": (x1 - x0) / (y1 - y0), "mean": mean}


def slot_ref_grid(img: Image.Image):
    """A slot picture (a shop thumbnail, or an icon you matched by hand) as a grid,
    with the stack-count corner marked unknown."""
    info = _slot_info(_slot_inner(img))
    if not info:
        return None
    x0, y0, x1, y1 = info["left"], info["top"], info["right"], info["bottom"]
    zx, zy = info["zone"]
    px = info["rgba"].load()
    n = TM_GRID
    cells = []
    tot = [0, 0, 0]
    k = 0
    for j in range(n):
        for i in range(n):
            x = min(x1 - 1, x0 + int((i + 0.5) * (x1 - x0) / n))
            y = min(y1 - 1, y0 + int((j + 0.5) * (y1 - y0) / n))
            if x < zx and y >= zy:
                cells.append((None, None))
            elif info["mask"][y][x]:
                p = px[x, y]
                cells.append((True, p[:3]))
                tot[0] += p[0]; tot[1] += p[1]; tot[2] += p[2]
                k += 1
            else:
                cells.append((False, None))
    if not k:
        return None
    return {"cells": cells, "aspect": (x1 - x0) / max(1, y1 - y0), "mean": (tot[0] / k, tot[1] / k, tot[2] / k)}


def _slot_inner(img: Image.Image) -> Image.Image:
    """The inside of an item slot: its beige area without the border."""
    rgb = img.convert("RGB")
    W, H = rgb.size
    beige = _rgb_mask(rgb, tuple(v - 12 for v in INV_SLOT_BG), tuple(v + 12 for v in INV_SLOT_BG))
    box = beige.getbbox()
    if not box or (box[2] - box[0]) < W * 0.5 or (box[3] - box[1]) < H * 0.5:
        box = (0, 0, W, H)
    x0, y0, x1, y1 = box
    m = max(1, int(round((x1 - x0) * 0.04)))
    return rgb.crop((x0 + m, y0 + m, x1 - m, y1 - m))


def slot_query(img: Image.Image):
    """What's needed to compare an item slot with icons: its mask, where the icon
    is, and its overall colour (outside the stack-count corner)."""
    info = _slot_info(_slot_inner(img))
    if not info:
        return None
    zx, zy = info["zone"]
    px = info["rgba"].load()
    mask, W, H = info["mask"], info["W"], info["H"]
    tot = [0, 0, 0]
    k = n_on = 0
    for y in range(H):
        for x in range(W):
            if not mask[y][x] or (x < zx and y >= zy):
                continue
            n_on += 1
            # the colour inside the icon: its edges blend into the background
            if 0 < x < W - 1 and 0 < y < H - 1 and mask[y][x - 1] and mask[y][x + 1] \
                    and mask[y - 1][x] and mask[y + 1][x]:
                p = px[x, y]
                if max(p[:3]) > 60:            # not the outline
                    tot[0] += p[0]; tot[1] += p[1]; tot[2] += p[2]
                    k += 1
    if n_on < max(6, W * H // 60) or not k:
        return None
    info["mean"] = (tot[0] / k, tot[1] / k, tot[2] / k)
    info["aspect"] = (info["right"] - info["left"]) / max(1, info["bottom"] - info["top"])
    return info


def grid_distance(q, ref) -> float:
    """How unlike a slot and an icon are (0 = the same), lined up by the icon's top
    and right edges and tried at a few sizes; the stack-count corner is skipped."""
    zx, zy = q["zone"]
    W, H = q["W"], q["H"]
    mask, px = q["mask"], q["rgba"].load()
    asp = ref["aspect"]
    top, right = q["top"], q["right"]
    heights = {q["bottom"] - top, q["out_bottom"] - top, int((right - q["left"]) / asp),
               int((right - q["out_left"]) / asp)}
    best = 9.9
    n = TM_GRID
    for h in heights:
        if h < 4:
            continue
        w = h * asp
        x0 = right - w
        if x0 < -W * 0.15 or top + h > H * 1.15:
            continue
        cost = 0.0
        used = 0
        for j in range(n):
            y = int(top + (j + 0.5) * h / n)
            for i in range(n):
                on, col = ref["cells"][j * n + i]
                if on is None:
                    continue
                x = int(x0 + (i + 0.5) * w / n)
                if not (0 <= x < W and 0 <= y < H):
                    if on:
                        cost += 1.0
                        used += 1
                    continue
                if x < zx and y >= zy:
                    continue
                used += 1
                if on:
                    if mask[y][x]:
                        p = px[x, y]
                        cost += (abs(p[0] - col[0]) + abs(p[1] - col[1]) + abs(p[2] - col[2])) / 765 * 2.5
                    else:
                        cost += 1.0
                elif mask[y][x]:
                    cost += 1.0
        if used:
            best = min(best, cost / used)
    return best


ICON_SIGS_PATH = CACHE_DIR / "icon_signatures.pickle"
ICON_SIG_VERSION = 2


def _color_key(c):
    """(colour balance, brightness) of an average colour."""
    t = max(1.0, c[0] + c[1] + c[2])
    return (c[0] / t, c[1] / t, c[2] / t), t / 3


class IconMatcher:
    """Matches item slots from the screen against every icon the app has: the
    official ones (downloaded with Update), icons you matched by hand, and the
    ones captured from shops."""

    def __init__(self, con):
        import io
        import pickle

        keys = {}
        for item, key in _icon_keys().items():
            keys.setdefault(str(key), []).append(item)
        known = _dict().names if has_item_list() else None
        self.refs = []          # (item, grid, official)
        seen = set()
        # grids of the official icons are kept, so only new icons are worked out
        try:
            cache = pickle.loads(ICON_SIGS_PATH.read_bytes())
            if cache.get("version") != ICON_SIG_VERSION:
                cache = {}
        except Exception:
            cache = {}
        grids = cache.get("grids", {})
        changed = False
        for key, png in con.execute("SELECT icon_key, png FROM cache.meowdb_icons"):
            items = keys.get(str(key))
            if not items:
                continue
            if key in grids:
                g = grids[key]
            else:
                try:
                    g = ref_grid(Image.open(io.BytesIO(png)))
                except Exception:
                    g = None
                grids[key] = g
                changed = True
            if g:
                for item in items:
                    if known is None or item in known or len(items) == 1:
                        self.refs.append((item, g, True))
                        seen.add(item)
        if changed:
            try:
                ICON_SIGS_PATH.write_bytes(pickle.dumps({"version": ICON_SIG_VERSION, "grids": grids}))
            except OSError:
                pass
        for item, png in con.execute("SELECT item, png FROM cache.learned_icons"):
            try:
                g = slot_ref_grid(Image.open(io.BytesIO(png)))
            except Exception:
                g = None
            if g:
                self.refs.append((item, g, True))
        for item, png, faded in con.execute("SELECT item, png, faded FROM cache.icons"):
            if item in seen or faded:
                continue
            try:
                g = slot_ref_grid(Image.open(io.BytesIO(png)))
            except Exception:
                g = None
            if g:
                self.refs.append((item, g, False))

    def add(self, item: str, slot: Image.Image) -> None:
        """Use this slot's picture for this item from now on (an icon matched by hand)."""
        g = slot_ref_grid(slot)
        if g:
            self.refs.append((item, g, True))

    def match_slot(self, slot: Image.Image, top: int = 3) -> list[tuple[str, float]]:
        """The closest items to an item slot, best first: [(item, distance)]; [] if
        the slot is empty."""
        q = slot_query(slot)
        if q is None or not self.refs:
            return []
        a = q["aspect"]
        hue, light = _color_key(q["mean"])

        def near(r):
            # colour balance first (on-screen icons come out lighter or darker than the
            # official ones, but keep their colours), then brightness and shape
            h, l = _color_key(r[1]["mean"])
            return (300 * (abs(h[0] - hue[0]) + abs(h[1] - hue[1]) + abs(h[2] - hue[2]))
                    + 0.2 * abs(l - light) + 20 * abs(r[1]["aspect"] - a))
        coarse = sorted(self.refs, key=near)[:800]
        scored = {}
        for item, g, official in coarse:
            d = grid_distance(q, g) - (0.01 if official else 0)
            if item not in scored or d < scored[item]:
                scored[item] = d
        return sorted(scored.items(), key=lambda t: t[1])[:top]


INV_MATCH_OK = 0.45       # a match closer than this, and clearly ahead of the next, is found
INV_MATCH_MARGIN = 0.02


def decide_match(best: list[tuple[str, float]]) -> dict:
    """From match_slot's list: the item if it's a clear match (else None, for you to
    review), the best guess, and how alike it is (0-1)."""
    if not best:
        return {"item": None, "guess": None, "score": 0.0}
    item, dist = best[0]
    clear = dist <= INV_MATCH_OK and (len(best) < 2 or best[1][1] - dist >= INV_MATCH_MARGIN)
    return {"item": item if clear else None, "guess": item, "score": max(0.0, min(1.0, 1 - dist))}


def learn_icon(con, item: str, icon: Image.Image) -> None:
    """Remember an inventory icon you matched by hand, so it's recognised next time."""
    import io

    buf = io.BytesIO()
    icon.convert("RGB").save(buf, format="PNG")
    con.execute("INSERT INTO cache.learned_icons (item, png, added) VALUES (?,?,?)",
                (item, buf.getvalue(), utc_now()))
    con.commit()


def read_slot_count(slot: Image.Image, cfg: dict) -> int:
    """The stack count at a slot's bottom left (1 when it can't be read)."""
    import pytesseract

    W, H = slot.size
    part = slot.crop((0, int(H * 0.5), int(W * 0.6), H))
    setup_tesseract(cfg)
    try:
        text = pytesseract.image_to_string(white_text_mask(part, up=4, thr=215),
                                           config="--psm 7 -c tessedit_char_whitelist=0123456789")
    except Exception:
        return 1
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits and 0 < int(digits) <= 9999 else 1


def scan_inventory(win: Image.Image, matcher: IconMatcher, cfg: dict, read_counts: bool = True) -> list[dict]:
    """The items in an inventory window: [{"slot", "item", "score", "qty", "icon"}] for
    every slot with something in it; "item" is None when no icon is close enough."""
    out = []
    for i, (x0, y0, x1, y1) in enumerate(inventory_slots(win)):
        slot = win.crop((x0, y0, x1, y1))
        best = matcher.match_slot(slot)
        if not best:
            continue
        out.append({"slot": i, **decide_match(best),
                    "qty": read_slot_count(slot, cfg) if read_counts else 1, "icon": slot})
    return out


# My Inventory: the items you own, in their own database (inventory/inventory.db).
INVENTORY_DIR = DATA / "inventory"
INVENTORY_DB_PATH = INVENTORY_DIR / "inventory.db"
INVENTORY_PATH = SETTINGS_DIR / "inventory.json"      # kept by 2610.13.001 builds; moved in once
MESO_TAX = 0.03                                       # the Free Market's fee on every sale
INV_FIELDS = ("item", "qty", "notes", "list_price", "sold", "sold_at", "sold_ids", "added")


def inventory_db():
    """The My Inventory database (created on first use)."""
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(INVENTORY_DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY, item TEXT NOT NULL, qty INTEGER NOT NULL DEFAULT 1,
        notes TEXT, list_price INTEGER, sold INTEGER NOT NULL DEFAULT 0, sold_at TEXT,
        sold_ids TEXT, added TEXT)""")
    if INVENTORY_PATH.exists():
        # the list kept in settings/inventory.json by the first builds: moved in once
        try:
            old = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
            for e in old:
                con.execute("INSERT INTO items (item, qty, added) VALUES (?,?,?)",
                            (e["item"], int(e.get("qty") or 1), e.get("added") or utc_now()))
            con.commit()
            INVENTORY_PATH.rename(INVENTORY_PATH.with_suffix(".json.moved"))
        except (OSError, ValueError, KeyError):
            pass
    return con


def load_inventory(con=None) -> list[dict]:
    """Every row of My Inventory: owned items first, then sold ones."""
    con = con or inventory_db()
    rows = con.execute("SELECT id, " + ", ".join(INV_FIELDS) + " FROM items ORDER BY sold, id").fetchall()
    return [dict(zip(("id",) + INV_FIELDS, r)) for r in rows]


def add_inventory(con, item: str, qty: int = 1, notes: str | None = None, list_price: int | None = None) -> None:
    """Add owned items. Matching unsold rows (same item, note and list price) get the quantity added."""
    row = con.execute("SELECT id FROM items WHERE item = ? AND sold = 0 AND COALESCE(notes, '') = ? "
                      "AND COALESCE(list_price, -1) = ?", (item, notes or "", list_price if list_price else -1)
                      ).fetchone()
    if row:
        con.execute("UPDATE items SET qty = qty + ? WHERE id = ?", (qty, row[0]))
    else:
        con.execute("INSERT INTO items (item, qty, notes, list_price, added) VALUES (?,?,?,?,?)",
                    (item, qty, notes or None, list_price or None, utc_now()))
    con.commit()


def update_inventory(con, ids, **fields) -> None:
    """Change qty, notes or list_price of rows."""
    ok = {k: v for k, v in fields.items() if k in ("qty", "notes", "list_price", "item")}
    for i in ids:
        for k, v in ok.items():
            con.execute(f"UPDATE items SET {k} = ? WHERE id = ?", (v, i))
    con.commit()


def delete_inventory(con, ids) -> None:
    con.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in ids])
    con.commit()


def clear_inventory(con) -> Path | None:
    """Delete everything in My Inventory, after copying the database to backups/."""
    backup = None
    if INVENTORY_DB_PATH.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"inventory-{_stamp()}.db"
        shutil.copy2(INVENTORY_DB_PATH, backup)
    con.execute("DELETE FROM items")
    con.commit()
    return backup


def mark_sold(con, prices_con, row_id: int, n: int | None = None, price: int | None = None) -> int:
    """Mark n of a row as sold (all when n is None) at `price` each (else its list
    price). Selling only some splits the row: the rest stays owned. Each one sold is
    recorded in the price database as a confirmed sale. Returns the sold row's id."""
    r = con.execute("SELECT item, qty, notes, list_price, sold FROM items WHERE id = ?", (row_id,)).fetchone()
    if not r or r[4]:
        return row_id
    item, qty, notes, lp, _ = r
    n = qty if n is None else max(1, min(int(n), qty))
    price = price or lp
    if n < qty:
        con.execute("UPDATE items SET qty = qty - ? WHERE id = ?", (n, row_id))
        row_id = con.execute("INSERT INTO items (item, qty, notes, list_price, added) VALUES (?,?,?,?,?)",
                             (item, n, notes, price, utc_now())).lastrowid
    now = utc_now()
    ids = []
    if price:
        for _ in range(n):
            ids.append(prices_con.execute(
                "INSERT INTO listings (ts, item, price, qty, shop, seller, map, channel, raw, session, sold, "
                "last_seen, sold_ts, notes) VALUES (?,?,?,1,?,?,?,?,?,?,1,?,?,?)",
                (now, item, int(price), "My Inventory", "", "", None, "sold from My Inventory",
                 "inventory", now, now, notes)).lastrowid)
        prices_con.commit()
    con.execute("UPDATE items SET sold = 1, sold_at = ?, list_price = ?, sold_ids = ? WHERE id = ?",
                (now, price, json.dumps(ids), row_id))
    con.commit()
    return row_id


def unmark_sold(con, prices_con, row_id: int) -> int:
    """Undo mark_sold: the sale records are removed from the price database (any that
    are still there) and the items are owned again, joined to a matching owned row."""
    r = con.execute("SELECT item, qty, notes, list_price, sold, sold_ids FROM items WHERE id = ?",
                    (row_id,)).fetchone()
    if not r or not r[4]:
        return 0
    item, qty, notes, lp, _, sold_ids = r
    try:
        ids = [int(i) for i in json.loads(sold_ids or "[]")]
    except (ValueError, TypeError):
        ids = []
    removed = 0
    if ids:
        removed = delete_listings(prices_con, ids)
    other = con.execute("SELECT id FROM items WHERE item = ? AND sold = 0 AND COALESCE(notes, '') = ? "
                        "AND COALESCE(list_price, -1) = ? AND id != ?",
                        (item, notes or "", lp if lp else -1, row_id)).fetchone()
    if other:
        con.execute("UPDATE items SET qty = qty + ? WHERE id = ?", (qty, other[0]))
        con.execute("DELETE FROM items WHERE id = ?", (row_id,))
    else:
        con.execute("UPDATE items SET sold = 0, sold_at = NULL, sold_ids = NULL WHERE id = ?", (row_id,))
    con.commit()
    return removed


INV_CSV = ("item", "qty", "notes", "list_price", "status", "sold_at_utc", "added_utc")


def export_inventory(con, path) -> int:
    """My Inventory as a CSV file (opens in any spreadsheet app; can be imported again)."""
    rows = load_inventory(con)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(INV_CSV)
        for r in rows:
            w.writerow([r["item"], r["qty"], r["notes"] or "", r["list_price"] or "",
                        "sold" if r["sold"] else "owned", r["sold_at"] or "", r["added"] or ""])
    return len(rows)


def import_inventory(con, path, replace: bool = False) -> int:
    """Load a My Inventory CSV (from Export): added to the list, or replacing it.
    Sold rows come back as sold (without new sale records)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows or "item" not in rows[0]:
        raise ValueError("This isn't a My Inventory file (it needs an 'item' column).")
    if replace:
        con.execute("DELETE FROM items")
    n = 0
    for r in rows:
        item = (r.get("item") or "").strip()
        if not item:
            continue
        try:
            qty = max(1, int(float(r.get("qty") or 1)))
        except ValueError:
            qty = 1
        lp = parse_price(r.get("list_price") or "") if (r.get("list_price") or "").strip() else None
        sold = (r.get("status") or "").strip().lower() == "sold"
        con.execute("INSERT INTO items (item, qty, notes, list_price, sold, sold_at, added) VALUES (?,?,?,?,?,?,?)",
                    (item, qty, (r.get("notes") or "").strip() or None, lp, int(sold),
                     (r.get("sold_at_utc") or None) if sold else None, r.get("added_utc") or utc_now()))
        n += 1
    con.commit()
    return n


def inventory_stats(con, item: str, cfg: dict) -> dict:
    """What one owned item is worth: market price, the range of normal prices seen
    (within the market window), how many sightings there are, how many sold in the
    last 24 hours, and the average of the last 3 sale prices."""
    mkt, _ = market_rate(con, item, cfg)
    bounds = outlier_bounds(con, item, cfg)
    prices = [p for (p,) in con.execute(
        "SELECT price FROM listings WHERE item = ? AND COALESCE(last_seen, ts) >= ?",
        (item, utc_ago(days=cfg["history_days"]))) if not is_outlier(p, bounds)]
    points = con.execute("SELECT COUNT(*) FROM listings WHERE item = ?", (item,)).fetchone()[0]
    sold24 = con.execute("SELECT COUNT(*) FROM listings WHERE item = ? AND COALESCE(sold, 0) = 1 "
                         "AND COALESCE(sold_ts, last_seen, ts) >= ?", (item, utc_ago(hours=24))).fetchone()[0]
    last3 = [p for (p,) in con.execute(
        "SELECT price FROM listings WHERE item = ? AND COALESCE(sold, 0) = 1 "
        "ORDER BY COALESCE(sold_ts, last_seen, ts) DESC LIMIT 3", (item,))]
    return {"market": mkt, "low": min(prices) if prices else None, "high": max(prices) if prices else None,
            "points": points, "sold24": sold24,
            "last3": int(round(sum(last3) / len(last3))) if last3 else None, "last3_n": len(last3)}

def is_auto(region, full_size) -> bool:
    """Use automatic detection when no area is set, or when the saved area is
    most of the screen (e.g. an accidental full-screen drag)."""
    if not region:
        return True
    return region[2] * region[3] >= 0.4 * full_size[0] * full_size[1]


def white_text_mask(img: Image.Image, up: int = 4, thr: int = 190) -> Image.Image:
    """Keep only near-white pixels (minimap text is white with a dark outline)
    and return them as black text on white for Tesseract."""
    from PIL import ImageChops

    big = img.convert("RGB").resize((img.width * up, img.height * up), Image.LANCZOS)
    r, g, b = big.split()
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    sat = ImageChops.subtract(mx, mn)
    bright = mn.point(lambda p: 255 if p >= thr else 0)
    neutral = sat.point(lambda p: 255 if p < 40 else 0)
    return ImageOps.invert(ImageChops.multiply(bright, neutral))


def _blank_minimap_icon(img: Image.Image) -> Image.Image:
    """The minimap's name panel has the area's icon at its left; OCR reads it as
    junk ("ies ||"). Paint it over with the panel colour: the icon is the run of
    columns at the left that are mostly not steel-blue over the panel's rows."""
    rgb = img.convert("RGB")
    W, H = rgb.size
    px = rgb.load()

    def blue(p):
        return p[2] - p[0] >= 25 and p[1] >= 100
    rows = [y for y in range(H) if sum(blue(px[x, y]) for x in range(0, W, 2)) > 0.25 * W]
    if len(rows) < 8:
        return rgb
    ys = rows[:: max(1, len(rows) // 40)]
    frac = [sum(not blue(px[x, y]) for y in ys) / len(ys) for x in range(W)]
    text_level = sorted(frac[W // 3:])[len(frac[W // 3:]) // 2] if W > 30 else 0
    x, limit = 0, W // 4
    while x < limit and frac[x] < text_level + 0.3:
        x += 1
    if x >= limit:
        return rgb                               # no icon (e.g. a hand-drawn area without it)
    start = x
    while x < W // 3 and frac[x] >= text_level + 0.3:
        x += 1
    if x - start < max(6, 0.4 * len(rows)):      # too thin to be the icon
        return rgb
    out = rgb.copy()
    fill = tuple(int(v) for v in px[min(W - 1, x + 2), rows[len(rows) // 2]])
    from PIL import ImageDraw
    ImageDraw.Draw(out).rectangle((0, rows[0], x + 2, rows[-1]), fill=fill)
    return out


def _trim_to_panel(img: Image.Image) -> Image.Image:
    """Cut a map capture down to the minimap's name panel. The minimap is as wide
    as its map's name, so an area measured on one map can be too wide (or tall)
    on the next: the Free Market's minimap is narrower than Victoria Road's, and
    the shop signs beside it were read into the name ("Hidden Steet $_L—z").
    The panel is one flat steel-blue colour; its rows are the ones mostly that
    colour, and it ends at the first columns, going right, without any of it."""
    rgb = img.convert("RGB")
    W, H = rgb.size
    if W < 40 or H < 20:
        return rgb
    px = rgb.load()

    def blue(p):
        return p[2] - p[0] >= 25 and p[1] >= 100
    cols = range(0, W, 2)
    counts = {}
    for y in range(H):
        for x in cols:
            p = px[x, y]
            if blue(p):
                k = (p[0] // 8, p[1] // 8, p[2] // 8)
                counts[k] = counts.get(k, 0) + 1
    if not counts:
        return rgb
    k = max(counts, key=counts.get)
    ref = (k[0] * 8 + 4, k[1] * 8 + 4, k[2] * 8 + 4)

    def panel(p):
        return abs(p[0] - ref[0]) <= 14 and abs(p[1] - ref[1]) <= 14 and abs(p[2] - ref[2]) <= 14
    def panel_rows(w):
        cs = range(0, w, 2)
        rows = [y for y in range(H) if sum(panel(px[x, y]) for x in cs) >= 0.2 * len(cs)]
        if len(rows) < 8:
            return None
        # the panel's rows: the longest run (gaps allowed between text lines)
        runs, start, prev = [], rows[0], rows[0]
        for y in rows[1:]:
            if y - prev > 4:
                runs.append((start, prev))
                start = y
            prev = y
        runs.append((start, prev))
        top, bot = max(runs, key=lambda r: r[1] - r[0])
        return (top, bot) if bot - top >= 8 else None

    span = panel_rows(W)
    if not span:
        return rgb
    ys = range(span[0], span[1] + 1)
    frac = [sum(panel(px[x, y]) for y in ys) / len(ys) for x in range(W)]
    # walk right from the most panel-like column to where the panel stops
    x = max(range(W), key=lambda x: frac[x])
    gap, right = 0, x
    while x < W and gap < 4:
        if frac[x] >= 0.05:
            right, gap = x, 0
        else:
            gap += 1
        x += 1
    right = min(W, right + 3)
    span = panel_rows(right) or span            # measured again without the scenery
    top, bot = span
    # the title strip (MINI MAP) above the panel stays; it's skipped when read
    out_top = 0 if top < H * 0.5 else max(0, top - 2)
    return rgb.crop((0, out_top, right, min(H, bot + 3)))


def _clean_map_line(ln: str) -> str:
    ln = re.sub(r"^[\W_]+", "", ln)                  # crumbs on the left
    ln = re.sub(r"[^\w<>)\]]+$", "", ln).strip()
    toks = ln.split()
    while toks and len(re.sub(r"[\W_]", "", toks[0])) < 3 and not re.fullmatch(r"<\d+>", toks[0]):
        toks.pop(0)                     # crumbs from icons or the mouse cursor: "Se’ Free..."
    ln = " ".join(toks)
    if "<" in ln and ">" in ln[ln.index("<"):]:
        ln = ln[: ln.rindex(">") + 1]   # "Line 1 <Area 1> [@l" -> "Line 1 <Area 1>"
    else:
        toks = ln.split()
        while toks and is_junk(toks[-1]):
            toks.pop()                  # minimap window-button crumbs
        ln = " ".join(toks)
    ln = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", ln)       # "FreeMarket" -> "Free Market"
    ln = re.sub(r"(?<=\S)<", " <", ln)                 # "Market<4>" -> "Market <4>"
    return ln


def read_map(img: Image.Image, cfg: dict, known_maps: list[str] = ()) -> str:
    """Read the full map name from the minimap header. It's on two lines: the area
    (street) on top and the zone below; together "Kerning City Subway: Line 1 <Area 1>"
    or "Hidden Street: Free Market <2>"."""
    import pytesseract

    setup_tesseract(cfg)
    if img.height > img.width * 0.6:
        # a manual area that includes the minimap picture: the names are at the top
        img = img.crop((0, 0, img.width, int(img.width * 0.42)))
    img = _trim_to_panel(img)
    img = _blank_minimap_icon(img)
    text = pytesseract.image_to_string(white_text_mask(img), config="--psm 6")
    good = []
    for ln in text.splitlines():
        if re.search(r"(?i)mini\s*ma[pr]", ln):        # the window's title strip
            continue
        ln = _clean_map_line(ln)
        if len(re.findall(r"[A-Za-z]", ln)) >= 4 or re.search(r"<\w*\s*\d+>", ln):
            good.append(ln)
    if not good:
        return ""
    zone = good[-1]
    street = good[-2] if len(good) >= 2 else ""
    name = full_map_name(street, zone)
    if _map_names():
        return snap_map(name)                            # validated against the map lists
    return normalize_name(name, list(known_maps)) if known_maps else name


ITEMS_PATH = CACHE_DIR / "items.txt"
ITEM_IDS_PATH = CACHE_DIR / "item_ids.json"
ITEM_ICONS_PATH = CACHE_DIR / "item_icons.json"     # item name -> meowdb icon key
ITEM_TYPES_PATH = CACHE_DIR / "item_types.json"     # item name -> meowdb subtype ("Throwing Star", ...)
MEOWDB_API = "https://meowdb.com/msclassic/api"
USER_AGENT = "mfm-tracker/1.0 (Free Market price tracker for MapleStory Classic World)"


# Trusted certificate authorities for HTTPS. A packaged app carries a Python built on
# another machine, whose idea of where the system's certificates are can be wrong
# (built on GitHub's Ubuntu, run on Arch: "unable to get local issuer certificate").
_CA_FILES = ("/etc/ssl/certs/ca-certificates.crt",      # Debian, Ubuntu, Arch, Gentoo
             "/etc/pki/tls/certs/ca-bundle.crt",        # Fedora, RHEL
             "/etc/ssl/ca-bundle.pem",                  # openSUSE
             "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem",   # OpenELEC, Alpine, macOS
             "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem")


def ssl_context():
    """An SSL context that trusts the usual certificate authorities: the bundle
    shipped with the app (certifi), plus the system's own (Windows certificate
    store, or the first CA file found on Linux/macOS)."""
    if ssl_context.cache is not None:
        return ssl_context.cache
    import ssl

    ctx = ssl.create_default_context()           # the system store (works on Windows)
    loaded = False
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        loaded = True
    except Exception:
        pass
    for f in _CA_FILES:
        if os.path.isfile(f):
            try:
                ctx.load_verify_locations(cafile=f)
                loaded = True
                break
            except Exception:
                continue
    if not loaded and FROZEN:
        try:
            with open(DATA / "app.log", "a", encoding="utf-8") as fh:
                fh.write(f"{utc_now()} no CA certificates found; HTTPS may fail\n")
        except OSError:
            pass
    ssl_context.cache = ctx
    return ctx


ssl_context.cache = None


def _get(url: str, timeout: int = 30) -> bytes:
    """HTTP GET with a descriptive user agent and polite retry on rate limiting."""
    import urllib.error
    import urllib.request

    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"giving up on {url}")


MEOWDB_ITEMS_PATH = CACHE_DIR / "meowdb_items.json"   # master copy: {id: {name, displayName, iconKey, subtype}}
MEOWDB_SYNC_PATH = CACHE_DIR / "meowdb_sync.json"     # what we have and when we last asked
ITEM_CHECK_EVERY = timedelta(days=1)              # look for new items at most daily
ITEM_FULL_EVERY = timedelta(days=30)              # re-download everything at most monthly
MAP_CHECK_EVERY = timedelta(days=7)               # the map list rarely changes
PAGE = 500


@dataclass
class SyncResult:
    items_total: int = 0
    items_new: int = 0
    requests: int = 0
    full: bool = False
    maps_updated: bool = False
    message: str = ""


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _get_cond(url: str, etag: str | None = None, last_modified: str | None = None):
    """Conditional GET: returns (status, body, etag, last_modified). 304 = unchanged,
    with no body, so an unchanged page costs meowdb almost nothing."""
    import urllib.error
    import urllib.request

    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    for attempt in range(4):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as r:
                return r.status, r.read(), r.headers.get("ETag"), r.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, b"", etag, last_modified
            if e.code in (429, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"giving up on {url}")


def _ago(ts: str | None) -> timedelta | None:
    try:
        return datetime.now() - datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _page_guard(page: int, data: dict) -> None:
    """meowdb says how many pages there are (totalPages) and whether more follow
    (hasMore); pages are read until hasMore is false, however many there are. This
    only stops a broken response from looping forever."""
    total = data.get("totalPages")
    limit = (int(total) + 5) if str(total or "").isdigit() else 400
    if page > max(limit, 20):
        raise RuntimeError(f"meowdb kept saying more pages follow (page {page} of {total}); stopped")


REC_FIELDS = ("name", "displayName", "iconKey", "subtype", "type", "levelReq", "reqJob", "sources", "npcSell")


def _meow_record(it: dict) -> dict:
    """What's kept of a meowdb item: names, icon, category (type: Equip / Use / Etc /
    Setup / Cash, and subtype: Shoes, Scroll, ...), required level and job, and where
    it comes from (npc / mob / quest ...). Used for spelling, icons and the Items
    tab's filters."""
    try:
        stats = json.loads(it.get("stats") or "{}") if isinstance(it.get("stats"), str) else (it.get("stats") or {})
    except ValueError:
        stats = {}
    lvl = it.get("levelReq")
    return {"name": it.get("name"), "displayName": it.get("displayName"), "iconKey": it.get("iconKey"),
            "subtype": it.get("displaySubtype") or it.get("subtype"), "type": it.get("type"),
            "levelReq": int(lvl) if str("" if lvl is None else lvl).isdigit() else None,
            "reqJob": stats.get("reqJob") or None,
            "npcSell": int(stats["price"]) if str(stats.get("price", "")).isdigit() else None,
            "sources": [str(x) for x in (it.get("sources") or []) if x]}


def update_items_meowdb(progress=None, full: bool = False, force: bool = False) -> SyncResult:
    """Bring the local copy of NiaMeowDB's item list up to date with as few
    requests as possible.

    meowdb's item API returns items in id order and new items get new ids, so a
    normal update re-checks only the page holding our last known item (with a
    conditional request; "304 Not Modified" if nothing changed) and any pages
    after it: usually one request. It checks at most once a day unless forced.
    A full re-download happens only on request, the first time, or after 30 days
    (to pick up edits to existing items)."""
    sync = _load_json(MEOWDB_SYNC_PATH, {})
    st = sync.setdefault("items", {})
    recs: dict = _load_json(MEOWDB_ITEMS_PATH, {})
    res = SyncResult(items_total=len(recs))
    last_full = _ago(st.get("last_full"))
    need_full = (full or not recs or last_full is None or last_full > ITEM_FULL_EVERY
                 or any("npcSell" not in r for r in recs.values()))  # saved before filters / NPC prices
    last_check = _ago(st.get("last_check"))
    if not need_full and not force and last_check is not None and last_check < ITEM_CHECK_EVERY:
        res.message = f"Item list checked {_fmt_ago(last_check)} ago; no request made."
        _sync_maps(sync, res, False)
        _sync_npc_shops(sync, res, False)    # only if they've never been downloaded
        return res

    def fetch(page, conditional):
        url = f"{MEOWDB_API}/items?page={page}&limit={PAGE}"
        tag = st.get("etags", {}).get(url, [None, None]) if conditional else [None, None]
        status, body, et, lm = _get_cond(url, *tag)
        res.requests += 1
        if status == 200:
            st.setdefault("etags", {})[url] = [et, lm]
            return json.loads(body.decode("utf-8"))
        return None   # 304: unchanged

    def add(batch, into):
        n = 0
        for it in batch:
            iid = str(it.get("id"))
            if iid and iid != "None" and iid not in into:
                n += 1
            into[iid] = _meow_record(it)
        return n

    if need_full:
        res.full = True
        fresh: dict = {}
        page = 1
        while True:
            data = fetch(page, conditional=False)
            batch = (data or {}).get("items") or []
            if not batch or not add(batch, fresh):
                break
            if progress:
                progress(len(fresh))
            if not data.get("hasMore"):
                break
            page += 1
            _page_guard(page, data)
            time.sleep(1.0)
        if len(fresh) < 100:
            raise RuntimeError(f"only got {len(fresh)} items from meowdb; the API may have changed")
        res.items_new = len(set(fresh) - set(recs))
        recs = fresh
        st["last_full"] = datetime.now().isoformat(timespec="seconds")
    else:
        known = sorted(int(k) for k in recs)
        known_max = known[-1]
        first_page = (len(known) - 1) // PAGE + 1   # the page holding our last item
        page = first_page
        while True:
            data = fetch(page, conditional=True)
            if data is None:
                break                               # 304: nothing new
            batch = data.get("items") or []
            ids = sorted(int(it["id"]) for it in batch if str(it.get("id", "")).isdigit())
            if page == first_page and ids and ids[0] != known[(page - 1) * PAGE]:
                # items before this page were removed or added in the middle, so the
                # pages no longer line up with our copy: refresh fully next time
                st["last_full"] = None
                res.message = "The whole item list will be downloaded at the next update."
            res.items_new += add([it for it in batch if int(it.get("id") or 0) > known_max], recs)
            if progress:
                progress(len(recs))
            if not data.get("hasMore"):
                break
            page += 1
            _page_guard(page, data)
            time.sleep(1.0)
    st["last_check"] = datetime.now().isoformat(timespec="seconds")
    res.items_total = len(recs)
    if res.full or res.items_new or not ITEM_TYPES_PATH.exists():
        MEOWDB_ITEMS_PATH.write_text(json.dumps(recs), encoding="utf-8")
        _write_item_files(recs)
    _sync_maps(sync, res, res.full)     # the map list keeps its weekly limit except on full refreshes
    _sync_npc_shops(sync, res, True)    # NPC prices are part of the item data: fetched with it
    MEOWDB_SYNC_PATH.write_text(json.dumps(sync, indent=1), encoding="utf-8")
    if not res.message:
        what = "full download" if res.full else ("new items" if res.items_new else "no new items")
        res.message = (f"Item list: {res.items_total:,} items ({what}"
                       + (f", +{res.items_new}" if res.items_new and not res.full else "")
                       + f"); {res.requests} request{'s' if res.requests != 1 else ''} to meowdb.")
    return res


def _fmt_ago(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    return f"{mins} min" if mins < 120 else f"{mins // 60} h" if mins < 2880 else f"{mins // 1440} days"


def _write_item_files(recs: dict) -> None:
    """Derived lookup files (names for spelling, ids for links, icons, categories)."""
    names, ids, icons, types = set(), {}, {}, {}
    for iid, r in sorted(recs.items(), key=lambda kv: int(kv[0])):
        for key in ("name", "displayName"):
            nm = (r.get(key) or "").strip()
            if nm:
                names.add(nm)
                ids.setdefault(nm, int(iid))
                if r.get("iconKey"):
                    icons.setdefault(nm, str(r["iconKey"]))
                if r.get("subtype"):
                    types.setdefault(nm, str(r["subtype"]))
    ITEMS_PATH.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    ITEM_IDS_PATH.write_text(json.dumps(ids), encoding="utf-8")
    ITEM_ICONS_PATH.write_text(json.dumps(icons), encoding="utf-8")
    ITEM_TYPES_PATH.write_text(json.dumps(types), encoding="utf-8")
    (CACHE_DIR / "items_source.txt").write_text(
        f"NiaMeowDB (meowdb.com), updated {datetime.now():%Y-%m-%d}\n", encoding="utf-8")
    _reset_name_caches()
    fix_throwing_star_rows(db())


def _sync_maps(sync: dict, res: SyncResult, force: bool) -> None:
    st = sync.setdefault("maps", {})
    last = _ago(st.get("last_check"))
    if not force and MAPS_PATH.exists() and last is not None and last < MAP_CHECK_EVERY:
        return
    try:
        if res.requests:
            time.sleep(1.0)
        status, body, et, lm = _get_cond(MEOWDB_MAPS_URL, *(st.get("etag") or [None, None])
                                         if MAPS_PATH.exists() else (None, None))
        res.requests += 1
        st["last_check"] = datetime.now().isoformat(timespec="seconds")
        if status == 200:
            maps = parse_meowdb_maps(body.decode("utf-8", "replace"))
            if len(maps) < 50:
                raise RuntimeError(f"only found {len(maps)} maps; the page may have changed")
            old = _load_json(MAPS_PATH, [])
            if maps != old:
                MAPS_PATH.write_text(json.dumps(maps), encoding="utf-8")
                _reset_name_caches()
                fix_map_rows(db())
                res.maps_updated = True
                st.pop("source", None)       # no longer an imported list
            st["etag"] = [et, lm]
    except Exception as e:  # the item list is still useful on its own
        res.message = (res.message + " " if res.message else "") + f"(map list not updated: {e})"
    MEOWDB_SYNC_PATH.write_text(json.dumps(sync, indent=1), encoding="utf-8")


def _item_types() -> dict:
    if _item_types.cache is None:
        try:
            types = json.loads(ITEM_TYPES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            types = {}
        for name, e in osms_extras()["items"].items():
            if e["subtype"]:
                types.setdefault(name, e["subtype"])
        _item_types.cache = types
    return _item_types.cache


_item_types.cache = None


def item_subtype(name: str) -> str:
    return _item_types().get(name, "")


def is_throwing_star(name: str) -> bool:
    """Category from meowdb, not the name: Icicle and Wooden Top are stars too."""
    return item_subtype(name) == "Throwing Star"


def needs_type_refresh() -> bool:
    """A meowdb list from an older version (no categories / no master copy yet)."""
    if not ITEM_ICONS_PATH.exists():
        return False
    if not ITEM_TYPES_PATH.exists() or not MEOWDB_ITEMS_PATH.exists():
        return True
    recs = _load_json(MEOWDB_ITEMS_PATH, {})
    return bool(recs) and any("type" not in r for r in recs.values())   # no details for the filters yet


def fix_throwing_star_rows(con) -> int:
    """Throwing stars are priced per set, whatever the stack size: quantity 1."""
    stars = [n for n, t in _item_types().items() if t == "Throwing Star"]
    n = 0
    for i in range(0, len(stars), 500):
        chunk = stars[i:i + 500]
        n += con.execute(f"UPDATE listings SET qty = 1 WHERE qty != 1 AND item IN "
                         f"({','.join('?' * len(chunk))})", chunk).rowcount
    con.commit()
    return n


MAPS_PATH = CACHE_DIR / "maps.json"          # [{id, name, street}] from meowdb
MEOWDB_MAPS_URL = "https://meowdb.com/msclassic/maps/all"
MEOWDB_NPC_SHOPS_URL = "https://meowdb.com/msclassic/npc-shops"
NPC_SHOPS_PATH = CACHE_DIR / "npc_shops.json"   # {meowdb item id: cheapest NPC shop price}


def parse_meowdb_npc_shops(html_text: str) -> dict[str, int]:
    """Item prices from meowdb's NPC Shop Database page: each shop row links the item
    (/msclassic/item-db/<id>) and shows its price ("50mesos", "1,000 mesos"). The same
    item in several shops keeps its cheapest price."""
    import html as htmlmod

    link = re.compile(r"<a\b[^>]*href=[\"'](?:https?://(?:www\.)?meowdb\.com)?/msclassic/(?:[a-z]{2}/)?"
                      r"item-db/(\d+)[\"'][^>]*>", re.I)
    links = list(link.finditer(html_text))
    prices: dict[str, int] = {}
    for i, m in enumerate(links):
        end = links[i + 1].start() if i + 1 < len(links) else len(html_text)
        seg = htmlmod.unescape(re.sub(r"<[^>]+>", " ", html_text[m.end(): min(end, m.end() + 3000)]))
        p = re.search(r"(\d[\d,]*)\s*mesos?\b", seg, re.I)
        if p:
            price = int(p.group(1).replace(",", ""))
            prices[m.group(1)] = min(prices.get(m.group(1), price), price)
    return prices


def _sync_npc_shops(sync: dict, res, force: bool) -> None:
    """NPC shop prices: downloaded together with the item list, every time it's
    checked (a conditional request, so an unchanged page costs almost nothing).
    Without force, only if they've never been downloaded."""
    st = sync.setdefault("npc_shops", {})
    if not force and NPC_SHOPS_PATH.exists():
        return
    try:
        if res.requests:
            time.sleep(1.0)
        status, body, et, lm = _get_cond(MEOWDB_NPC_SHOPS_URL, *(st.get("etag") or [None, None])
                                         if NPC_SHOPS_PATH.exists() else (None, None))
        res.requests += 1
        st["last_check"] = datetime.now().isoformat(timespec="seconds")
        if status == 200:
            prices = parse_meowdb_npc_shops(body.decode("utf-8", "replace"))
            if len(prices) < 50:
                raise RuntimeError(f"only found {len(prices)} NPC shop prices; the page may have changed")
            NPC_SHOPS_PATH.write_text(json.dumps(prices), encoding="utf-8")
            item_info.cache = None
            st["etag"] = [et, lm]
    except Exception as e:      # the item list is still useful on its own
        res.message = (res.message + " " if res.message else "") + f"(NPC shop prices not updated: {e})"
    MEOWDB_SYNC_PATH.write_text(json.dumps(sync, indent=1), encoding="utf-8")


def clean_street(text: str) -> str:
    """An area name from meowdb's map index without the map count its heading shows:
    "Kerning City Subway (8)" or "Kerning City Subway ( 8 )" (the number is in its own
    tag) -> "Kerning City Subway"."""
    return re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", " ".join((text or "").split()))


def parse_meowdb_maps(html_text: str) -> list[dict]:
    """Map links on meowdb's A-Z map index: <a href="/msclassic/maps/080002005">Free
    Market &lt;5&gt; (town)</a>, grouped under street headings (### Hidden Street (23))."""
    import html as htmlmod

    maps, seen = [], set()
    street = ""
    token = re.compile(
        r"<h[2-4][^>]*>(?P<head>.*?)</h[2-4]>"
        r"|<a\b[^>]*href=[\"'](?:https?://(?:www\.)?meowdb\.com)?/msclassic/(?:[a-z]{2}/)?maps/"
        r"(?P<id>\d{9})[\"'][^>]*>(?P<text>.*?)</a>", re.S | re.I)
    for m in token.finditer(html_text):
        if m.group("head") is not None:
            head = htmlmod.unescape(re.sub(r"<[^>]+>", " ", m.group("head")))
            street = clean_street(head)
            continue
        name = htmlmod.unescape(re.sub(r"<[^>]+>", " ", m.group("text")))
        name = " ".join(name.split())
        name = re.sub(r"\s*\((?:town|field|dungeon|spot)\)\s*$", "", name, flags=re.I)
        if name and m.group("id") not in seen:
            seen.add(m.group("id"))
            maps.append({"id": m.group("id"), "name": name, "street": street})
    return maps


def update_maps_meowdb(force: bool = True) -> int:
    """Fetch meowdb's map list now (conditional request; skipped if unchanged)."""
    sync = _load_json(MEOWDB_SYNC_PATH, {})
    res = SyncResult()
    _sync_maps(sync, res, force)
    if "not updated" in res.message:
        raise RuntimeError(res.message)
    return len(_map_names())


def full_map_name(street: str, name: str) -> str:
    """The full map name, as the minimap shows it on two lines: the area (street)
    on top and the zone below, e.g. "Kerning City Subway: Line 1 <Area 1>"."""
    street, name = (street or "").strip(), (name or "").strip()
    return f"{street}: {name}" if street and name else name or street


def _split_map(full: str) -> tuple[str, str]:
    street, sep, zone = (full or "").partition(": ")
    return (street, zone) if sep else ("", full or "")


def _map_names() -> list[str]:
    """Every known map's full name ("Area: Zone"), from meowdb plus the backup source."""
    if _map_names.cache is None:
        try:
            names = {full_map_name(clean_street(m.get("street", "")), m["name"])
                     for m in json.loads(MAPS_PATH.read_text(encoding="utf-8")) if m.get("name")}
        except (OSError, ValueError, KeyError, TypeError):
            names = set()
        _map_names.cache = sorted(names | set(osms_extras()["maps"]))
    return _map_names.cache


_map_names.cache = None


def needs_map_refresh() -> bool:
    return ITEM_ICONS_PATH.exists() and not MAPS_PATH.exists()


def _map_key(text: str) -> str:
    return _norm(text.replace("<", " <").replace(">", "> "))


def _map_score(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, fold(a.replace(" ", "")), fold(b.replace(" ", ""))).ratio()


def snap_map(name: str) -> str:
    """Correct an OCR'd map name to a real one, as "Area: Zone"
    ('Kerning City Subvvay: Line 1 <Area 1>' -> 'Kerning City Subway: Line 1 <Area 1>').
    Numbers must match exactly, so a room or area number is never changed. If the
    area line was misread (or missing, as in names saved by older versions), the
    zone alone decides when only one map has that zone."""
    names = _map_names()
    if not name or not names:
        return name
    street, zone = _split_map(name)
    key, zkey = _map_key(name), _map_key(zone)
    best, best_score = None, 0.0
    for n in names:
        nk = _map_key(n)
        if nk == key:
            return n
        if street and re.findall(r"\d+", nk) == re.findall(r"\d+", key):
            sc = _map_score(key, nk)
            if sc > best_score:
                best, best_score = n, sc
    if best_score >= 0.85:
        return best
    # the zone line on its own (the area line can be cut off or garbled)
    zbest, zscore = [], 0.0
    for n in names:
        nz = _map_key(_split_map(n)[1])
        if re.findall(r"\d+", nz) != re.findall(r"\d+", zkey):
            continue
        sc = 1.0 if nz == zkey else _map_score(zkey, nz)
        if sc > zscore + 1e-9:
            zbest, zscore = [n], sc
        elif abs(sc - zscore) <= 1e-9:
            zbest.append(n)
    if zscore >= 0.85 and zbest:
        if len(zbest) == 1:
            return zbest[0]
        if street:          # several areas have this zone: the area line picks one
            pick = max(zbest, key=lambda n: _map_score(_map_key(street), _map_key(_split_map(n)[0])))
            if _map_score(_map_key(street), _map_key(_split_map(pick)[0])) >= 0.6:
                return pick
    if best_score >= 0.8:
        return best
    return name


def fix_map_rows(con) -> int:
    """Re-snap map names already logged: fixes earlier misreads like 'Free Marker <3>',
    and gives names saved by versions before 2610.8 (zone only) their area:
    'Free Market <3>' -> 'Hidden Street: Free Market <3>'."""
    n = 0
    for (mp,) in con.execute("SELECT DISTINCT map FROM listings WHERE map IS NOT NULL AND map != ''").fetchall():
        fixed = snap_map(mp)
        if fixed != mp:
            n += con.execute("UPDATE listings SET map = ? WHERE map = ?", (fixed, mp)).rowcount
    con.commit()
    return n


def items_source() -> str:
    p = CACHE_DIR / "items_source.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else (
        "imported list" if ITEMS_PATH.exists() else "")


# ---- backup source: OSMS Data Explorer (osmsdataexplorer.com, by @ohmi) ----------
# Game data mined from MapleStory Classic World, published as static JSON. Used only
# where NiaMeowDB has nothing: items and maps missing from meowdb's lists (matched by
# the game's own ids, which meowdb uses as icon keys and map ids), categories meowdb
# doesn't give, and icons meowdb can't supply.
OSMS_BASE = "https://osmsdataexplorer.com/data/current"
OSMS_ITEMS_PATH = CACHE_DIR / "osms_items.json"
OSMS_MAPS_PATH = CACHE_DIR / "osms_maps.json"
OSMS_CHECK_EVERY = timedelta(days=7)


def _osms_items_raw() -> list:
    data = _load_json(OSMS_ITEMS_PATH, {})
    items = data.get("items") if isinstance(data, dict) else data
    return [i for i in (items or []) if isinstance(i, dict) and i.get("name") and i.get("id") is not None]


def _osms_maps_raw() -> list:
    data = _load_json(OSMS_MAPS_PATH, {})
    regions = data.get("regions") if isinstance(data, dict) else data
    out = []
    for r in regions or []:
        if isinstance(r, dict) and isinstance(r.get("maps"), list):
            out += [m for m in r["maps"] if isinstance(m, dict) and m.get("name")]
        elif isinstance(r, dict) and r.get("name"):
            out.append(r)
    return out


_OSMS_TYPES = {"equipment": "Equip", "equip": "Equip", "use": "Use", "etc": "Etc", "setup": "Setup",
               "cash": "Cash", "cash shop": "Cash"}


def _osms_subtype(it: dict) -> str:
    text = " ".join(str(it.get(k) or "") for k in ("sub_category", "weapon_type", "category"))
    if re.search(r"throwing\s*star|\bstars?\b", text, re.I):
        return "Throwing Star"
    return str(it.get("sub_category") or "")


def osms_extras() -> dict:
    """What the backup source adds: {"items": {name: {"id", "subtype"}}, "maps": [names]}.
    Only items whose game id isn't in the meowdb list (and whose name isn't either),
    and maps whose id/name meowdb doesn't have."""
    if osms_extras.cache is not None:
        return osms_extras.cache
    meow_recs = _load_json(MEOWDB_ITEMS_PATH, {})
    meow_ids = {str(r.get("iconKey")).lstrip("0") for r in meow_recs.values() if r.get("iconKey")}
    meow_names = set()
    if ITEMS_PATH.exists():
        meow_names = {_norm(n) for n in ITEMS_PATH.read_text(encoding="utf-8").splitlines() if n.strip()}
    items = {}
    for it in _osms_items_raw():
        gid, name = str(it["id"]).lstrip("0"), str(it["name"]).strip()
        if gid in meow_ids or _norm(name) in meow_names or name in items:
            continue
        st = it.get("stats") if isinstance(it.get("stats"), dict) else {}
        lvl = st.get("reqLevel", it.get("req_level"))
        job = str(it.get("req_job_label") or "").strip()
        items[name] = {"id": gid, "subtype": _osms_subtype(it),
                       "type": _OSMS_TYPES.get(str(it.get("category") or "").lower(), it.get("category")),
                       "levelReq": int(lvl) if str("" if lvl is None else lvl).isdigit() else None,
                       "reqJob": None if job.lower() in ("", "all", "any", "none") else job,
                       "sources": [],
                       "npcSell": int(it["price"]) if str(it.get("price") or "").isdigit() else None}
    meow_maps = _load_json(MAPS_PATH, [])
    meow_map_ids = {str(m.get("id") or "").lstrip("0") for m in meow_maps}
    meow_map_names = {_norm(m.get("name", "")) for m in meow_maps}
    maps, seen_maps = [], set()
    for m in _osms_maps_raw():
        mid, name = str(m.get("id") or "").lstrip("0"), str(m["name"]).strip()
        street = clean_street(str(m.get("street_name") or m.get("street") or ""))
        full = full_map_name(street, name)
        if (mid and mid in meow_map_ids) or _norm(name) in meow_map_names or full in seen_maps:
            continue
        seen_maps.add(full)
        maps.append(full)
    osms_extras.cache = {"items": items, "maps": maps}
    return osms_extras.cache


osms_extras.cache = None


def item_info(name: str) -> dict | None:
    """Category, subtype, required level and job, and sources of an item, from the
    meowdb list (or the backup source for items meowdb doesn't have)."""
    if item_info.cache is None:
        idx = {}
        for name_, e in osms_extras()["items"].items():
            idx[name_] = {k: e.get(k) for k in ("type", "subtype", "levelReq", "reqJob", "sources", "npcSell")}
        shop = _load_json(NPC_SHOPS_PATH, {})
        for iid, r in _load_json(MEOWDB_ITEMS_PATH, {}).items():
            info = {k: r.get(k) for k in ("type", "subtype", "levelReq", "reqJob", "sources", "npcSell")}
            info["npcBuy"] = shop.get(str(iid))
            for n in (r.get("name"), r.get("displayName")):
                if n:
                    idx[n] = info
        item_info.cache = idx
    return item_info.cache.get(name)


item_info.cache = None


def item_filter_options(items: list[str]) -> dict:
    """Values to offer in the Items tab's filters, from the items you have:
    {"types": [...], "subtypes": {type: [...]}, "jobs": [...]}."""
    types, subs, jobs = set(), {}, set()
    for it in items:
        i = item_info(it)
        if not i:
            continue
        if i.get("type"):
            types.add(i["type"])
            if i.get("subtype"):
                subs.setdefault(i["type"], set()).add(i["subtype"])
        if i.get("reqJob"):
            jobs.add(i["reqJob"])
    order = {"Equip": 0, "Use": 1, "Etc": 2, "Setup": 3, "Cash": 4}
    return {"types": sorted(types, key=lambda t: (order.get(t, 9), t)),
            "subtypes": {t: sorted(v) for t, v in subs.items()}, "jobs": sorted(jobs)}


def item_matches(item: str, f: dict) -> bool:
    """Does the item pass the Items tab's filter? f: name, type, subtype, job,
    all_class (bool), level_min, level_max, npc (bool). Items the item lists don't
    know only pass filters on their name."""
    if f.get("name") and f["name"].strip().lower() not in item.lower():
        return False
    wants = any(f.get(k) for k in ("type", "subtype", "job", "npc")) or \
        f.get("level_min") is not None or f.get("level_max") is not None
    if not wants:
        return True
    i = item_info(item)
    if not i:
        return False
    if f.get("type") and i.get("type") != f["type"]:
        return False
    if f.get("subtype") and i.get("subtype") != f["subtype"]:
        return False
    if f.get("job"):            # a class only applies to equipment, as on meowdb
        job = i.get("reqJob")
        if i.get("type") != "Equip" or (job != f["job"] and not (f.get("all_class", True) and not job)):
            return False
    lvl = i.get("levelReq") or 0
    if f.get("level_min") is not None and lvl < f["level_min"]:
        return False
    if f.get("level_max") is not None and lvl > f["level_max"]:
        return False
    if f.get("npc") and "npc" not in (i.get("sources") or []) and not i.get("npcBuy"):
        return False
    return True


def _reset_name_caches() -> None:
    item_info.cache = None
    osms_extras.cache = None
    _icon_keys.cache = None
    _item_types.cache = None
    _map_names.cache = None
    _Dictionary.cache = None
    _TitleWords.cache = None


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def osms_info() -> dict:
    sync = _load_json(MEOWDB_SYNC_PATH, {}).get("osms", {})
    ex = osms_extras()
    return {"items": len(_osms_items_raw()), "maps": len(_osms_maps_raw()),
            "extra_items": len(ex["items"]), "extra_maps": len(ex["maps"]),
            "checked": (sync.get("last_check") or "")[:10]}


def update_osms(force: bool = False) -> str:
    """Download the backup source's item and map lists (at most weekly unless
    forced; conditional requests, so unchanged files cost almost nothing)."""
    sync = _load_json(MEOWDB_SYNC_PATH, {})
    st = sync.setdefault("osms", {})
    last = _ago(st.get("last_check"))
    have = OSMS_ITEMS_PATH.exists() and OSMS_MAPS_PATH.exists()
    if have and not force and last is not None and last < OSMS_CHECK_EVERY:
        return f"Backup source checked {_fmt_ago(last)} ago; no request made."
    changed = []
    for name, path, check in (
            ("items", OSMS_ITEMS_PATH, lambda d: len(d.get("items") or []) >= 100),
            ("maps", OSMS_MAPS_PATH, lambda d: len(d.get("regions") or []) >= 1)):
        url = f"{OSMS_BASE}/{name}.json"
        tag = st.get("etags", {}).get(url, [None, None]) if path.exists() else [None, None]
        status, body, et, lm = _get_cond(url, *tag)
        if status == 200:
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict) or not check(data):
                raise RuntimeError(f"the backup source's {name}.json looks different than expected")
            path.write_text(json.dumps(data), encoding="utf-8")
            st.setdefault("etags", {})[url] = [et, lm]
            changed.append(name)
        time.sleep(1.0)
    st["last_check"] = datetime.now().isoformat(timespec="seconds")
    MEOWDB_SYNC_PATH.write_text(json.dumps(sync, indent=1), encoding="utf-8")
    _reset_name_caches()
    if changed:
        con = db()
        fix_throwing_star_rows(con)
        fix_map_rows(con)
    i = osms_info()
    return (f"Backup source (OSMS Data Explorer): {plural(i['items'], 'item')}, {plural(i['maps'], 'map')}"
            + (f"; adds {plural(i['extra_items'], 'item')} and {plural(i['extra_maps'], 'map')} "
               "meowdb doesn't have" if i["extra_items"] or i["extra_maps"] else "")
            + ("" if changed else " (unchanged)") + ".")


def _icon_keys() -> dict:
    """item name -> icon key (the game's item id): meowdb's, plus items only the
    backup source has."""
    if _icon_keys.cache is None:
        keys = _load_json(ITEM_ICONS_PATH, {})
        for name, e in osms_extras()["items"].items():
            keys.setdefault(name, e["id"])
        _icon_keys.cache = keys
    return _icon_keys.cache


_icon_keys.cache = None


def _get_once(url: str, timeout: int = 15) -> bytes:
    """One plain GET (no retries: the caller's rate limiter decides what to do)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read()


def _download_icon(key: str) -> bytes:
    """An icon by key (the game's item id): from meowdb, or from the backup source
    if meowdb doesn't have it."""
    import io
    import urllib.error

    try:
        raw = _get_once(f"{MEOWDB_API}/assets/icons/{key}")
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410) or not str(key).isdigit():
            raise
        raw = _get_once(f"{OSMS_BASE}/images/items/{int(key):08d}.png")   # 404 here too: really missing
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class RateLimiter:
    """At most `rate` requests per second, shared by several download threads, and
    slowing down by itself when the server asks for it:
    - "429 Too Many Requests" / "503": half the speed, and a pause as long as the
      server's Retry-After asks (10 s if it doesn't say);
    - slow answers (over 3 s): a quarter slower;
    - after 50 good answers in a row it speeds up again by a quarter, never above `rate`.
    Never slower than one request per 2 s."""
    FLOOR = 0.5

    def __init__(self, rate: float = 10.0):
        import threading

        self.max_rate = self.rate = max(float(rate), self.FLOOR)
        self.next = self.pause_until = self.calm_until = 0.0
        self.good = self.slowdowns = 0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next, self.pause_until)
            self.next = t + 1.0 / self.rate
        if t > now:
            time.sleep(t - now)

    def _slow(self, factor: float) -> None:
        if factor > 0.5 and time.monotonic() < self.calm_until:
            return                    # answers already in flight before the last slow-down
        self.calm_until = time.monotonic() + 1.0
        self.rate = max(self.FLOOR, self.rate * factor)
        self.good = 0
        self.slowdowns += 1

    def answered(self, seconds: float) -> None:
        with self.lock:
            if seconds > 3:
                self._slow(0.75)
                return
            self.good += 1
            if self.rate < self.max_rate and self.good >= 50:
                self.rate = min(self.max_rate, self.rate * 1.25)
                self.good = 0

    def throttled(self, retry_after=None) -> None:
        with self.lock:
            if time.monotonic() >= self.pause_until:
                self._slow(0.5)       # one halving per burst, not one per thread that saw it
            try:
                pause = min(max(float(retry_after), 1.0), 300.0)
            except (TypeError, ValueError):
                pause = 10.0
            self.pause_until = max(self.pause_until, time.monotonic() + pause)


def _store_icon(con, key: str, png: bytes) -> None:
    con.execute("INSERT OR REPLACE INTO cache.meowdb_icons (icon_key, png, fetched) VALUES (?,?,?)",
                (str(key), png, utc_now()))


def fetch_official_icons(con, items: list[str], delay: float = 0.5) -> int:
    """Download meowdb's official icons for the given items if they aren't here yet
    (screen-captured thumbnails are then no longer used), at most ~2 requests/s."""
    keys = _icon_keys()
    if not keys:
        return 0
    have = {r[0] for r in con.execute("SELECT icon_key FROM cache.meowdb_icons")}
    done = 0
    for item in dict.fromkeys(items):
        key = keys.get(item)
        if not key or key in have:
            continue
        try:
            _store_icon(con, key, _download_icon(key))
        except Exception:
            continue
        con.commit()
        have.add(key)
        done += 1
        time.sleep(delay)
    return done


def adopt_old_icons(con) -> None:
    """Versions before 2610.4 kept official icons per item name in the screen-icon
    table; move them to the per-icon-key store."""
    keys = _icon_keys()
    rows = con.execute("SELECT item, png FROM icons WHERE source = 'meowdb'").fetchall()
    for item, png in rows:
        if keys.get(item):
            con.execute("INSERT OR IGNORE INTO cache.meowdb_icons (icon_key, png, fetched) VALUES (?,?,?)",
                        (keys[item], png, utc_now()))
    if rows and keys:
        con.execute("DELETE FROM icons WHERE source = 'meowdb'")
        con.commit()


def icon_status(con) -> tuple[int, int]:
    """(official icons downloaded, icons in the item list)."""
    adopt_old_icons(con)
    want = set(_icon_keys().values())
    if not want:
        return 0, 0
    have = {r[0] for r in con.execute("SELECT icon_key FROM cache.meowdb_icons")}
    return len(want & have), len(want)


def download_all_icons(con, progress=None, stop=None, rate: float = 10.0,
                       workers: int = 4) -> tuple[int, int]:
    """Download every meowdb icon not here yet: up to `rate` per second (a few at a
    time, since one request alone takes longer than 1/rate), slowing down
    automatically if meowdb asks or gets slow (see RateLimiter). Already downloaded
    icons are never fetched again (an item whose picture changes gets a new icon
    key), so after the first run this only fetches icons of new items. Can be
    stopped at any time and picks up where it left off.
    progress(done_so_far, total, current_rate). Returns (downloaded, failed)."""
    import threading
    import urllib.error
    from concurrent.futures import ThreadPoolExecutor, as_completed

    adopt_old_icons(con)
    have = {r[0] for r in con.execute("SELECT icon_key FROM cache.meowdb_icons")}
    have |= {r[0] for r in con.execute("SELECT icon_key FROM cache.meowdb_icons_missing WHERE checked > ?",
                                        (utc_ago(days=30),))}
    todo = sorted(set(_icon_keys().values()) - have, key=lambda k: (len(k), k))
    if not todo:
        return 0, 0
    lim = RateLimiter(rate)
    halt = threading.Event()

    def stopped():
        return halt.is_set() or (stop is not None and stop.is_set())

    def job(key):
        for _ in range(5):
            if stopped():
                return key, None, None
            lim.wait()
            t0 = time.monotonic()
            try:
                png = _download_icon(key)
                lim.answered(time.monotonic() - t0)
                return key, png, None
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):            # meowdb has no picture for it
                    lim.answered(time.monotonic() - t0)
                    return key, None, "missing"
                if e.code in (429, 503):            # too fast: slow down and try again
                    lim.throttled(e.headers.get("Retry-After") if e.headers else None)
                    continue
                return key, None, e
            except Exception as e:                  # timeout, network error, bad image
                lim.answered(99)                    # counts as a slow answer
                return key, None, e
        return key, None, RuntimeError("meowdb kept asking to slow down")

    done = failed = streak = finished = 0
    last_error = None
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(job, k) for k in todo]
        for fut in as_completed(futures):
            key, png, err = fut.result()
            if png is None and err is None:         # skipped because we're stopping
                continue
            finished += 1
            if png is not None:
                _store_icon(con, key, png)
                done += 1
                streak = 0
            elif err == "missing":
                con.execute("INSERT OR REPLACE INTO cache.meowdb_icons_missing VALUES (?,?)", (key, utc_now()))
                failed += 1
                streak = 0
            else:
                failed += 1
                streak += 1
                last_error = err
            if streak >= 10:      # meowdb or the network is down: try again next update
                halt.set()
                con.commit()
                raise RuntimeError(f"icon downloads keep failing ({last_error}); "
                                   f"{done:,} downloaded, the rest will be fetched next time")
            if finished % 50 == 0:
                con.commit()
            if progress:
                progress(finished, len(todo), lim.rate)
    finally:
        halt.set()
        pool.shutdown(wait=True, cancel_futures=True)
        con.commit()
    return done, failed


ITEMS_FORMAT = "mfm-tracker item list"
MAPS_FORMAT = "mfm-tracker map list"


def _rows_from_file(p: Path) -> list:
    """Rows of a .json (list, or {"items"/"maps"/"data": [...]}), .csv (header row)
    or .txt (one value per line) file: dicts with lower-case keys, or strings."""
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = (data.get("items") or data.get("maps") or data.get("data")
                    or list(data.values()))
        return [({str(k).lower(): v for k, v in d.items()} if isinstance(d, dict) else d)
                for d in data if isinstance(d, (dict, str))]
    if p.suffix.lower() == ".csv":
        return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                for row in csv.DictReader(text.splitlines())]
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def names_only_items() -> bool:
    """The list in use was imported from a names-only file (the meowdb copy, if any,
    is kept for "Update from meowdb" but isn't what's in use)."""
    return "(names only)" in items_source()


def items_info() -> dict:
    """What item list is in use, for Settings."""
    recs = {} if names_only_items() else _load_json(MEOWDB_ITEMS_PATH, {})
    names = ITEMS_PATH.read_text(encoding="utf-8").split("\n") if ITEMS_PATH.exists() else []
    return {"names": sum(1 for n in names if n.strip()), "items": len(recs),
            "source": items_source(), "has_details": bool(recs) or ITEM_TYPES_PATH.exists()}


def import_items(path: str) -> int:
    """Use an item list file as the name dictionary.

    Best is a file made by Export (or any JSON/CSV with "id" and "name" per item,
    optionally "subtype"/"type" and "iconKey"/"icon_key"): it carries meowdb ids,
    categories and icon keys too, and becomes the local copy that "Update from
    meowdb" builds on, so another PC needn't download everything again.
    A .txt (one name per line), or a .json/.csv with names only, sets just the
    names used for spelling."""
    p = Path(path)
    if p.suffix.lower() == ".zip":
        import zipfile

        with zipfile.ZipFile(p) as z:
            if "items.json" not in z.namelist():
                raise ValueError("that .zip has no items.json (was it made by Export?)")
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "items.json").write_bytes(z.read("items.json"))
                n = import_items(str(Path(td) / "items.json"))
            _import_icons_zip(z)
            for f in (OSMS_ITEMS_PATH, OSMS_MAPS_PATH):
                if f"backup/{f.name}" in z.namelist():
                    f.write_bytes(z.read(f"backup/{f.name}"))
            _reset_name_caches()
        (CACHE_DIR / "items_source.txt").write_text(
            items_source().replace("items.json", p.name) + "\n", encoding="utf-8")
        return n
    rows = _rows_from_file(p)
    full = rows and all(isinstance(r, dict) and r.get("name") and str(r.get("id") or "").strip().isdigit()
                        for r in rows)
    if full:
        recs = {}
        for r in rows:
            lvl = r.get("levelreq", r.get("level"))
            lvl = "" if lvl is None else str(lvl).strip()
            src = r.get("sources") or []
            if isinstance(src, str):
                src = [x for x in re.split(r"[|;,]\s*", src) if x]
            recs[str(int(str(r["id"]).strip()))] = {
                "name": str(r["name"]).strip(),
                "displayName": (str(r.get("displayname") or "").strip() or None),
                "iconKey": (str(r.get("iconkey") or r.get("icon_key") or "").strip() or None),
                "subtype": (str(r.get("subtype") or "").strip() or None),
                "type": (str(r.get("type") or "").strip() or None),
                "levelReq": int(lvl) if lvl.isdigit() else None,
                "reqJob": (str(r.get("reqjob") or "").strip() or None),
                "npcSell": int(str(r.get("npcsell")).strip()) if str(r.get("npcsell") or "").strip().isdigit() else None,
                "sources": [str(x) for x in src]}
        MEOWDB_ITEMS_PATH.write_text(json.dumps(recs), encoding="utf-8")
        shop = {str(int(str(r["id"]).strip())): int(str(r.get("npcbuy")).strip())
                for r in rows if str(r.get("npcbuy") or "").strip().isdigit()}
        if shop:
            NPC_SHOPS_PATH.write_text(json.dumps(shop), encoding="utf-8")
        _write_item_files(recs)
        # it's a complete list: the next meowdb update only asks for newer items
        sync = _load_json(MEOWDB_SYNC_PATH, {})
        st = sync.setdefault("items", {})
        st["last_full"] = st["last_check"] = datetime.now().isoformat(timespec="seconds")
        st.pop("etags", None)
        MEOWDB_SYNC_PATH.write_text(json.dumps(sync, indent=1), encoding="utf-8")
        (CACHE_DIR / "items_source.txt").write_text(
            f"imported from {p.name}, {datetime.now():%Y-%m-%d}\n", encoding="utf-8")
        return len({n for r in recs.values() for n in (r["name"], r["displayName"]) if n})
    names, ids = [], {}
    for r in rows:
        if isinstance(r, str):
            names.append(r)
        elif r.get("name"):
            names.append(str(r["name"]).strip())
            if str(r.get("id") or "").strip():
                ids[str(r["name"]).strip()] = r["id"]
    names = sorted(set(n.strip() for n in names if n.strip()))
    if not names:
        raise ValueError("no item names found in that file")
    ITEMS_PATH.write_text("\n".join(names) + "\n", encoding="utf-8")
    ITEM_IDS_PATH.write_text(json.dumps(ids), encoding="utf-8")
    (CACHE_DIR / "items_source.txt").write_text(
        f"imported from {p.name} (names only), {datetime.now():%Y-%m-%d}\n", encoding="utf-8")
    _reset_name_caches()
    return len(names)


def export_items(path: str) -> int:
    """Save the item list in use. .json and .csv keep everything (meowdb id, name,
    category, icon key) and can be imported on another PC; .txt is names only."""
    p = Path(path)
    recs = {} if names_only_items() else _load_json(MEOWDB_ITEMS_PATH, {})
    if recs:
        shop = _load_json(NPC_SHOPS_PATH, {})
        rows = [{"id": int(i), **{k: r.get(k) for k in REC_FIELDS}, "npcBuy": shop.get(i)}
                for i, r in sorted(recs.items(), key=lambda kv: int(kv[0]))]
    else:      # an imported names-only list
        if not ITEMS_PATH.exists():
            raise ValueError("there is no item list to export yet")
        ids = _load_json(ITEM_IDS_PATH, {})
        types, icons = _load_json(ITEM_TYPES_PATH, {}), _load_json(ITEM_ICONS_PATH, {})
        rows = [{"id": ids.get(n), "name": n, "displayName": None, "subtype": types.get(n),
                 "iconKey": icons.get(n), "type": None, "levelReq": None, "reqJob": None, "sources": [],
                 "npcSell": None, "npcBuy": None}
                for n in ITEMS_PATH.read_text(encoding="utf-8").splitlines() if n.strip()]
    ext = p.suffix.lower()
    if ext == ".zip":
        return _export_items_zip(p, rows)
    if ext == ".txt":
        names = sorted({n for r in rows for n in (r["name"], r["displayName"]) if n})
        p.write_text("\n".join(names) + "\n", encoding="utf-8")
        return len(names)
    if ext == ".csv":
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "displayName", "subtype", "iconKey", "type", "levelReq", "reqJob",
                        "sources", "npcSell", "npcBuy"])
            for r in rows:
                w.writerow([r["id"] if r["id"] is not None else "", r["name"] or "",
                            r["displayName"] or "", r["subtype"] or "", r["iconKey"] or "",
                            r.get("type") or "", "" if r.get("levelReq") is None else r["levelReq"],
                            r.get("reqJob") or "", "|".join(r.get("sources") or []),
                            "" if r.get("npcSell") is None else r["npcSell"],
                            "" if r.get("npcBuy") is None else r["npcBuy"]])
        return len(rows)
    p.write_text(_items_json(rows), encoding="utf-8")
    return len(rows)


def _items_json(rows) -> str:
    return json.dumps({"format": ITEMS_FORMAT, "source": items_source(),
                       "exported_utc": utc_now(), "items": rows}, indent=1)


def _export_items_zip(p: Path, rows) -> int:
    """items.json (the list, same as a .json export) plus every icon:
    icons/meowdb/<icon key>.png (official) and icons/captured/<n>.png (cut from
    the screen, for items meowdb doesn't have; names in icons/captured/index.json)."""
    import zipfile

    con = db()
    adopt_old_icons(con)
    tmp = p.with_name(p.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("items.json", _items_json(rows))
        for key, png in con.execute("SELECT icon_key, png FROM cache.meowdb_icons ORDER BY icon_key"):
            z.writestr(f"icons/meowdb/{key}.png", png)
        index = []
        for i, (item, png, faded, updated) in enumerate(con.execute(
                "SELECT item, png, faded, updated FROM icons ORDER BY item")):
            z.writestr(f"icons/captured/{i + 1}.png", png)
            index.append({"file": f"{i + 1}.png", "item": item, "faded": faded, "updated": updated})
        z.writestr("icons/captured/index.json", json.dumps(index, indent=1))
        for f in (OSMS_ITEMS_PATH, OSMS_MAPS_PATH):     # the backup source's lists
            if f.exists():
                z.write(f, f"backup/{f.name}")
    con.close()
    tmp.replace(p)
    return len(rows)


def _import_icons_zip(z) -> tuple[int, int]:
    """Icons from an item-list .zip. Official ones replace what's here; captured
    ones only fill in items that have none."""
    con = db()
    official = captured = 0
    for name in z.namelist():
        m = re.fullmatch(r"icons/meowdb/([^/]+)\.png", name)
        if m:
            con.execute("INSERT OR REPLACE INTO cache.meowdb_icons (icon_key, png, fetched) VALUES (?,?,?)",
                        (m.group(1), z.read(name), utc_now()))
            official += 1
    if "icons/captured/index.json" in z.namelist():
        for e in json.loads(z.read("icons/captured/index.json")):
            path = f"icons/captured/{e['file']}"
            if e.get("item") and path in z.namelist():
                captured += con.execute(
                    "INSERT OR IGNORE INTO icons (item, png, faded, updated, source) VALUES (?,?,?,?, 'screen')",
                    (e["item"], z.read(path), int(e.get("faded") or 0), e.get("updated") or utc_now())).rowcount
    con.commit()
    con.close()
    return official, captured


def maps_info() -> dict:
    maps = _load_json(MAPS_PATH, [])
    sync = _load_json(MEOWDB_SYNC_PATH, {}).get("maps", {})
    return {"maps": len(maps), "source": sync.get("source")
            or ("NiaMeowDB (meowdb.com)" + (f", checked {sync['last_check'][:10]}" if sync.get("last_check") else "")
                if maps else "")}


def import_maps(path: str) -> int:
    """Use a map list file for correcting misread map names. A file made by Export
    (.json/.csv with "name", optionally "street" and "id"), or a .txt with one map
    name per line."""
    p = Path(path)
    maps, seen = [], set()
    for r in _rows_from_file(p):
        if isinstance(r, str):
            r = {"name": r}
        name = str(r.get("name") or r.get("map") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        mid = str(r.get("id") or "").strip()
        if mid.isdigit() and len(mid) < 9:
            mid = mid.zfill(9)      # meowdb map ids are 9 digits; spreadsheets drop leading zeros
        maps.append({"id": mid or None, "name": name, "street": str(r.get("street") or "").strip()})
    if not maps:
        raise ValueError("no map names found in that file")
    MAPS_PATH.write_text(json.dumps(maps), encoding="utf-8")
    sync = _load_json(MEOWDB_SYNC_PATH, {})
    st = sync.setdefault("maps", {})
    st["last_check"] = datetime.now().isoformat(timespec="seconds")
    st["source"] = f"imported from {p.name}, {datetime.now():%Y-%m-%d}"
    st.pop("etag", None)
    MEOWDB_SYNC_PATH.write_text(json.dumps(sync, indent=1), encoding="utf-8")
    _reset_name_caches()
    fix_map_rows(db())
    return len(maps)


def export_maps(path: str) -> int:
    """Save the map list in use (.json / .csv with id, name, street; .txt names only)."""
    p = Path(path)
    maps = [{**m, "street": clean_street(m.get("street") or "")} for m in _load_json(MAPS_PATH, [])]
    if not maps:
        raise ValueError("there is no map list to export yet")
    ext = p.suffix.lower()
    if ext == ".txt":
        p.write_text("\n".join(m["name"] for m in maps) + "\n", encoding="utf-8")
    elif ext == ".csv":
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "street"])
            for m in maps:
                w.writerow([m.get("id") or "", m.get("name") or "", clean_street(m.get("street") or "")])
    else:
        p.write_text(json.dumps({"format": MAPS_FORMAT, "source": maps_info()["source"],
                                 "exported_utc": utc_now(), "maps": maps}, indent=1), encoding="utf-8")
    return len(maps)


def meowdb_url(item: str) -> str:
    """Link to the item on NiaMeowDB: its own page when the imported list gave us
    an id, otherwise a site search for the name."""
    import urllib.parse

    ids = {}
    if ITEM_IDS_PATH.exists():
        try:
            ids = json.loads(ITEM_IDS_PATH.read_text(encoding="utf-8"))
        except ValueError:
            ids = {}
    if item in ids:
        return f"https://meowdb.com/msclassic/item-db/{ids[item]}"
    q = urllib.parse.quote_plus(f'site:meowdb.com/msclassic "{item.rstrip("…")}"')
    return f"https://duckduckgo.com/?q={q}"


class _Dictionary:
    """Official item names (items.txt) plus your reference prices, with a word
    index so fuzzy lookups only compare against plausible candidates."""
    cache = None

    def __init__(self, names: list[str]):
        self.names = names
        self.lower = {_norm(n): n for n in names}
        self.by_word: dict[str, list[str]] = {}
        for n in names:
            for wd in set(re.findall(r"[a-z']{4,}", n.lower())):
                self.by_word.setdefault(wd, []).append(n)
        self.memo: dict[str, str | None] = {}
        best_match.memo = {}
        # graded scrolls, for deciding a cut-off scroll's grade by its icon colour
        self.graded_scrolls = [n for n in names if re.search(r"\bScroll: (Chaos|Greater|Intermediate|Lesser)$", n)]
        self.scroll_base_fold = {n: fold(_norm(n.rsplit(":", 1)[0])) for n in self.graded_scrolls}
        # word vocabulary for fixing words in names that aren't in the list at all
        vocab = set(CLASSIC_WORDS)
        for n in names:
            vocab.update(re.findall(r"[A-Za-z][A-Za-z'-]{2,}", n))
        self.vocab = {v.lower(): v for v in sorted(vocab)}
        self.vocab_by_key: dict[tuple, list[str]] = {}
        for low in self.vocab:
            self.vocab_by_key.setdefault((fold(low)[0], len(low)), []).append(low)
        self.word_memo: dict[str, str | None] = {}

    @classmethod
    def get(cls, con=None) -> "_Dictionary":
        if cls.cache is None:
            names = []
            if ITEMS_PATH.exists():
                names = [ln.strip() for ln in ITEMS_PATH.read_text(encoding="utf-8").splitlines()
                         if ln.strip()]
            names += list(osms_extras()["items"])      # backup source: items meowdb lacks
            if con is not None:
                names += [r[0] for r in con.execute("SELECT item FROM reference")]
            cls.cache = cls(sorted(set(names)))
        return cls.cache

    def lookup(self, name: str) -> str | None:
        if name in self.memo:
            return self.memo[name]
        low = name.lower()
        hit = self.lower.get(_norm(name))
        if hit is None and low.endswith("…"):
            prefix = low[:-1]
            full = [n for n in self._candidates(prefix) if n.lower().startswith(prefix)]
            hit = full[0] if len(full) == 1 else None
        if hit is None:
            scored = []
            for cand in self._candidates(low):
                cl = cand.lower()
                if not _could_be_typo(low, cl):
                    continue
                scored.append((difflib.SequenceMatcher(None, fold(_norm(low)), fold(_norm(cl))).ratio(), cand))
            scored.sort(reverse=True)
            hit = None
            if scored and scored[0][0] >= 0.85:
                # two different items about equally close ("Toga"/"Topaz" for a smudged
                # read): don't guess; leave it unmatched (red !) for you to match
                rival = next((s for s, c in scored[1:] if _norm(c) != _norm(scored[0][1])), None)
                if rival is None or scored[0][0] == 1.0 > rival or scored[0][0] - rival >= 0.04:
                    hit = scored[0][1]
        if hit is None and not low.endswith("…"):
            hit = self._relaxed(low)
        self.memo[name] = hit
        return hit

    def _relaxed(self, low: str) -> str | None:
        """Second, more tolerant tier for heavier misreads (faded text: 'Rennae Hat' for
        'Reggae Hat'): same numbers and word count, every word at least 0.65 alike
        (folded), whole name at least 0.78 alike, and clearly the single best match.
        'Wolbi' vs 'Mokbi' (0.6) still never merges."""
        a = _norm(low)
        ta = a.split()
        fa = fold(a)
        pool = set(self._candidates(low))
        # names sharing no long word with the misread ("Rennae Hat"): shortlist by
        # same word count, similar length and overall look
        pool.update(n for n in self.names
                    if len(_norm(n).split()) == len(ta) and abs(len(n) - len(a)) <= 2
                    and difflib.SequenceMatcher(None, fa, fold(_norm(n))).quick_ratio() >= 0.7)
        scored = []
        for cand in pool:
            b = _norm(cand)
            tb = b.split()
            if len(ta) != len(tb) or re.findall(r"\d+", a) != re.findall(r"\d+", b):
                continue
            ok = True
            for x, y in zip(ta, tb):
                if (len(x) <= 2 or len(y) <= 2) and x != y:
                    ok = False
                    break
                if x != y and difflib.SequenceMatcher(None, fold(x), fold(y)).ratio() < 0.65:
                    ok = False
                    break
                if min(len(x), len(y)) <= 4 and len(x) != len(y):
                    ok = False
                    break
            if ok:
                scored.append((difflib.SequenceMatcher(None, fold(a), fold(b)).ratio(), cand))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 0.78 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.06):
            return scored[0][1]
        return None

    def fix_word(self, word: str) -> str | None:
        """Closest vocabulary word for one OCR'd word ('Scrall' -> 'Scroll')."""
        low = word.lower()
        if low in self.vocab:
            return self.vocab[low]
        for variant in (low[:-1] if low.endswith("s") else None, low + "s"):
            if variant and variant in self.vocab:
                return word   # a plural/singular of a known word, not a typo
        if low in self.word_memo:
            return self.word_memo[low]
        best, best_score = None, 0.0
        f = fold(low)
        for ln in range(len(low) - 2, len(low) + 3):
            for cand in self.vocab_by_key.get((f[0], ln), ()):
                sc = difflib.SequenceMatcher(None, f, fold(cand)).ratio()
                if sc > best_score:
                    best, best_score = cand, sc
        hit = None
        if best and best_score >= 0.83:
            # accept swapped letters ("Scrall"/"Scroll") but not a word that is just
            # a prefix of another ("Steel"/"Steely", truncated "Great"/"Greater")
            if not (best.startswith(low) or low.startswith(best)) and abs(len(best) - len(low)) <= 1:
                hit = self.vocab[best]
        self.word_memo[low] = hit
        return hit

    def fix_words(self, name: str) -> str:
        """Correct individual words and split merged ones ('PetEquip' -> 'Pet Equip')."""
        out = []
        for tok in name.split():
            m = re.match(r"^([^A-Za-z]*)([A-Za-z][A-Za-z'-]*)([^A-Za-z]*)$", tok)
            if not m:
                out.append(tok)
                continue
            lead, core, trail = m.groups()
            parts = re.split(r"(?<=[a-z])(?=[A-Z])", core)
            if len(parts) > 1 and all(self.fix_word(p) for p in parts):
                core = " ".join(self.fix_word(p) for p in parts)
            elif len(core) >= 3 and core.lower() in self.vocab:
                if core.islower():
                    core = self.vocab[core.lower()]   # "speed" -> "Speed"
            elif len(core) >= 4:
                core = self.fix_word(core) or core
            out.append(lead + core + trail)
        return " ".join(out)

    def _candidates(self, low: str) -> list[str]:
        words = set(re.findall(r"[a-z']{4,}", low))
        cands = {n for wd in words for n in self.by_word.get(wd, ())}
        if not cands or len(low) <= 12:
            # every word misread or merged, or a short name ("Topa" for "Toga") whose
            # only word isn't in the index: cheap length + quick_ratio filter, on the
            # folded spelling so commonly-confused letters (g/p, a/o) count as equal
            f = fold(low)
            cands |= {n for n in self.names if abs(len(n) - len(low)) <= 3
                      and difflib.SequenceMatcher(None, f, fold(n.lower())).quick_ratio() >= 0.85}
        return list(cands)


def normalize_name(name: str, known_items: list[str]) -> str:
    """Snap OCR'd names onto items we've seen before, to undo small OCR typos.
    Truncated names ('Crossbow Attack Scroll: I…') resolve to a full name only
    when exactly one known item starts with that prefix."""
    clean = " ".join(name.split())
    if clean.islower():
        clean = clean.title()
    elif clean[:1].islower():
        clean = clean[0].upper() + clean[1:]   # item names always start with a capital
    # official names win over anything seen before (which may itself be a misread)
    d = _Dictionary.cache
    if d is not None:
        hit = d.lookup(clean) if d.names else None
        if hit:
            return hit
        # not a known name as a whole: fix individual words, then try again
        fixed = d.fix_words(clean)
        if fixed != clean:
            clean = fixed
            hit = d.lookup(clean) if d.names else None
            if hit:
                return hit
        if d.names and not is_known_item(clean) and len(_scroll_family(clean)[0]) > 1:
            # several scrolls fit (grades, or even DEF/DEX): the icon colour decides
            # (resolve_scroll) or it's left for review, never whichever one happened
            # to be logged before
            return clean
    low = clean.lower()
    if low.endswith("…"):
        prefix = low[:-1]
        full = [k for k in known_items if not k.endswith("…") and k.lower().startswith(prefix)]
        if len(full) == 1:
            return full[0]
    best, best_score = None, 0.0
    for k in known_items:
        kl = k.lower()
        if kl == low:
            return k
        if not _could_be_typo(low, kl):
            continue
        score = difflib.SequenceMatcher(None, low, kl).ratio()
        if score > best_score:
            best, best_score = k, score
    return best if best_score >= 0.85 else clean


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("-", " ").split())


# letters OCR commonly confuses in the game's font, folded together for comparison
_FOLD = str.maketrans({"g": "p", "q": "p", "l": "i", "1": "i", "|": "i", "!": "i", "0": "o",
                       "a": "o", "c": "e", "5": "s", "v": "u", "8": "b"})


def fold(s: str) -> str:
    """'Reppae' and 'Reggae', 'Scrall' and 'Scroll' fold to the same string."""
    s = s.lower().replace("rn", "m").replace("vv", "w")
    return s.translate(_FOLD)


# words that appear in Classic World item names, for fixing words when no item list is installed
CLASSIC_WORDS = """Scroll Lesser Intermediate Greater Attack Accuracy Defense Avoidability Speed Jump
Magic Pet Equip Overall Armor Topwear Bottomwear Gloves Cape Helmet Shoes Shield Earring Earrings
One-Handed Two-Handed Two-handed Sword Axe Blunt Weapon Spear Polearm Bow Crossbow Claw Dagger
Wand Staff Knuckle Gun STR DEX INT LUK HP MP DEF ATT Throwing Stars Bullets Arrows Crafting
Catalyst Ore Plate Crystal Potion Elixir Mana Power""".split()


def _could_be_typo(a: str, b: str) -> bool:
    """Guard fuzzy matching so genuinely different items never merge:
    numbers must match exactly (scroll 10% vs 60%), short words must match
    exactly (+5 vs +7), and every word must be individually close, so
    'Mokhbi'~'Mokbi' passes but 'Wolbi'~'Mokbi' does not."""
    a, b = _norm(a), _norm(b)
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    if a.endswith("…") != b.endswith("…"):
        return False
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        # merged or split words ("Cherub'sBow"): compare without spaces, strictly
        return difflib.SequenceMatcher(None, a.replace(" ", ""), b.replace(" ", "")).ratio() >= 0.92
    for x, y in zip(ta, tb):
        if len(x) <= 2 or len(y) <= 2:
            if x != y:
                return False
        elif x != y and difflib.SequenceMatcher(None, fold(x), fold(y)).ratio() < 0.75:
            return False
        elif min(len(x), len(y)) <= 4 and len(fold(x)) != len(fold(y)):
            # a short word that gained or lost a letter is a different word
            # ("Topa" is not "Topaz"; "Toga" isn't either), not a typo
            return False
    return True


ALIASES_PATH = SETTINGS_DIR / "aliases.json"   # your manual matches: OCR'd name -> meowdb name


def _aliases() -> dict:
    if _aliases.cache is None:
        _aliases.cache = _load_json(ALIASES_PATH, {})
    return _aliases.cache


_aliases.cache = None


def _alias_key(name: str, icon=None, faded: bool = False) -> str:
    """Truncated scroll names are shared by every grade, so their alias also
    includes the icon colour ('Overall Armor LUK Scroll:…|red')."""
    if icon is not None and ((name.endswith("…") and "scroll" in name.lower())
                             or looks_like_partial_scroll(name)):
        col = scroll_color(icon, faded)
        if col:
            return f"{name}|{col}"
    return name


def resolve_name(name: str, icon=None, faded: bool = False) -> str:
    """Final name for a listing: your colour-specific manual match, else the scroll
    grade from the icon colour, else your plain manual match. (A plain match saved
    for a cut-off scroll name must not force every colour onto one grade.)"""
    al = _aliases()
    key = _alias_key(name, icon, faded)
    if key != name and key in al:
        return al[key]
    graded = resolve_scroll(name, icon, faded)
    if graded != name:
        return graded
    return al.get(name) or name


def apply_alias(name: str, icon=None, faded: bool = False) -> str:
    al = _aliases()
    return al.get(_alias_key(name, icon, faded)) or al.get(name) or name


def add_alias(ocr_name: str, item: str, icon=None) -> None:
    al = _aliases()
    al[_alias_key(ocr_name, icon)] = item
    ALIASES_PATH.write_text(json.dumps(al, indent=1, ensure_ascii=False), encoding="utf-8")
    _aliases.cache = al


_SHOP_LABEL = ("(CASE WHEN COALESCE(shop,'') != '' AND COALESCE(seller,'') != '' "
               "THEN shop || ' (' || seller || ')' ELSE COALESCE(NULLIF(shop,''), seller, '') END)")


def _scope_sql(scope: dict | None) -> tuple[str, list]:
    """Extra WHERE terms that narrow an item to one shop's listing: price, qty and
    either the shop label shown in Live ("Title (Seller)") or shop + seller."""
    if not scope:
        return "", []
    sql, args = " AND price = ? AND qty = ?", [scope["price"], scope["qty"]]
    if "label" in scope:
        sql += f" AND {_SHOP_LABEL} = ?"
        args.append(scope["label"])
    else:
        sql += " AND COALESCE(shop,'') = ? AND COALESCE(seller,'') = ?"
        args += [scope.get("shop") or "", scope.get("seller") or ""]
    return sql, args


def rename_item(con, old: str, new: str, scope: dict | None = None) -> int:
    """Re-label logged listings of `old` as `new` (and keep the better icon): all of
    them, or with `scope` only that one shop's listing (a one-off misread)."""
    if old == new:
        return 0
    extra, args = _scope_sql(scope)
    n = con.execute(f"UPDATE listings SET item = ? WHERE item = ?{extra}", [new, old] + args).rowcount
    if scope:
        # the old name may be a genuine other item (Topaz): its icon stays with it
        if not con.execute("SELECT 1 FROM listings WHERE item = ?", (old,)).fetchone():
            con.execute("DELETE FROM icons WHERE item = ?", (old,))
        con.commit()
        return n
    if con.execute("SELECT 1 FROM icons WHERE item = ?", (new,)).fetchone():
        con.execute("DELETE FROM icons WHERE item = ?", (old,))
    else:
        con.execute("UPDATE icons SET item = ? WHERE item = ?", (new, old))
    con.commit()
    return n


def suggest_items(text: str, limit: int = 40) -> list[str]:
    """meowdb names for the manual-match box: words typed must all appear; ranked by
    how close they look to what was read (confusable letters count as close)."""
    d = _dict()
    if not d or not d.names:
        return []
    low = _norm(text.rstrip("…"))
    words = [w for w in re.findall(r"[a-z0-9']+", low) if len(w) >= 2]
    pool = [n for n in d.names if all(w in _norm(n) for w in words)] if words else []
    if len(pool) < limit:
        pool += [n for n in d._candidates(low) if n not in pool]
    if len(pool) < 5:
        f = fold(low)
        pool += [n for n in d.names if n not in pool and
                 difflib.SequenceMatcher(None, f, fold(_norm(n))).quick_ratio() >= 0.7]
    f = fold(low)
    pool.sort(key=lambda n: -difflib.SequenceMatcher(None, f, fold(_norm(n))[:len(f) + 8]).ratio())
    return pool[:limit]


KEPT_PATH = SETTINGS_DIR / "kept_names.json"   # unmatched names you've confirmed are right


def load_kept() -> set[str]:
    if load_kept.cache is None:
        load_kept.cache = set(str(x) for x in _load_json(KEPT_PATH, []))
    return load_kept.cache


load_kept.cache = None


def save_kept(names) -> None:
    load_kept.cache = set(names)
    KEPT_PATH.write_text(json.dumps(sorted(load_kept.cache, key=str.lower), indent=1,
                                    ensure_ascii=False), encoding="utf-8")


def needs_review(name: str) -> bool:
    """Not a meowdb item, and you haven't said to keep it as it is."""
    return not is_known_item(name) and name not in load_kept()


def best_match(con, name: str) -> tuple[str | None, float]:
    """The most likely meowdb item for an unmatched name, and how alike they look
    (0-1, confusable letters counted as equal). Truncated scroll names use the
    stored icon's colour to pick the grade."""
    if name in best_match.memo:
        return best_match.memo[name]
    pick, score = None, 0.0
    if name.endswith("…") or looks_like_partial_scroll(name):
        graded = resolve_scroll(name, load_icon(con, name))
        if graded != name and is_known_item(graded):
            pick, score = graded, 0.95
    if pick is None:
        sugg = suggest_items(name, 5)
        if sugg:
            a = fold(_norm(name.rstrip("…")))
            def sim(n):
                b = fold(_norm(n))
                b = b[:len(a)] if name.endswith("…") else b   # compare only the visible part
                return difflib.SequenceMatcher(None, a, b).ratio()
            scored = sorted(((sim(n), n) for n in sugg), reverse=True)
            score, pick = scored[0]
    best_match.memo[name] = (pick, score)
    return pick, score


best_match.memo = {}


def unmatched_items(con, include_kept: bool = False) -> list[dict]:
    """Every logged item name that isn't in the meowdb list, most-seen first, with a
    suggested match: what the Review tab shows."""
    if not has_item_list():
        return []
    kept = load_kept()
    out = []
    for item, n, sold, last, raw in con.execute(
            "SELECT item, COUNT(*), SUM(COALESCE(sold, 0)), MAX(COALESCE(last_seen, ts)), "
            "(SELECT raw FROM listings l2 WHERE l2.item = l1.item ORDER BY ts DESC LIMIT 1) "
            "FROM listings l1 GROUP BY item"):
        if is_known_item(item) or (item in kept and not include_kept):
            continue
        pick, score = best_match(con, item)
        out.append({"item": item, "count": n, "sold": sold or 0, "last": last or "",
                    "raw": (raw or "").replace(" (slot re-read)", ""), "suggest": pick,
                    "score": score, "kept": item in kept})
    out.sort(key=lambda r: (r["kept"], -r["count"], r["item"].lower()))
    return out


def _dict() -> "_Dictionary":
    """The item-name dictionary, loaded on first use (also when not monitoring)."""
    if _Dictionary.cache is None:
        _Dictionary.get(db())
    return _Dictionary.cache


# ---- watchlist alerts ---------------------------------------------------------
WATCH_PATH = SETTINGS_DIR / "watchlist.json"
# [{"item", "max_each" (or null),
#   "last_found": {"each", "shop", "player", "map", "channel", "time" (UTC)}}]
PING_PATH = CACHE_DIR / "ping.wav"


def load_watchlist() -> list[dict]:
    return _load_json(WATCH_PATH, [])


def save_watchlist(watch: list[dict]) -> None:
    WATCH_PATH.write_text(json.dumps(watch, indent=1, ensure_ascii=False), encoding="utf-8")


def watch_match(watch: list[dict], item: str, each: float) -> dict | None:
    """The watchlist entry this listing satisfies (item matches and, if a price cap
    is set, the per-item price is at or under it)."""
    for w in watch:
        if w.get("item") == item and (w.get("max_each") in (None, "") or each <= float(w["max_each"])):
            return w
    return None


# Watched listings: one player's listing of an item, e.g. a Korean Fan with +5 ATK.
# [{"item", "price", "shop", "seller", "map", "channel", "notes", "added" (UTC),
#   "last": {"price", "shop", "seller", "map", "channel", "time" (UTC), "sold"} or null}]
WATCHED_LISTINGS_PATH = SETTINGS_DIR / "watched_listings.json"


def load_watched_listings() -> list[dict]:
    return _load_json(WATCHED_LISTINGS_PATH, [])


def save_watched_listings(entries: list[dict]) -> None:
    WATCHED_LISTINGS_PATH.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")


def watched_listing_match(entries: list[dict], item: str, title: str, seller: str,
                          price: int | None = None) -> dict | None:
    """The watched listing this sighting is: same item from the same player (or, when
    no player name was read, the same shop name). With several watched listings of
    the item from that player, the one at this price."""
    found = []
    for e in entries:
        if e.get("item") != item:
            continue
        if seller and e.get("seller"):
            same = _same_name(e["seller"], seller)
        else:
            same = bool(title and e.get("shop")) and _same_name(e["shop"], title)
        if same:
            found.append(e)
    if not found:
        return None
    return next((e for e in found if (e.get("last") or {}).get("price", e.get("price")) == price), found[0])


IGNORE_PATH = SETTINGS_DIR / "ignorelist.json"   # item names that never count as deals


def load_ignorelist() -> list[str]:
    return [str(x) for x in _load_json(IGNORE_PATH, [])]


def save_ignorelist(names) -> None:
    names = sorted(set(names), key=str.lower)
    IGNORE_PATH.write_text(json.dumps(names, indent=1, ensure_ascii=False), encoding="utf-8")


def ensure_ping() -> Path:
    """Generate the default alert sound once: two short bright 'ping' tones."""
    if PING_PATH.exists():
        return PING_PATH
    import math
    import struct
    import wave

    rate, frames = 44100, bytearray()
    for start, freq in ((0.0, 1318.5), (0.16, 1760.0)):
        pad = int(rate * start) - len(frames) // 2
        frames += b"\x00\x00" * max(0, pad)
        for i in range(int(rate * 0.22)):
            t = i / rate
            v = math.sin(2 * math.pi * freq * t) * math.exp(-t * 16) * 0.6
            frames += struct.pack("<h", int(v * 32767))
    with wave.open(str(PING_PATH), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return PING_PATH


def play_sound(path: str | Path | None = None) -> bool:
    """Play a .wav without blocking. Windows: winsound. Linux: pw-play (PipeWire),
    paplay (PulseAudio) or aplay (ALSA), whichever is installed. Returns False if
    nothing could play it (the GUI then falls back to the system bell)."""
    path = str(path or ensure_ping())
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return True
        players = [["afplay", path]] if sys.platform == "darwin" else \
                  [["pw-play", path], ["paplay", path], ["aplay", "-q", path]]
        for cmd in players:
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
    except Exception:
        pass
    return False


def has_item_list() -> bool:
    d = _dict()
    return bool(d and d.names)


def is_known_item(name: str) -> bool:
    """True if the name is exactly an item in the downloaded meowdb list (or one of
    your reference prices). Truncated names that couldn't be resolved are not."""
    d = _dict()
    if not d or not d.names:
        return True   # no list installed: nothing to compare against
    return _norm(name) in d.lower


def known_items(con) -> list[str]:
    _Dictionary.get(con)  # load / refresh the official-name dictionary
    rows = con.execute("SELECT item FROM reference UNION SELECT DISTINCT item FROM listings").fetchall()
    return [r[0] for r in rows]


def known_maps(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT map FROM listings WHERE map IS NOT NULL AND map != ''")]


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# --------------------------------------------------------------------------- market rate


# Outliers: troll listings (69,420,420), fat-fingered prices and OCR slips (4 for
# 4,000) would skew an item's market rate. Once an item has `min_sightings`
# sightings in the history window, anything more than `outlier_ceiling_pct` above
# or `outlier_floor_pct` below their MEDIAN is set aside. The median is the
# baseline because one absurd price barely moves it, where it would drag an
# average far enough to hide itself.

def outlier_bounds(con, item: str, cfg: dict) -> tuple[float, float] | None:
    """(lowest, highest) normal per-unit price for an item, or None when outlier
    filtering is off or there isn't enough data yet."""
    if not cfg.get("outliers_on", True):
        return None
    since = utc_ago(days=cfg["history_days"])
    unit = [p for (p,) in con.execute(
        "SELECT price FROM listings WHERE item = ? AND COALESCE(last_seen, ts) >= ?",
        (item, since))]
    if len(unit) < max(3, min_sightings(cfg)):
        return None
    med = statistics.median(unit)
    lo = med * max(0.0, 1 - float(cfg.get("outlier_floor_pct", 75)) / 100)
    hi = med * (1 + float(cfg.get("outlier_ceiling_pct", 200)) / 100)
    return lo, hi


def is_outlier(each: float, bounds) -> bool:
    return bool(bounds) and not (bounds[0] <= each <= bounds[1])


def price_stats(con, item: str, cfg: dict, bounds="auto") -> dict:
    """Per-unit medians of what's listed and what has sold, over the history
    window, leaving out outliers."""
    if bounds == "auto":
        bounds = outlier_bounds(con, item, cfg)
    since = utc_ago(days=cfg["history_days"])
    listed, sold, out = [], [], 0
    for price, qty, is_sold in con.execute(
            "SELECT price, qty, COALESCE(sold, 0) FROM listings "
            "WHERE item = ? AND COALESCE(last_seen, ts) >= ?", (item, since)):
        each = price
        if is_outlier(each, bounds):
            out += 1
            continue
        (sold if is_sold else listed).append(each)
    return {
        "listed_median": int(statistics.median(listed)) if listed else None,
        "listed_n": len(listed),
        "sold_median": int(statistics.median(sold)) if sold else None,
        "sold_n": len(sold),
        "outliers": out,
    }


def min_sightings(cfg: dict) -> int:
    """Sightings an item needs (in the history window) before it's judged: before
    your own sightings give it a market price, and so before DEAL / HIGH, and
    before unusual prices are set aside as outliers."""
    try:
        return max(1, int(cfg.get("min_sightings", 5)))
    except (TypeError, ValueError):
        return 5


def market_rate(con, item: str, cfg: dict) -> tuple[int | None, str]:
    """What an item is worth, in order of preference:
    1. a reference price you imported (e.g. from meso.watch)
    2. the median price it has actually SOLD for (2+ sales seen)
    3. the median of everything seen, listed and sold
    2 and 3 only once the item has `min_sightings` sightings in the history window,
    so a listing isn't called a DEAL or HIGH against one or two prices."""
    ref = con.execute("SELECT price, source FROM reference WHERE item = ?", (item,)).fetchone()
    if ref:
        return ref[0], ref[1] or "reference"
    need = min_sightings(cfg)
    seen = con.execute("SELECT COUNT(*) FROM listings WHERE item = ? AND COALESCE(last_seen, ts) >= ?",
                       (item, utc_ago(days=cfg["history_days"]))).fetchone()[0]
    if seen < need:
        return None, f"not enough data ({seen} of {need} sightings)"
    bounds = outlier_bounds(con, item, cfg)
    st = price_stats(con, item, cfg, bounds)
    skipped = f", {st['outliers']} outlier{'s' if st['outliers'] != 1 else ''} ignored" if st["outliers"] else ""
    if st["sold_n"] >= 2:
        return st["sold_median"], f"median of {st['sold_n']} sold{skipped}"
    since = utc_ago(days=cfg["history_days"])
    unit = [p for (p,) in con.execute(
        "SELECT price FROM listings WHERE item = ? AND COALESCE(last_seen, ts) >= ?",
        (item, since)) if not is_outlier(p, bounds)]
    if unit:
        return int(statistics.median(unit)), f"median of {len(unit)} seen{skipped}"
    return None, "not enough data"


def verdict(price_each: float, market: int | None, threshold: float,
            bounds=None) -> tuple[str, float | None]:
    """DEAL / fair / HIGH against the market rate; OUTLIER when the price is so far
    from everything else seen that it's likely a troll listing or a misread
    (never a DEAL, so it doesn't ring the bell)."""
    if is_outlier(price_each, bounds):
        pct = ((price_each - market) / market * 100) if market else None
        if price_each < bounds[0] and pct is not None and pct <= -threshold:
            # far below normal: still a deal to show you (the market rate leaves it out)
            return "DEAL", pct
        return "OUTLIER", pct
    if not market:
        return "?", None
    pct = (price_each - market) / market * 100
    if pct <= -threshold:
        return "DEAL", pct
    if pct >= threshold:
        return "HIGH", pct
    return "fair", pct


def cheapest(con, hours: int = 24, cfg: dict | None = None) -> dict[str, tuple]:
    """For each item, the lowest per-unit listing still for sale (seen within the
    window, not sold) and where it is: {item: (each, shop, seller, map, channel, ts)}.
    With `cfg`, outliers (likely misreads like 4 for 4,000) are skipped."""
    since = utc_ago(hours=hours)
    best: dict[str, tuple] = {}
    bounds: dict[str, object] = {}
    for item, each, shop, seller, mp, channel, ts in con.execute(
            "SELECT item, price, shop, seller, map, channel, COALESCE(last_seen, ts) "
            "FROM listings WHERE COALESCE(last_seen, ts) >= ? AND COALESCE(sold, 0) = 0 "
            "ORDER BY price, COALESCE(last_seen, ts) DESC", (since,)):
        if item in best:
            continue
        if cfg is not None:
            if item not in bounds:
                bounds[item] = outlier_bounds(con, item, cfg)
            b = bounds[item]
            if b and each > b[1]:
                continue          # absurdly high: skipped; very low ones are real deals
        best[item] = (each, shop, seller, mp, channel, ts)
    return best


DUPLICATE_HOURS = 24   # the same listing seen again within this long isn't logged again
SOLD_LINK_DAYS = 7     # a listing that faded within this long is marked sold, not re-added


def _same_name(a: str, b: str) -> bool:
    """Shop titles / player names from two visits: equal, or the same apart from
    an OCR slip ("lynn" / "Iynn", "55 burgers" / "55 burpers")."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if a == b:
        return True
    if not a or not b:
        return False
    fa, fb = fold(a), fold(b)
    return fa == fb or difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.85


def _same_shop(row_title, row_seller, row_map, title, seller, mp) -> bool:
    """Same shop: shop name and player name match (OCR slips allowed) and so does
    the map. A field read on only one of the two visits isn't held against it,
    but at least one of shop name / player name must have been read on both.
    The channel is deliberately not compared (it's easy to forget to change)."""
    both = [(x, y) for x, y in ((row_title, title), (row_seller, seller)) if x and y]
    if not both or not all(_same_name(x, y) for x, y in both):
        return False
    return not (row_map and mp) or row_map == mp


def log_listing(con, l: Listing, title: str, seller: str, mp: str, channel, session, ts: str,
                info: dict | None = None) -> str:
    """Save a sighting, unless it's already in the database. Returns "new", "seen"
    (already logged: same item, price, shop name, player name and map
    within the last 24 hours, whatever the channel; only its last-seen time is
    updated) or "sold" (it was logged for sale within the last 7 days and is
    faded now: that row is marked sold instead of adding a second one)."""
    def match(hours, sold_state):
        since = utc_ago(hours=hours)
        # rows already matched during this visit are skipped, so two identical
        # listings in one shop stay two rows
        rows = con.execute(
            "SELECT id, shop, seller, map FROM listings WHERE item = ? AND price = ? AND qty = ? "
            "AND COALESCE(sold, 0) = ? AND COALESCE(last_seen, ts) >= ? AND COALESCE(session, '') != ? "
            "ORDER BY COALESCE(last_seen, ts) DESC",
            (l.item, l.price, l.qty, sold_state, since, session or "")).fetchall()
        for rid, r_title, r_seller, r_map in rows:
            if _same_shop(r_title, r_seller, r_map, title, seller, mp):
                return rid
        return None

    # 1. the same listing, in the same state, logged in the last 24 hours
    def done(status, rid):
        if info is not None:
            info["id"] = rid
            info["notes"] = (con.execute("SELECT notes FROM listings WHERE id = ?", (rid,)).fetchone()
                             or [None])[0] or ""
        return status

    rid = match(DUPLICATE_HOURS, int(l.sold))
    if rid:
        con.execute("UPDATE listings SET last_seen = ?, session = ? WHERE id = ?", (ts, session, rid))
        con.commit()
        return done("seen", rid)
    # 2. now faded, but it was logged for sale recently: it sold
    if l.sold:
        rid = match(SOLD_LINK_DAYS * 24, 0)
        if rid:
            con.execute("UPDATE listings SET sold = 1, sold_ts = ?, last_seen = ?, session = ? "
                        "WHERE id = ?", (ts, ts, session, rid))
            con.commit()
            return done("sold", rid)
    cur = con.execute(
        "INSERT INTO listings (ts, item, price, qty, shop, seller, map, channel, raw, session, "
        "sold, last_seen, sold_ts, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, l.item, l.price, l.qty, title, seller, mp, channel, l.raw, session,
         int(l.sold), ts, ts if l.sold else None, carried_notes(con, l.item, title, seller, l.price)))
    con.commit()
    return done("new", cur.lastrowid)


def carried_notes(con, item: str, title: str, seller: str, price: int) -> str | None:
    """The note a new sighting starts with: the note of the watched listing it is, or
    else of an earlier sighting of the same item, price and player (or shop name)."""
    w = watched_listing_match(load_watched_listings(), item, title, seller, price)
    if w and w.get("notes"):
        return w["notes"]
    who, val = ("seller", seller) if seller else ("shop", title)
    if not val:
        return None
    row = con.execute(f"SELECT notes FROM listings WHERE item = ? AND price = ? AND {who} = ? "
                      "AND COALESCE(notes, '') != '' ORDER BY id DESC LIMIT 1", (item, price, val)).fetchone()
    return row[0] if row else None


def set_listing_notes(con, item: str, notes: str, scope: dict | None = None, ids=None) -> int:
    """Set the note of listings: the ones with these ids, or the listing of `item`
    that `scope` points at (Live / Deals rows). An empty note removes it."""
    notes = (notes or "").strip() or None
    if ids is not None:
        marks, ids = _ids_sql(ids)
        if not ids:
            return 0
        n = con.execute(f"UPDATE listings SET notes = ? WHERE id IN ({marks})", [notes] + ids).rowcount
    else:
        extra, args = _scope_sql(scope)
        n = con.execute(f"UPDATE listings SET notes = ? WHERE item = ?{extra}", [notes, item] + args).rowcount
    con.commit()
    return n


def parse_channel(value) -> str | None:
    """A channel as stored: the number 1-100 as text ("5"). Accepts "5", "ch5",
    "Ch 5", "channel 5"; anything else (blank, 0, 101, words) is None, except
    old free-text values, which are kept as they were."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = re.fullmatch(r"(?i)(?:ch(?:annel)?\.?\s*)?(\d{1,3})", text)
    if m:
        n = int(m.group(1))
        return str(n) if 1 <= n <= 100 else None
    return text


def channel_label(channel) -> str:
    """How a stored channel is shown: "5" -> "ch5"."""
    if not channel:
        return ""
    return f"ch{channel}" if str(channel).isdigit() else str(channel)


def location(shop, mp, channel) -> str:
    parts = [p for p in (shop, mp, channel_label(channel)) if p]
    return " · ".join(parts)


def save_icon(con, item: str, icon: Image.Image | None, faded: bool) -> None:
    """Keep one thumbnail per item. A faded (sold-row) icon is replaced as soon
    as the item is seen in a normal row."""
    if icon is None:
        return
    row = con.execute("SELECT faded, source FROM icons WHERE item = ?", (item,)).fetchone()
    if row and (row[1] == "meowdb" or faded or not row[0]):
        return   # keep official icons, and non-faded screen crops
    import io

    buf = io.BytesIO()
    icon.save(buf, format="PNG")
    con.execute("INSERT OR REPLACE INTO icons (item, png, faded, updated, source) "
                "VALUES (?,?,?,?, 'screen')",
                (item, buf.getvalue(), int(faded), utc_now()))
    con.commit()


def load_icon(con, item: str) -> Image.Image | None:
    """meowdb's official icon if it's been downloaded, else the one captured from
    the screen."""
    import io

    row = None
    key = _icon_keys().get(item)
    if key:
        row = con.execute("SELECT png FROM cache.meowdb_icons WHERE icon_key = ?", (key,)).fetchone()
    row = row or con.execute("SELECT png FROM icons WHERE item = ?", (item,)).fetchone()
    if not row:
        return None
    img = Image.open(io.BytesIO(row[0]))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
        return bg
    return img.convert("RGB")


def forget_item(con, item: str, scope: dict | None = None) -> int:
    """Delete every listing of an item, or with `scope` just one shop's listing."""
    extra, args = _scope_sql(scope)
    n = con.execute(f"DELETE FROM listings WHERE item = ?{extra}", [item] + args).rowcount
    if not con.execute("SELECT 1 FROM listings WHERE item = ?", (item,)).fetchone():
        con.execute("DELETE FROM icons WHERE item = ?", (item,))
    con.commit()
    return n


def _ids_sql(ids) -> tuple[str, list[int]]:
    ids = [int(i) for i in ids]
    return ",".join("?" * len(ids)), ids


def delete_listings(con, ids) -> int:
    """Delete individual listings (by id), e.g. from an item's history."""
    if not ids:
        return 0
    marks, ids = _ids_sql(ids)
    items = [r[0] for r in con.execute(f"SELECT DISTINCT item FROM listings WHERE id IN ({marks})", ids)]
    n = con.execute(f"DELETE FROM listings WHERE id IN ({marks})", ids).rowcount
    for item in items:
        if not con.execute("SELECT 1 FROM listings WHERE item = ?", (item,)).fetchone():
            con.execute("DELETE FROM icons WHERE item = ?", (item,))
    con.commit()
    return n


def set_listing_price(con, ids, each: int) -> int:
    """Correct the price of listings (by id). `each` is per item, like everywhere
    in the app."""
    if not ids or each is None or each <= 0:
        return 0
    marks, ids = _ids_sql(ids)
    n = con.execute(f"UPDATE listings SET price = ?, qty = 1 WHERE id IN ({marks})",
                    [int(each)] + ids).rowcount
    con.commit()
    return n


def set_listing_channel(con, ids, channel) -> int:
    """Set the channel (1-100, or blank for unknown) of listings (by id)."""
    if not ids:
        return 0
    marks, ids = _ids_sql(ids)
    n = con.execute(f"UPDATE listings SET channel = ? WHERE id IN ({marks})",
                    [parse_channel(channel)] + ids).rowcount
    con.commit()
    return n


# --------------------------------------------------------------------------- recording


def crop(full: Image.Image, region) -> Image.Image | None:
    if not region:
        return None
    l, t, w, h = region
    return full.crop((l, t, l + w, t + h))


def record(img: Image.Image, channel: str | None, cfg: dict, map_img=None, quiet=False) -> list[Listing]:
    # each one-shot capture is its own "visit", so the duplicate check compares
    # it with earlier captures (and two identical listings in it stay two rows)
    session = f"capture-{datetime.now():%Y%m%d-%H%M%S-%f}"
    con = db()
    frame = read_shop(img, cfg, known_items(con))
    mp = read_map(map_img, cfg, known_maps(con)) if map_img is not None else ""
    ts = utc_now()
    if cfg.get("save_captures"):
        cap_dir = CACHE_DIR / "captures"
        cap_dir.mkdir(exist_ok=True)
        img.save(cap_dir / f"{ts.replace(':', '-')}.png")
    for l in frame.listings:
        log_listing(con, l, frame.title, frame.seller, mp, channel, session, ts)
        save_icon(con, l.item, l.icon, l.sold)
    if not quiet:
        if frame.listings:
            print(f"shop: {frame.shop or '?'}   map: {mp or '?'}")
        print_listings(con, frame.listings, cfg)
        if not frame.listings:
            print("No listings recognised. Make sure the capture area covers the store window"
                  " (re-run `mfm-tracker calibrate`), or set \"save_captures\": true to inspect captures.")
    return frame.listings


def record_full(full: Image.Image, channel, cfg) -> list[Listing]:
    """Record from a full-screen image, using calibrated areas or auto-detection."""
    shop_r, map_r = Monitor(cfg).regions_for(full)
    if shop_r is None:
        print("No store window found on screen.")
        return []
    return record(crop(full, shop_r), channel, cfg, crop(full, map_r))


def fmt(n) -> str:
    return f"{int(n):,}" if n is not None else "-"


def print_listings(con, listings: list[Listing], cfg: dict, header=True) -> None:
    if not listings:
        return
    colors = {"DEAL": "\033[92m", "HIGH": "\033[91m", "fair": "", "?": "\033[90m", "OUTLIER": "\033[33m"}
    reset = "\033[0m"
    if sys.platform == "win32":
        os.system("")  # enables ANSI colours in Windows Terminal / conhost
    if header:
        print(f"{'item':34} {'price':>14} {'market':>14} {'diff':>8}  verdict")
    for l in listings:
        each = l.price
        mkt, _ = market_rate(con, l.item, cfg)
        v, pct = verdict(each, mkt, cfg["deal_threshold_pct"], outlier_bounds(con, l.item, cfg))
        diff = f"{pct:+.0f}%" if pct is not None else ""
        if l.sold:
            v, colors["SOLD"] = "SOLD", "\033[90m"
        print(f"{colors[v]}{l.item[:34]:34} {fmt(each):>14} {fmt(mkt):>14} {diff:>8}  {v}{reset}")


# --------------------------------------------------------------------------- modes


def frame_hash(img: Image.Image) -> int:
    """Average-hash of the frame, used to skip OCR when nothing on screen changed."""
    small = ImageOps.grayscale(img).resize((24, 24))
    px = small.tobytes()
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


@dataclass
class SeenListing:
    item: str
    price: int
    qty: int
    each: float
    market: int | None
    verdict: str
    pct: float | None
    first_seen: str
    shop: str = ""
    map: str = ""
    sold: bool = False
    status: str = "new"     # new / seen (logged on an earlier visit) / sold (sold since)
    channel: str | None = None
    title: str = ""         # the shop's own name
    seller: str = ""        # the player selling
    id: int | None = None   # its row in the price database
    notes: str = ""         # your note on this listing (e.g. "+5 ATK")


# --------------------------------------------------------------------------- megaphones
# The game's chat window: a white chat bar along its bottom with a dark-blue button
# at its left ("All", "Party", ...) and a light-blue minimize button at its right,
# and a scroll bar up its right side. Megaphones are the chat lines on a solid
# colour with a channel badge (a white box with a red "CHxx" and an ear) after the
# player's name:  [04:01] PlayerName [CH07] : message   (the time is UTC).

CHAT_BTN_LO, CHAT_BTN_HI = (35, 65, 100), (72, 108, 142)


def find_chat(full: Image.Image) -> list | None:
    """The chat messages area (left of the scroll bar, above the chat bar):
    [left, top, width, height], or None."""
    img = full.convert("RGB")
    W, H = img.size
    sc = 2 if W > 1200 else 1
    small = img.resize((W // sc, H // sc), Image.NEAREST) if sc > 1 else img
    px = img.load()
    for b in _blobs(_rgb_mask(small, CHAT_BTN_LO, CHAT_BTN_HI), min_w=max(8, 20 // sc), min_h=max(4, 8 // sc)):
        b = tuple(v * sc for v in b)
        w, h = b[2] - b[0], b[3] - b[1]
        if not (10 <= h <= 70 and 2.5 <= w / h <= 9):
            continue
        ym = (b[1] + b[3]) // 2
        # the white chat bar right of the button (the typing box in it may hold text
        # or have a dark outline: its rows just above the box are plain white)
        xs = range(min(W - 1, b[2] + 3), min(W, b[2] + 3 + 8 * h), 2)
        rows = range(max(0, b[1] - int(0.3 * h)), min(H, ym + 1))
        if not xs or max(sum(1 for x in xs if min(px[x, y]) >= 225) for y in rows) < 0.6 * len(xs):
            continue
        # its far end: the light-blue minimize button
        cyan = next((x for x in range(b[2] + 3, W) if px[x, ym][2] >= 190 and 120 <= px[x, ym][1] <= 240
                     and px[x, ym][0] <= 110), None)
        if cyan is None:
            continue
        # the bottom of the messages: up from the button, past the white bar
        y = b[1] - 1
        while y > 0 and min(px[min(W - 1, b[2] + 6), y]) >= 225:
            y -= 1
        # the scroll bar's white left edge runs up the messages' right side
        edge = None
        ys = range(max(0, y - 4 * h), max(0, y - h), 2)
        for x in range(max(0, cyan - 2 * h), min(W, cyan + 2 * h)):
            if ys and all(min(px[x, yy]) >= 240 for yy in ys):
                edge = x
                break
        if edge is None:
            continue
        top = bot = max(0, y - h)
        while top > 0 and min(px[edge, top - 1]) >= 235:
            top -= 1
        while bot < H - 1 and min(px[edge, bot + 1]) >= 235:
            bot += 1
        left = max(0, b[0] - int(0.2 * h))
        if bot - top < h or edge - left < 6 * h:
            continue
        find_chat.unit = h          # about one chat line's height
        return [left, top, edge - left, bot - top + 1]
    return None


def chat_badges(img: Image.Image) -> list[tuple]:
    """Channel badges in a crop of the chat: white boxes with red text, top to bottom."""
    white = _rgb_mask(img, (245, 245, 245), (255, 255, 255))
    rpx = _rgb_mask(img, (170, 0, 0), (255, 90, 90)).load()
    out = []
    for b in _blobs(white, min_w=16, min_h=6):
        w, h = b[2] - b[0], b[3] - b[1]
        if not (6 <= h <= 60 and 2.2 <= w / h <= 3.8):
            continue
        n = sum(1 for y in range(b[1], b[3]) for x in range(b[0], b[2]) if rpx[x, y])
        if n >= 0.02 * w * h:
            out.append(b)
    return sorted(out, key=lambda b: b[1])


# the badge's two digits, as 6x8 grids of redness 0-9 (averaged from real badges)
BADGE_DIGITS = {
    "0": "049930299990860077860077860077860077089970039920",
    "1": "059950009950000950000950000950000950000950000950",
    "2": "799930099996000069039999089999860000799999099999",
    "3": "899940699992000077049996039996000077799960799930",
    "4": "750186850286650186799996599996000186000286000175",
    "5": "999996998990960000999930999980000178999960999930",
    "6": "079950699700740000799940799970740284079950069710",
    "7": "199990089990000590000590001880018500017610006500",
    "8": "039940078882760069039950049970860069049980039930",
    "9": "079940699981870175089994039994000175049940069940",
}


def _redness(p) -> int:
    r, g, b = p
    return max(0, min(255, (r - max(g, b)) * 2)) if g < 140 else 0


def badge_channel(img: Image.Image, b: tuple) -> str | None:
    """The channel number on a badge ("CH07" -> "7")."""
    bx = img.crop(b).convert("RGB")
    W, H = bx.size
    px = bx.load()
    lim = int(W * 0.62)                      # the ear is on the right
    R = [[_redness(px[x, y]) for x in range(W)] for y in range(H)]
    groups: list[list[int]] = []
    for x in range(lim):
        if max(R[y][x] for y in range(H)) > 150:
            if groups and x - groups[-1][1] <= 1:
                groups[-1][1] = x
            else:
                groups.append([x, x])
    while len(groups) > 4:                   # C, H and two digits
        i = min(range(len(groups) - 1), key=lambda i: groups[i + 1][0] - groups[i][1])
        groups[i:i + 2] = [[groups[i][0], groups[i + 1][1]]]
    if len(groups) != 4:
        return None
    digits = ""
    for gx0, gx1 in groups[2:]:
        rows = [y for y in range(H) if max(R[y][x] for x in range(gx0, gx1 + 1)) > 150]
        if not rows:
            return None
        y0, gh = rows[0], rows[-1] + 1 - rows[0]
        cx, bw = (gx0 + gx1 + 1) / 2, gh * 0.9
        grid = []
        for gy in range(8):
            for gx in range(6):
                s = 0
                for sy in range(3):
                    for sx in range(3):
                        xx = cx - bw / 2 + (gx + (sx + .5) / 3) * bw / 6
                        yy = y0 + (gy + (sy + .5) / 3) * gh / 8
                        if 0 <= xx < W:
                            s += R[min(H - 1, int(yy))][int(xx)]
                grid.append(s / 9 / 255)
        digits += min(BADGE_DIGITS, key=lambda d: sum((g - int(c) / 10) ** 2
                                                    for g, c in zip(grid, BADGE_DIGITS[d])))
    n = int(digits)
    return str(n) if n > 0 else None


def _median_color(img: Image.Image) -> tuple:
    out = []
    for c in img.convert("RGB").split():
        hist, half, s = c.histogram(), c.width * c.height // 2, 0
        for i, k in enumerate(hist):
            s += k
            if s > half:
                out.append(i)
                break
    return tuple(out)


def _chat_text(img: Image.Image, up: int = 3) -> Image.Image:
    """A chat line as dark text on white: whatever differs from the line's colour."""
    from PIL import ImageChops

    rgb = img.convert("RGB").resize((img.width * up, img.height * up), Image.LANCZOS)
    bg = _median_color(rgb)
    d = [ImageChops.difference(c, Image.new("L", c.size, v)) for c, v in zip(rgb.split(), bg)]
    m = ImageChops.lighter(ImageChops.lighter(d[0], d[1]), d[2])
    return ImageOps.invert(m.point(lambda v: max(0, min(255, (v - 40) * 3))))


def _has_text(img: Image.Image, bg: tuple, thr: int = 90) -> bool:
    px = img.convert("RGB").load()
    W, H = img.size
    hits = sum(1 for y in range(0, H, 2) for x in range(0, W, 2)
               if max(abs(a - b) for a, b in zip(px[x, y], bg)) > thr)
    return hits >= 3


def _join_wrapped(a: str, b: str) -> str:
    """Join a line and its wrapped continuation. The game breaks long lines
    anywhere, even inside a word ("TRADI" / "NG"): join without a space when that
    makes a known word that the two halves aren't."""
    if not a or not b:
        return a + b
    wa, wb = re.findall(r"[A-Za-z']+$", a), re.findall(r"^[A-Za-z']+", b)
    if wa and wb:
        known = _TitleWords.get().known
        x, y = wa[0].lower(), wb[0].lower()
        if (x + y) in known and not (x in known and y in known and len(x) > 1 and len(y) > 1):
            return a + b
    return a + " " + b


_DIGITISH = str.maketrans({"o": "0", "O": "0", "D": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                           "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"})


def parse_chat_line(text: str) -> tuple[str, str, str] | None:
    """'[04:01] Player : message' -> ("04:01", "Player", "message")."""
    m = re.search(r"([0-9oOlIDSZz|]{2})\s*[:;.\-,]\s*([0-9oOlIDSZz|]{2})\s*[\]\)}|lI1J]?\s*(.+)$", text)
    if not m:
        return None
    hh, mm = (g.translate(_DIGITISH) for g in m.group(1, 2))
    if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
        return None
    rest = m.group(3)
    if ":" not in rest:
        return None
    name, msg = rest.split(":", 1)
    # the ':' after the badge, read twice, and specks around it
    msg = re.sub(r"^\s*(?:[^\w\s\[@#$(]{1,3}\s+)*[^\w\s\[@#$(]*:[^\w\s\[@#$(]*\s*", "", msg)
    name = re.sub(r"[^\w().'\-]+", "", name.strip().split()[0] if name.strip() else "")
    if name.endswith(")") and "(" not in name:
        name = name[:-1]
    return f"{hh}:{mm}", name, msg.strip()


def clean_megaphone(msg: str) -> str:
    """Drop repeated symbols at the end (@@@@@@, !!!!!!, ~~~~) and stray edge marks."""
    msg = msg.strip()
    while True:
        # a run of one symbol at the end (@@@@@@), with whatever OCR made of the rest of
        # it (the @s often come out as O, C, Q, G ...)
        new = re.sub(r"\s*([^\w\s])(?:\1|[\sOoCcQGDB0@©®]){8,}$", "", msg)
        new = re.sub(r"\s*([^\w\s])\1{2,}\S*$", "", new)
        new = re.sub(r"\s+[|\[\]]$", "", new).strip()
        if new == msg:
            return msg
        msg = new


def fix_megaphone_words(msg: str, con=None) -> str:
    """Common misreads of the chat font: g read as q ("hiqhest"), O for 0 in prices
    ("7Ok"), and item links ("[Mystc Shield]") matched to the item list."""
    known = _TitleWords.get().known

    def word(m):
        w = m.group(0)
        lw = w.lower()
        if len(w) < 3 or lw in known:
            return w
        for cand in (lw.replace("q", "g"), re.sub(r"ina$", "ing", lw), re.sub(r"ina$", "ing", lw.replace("q", "g"))):
            if cand != lw and cand in known:
                return "".join(c.upper() if o.isupper() else c for o, c in zip(w, cand))
        return w
    msg = re.sub(r"[A-Za-z]+", word, msg)
    msg = re.sub(r"\b\d[\dOo,.]*[kKmM]?\b", lambda m: re.sub(r"[Oo]", "0", m.group(0)), msg)
    msg = re.sub(r"\b[5S][0-9Oo]{1,3}[kKmM]\b", lambda m: "5" + re.sub(r"[Oo]", "0", m.group(0)[1:]), msg)
    if con is not None and has_item_list():
        def link(m):
            name = m.group(1).strip()
            if is_known_item(name):
                return m.group(0)
            try:
                pick, score = best_match(con, name)
            except Exception:
                return m.group(0)
            return f"[{pick}]" if pick and score >= 0.75 else m.group(0)
        msg = re.sub(r"\[([^\[\]]{3,40})\]", link, msg)
    return msg


# ("S=" and "B=" are how the chat font's ">" is sometimes read)
SELL_TERMS = re.compile(r"(?<![A-Za-z])(S\s?>|S=(?=\S)|C\s?/\s?O\b|WTS\b|sell(?:s|ing)?\b|have\b|trad(?:e|es|ing)\b)", re.I)
BUY_TERMS = re.compile(r"(?<![A-Za-z])(B\s?>|B=(?=\S)|WTB\b|buy(?:s|ing)?\b|looking\s+for\b|LF\b|need\b|want\b)",
                       re.I)


def megaphone_kind(msg: str) -> str | None:
    """"sell", "buy", "both" (e.g. "S> ... B> ...") or None."""
    s, b = SELL_TERMS.search(msg), BUY_TERMS.search(msg)
    return "both" if s and b else "sell" if s else "buy" if b else None


def chat_time_utc(hhmm: str, now: datetime | None = None) -> str:
    """A chat time (UTC, no date) as a full UTC timestamp: today, or yesterday if
    that would be in the future."""
    now = now or datetime.now(timezone.utc)
    h, m = map(int, hhmm.split(":"))
    dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if dt > now + timedelta(minutes=10):
        dt -= timedelta(days=1)
    return dt.strftime(UTC_FMT)


def _line_span(row: Image.Image, bg: tuple, u: float) -> tuple[int, int]:
    """Where a chat line's colour starts and ends (outside it is the window's frame)."""
    px = row.convert("RGB").load()
    y = row.height // 2
    near = lambda x: max(abs(a - c) for a, c in zip(px[x, y], bg)) < 40
    x0 = next((x for x in range(min(row.width, int(1.5 * u))) if near(x)), 0)
    x1 = next((x + 1 for x in range(row.width - 1, max(0, row.width - int(1.5 * u)), -1) if near(x)), row.width)
    return x0, x1


def _text_band(img: Image.Image) -> Image.Image:
    """A chat line without the edges of the lines above and below it."""
    h = img.height
    return img.crop((0, int(round(0.12 * h)), img.width, h - 1))


def _ocr_words(img: Image.Image) -> list[tuple[str, int, float]]:
    """OCR one line: [(word, left x, confidence)]."""
    import pytesseract

    border = 12
    d = pytesseract.image_to_data(ImageOps.expand(img, border=border, fill=255), config="--psm 7",
                                  output_type=pytesseract.Output.DICT)
    return [(t.strip(), d["left"][i] - border, float(d["conf"][i])) for i, t in enumerate(d["text"])
            if t.strip()]


def _drop_junk(words: list[tuple[str, float]]) -> str:
    """Words of a line, without unsure bits of background at its end."""
    while words and words[-1][1] < 40 and len(re.sub(r"\W", "", words[-1][0])) <= 2:
        words = words[:-1]
    return " ".join(w for w, c in words)


def _read_chat_time(left: Image.Image, unit: float) -> str | None:
    """The "[hh:mm]" at the start of a chat line (dark text on white, `unit` = one
    line's height in its pixels), read on its own with digits only: "HH:MM"."""
    import pytesseract

    W, H = left.size
    px = left.load()
    ink = [any(px[x, y] < 128 for y in range(H)) for x in range(W)]
    end, run = None, 0
    for x in range(int(2 * unit), W):
        run = run + 1 if not ink[x] else 0
        if run >= max(2, int(0.2 * unit)):
            end = x - run + 1
            break
    if end is None:
        return None
    text = pytesseract.image_to_string(ImageOps.expand(left.crop((0, 0, end, H)), border=12, fill=255),
                                       config="--psm 7 -c tessedit_char_whitelist=0123456789:[]").strip()
    m = re.search(r"(\d{2}):?(\d{2})", text)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{m.group(1)}:{m.group(2)}"
    return None


def _fix_chat_times(rows: list[dict]) -> None:
    """Chat lines are in time order: a time far from the lines around it is a misread
    digit (16:10 for 18:10); use its neighbour's."""
    def mins(t):
        h, m = map(int, t.split(":"))
        return h * 60 + m

    def far(a, b):
        d = abs(mins(a) - mins(b))
        return min(d, 1440 - d) > 30
    for i, r in enumerate(rows):
        prev = rows[i - 1]["time"] if i > 0 else None
        nxt = rows[i + 1]["time"] if i + 1 < len(rows) else None
        near = [t for t in (prev, nxt) if t]
        if near and all(far(r["time"], t) for t in near) and (len(near) == 1 or not far(prev, nxt)):
            r["time"] = prev or nxt


# The colours megaphones are shown on (pink, teal, yellow; the blue-grey ones are
# megaphones in your own channel, without a channel badge). The game draws them
# see-through, so the colour on screen shifts a little with what's behind.
MEGAPHONE_COLORS = {
    "pink": (220, 170, 188),
    "teal": (90, 217, 196),
    "yellow": (232, 184, 12),
    "blue": (165, 185, 200),
}


def _megaphone_color(c: tuple) -> str | None:
    best = min(MEGAPHONE_COLORS, key=lambda k: max(abs(a - b) for a, b in zip(c, MEGAPHONE_COLORS[k])))
    return best if max(abs(a - b) for a, b in zip(c, MEGAPHONE_COLORS[best])) <= 45 else None


def _line_look(line: Image.Image) -> tuple[tuple, float]:
    """A chat line's colour, and how much of it is that plain colour (0-1)."""
    core = line.crop((0, int(0.15 * line.height), line.width, int(0.85 * line.height)))
    bg = _median_color(core)
    px = core.load()
    W, H = core.size
    pts = [(x, y) for y in range(0, H, 2) for x in range(0, W, 3)]
    near = sum(1 for x, y in pts if max(abs(a - c) for a, c in zip(px[x, y], bg)) < 30)
    return bg, near / max(1, len(pts))


def read_megaphones(chat: Image.Image, cfg: dict, memo: dict | None = None, con=None, unit: float | None = None,
                    channel: str | None = None, marks: list | None = None) -> list[dict]:
    """Megaphones in a crop of the chat messages: [{time, player, channel, message,
    color}]. `unit` is about one line's height (from find_chat); `channel` is used for
    megaphones without a channel badge (the ones in your own channel). The boxes of
    the lines read are added to `marks` as (box, color name)."""
    chat = chat.convert("RGB")
    W, H = chat.size
    badges = chat_badges(chat)
    if badges:
        bh = sorted(b[3] - b[1] for b in badges)[len(badges) // 2]
        pitch = 1.38 * bh
        gaps = sorted(b2[1] - b1[1] for b1, b2 in zip(badges, badges[1:]) if 0.8 * pitch <= b2[1] - b1[1] <= 1.2 * pitch)
        if gaps:
            pitch = gaps[len(gaps) // 2]
        phase = sorted(((b[1] + b[3]) / 2 - pitch / 2) % pitch for b in badges)[len(badges) // 2]
    elif unit:
        pitch = 1.035 * unit
        # line tops: where a megaphone colour starts, going down the middle of the chat
        tops = []
        px = chat.load()
        prev = None
        for y in range(H):
            c = _megaphone_color(px[W // 2, y]) if max(px[W // 2, y]) >= 120 else None
            row_ok = c is not None and sum(1 for x in range(int(0.1 * W), int(0.9 * W), max(1, W // 40))
                                           if _megaphone_color(px[x, y]) == c) >= 20
            cur = c if row_ok else None
            if cur and cur != prev:
                tops.append(y % pitch)
            prev = cur
        if not tops:
            return []
        phase = sorted(tops)[len(tops) // 2]
    else:
        return []
    u = pitch / 1.035
    right = W - int(0.3 * u)
    slack = int(0.15 * pitch)
    memo = memo if memo is not None else {}
    setup_tesseract(cfg)

    # the lines on screen, top to bottom
    lines = []
    k = -1
    while True:
        y0 = int(round(phase + k * pitch))
        y1 = y0 + int(round(pitch))
        k += 1
        if y0 >= H:
            break
        if y0 < -slack or y1 > H + slack:
            continue
        y0, y1 = max(0, y0), min(H, y1)
        row = chat.crop((0, y0, right, y1))
        bg, plain = _line_look(row)
        badge = next((b for b in badges if y0 <= (b[1] + b[3]) / 2 < y1), None)
        color = _megaphone_color(bg)
        if not (badge or (color and plain >= 0.55)):
            lines.append(None)
            continue
        lx0, lx1 = _line_span(row, bg, u)
        starts = _has_text(row.crop((lx0 + 1, int(0.25 * row.height), lx0 + int(1.0 * u),
                                     int(0.8 * row.height))), bg)
        lines.append({"box": (0, y0, right, y1), "row": row, "bg": bg, "color": color, "badge": badge,
                      "span": (lx0, lx1), "starts": starts})

    out = []
    for i, ln in enumerate(lines):
        if not ln or not ln["starts"]:
            continue
        # wrapped lines below: same colour, no time at the start
        extra = []
        for nxt in lines[i + 1:]:
            if not nxt or nxt["starts"] or nxt["badge"] or \
                    max(abs(a - c) for a, c in zip(nxt["bg"], ln["bg"])) > 45:
                break
            body = _text_band(nxt["row"].crop((nxt["span"][0] + 1, 0, nxt["span"][1], nxt["row"].height)))
            if not _has_text(body, nxt["bg"]):
                break
            extra.append((nxt, body))
        row, b, (lx0, lx1) = ln["row"], ln["badge"], ln["span"]
        color_name = ln["color"] or "badge"
        if marks is not None:
            box = ln["box"]
            marks.append(((box[0], box[1], box[2], (extra[-1][0]["box"][3] if extra else box[3])), color_name))
        key = hashlib_md5(b"".join(_chat_text(im, 1).point(lambda v: 255 if v > 128 else 0).tobytes()
                                   for im in [row] + [e for _, e in extra])
                          + str((b[0] - lx0) if b else -1).encode())
        if key not in memo:
            memo[key] = _read_megaphone_line(row, b, lx0, lx1, u, [e for _, e in extra], chat, con)
        if memo[key]:
            m = dict(memo[key])
            if not m["channel"]:
                m["channel"] = channel
            m["color"] = "#%02x%02x%02x" % (MEGAPHONE_COLORS[ln["color"]] if ln["color"] else ln["bg"])
            out.append(m)
    if len(memo) > 2000:
        memo.clear()
    _fix_chat_times(out)
    return out


def hashlib_md5(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()


def _read_megaphone_line(row, b, lx0, lx1, u, extra, chat, con) -> dict | None:
    """OCR one megaphone (its first line `row`, its badge `b` or None, and the
    wrapped lines below it)."""
    if b:
        left = _chat_text(_text_band(row.crop((lx0 + 1, 0, b[0] - 1, row.height))))
        msg = _chat_text(_text_band(row.crop((b[2] + 1, 0, lx1, row.height))))
        gap = int(2 * u)
        line = Image.new("L", (left.width + gap + msg.width, max(left.height, msg.height)), 255)
        line.paste(left, (0, 0))
        line.paste(msg, (left.width + gap, 0))
        words = _ocr_words(line)
        split = left.width + gap // 2
        head = " ".join(w for w, x, c in words if x < split)
        tail = _drop_junk([(w, c) for w, x, c in words if x >= split])
        parsed = parse_chat_line(head + " : " + tail)
    else:
        left = _chat_text(_text_band(row.crop((lx0 + 1, 0, lx1, row.height))))
        parsed = parse_chat_line(_drop_junk([(w, c) for w, x, c in _ocr_words(left)]))
    if not parsed:
        return None
    hhmm, name, message = parsed
    hhmm = _read_chat_time(left, u * 3) or hhmm
    # names are letters and digits only; the font's g is often read as q
    name = re.sub(r"q(?!u)", "g", re.sub(r"[^A-Za-z0-9]", "", name))
    for im in extra:
        t = _drop_junk([(w, c) for w, x, c in _ocr_words(_chat_text(im))])
        message = _join_wrapped(message, t)
    return {"time": hhmm, "player": name, "channel": badge_channel(chat, b) if b else None,
            "message": fix_megaphone_words(clean_megaphone(message), con)}


def _player_names(con) -> dict[str, str]:
    """Player names already known (megaphones and shop sellers), by their folded
    spelling: a misread like "Locki024" for "Lock1024" folds to the same."""
    counts: dict[str, int] = {}
    for name, n in con.execute("SELECT player, COUNT(*) FROM megaphones WHERE player IS NOT NULL GROUP BY player"):
        counts[name] = counts.get(name, 0) + n
    for name, n in con.execute("SELECT seller, COUNT(*) FROM listings WHERE seller IS NOT NULL AND seller != '' "
                               "GROUP BY seller"):
        counts[name] = counts.get(name, 0) + 3 * n     # shop names are read from a clear box
    out: dict[str, tuple[int, str]] = {}
    for name, n in counts.items():
        k = fold(name)
        if k not in out or n > out[k][0]:
            out[k] = (n, name)
    return {k: v[1] for k, v in out.items()}


def _vote(votes: dict, value: str) -> str:
    """Count one more read of `value`; returns the most-read value so far."""
    votes[value] = votes.get(value, 0) + 1
    return max(votes, key=lambda v: (votes[v], len(v)))


def save_megaphones(con, found: list[dict], now: datetime | None = None) -> int:
    """Store megaphones that aren't stored yet; returns how many are new.

    The same megaphone is read again every second while it's on screen (and moves up
    as the chat scrolls), sometimes with a letter read differently. A read within two
    minutes of a stored one, with nearly the same message and the same player (as
    far as the spelling goes) or channel, is that one: it counts as another read,
    and the player name and message read most often win."""
    new = 0
    seen = utc_now()
    names = _player_names(con)
    for f in found:
        player = re.sub(r"[^A-Za-z0-9]", "", f.get("player") or "")
        if len(player) < 2 or not f.get("message"):
            continue
        player = names.get(fold(player), player)
        ts = chat_time_utc(f["time"], now)
        t = datetime.strptime(ts, UTC_FMT)
        lo, hi = ((t + timedelta(minutes=d)).strftime(UTC_FMT) for d in (-2, 2))
        norm = re.sub(r"\W+", "", f["message"].lower())
        same = None
        for row in con.execute("SELECT id, player, channel, message, votes FROM megaphones "
                               "WHERE ts BETWEEN ? AND ?", (lo, hi)).fetchall():
            mid, p0, ch0, msg0, votes = row
            old = re.sub(r"\W+", "", (msg0 or "").lower())
            alike_msg = old == norm or old.startswith(norm) or norm.startswith(old) or \
                difflib.SequenceMatcher(None, old, norm).ratio() >= 0.85
            alike_name = fold(p0 or "") == fold(player) or \
                difflib.SequenceMatcher(None, fold(p0 or ""), fold(player)).ratio() >= 0.75
            same_ch = bool(ch0 and f.get("channel") and ch0 == f.get("channel"))
            if alike_msg and (alike_name or same_ch):
                same = row
                break
        if same:
            mid, p0, ch0, msg0, votes = same
            try:
                v = json.loads(votes) if votes else {}
            except ValueError:
                v = {}
            v.setdefault("player", {p0: 1} if p0 else {})
            v.setdefault("message", {msg0: 1} if msg0 else {})
            best_p = _vote(v["player"], player)
            best_m = _vote(v["message"], f["message"])
            con.execute("UPDATE megaphones SET player = ?, message = ?, kind = ?, votes = ?, "
                        "channel = COALESCE(channel, ?) WHERE id = ?",
                        (best_p, best_m, megaphone_kind(best_m), json.dumps(v), f.get("channel"), mid))
            continue
        con.execute("INSERT INTO megaphones (ts, seen, player, channel, message, kind, color, votes) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ts, seen, player, f.get("channel"), f["message"], megaphone_kind(f["message"]),
                     f.get("color"), json.dumps({"player": {player: 1}, "message": {f["message"]: 1}})))
        new += 1
    con.commit()
    return new


def purge_megaphones(con, hours: float) -> int:
    """Delete megaphones older than `hours`."""
    n = con.execute("DELETE FROM megaphones WHERE ts < ?", (utc_ago(hours=hours),)).rowcount
    con.commit()
    return n


class MegaphoneWatch:
    """While monitoring: about once a second, reads the chat window for megaphones in
    the background (so shop reading isn't slowed down) and saves new ones."""

    def __init__(self, cfg: dict, on_event, every: float = 1.0, channel=lambda: None):
        import threading

        self.cfg, self.on_event, self.every, self.channel = cfg, on_event, every, channel
        self.last = 0.0
        self.busy = threading.Event()
        self.memo: dict = {}
        self.found = None

    def due(self) -> bool:
        return not self.busy.is_set() and time.monotonic() - self.last >= self.every

    def offer(self, full: Image.Image) -> None:
        import threading

        self.last = time.monotonic()
        self.busy.set()
        threading.Thread(target=self._work, args=(full,), daemon=True).start()

    def _work(self, full):
        try:
            region, unit = self.cfg.get("chat_region"), None
            if not region or is_auto(region, full.size):
                region = find_chat(full)
                unit = getattr(find_chat, "unit", None)
            found = region is not None
            if found != self.found:
                self.found = found
                self.on_event("chat", {"found": found})
            if not region:
                return
            img = crop(full, region)
            marks: list = []
            con = db()
            try:
                got = read_megaphones(img, self.cfg, self.memo, con, unit=unit, channel=self.channel(),
                                      marks=marks)
                n = save_megaphones(con, got) if got else 0
                purge_megaphones(con, self.cfg.get("megaphone_hours", 24))
            finally:
                con.close()
            self.on_event("chat_frame", {"image": img, "marks": marks, "count": len(got)})
            if n:
                self.on_event("megaphones", n)
        except Exception as e:
            self.on_event("error", f"megaphone read failed: {type(e).__name__}: {e}")
        finally:
            self.busy.clear()


class Monitor:
    """
    Continuously OCRs the store window and turns a stream of scrolled frames into
    one clean set of listings per shop visit.

    Capture and OCR are decoupled (see `run`): the screen is captured at a steady
    rate into a queue, like a recording, and OCR works through the queue at its
    own pace. Scrolling faster than the OCR doesn't lose rows; they're read a
    few seconds later.

    - A visit starts when listings appear and ends after `close_frames` frames
      (spanning at least ~0.15 s each) with no store window, or when the store
      header (title, avatar, seller) changes, i.e. you opened a different shop.
      Scrolling never changes the header, so it never splits a visit.
    - Within a visit each (item, price, qty, sold) is logged once, however many
      frames it stays on screen; it must read identically in `confirm_frames`
      frames first, which filters one-off misreads mid-scroll.
    - The shop title, seller and map are read when the visit starts.

    Events are delivered to `on_event(kind, payload)`:
      "frame"   {"listings": [...], "image": PIL.Image, "ocr_ms": int}
      "new"     SeenListing
      "session" {"id", "shop", "map", "started"}
      "closed"  {"id", "shop", "map", "count", "sold"}
      "layout"  Layout   (store window found on screen)
      "error"   str
    """

    HEADER_CHANGE = 2.5   # header thumbnail difference that means "different shop"

    def __init__(self, cfg: dict, on_event=None, channel: str | None = None):
        self.cfg = cfg
        self.on_event = on_event or (lambda kind, payload: None)
        self.channel = channel
        self.session_id: str | None = None
        self.title = self.seller = self.map = ""
        self.header_sig: bytes | None = None
        self.header_ref = None
        self.header_reads = 0
        self.seen: dict[tuple, SeenListing] = {}
        self.pending: dict[tuple, int] = {}
        self.pending_unmatched: dict[tuple, Listing] = {}   # unmatched names still being re-read
        self.last_hidden = 0
        self.empty_streak = 0
        self.empty_first: float | None = None   # capture time of the first empty frame
        self._now = 0.0                          # capture time of the frame in step()
        self.abort = False   # set while stopping to throw away the queued frames
        self.last_hash = None
        self.last_keys: list[tuple] = []
        self.last_listings: list[Listing] = []
        self.map_img = None
        self.layout: Layout | None = None
        self.map_fresh = False   # minimap measured since the last visit ended
        self.chan_watch = ChannelWatch()   # the GUI may hand in its own (shared with its idle check)
        self.megaphones = MegaphoneWatch(cfg, self.on_event, channel=lambda: self.channel)
        self.map_tried = 0.0
        self.geom: dict | None = None
        self.geom_misses = 0
        self.votes: dict[str, dict[str, int]] = {"title": {}, "seller": {}}
        self.con = None  # created in the worker thread (sqlite is per-thread)
        self._split = False

    def request_split(self):
        """Thread-safe: end the current visit at the next frame (e.g. a 'next shop' button)."""
        self._split = True

    # -- session handling
    def shop_name(self) -> str:
        return ShopFrame([], self.title, self.seller).shop or (
            f"shop {self.session_id[-6:]}" if self.session_id else "")

    def _start_session(self, img: Image.Image):
        self.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.seen.clear()
        self.pending.clear()
        self.pending_unmatched.clear()
        self.votes = {"title": {}, "seller": {}}
        self.title = self.seller = self.map = ""
        self.header_sig = header_signature(img)
        self.header_ref = header_parts(img)
        self.header_reads = 0
        if self.map_img is not None:
            try:
                self.map = read_map(self.map_img, self.cfg, known_maps(self.con))
            except Exception as e:
                self.on_event("error", f"map read failed: {e}")
        self._read_header(img)
        self.on_event("session", {"id": self.session_id, "shop": self.shop_name(),
                                  "title": self.title or "", "seller": self.seller or "",
                                  "map": self.map, "started": datetime.now()})

    def _read_header(self, img: Image.Image):
        """Title and seller, read on the first few frames of a visit and voted on."""
        self.header_reads += 1
        for field, fn in (("title", read_title), ("seller", read_seller)):
            try:
                val = fn(img, self.cfg)
            except Exception:
                val = ""
            if val:
                self.votes[field][val] = self.votes[field].get(val, 0) + 1
                setattr(self, field, max(self.votes[field], key=self.votes[field].get))

    def end_session(self):
        self.map_fresh = False           # you may change maps before the next shop
        self.map_tried = 0.0
        if self.session_id and self.con is not None:
            # unmatched names that went out of view before their extra reads: if
            # read at least the usual number of times and not an OCR variant of
            # something already logged, they're logged now rather than lost
            for key, l in list(self.pending_unmatched.items()):
                if key not in self.seen and self.pending.get(key, 0) >= self.cfg["confirm_frames"] \
                        and not self._duplicate_of_seen(l):
                    self._log(key, l)
            self.pending_unmatched.clear()
            # rows logged early in the visit get the final (majority) shop name
            self.con.execute("UPDATE listings SET shop = ?, seller = ? WHERE session = ?",
                             (self.title, self.seller, self.session_id))
            self.con.commit()
        if self.session_id:
            self.on_event("closed", {"id": self.session_id, "shop": self.shop_name(),
                                     "title": self.title or "", "seller": self.seller or "",
                                     "map": self.map, "count": len(self.seen),
                                     "sold": sum(1 for s in self.seen.values() if s.sold)})
        self.session_id = None
        self.seen.clear()
        self.pending.clear()
        self.pending_unmatched.clear()
        self.last_keys = []
        self.header_sig = None
        self.header_ref = None

    # -- frame processing
    def _duplicate_of_seen(self, listing: Listing) -> bool:
        """Same price & qty and a near-identical name already logged this visit:
        an OCR variant of a row we have (e.g. a row clipped at the list edge)."""
        for s in self.seen.values():
            if s.item == listing.item:
                continue  # same name: identical copies are counted by key, not merged
            if s.price == listing.price and s.qty == listing.qty and s.sold == listing.sold and (
                    _could_be_typo(s.item.rstrip("…"), listing.item.rstrip("…"))
                    or s.item.rstrip("…").lower().startswith(listing.item.rstrip("…").lower())):
                # an OCR variant of a row we already have; "…INT Scroll" vs "…LUK Scroll"
                # at the same price are different items and don't pass _could_be_typo
                return True
            if s.price == listing.price and s.qty == listing.qty and s.sold == listing.sold \
                    and self._needs_extra_reads(listing.item) and not self._needs_extra_reads(s.item) \
                    and difflib.SequenceMatcher(None, fold(_norm(s.item)), fold(_norm(listing.item))).ratio() >= 0.75:
                # a garbled read ("Gold Kitty Slip Ss") of a row that has since been read
                # correctly ("Gold Kitty Slippers"): not a separate listing
                return True
        return False

    def _needs_extra_reads(self, item: str) -> bool:
        """A name that isn't in the meowdb list is given more screen captures before
        it's logged, in case a later read of the same row identifies the item."""
        return int(self.cfg.get("unmatched_extra_frames", 2)) > 0 and has_item_list() \
            and needs_review(item)

    def _sighting(self, key: tuple, listing: Listing):
        if key in self.seen or self._duplicate_of_seen(listing):
            return
        self.pending[key] = self.pending.get(key, 0) + 1
        need = self.cfg["confirm_frames"]
        if self._needs_extra_reads(listing.item):
            need += int(self.cfg.get("unmatched_extra_frames", 2))
            self.pending_unmatched[key] = listing
        if self.pending[key] < need:
            return
        self.pending_unmatched.pop(key, None)
        self._log(key, listing)

    def _log(self, key: tuple, listing: Listing):
        each = listing.price
        mkt, _ = market_rate(self.con, listing.item, self.cfg)
        v, pct = verdict(each, mkt, self.cfg["deal_threshold_pct"],
                         outlier_bounds(self.con, listing.item, self.cfg))
        ts = utc_now()
        info: dict = {}
        status = log_listing(self.con, listing, self.title, self.seller, self.map, self.channel,
                             self.session_id, ts, info)
        save_icon(self.con, listing.item, listing.icon, listing.sold)
        s = SeenListing(listing.item, listing.price, listing.qty, each, mkt,
                        "SOLD" if listing.sold else v, pct, ts, self.shop_name(), self.map,
                        listing.sold, status, self.channel, self.title or "", self.seller or "",
                        info.get("id"), info.get("notes") or "")
        self.seen[key] = s
        self.on_event("new", s)

    def _different_shop(self, img: Image.Image) -> bool:
        """Checked on every frame: is this a different shop from the current visit?

        The title-bar + seller-box fingerprint is compared first (cheap). If it
        changed, the shop title and seller are read and compared with the current
        visit's, so a quick hop to the next shop splits immediately while a cursor
        or tooltip over the header doesn't."""
        if not self.session_id or self.header_ref is None or img.width < 50:
            return False
        if not looks_like_shop(img):
            return False   # closing/fading out; the empty-frame count handles that
        title_img, seller_img = header_parts(img)
        change = max(changed_fraction(self.header_ref[0], title_img),
                     changed_fraction(self.header_ref[1], seller_img))
        if change >= 0.25:
            return True    # title or seller name clearly different: a new shop
        if change < 0.13:
            return False   # identical, or just a cursor/tooltip over the header
        # in between: compare the shop names themselves
        try:
            title, seller = read_title(img, self.cfg), read_seller(img, self.cfg)
        except Exception:
            return False
        title_ok = not title or not self.title or similar(title, self.title) >= 0.6
        seller_ok = not seller or not self.seller or similar(seller, self.seller) >= 0.6
        return not (title_ok and seller_ok)

    def step(self, img: Image.Image, map_img: Image.Image | None = None,
             ts: float | None = None) -> None:
        if self.con is None:
            self.con = db()
        self._now = time.monotonic() if ts is None else ts
        if map_img is not None:
            self.map_img = map_img
        if self._split:
            self._split = False
            self.end_session()
            self.last_hash = None  # re-read the screen so the next shop starts fresh
        # every frame, before anything is confirmed or logged: still the same shop?
        if self._different_shop(img):
            self.end_session()
            self.last_hash = None  # read this frame in full as the start of the new shop
        h = frame_hash(img)
        unchanged = self.last_hash is not None and bin(h ^ self.last_hash).count("1") <= 8 \
            and not self.last_hidden   # the cursor moving off a row barely changes the hash
        if unchanged and any(k in self.pending_unmatched for k in self.last_keys):
            unchanged = False   # an unmatched name gets real re-reads, not a repeat of the last one
        self.last_hash = h

        if unchanged:
            # Screen didn't move: count it as another sighting of the same rows.
            if self.last_keys:
                for key, l in zip(self.last_keys, self.last_listings):
                    self._sighting(key, l)
            elif self.session_id:
                self._count_empty()
            return

        if not looks_like_shop(img):
            self.last_keys, self.last_listings, self.last_hidden = [], [], 0
            self._count_empty()
            self.on_event("frame", {"listings": [], "image": img, "ocr_ms": 0})
            return

        t0 = time.perf_counter()
        names = known_items(self.con) + [s.item for s in self.seen.values()]
        frame = read_shop(img, self.cfg, names, with_title=False, geom=self.geom)
        ocr_ms = int((time.perf_counter() - t0) * 1000)
        self.last_hidden = frame.hidden
        if not frame.listings and frame.hidden:
            # every row is under the cursor/tooltip: the shop is still open, so this
            # isn't an empty frame and the layout hasn't changed; just wait
            self.last_keys, self.last_listings = [], []
            self.on_event("frame", {"listings": [], "image": img, "ocr_ms": ocr_ms,
                                    "hidden": frame.hidden})
            return
        if frame.listings:
            self.geom, self.geom_misses = frame.geom, 0
        elif self.geom is not None:
            self.geom_misses += 1
            if self.geom_misses >= 3:   # window layout changed? learn it again
                self.geom, self.geom_misses = None, 0

        if not frame.listings:
            self.last_keys, self.last_listings = [], []
            self._count_empty()
            self.on_event("frame", {"listings": [], "image": img, "ocr_ms": ocr_ms})
            return

        self.empty_streak = 0
        self.empty_first = None
        if not self.session_id:
            self._start_session(img)
        elif self.header_reads < 8:
            self._read_header(img)   # keep voting so a passing cursor gets outvoted
        self.last_listings = frame.listings
        # identical listings (two Cherub's Bows at the same price) are separate rows:
        # number them by how many times they appear in this frame
        counts: dict[tuple, int] = {}
        self.last_keys = []
        for l in frame.listings:
            base = (l.item, l.price, l.qty, l.sold)
            counts[base] = counts.get(base, 0) + 1
            self.last_keys.append(base + (counts[base],))
        for key, l in zip(self.last_keys, frame.listings):
            self._sighting(key, l)
        self.on_event("frame", {"listings": frame.listings, "image": img, "ocr_ms": ocr_ms,
                                "hidden": frame.hidden})

    def _count_empty(self):
        self.empty_streak += 1
        if self.empty_first is None:
            self.empty_first = self._now
        # frames can now be captured much faster than the old one-per-OCR pace, so
        # closing also needs the empty frames to span the time close_frames used to
        if self.session_id and self.empty_streak >= self.cfg["close_frames"] \
                and self._now - self.empty_first >= 0.15 * self.cfg["close_frames"]:
            self.end_session()

    def regions_for(self, full: Image.Image) -> tuple[list | None, list | None]:
        """Shop and map areas for this frame: the calibrated ones, or found
        automatically (re-located only when the cached spot stops looking like
        a store, e.g. you closed it or moved the game window)."""
        shop_r, map_r = self.cfg.get("region"), self.cfg.get("map_region")
        if is_auto(shop_r, full.size):
            if not (self.layout and is_store_window(crop(full, self.layout.store))):
                found = locate_store(full)
                if found:
                    if not self.layout or found.store != self.layout.store:
                        self.geom = None
                    self.layout = found
                    self.map_fresh = True
                    self.on_event("layout", found)
            if not self.layout:
                return None, None
            shop_r = self.layout.store
            if is_auto(map_r, full.size):
                if not self.map_fresh and not self.session_id and time.monotonic() - self.map_tried >= 3:
                    self.map_tried = time.monotonic()
                    # the minimap is as wide as the map's name, so it's measured
                    # again before each visit: shops open in the same spot on
                    # every map, and an area measured on the last map can take
                    # in the scenery beside this one's
                    mp = find_minimap(full, self.layout.store)
                    if mp:
                        self.map_fresh = True
                        if mp != self.layout.map:
                            self.layout.map = mp
                            self.on_event("layout", self.layout)
                map_r = self.layout.map
        return shop_r, map_r

    def run(self, stop_event) -> None:
        """Capture loop; call from a background thread. Stops when stop_event is set.

        Capture and OCR are decoupled, so scrolling faster than the OCR loses
        nothing: a capture thread grabs the screen every `interval_seconds`
        (cheap), crops the store window and queues it; this thread works through
        the queue at the OCR's pace. A quick sweep through shops piles up in the
        queue — like frames of a recording — and is read out while the screen is
        calm again. Frames where nothing moved are queued as the same image, so
        repeats cost no memory and skip the OCR.

        On stop the queued frames are still read, so a sweep made just before
        pressing Stop isn't lost; setting `abort` as well throws them away.
        """
        import queue as _queue
        import threading

        try:
            self.con = db()
        except Exception as e:
            self.on_event("error", f"can't open the price database: {type(e).__name__}: {e}")
            return
        blank = Image.new("RGB", (8, 8), (0, 0, 0))
        frames: _queue.Queue = _queue.Queue(maxsize=FRAME_QUEUE_MAX)
        dropped = [0]

        def capture() -> None:
            last_h, last_imgs = None, None
            while not stop_event.is_set():
                t0 = time.perf_counter()
                try:
                    full = grab_full_screen()
                    if self.cfg.get("channel_prompt") and self.chan_watch.due():
                        try:
                            if self.chan_watch.feed(full):
                                self.on_event("channel_change", None)
                        except Exception as e:
                            self.on_event("error", f"channel check failed: {type(e).__name__}: {e}")
                    if self.megaphones.due():
                        self.megaphones.offer(full)
                    shop_r, map_r = self.regions_for(full)
                    if shop_r is None:
                        imgs = (blank, None)   # no store window anywhere: counts as closed
                    else:
                        imgs = (crop(full, shop_r), crop(full, map_r))
                    h = frame_hash(imgs[0])
                    if last_h is not None and bin(h ^ last_h).count("1") <= 8 and last_imgs:
                        imgs = last_imgs   # nothing moved: requeue the same image object
                    else:
                        last_h, last_imgs = h, imgs
                    item = (imgs[0], imgs[1], time.monotonic())
                    while True:
                        try:
                            frames.put_nowait(item)
                            break
                        except _queue.Full:   # OCR far behind: the oldest frame gives way
                            try:
                                frames.get_nowait()
                                dropped[0] += 1
                            except _queue.Empty:
                                pass
                except Exception as e:
                    self.on_event("error", f"{type(e).__name__}: {e}")
                wait = self.cfg["interval_seconds"] - (time.perf_counter() - t0)
                stop_event.wait(max(wait, 0.02))

        grabber = threading.Thread(target=capture, daemon=True, name="mfm-capture")
        grabber.start()
        last_backlog = 0
        try:
            while True:
                try:
                    img, map_img, ts = frames.get(timeout=0.2)
                except _queue.Empty:
                    if stop_event.is_set():
                        break    # every captured frame was read: done
                    continue
                if stop_event.is_set() and self.abort:
                    break        # don't read what's left in the queue
                try:
                    self.step(img, map_img, ts)
                except Exception as e:  # keep monitoring through transient failures
                    self.on_event("error", f"{type(e).__name__}: {e}")
                n = frames.qsize()
                if (n == 0) != (last_backlog == 0) or abs(n - last_backlog) >= 5:
                    last_backlog = n   # how far the OCR is behind the screen, for the GUI
                    self.on_event("backlog", {"frames": n, "dropped": dropped[0]})
        finally:
            self.end_session()
            self.on_event("stopped", {"dropped": dropped[0]})


def watch(channel, cfg) -> None:
    import threading

    con = db()

    def on_event(kind, p):
        if kind == "session":
            print(f"\n=== {p['shop']}  @ {p['map'] or 'unknown map'}  "
                  f"opened {p['started']:%H:%M:%S} ===")
        elif kind == "new":
            print_listings(con, [Listing(p.item, p.price, p.qty, "", p.sold)], cfg, header=False)
        elif kind == "closed":
            print(f"=== closed, {p['count']} listings ({p['sold']} sold) ===")
        elif kind == "error":
            print(f"[error] {p}")

    print(f"Capturing every {cfg['interval_seconds']}s; the OCR follows at its own pace.\n"
          f"Open shops and scroll; Ctrl+C to stop.")
    stop = threading.Event()
    try:
        Monitor(cfg, on_event, channel).run(stop)
    except KeyboardInterrupt:
        stop.set()


def hotkey(channel, cfg) -> None:
    if session_type() == "wayland":
        sys.exit(
            "Global hotkeys aren't available to apps on Wayland. Instead, bind a key in your\n"
            "compositor to run `mfm-tracker capture`, e.g. for Hyprland:\n"
            "  bind = , F8, exec, mfm-tracker capture\n"
            "or use `mfm-tracker watch`."
        )
    try:
        from pynput import keyboard
    except ImportError:
        sys.exit("`hotkey` needs pynput: run `pip install pynput` (on Linux, install a C compiler\n"
                 "and kernel headers first, e.g. `sudo apt install build-essential linux-libc-dev`).\n"
                 "Or use `mfm-tracker watch` or the app.")

    key_name = cfg["hotkey"].lower()
    target = getattr(keyboard.Key, key_name, None) or keyboard.KeyCode.from_char(key_name)
    print(f"Press {key_name.upper()} with a shop open to capture. Ctrl+C to quit.")

    def on_press(key):
        if key == target:
            print(f"\n--- {datetime.now():%H:%M:%S} ---")
            record_full(grab_full_screen(), channel, cfg)

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


def deals(cfg, hours=24) -> None:
    con = db()
    since = utc_ago(hours=hours)
    rows = con.execute(
        "SELECT item, price, qty, shop, seller, map, channel, MAX(COALESCE(last_seen, ts)) "
        "FROM listings WHERE COALESCE(last_seen, ts) >= ? AND COALESCE(sold, 0) = 0 "
        "GROUP BY item, price, qty, shop, seller, map", (since,)
    ).fetchall()
    scored = []
    ignored = set(load_ignorelist())
    for item, price, qty, shop, seller, mp, channel, ts in rows:
        if item in ignored:
            continue
        each = price
        mkt, src = market_rate(con, item, cfg)
        v, pct = verdict(each, mkt, cfg["deal_threshold_pct"], outlier_bounds(con, item, cfg))
        if v == "OUTLIER":
            continue
        where = location(ShopFrame([], shop or "", seller or "").shop, mp, channel)
        scored.append((pct if pct is not None else 999, item, each, mkt, v, where))
    scored.sort()
    print(f"{'item':32} {'price/ea':>13} {'market':>13} {'diff':>7}  {'verdict':7} where")
    for pct, item, each, mkt, v, where in scored:
        d = "" if pct == 999 else f"{pct:+6.0f}%"
        print(f"{item[:32]:32} {fmt(each):>13} {fmt(mkt):>13} {d:>7}  {v:7} {where}")


def item_history(name, cfg) -> None:
    con = db()
    match = normalize_name(name, known_items(con))
    rows = con.execute(
        "SELECT ts, price, qty, shop, seller, map, channel, COALESCE(sold, 0) FROM listings "
        "WHERE item = ? ORDER BY ts", (match,)).fetchall()
    mkt, src = market_rate(con, match, cfg)
    st = price_stats(con, match, cfg)
    print(f"{match}: market {fmt(mkt)} ({src})")
    print(f"  listed median {fmt(st['listed_median'])} ({st['listed_n']})   "
          f"sold median {fmt(st['sold_median'])} ({st['sold_n']})")
    for ts, price, qty, shop, seller, mp, channel, sold in rows[-40:]:
        where = location(ShopFrame([], shop or "", seller or "").shop, mp, channel)
        print(f"  {ts}  {fmt(price):>13}  {'SOLD  ' if sold else 'listed'} {where}")
    best = cheapest(con, 24 * 7, cfg).get(match)
    if best:
        each, shop, seller, mp, channel, ts = best
        print(f"Cheapest this week: {fmt(each)}/ea at "
              f"{location(ShopFrame([], shop or '', seller or '').shop, mp, channel)} ({local_time(ts)})")


def import_ref(path) -> None:
    con = db()
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            item = (row.get("item") or "").strip()
            price = parse_price(row.get("price") or "")
            if item and price:
                con.execute(
                    "INSERT OR REPLACE INTO reference VALUES (?,?,?,?)",
                    (item, price, row.get("source") or "meso.watch", utc_now()),
                )
                n += 1
    con.commit()
    _Dictionary.cache = None  # reference names are part of the dictionary
    print(f"Imported {n} reference prices.")


# --------------------------------------------------------------------------- export / clear


def export_prices(con, path, cfg: dict, kind: str = "market") -> int:
    """Write prices to a CSV file and return the number of rows.

    kind="market":   one row per item: market price and how it was worked out, listed
                     and sold medians, and the cheapest listing still for sale. The
                     first columns are item,price,source, so the file can be imported
                     again (Import prices) as reference prices.
    kind="listings": every sighting, for spreadsheets."""
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if kind == "listings":
            # times in UTC (ISO 8601, "Z"), so the file means the same in any time zone
            w.writerow(["first_seen_utc", "last_seen_utc", "item", "price", "status",
                        "sold_at_utc", "shop", "seller", "map", "channel", "notes"])
            for ts, last, item, qty, price, sold, sold_ts, shop, seller, mp, channel, notes in con.execute(
                    "SELECT ts, COALESCE(last_seen, ts), item, qty, price, COALESCE(sold, 0), sold_ts, "
                    "shop, seller, map, channel, notes FROM listings ORDER BY ts"):
                w.writerow([ts, last, item, price,
                            "sold" if sold else "for sale", sold_ts or "", shop or "", seller or "",
                            mp or "", channel or "", notes or ""])
                rows += 1
            return rows
        w.writerow(["item", "price", "source", "listed_median", "listed_count", "sold_median",
                    "sold_count", "outliers_ignored", "cheapest_for_sale", "cheapest_at"])
        best = cheapest(con, 24 * 7, cfg)
        items = [r[0] for r in con.execute(
            "SELECT item FROM listings UNION SELECT item FROM reference ORDER BY 1 COLLATE NOCASE")]
        for item in items:
            mkt, src = market_rate(con, item, cfg)
            if not mkt:
                continue
            st = price_stats(con, item, cfg)
            b = best.get(item)
            where = location(ShopFrame([], b[1] or "", b[2] or "").shop, b[3], b[4]) if b else ""
            w.writerow([item, int(mkt), src, st["listed_median"] or "", st["listed_n"],
                        st["sold_median"] or "", st["sold_n"], st["outliers"],
                        int(b[0]) if b else "", where])
            rows += 1
    return rows


EXPORT_STAMP = "%Y-%m-%d %H-%M-%S"


def stamped(path, when: datetime | None = None) -> str:
    """An export's file name with the local date and time added before the
    extension: "prices.csv" -> "prices 2026-10-13 08-11-00.csv"."""
    p = Path(path)
    stamp = (when or datetime.now()).strftime(EXPORT_STAMP)
    return str(p.with_name(f"{p.stem} {stamp}{p.suffix}"))


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- retention

ARCHIVE_DIR = DATA / "archives"
ARCHIVE_NAME = "prices-archive.db"
DB_WARN_BYTES = 100 * 1024 * 1024     # warn when prices.db grows past 100 MB
_AGE = "COALESCE(sold_ts, last_seen, ts)"   # a listing's age: when it was last seen (or sold)


def db_size() -> int:
    """Bytes used by the prices database (including its write-ahead log, if any)."""
    return sum(p.stat().st_size for p in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-wal"))
               if p.exists())


def archive_dir(cfg: dict) -> Path:
    return Path(cfg.get("archive_dir") or ARCHIVE_DIR).expanduser()


def old_listing_count(con, days: int) -> int:
    return con.execute(f"SELECT COUNT(*) FROM listings WHERE {_AGE} < ?",
                       (utc_ago(days=int(days)),)).fetchone()[0]


def prune_old(con, days: int, archive_to: Path | None = None) -> tuple[int, Path | None]:
    """Remove listings not seen for `days` days. With `archive_to`, they're first
    copied into <archive_to>/prices-archive.db (same columns as prices.db, added to
    on every run, never overwritten) and only deleted once that copy is complete.
    Returns (listings removed, archive file)."""
    days = max(1, int(days))
    cutoff = utc_ago(days=days)
    n = con.execute(f"SELECT COUNT(*) FROM listings WHERE {_AGE} < ?", (cutoff,)).fetchone()[0]
    if not n:
        return 0, None
    path = None
    if archive_to is not None:
        archive_to.mkdir(parents=True, exist_ok=True)
        path = archive_to / ARCHIVE_NAME
        con.commit()
        con.execute("ATTACH DATABASE ? AS arc", (str(path),))
        try:
            cols = [r[1] for r in con.execute("PRAGMA main.table_info(listings)")]
            con.execute("CREATE TABLE IF NOT EXISTS arc.listings AS SELECT * FROM main.listings WHERE 0")
            have = {r[1] for r in con.execute("PRAGMA arc.table_info(listings)")}
            for c in cols:                      # columns added by newer versions
                if c not in have:
                    con.execute(f"ALTER TABLE arc.listings ADD COLUMN {c}")
            con.execute("CREATE TABLE IF NOT EXISTS arc.archive_log "
                        "(archived_utc TEXT, listings INTEGER, older_than_utc TEXT, app_version TEXT)")
            collist = ", ".join(cols)
            con.execute(
                f"INSERT INTO arc.listings ({collist}) SELECT {collist} FROM main.listings l "
                f"WHERE {_AGE} < ? AND NOT EXISTS (SELECT 1 FROM arc.listings a "
                "WHERE a.id = l.id AND a.ts = l.ts AND a.item = l.item)", (cutoff,))
            copied = con.execute(
                f"SELECT COUNT(*) FROM main.listings l WHERE {_AGE} < ? AND EXISTS (SELECT 1 FROM "
                "arc.listings a WHERE a.id = l.id AND a.ts = l.ts AND a.item = l.item)",
                (cutoff,)).fetchone()[0]
            if copied != n:
                con.rollback()
                raise RuntimeError(f"only {copied} of {n} listings reached the archive; nothing deleted")
            con.execute("INSERT INTO arc.archive_log VALUES (?,?,?,?)", (utc_now(), n, cutoff, VERSION))
            con.commit()
        finally:
            con.execute("DETACH DATABASE arc")
    con.execute(f"DELETE FROM listings WHERE {_AGE} < ?", (cutoff,))
    con.commit()
    con.execute("VACUUM main")       # give the space back, so the file actually shrinks
    best_match.memo = {}
    return n, path


def apply_retention(con, cfg: dict) -> tuple[int, Path | None]:
    """The retention setting: archive-then-delete, delete, or keep everything."""
    mode = cfg.get("retention", "archive")
    if mode not in ("archive", "delete"):
        return 0, None
    return prune_old(con, cfg.get("keep_days", 30), archive_dir(cfg) if mode == "archive" else None)


def clear_prices(con) -> Path:
    """Delete every logged listing and imported reference price, after saving a
    backup copy of the prices database. Item icons and the meowdb lists stay."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"prices-{_stamp()}.db"
    dst = sqlite3.connect(backup)
    con.commit()
    con.backup(dst, name="main")
    dst.close()
    con.execute("DELETE FROM listings")
    con.execute("DELETE FROM reference")
    con.execute("DELETE FROM megaphones")
    con.commit()
    con.execute("VACUUM main")
    _Dictionary.cache = None
    best_match.memo = {}
    return backup


def reset_settings(parts) -> Path:
    """Delete the chosen personal files (keys of SETTINGS_FILES) after copying the
    whole settings folder to a backup. Returns the backup folder."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"settings-{_stamp()}"
    shutil.copytree(SETTINGS_DIR, backup)
    for key in parts:
        for name in SETTINGS_FILES[key][1]:
            (SETTINGS_DIR / name).unlink(missing_ok=True)
    _aliases.cache = None
    load_kept.cache = None
    best_match.memo = {}
    return backup


def report(cfg, out="report.html") -> None:
    import html as h

    con = db()
    best = cheapest(con, 24, cfg)
    items = [r[0] for r in con.execute(
        "SELECT item FROM listings GROUP BY item ORDER BY MAX(ts) DESC").fetchall()]
    rows_html = []
    for item in items:
        obs = con.execute(
            "SELECT ts, price, qty FROM listings WHERE item = ? ORDER BY ts", (item,)
        ).fetchall()
        each = [p for _, p, _q in obs]
        mkt, src = market_rate(con, item, cfg)
        b = best.get(item)
        v, pct = verdict(b[0] if b else each[-1], mkt, cfg["deal_threshold_pct"])
        lo, hi = min(each), max(each)
        pts = " ".join(
            f"{i * 100 / max(len(each[-50:]) - 1, 1):.1f},{28 - (e - lo) / ((hi - lo) or 1) * 26:.1f}"
            for i, e in enumerate(each[-50:])
        )
        where = location(ShopFrame([], b[1] or "", b[2] or "").shop, b[3], b[4]) if b else ""
        rows_html.append(
            f"<tr class='{v.lower().replace('?', 'unknown')}'><td>{h.escape(item)}</td>"
            f"<td class=n>{fmt(b[0]) if b else '-'}</td><td>{h.escape(where)}</td>"
            f"<td class=n>{fmt(mkt)}</td>"
            f"<td class=n>{'' if pct is None else f'{pct:+.0f}%'}</td><td>{v}</td>"
            f"<td class=n>{fmt(lo)}</td><td class=n>{fmt(hi)}</td><td class=n>{len(obs)}</td>"
            f"<td><svg viewBox='0 0 100 30' width=110 height=30><polyline fill=none "
            f"stroke=currentColor stroke-width=1.5 points='{pts}'/></svg></td>"
            f"<td class=src>{src}</td></tr>"
        )
    html = f"""<!doctype html><meta charset=utf-8><title>Free Market Prices</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#77756f;--line:#e4e1da;--deal:#1f7a3d;--high:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16171a;--fg:#ecebe7;--mut:#9a988f;--line:#2c2d31;--deal:#5fcf86;--high:#ff8a80}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:24px}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:var(--mut);margin:0 0 16px}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{color:var(--mut);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.n{{text-align:right;font-variant-numeric:tabular-nums}} .src{{color:var(--mut);font-size:12px}}
tr.deal td:nth-child(6){{color:var(--deal);font-weight:600}} tr.high td:nth-child(6){{color:var(--high);font-weight:600}}
tr.deal svg{{color:var(--deal)}} tr.high svg{{color:var(--high)}} tr.fair svg,tr.unknown svg{{color:var(--mut)}}
</style>
<h1>Free Market prices</h1>
<p>Generated {datetime.now():%Y-%m-%d %H:%M}. Cheapest listing in the last 24 hours and where it was. Deal threshold &plusmn;{cfg['deal_threshold_pct']}%.</p>
<table><tr><th>Item</th><th class=n>Cheapest</th><th>Where</th><th class=n>Market</th><th class=n>Diff</th><th>Verdict</th>
<th class=n>Low</th><th class=n>High</th><th class=n>Seen</th><th>Trend</th><th>Market source</th></tr>
{''.join(rows_html)}</table>"""
    Path(out).write_text(html, encoding="utf-8")
    print(f"Wrote {Path(out).resolve()}")


# --------------------------------------------------------------------------- cli


def _channel_arg(value: str) -> str:
    ch = parse_channel(value)
    if ch is None or not ch.isdigit():
        raise argparse.ArgumentTypeError("channel must be a number from 1 to 100")
    return ch


def selftest(cfg: dict) -> int:
    """Read a rendered test line with Tesseract (used after building the apps)."""
    import pytesseract
    from PIL import ImageDraw, ImageFont

    setup_tesseract(cfg)
    print(f"tesseract: {pytesseract.pytesseract.tesseract_cmd}")
    try:
        print(f"version:   {pytesseract.get_tesseract_version()}")
    except Exception as e:
        print(f"FAILED to run tesseract: {e}")
        return 1
    text = "Free Market 12,345"
    img = None
    for d in (BUNDLE / "fonts", Path(__file__).resolve().parent / "packaging" / "fonts"):
        try:
            font = ImageFont.truetype(str(d / "Inter-Regular.otf"), 40)
        except Exception:        # no font file, or Pillow built without FreeType
            continue
        img = Image.new("RGB", (900, 80), "white")
        ImageDraw.Draw(img).text((20, 12), text, fill="black", font=font)
        break
    if img is None:              # Pillow's built-in bitmap font, enlarged
        img = Image.new("RGB", (160, 20), "white")
        ImageDraw.Draw(img).text((8, 4), text, fill="black", font=ImageFont.load_default_imagefont()
                                 if hasattr(ImageFont, "load_default_imagefont") else ImageFont.load_default())
        img = img.resize((img.width * 4, img.height * 4), Image.NEAREST)
    got = pytesseract.image_to_string(img, config="--psm 7").strip()
    print(f"read:      {got!r}")
    ok = "12,345" in got or "Market" in got
    print("OCR OK" if ok else "OCR FAILED")
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="mfm-tracker", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("calibrate")
    sp.add_argument("--map", action="store_true", help="set the minimap area instead")
    sp.add_argument("--auto", action="store_true", help="forget saved areas; find them automatically")
    sub.add_parser("gui", help="live monitoring window")
    for name in ("capture", "watch", "hotkey"):
        sp = sub.add_parser(name)
        sp.add_argument("--channel", "--note", dest="channel", type=_channel_arg,
                        help="channel you're on (1-100), saved with each listing")
    sp = sub.add_parser("scan")
    sp.add_argument("images", nargs="+")
    sp.add_argument("--channel", "--note", dest="channel", type=_channel_arg)
    sp.add_argument("--full", action="store_true",
                    help="images are full-screen screenshots: crop the calibrated areas")
    sp = sub.add_parser("match", help="manually match a misread item name to an item in the database")
    sp.add_argument("ocr_name")
    sp.add_argument("item")
    sp = sub.add_parser("review", help="list logged names not matched to any item in the database, with suggestions")
    sp.add_argument("--accept", action="store_true",
                    help="match every name whose suggestion is at least 90%% alike")
    sp = sub.add_parser("forget", help="delete every logged listing of an item")
    sp.add_argument("name", nargs="+")
    sp = sub.add_parser("deals")
    sp.add_argument("--hours", type=int, default=24)
    sp = sub.add_parser("item")
    sp.add_argument("name", nargs="+")
    sp = sub.add_parser("report")
    sp.add_argument("--out", default="report.html")
    sp = sub.add_parser("import-ref")
    sp.add_argument("csv")
    sp = sub.add_parser("export", help="export prices to CSV (market prices, or --all sightings)")
    sp.add_argument("csv")
    sp.add_argument("--all", action="store_true", help="every sighting instead of one row per item")
    sub.add_parser("where", help="print where config and data are stored")
    sub.add_parser("purge-old", help="delete listings logged by the old one-line parser")
    sp = sub.add_parser("meowdb", help="update Classic item + map data from NiaMeowDB (only what changed)")
    sp.add_argument("--full", action="store_true", help="re-download everything (rarely needed)")
    sp.add_argument("--force", action="store_true", help="check now even if checked recently")
    sp.add_argument("--icons", action="store_true",
                    help="also download every item icon not here yet")
    sub.add_parser("fetch-icons", help="download official meowdb icons for every item you've logged")
    sp = sub.add_parser("prune", help="archive and/or delete listings older than the retention setting")
    sp.add_argument("--days", type=int, help="override keep_days")
    sp.add_argument("--delete", action="store_true", help="delete without archiving")
    sp.add_argument("--archive", metavar="DIR", help="archive into this folder first")
    sp = sub.add_parser("osms", help="update the backup item + map lists from OSMS Data Explorer")
    sp.add_argument("--force", action="store_true", help="check now even if checked this week")
    sp = sub.add_parser("import-items", help="use an item list file for spelling")
    sp.add_argument("file")
    sp = sub.add_parser("export-items", help="save the item list in use (.json, .csv or .txt)")
    sp.add_argument("file")
    sp = sub.add_parser("import-maps", help="use a map list file for correcting map names")
    sp.add_argument("file")
    sp = sub.add_parser("export-maps", help="save the map list in use (.json, .csv or .txt)")
    sp.add_argument("file")
    sub.add_parser("selftest", help="check that OCR works (reads a test image)")
    a = p.parse_args()
    cfg = load_config()

    if a.cmd == "selftest":
        sys.exit(selftest(cfg))

    if a.cmd == "gui":
        import mfm_gui
        mfm_gui.main()
        return

    if a.cmd == "calibrate" and a.auto:
        cfg["region"] = cfg["map_region"] = cfg["inventory_region"] = cfg["chat_region"] = None
        save_config(cfg)
        print("Shop and map areas will be found automatically.")
    elif a.cmd == "calibrate":
        calibrate(key="map_region" if a.map else "region")
    elif a.cmd == "capture":
        record_full(grab_full_screen(), a.channel, cfg)
    elif a.cmd == "watch":
        watch(a.channel, cfg)
        print("\nStopped.")
    elif a.cmd == "hotkey":
        try:
            hotkey(a.channel, cfg)
        except KeyboardInterrupt:
            print("\nStopped.")
    elif a.cmd == "scan":
        for path in a.images:
            print(f"\n=== {path} ===")
            img = Image.open(path).convert("RGB")
            if a.full:
                record_full(img, a.channel, cfg)
            else:
                record(img, a.channel, cfg)
    elif a.cmd == "review":
        con = db()
        rows = unmatched_items(con)
        if not has_item_list():
            print("No item database installed yet: run `mfm-tracker meowdb` first.")
        elif not rows:
            print("Nothing to review: every logged item matches an item in the database.")
        for r in rows:
            sug = f"{r['suggest']}  ({r['score']:.0%})" if r["suggest"] else "-"
            if a.accept and r["suggest"] and r["score"] >= 0.9:
                n = rename_item(con, r["item"], r["suggest"])
                add_alias(r["item"], r["suggest"], load_icon(con, r["suggest"]) if r["item"].endswith("…") else None)
                print(f"matched  {r['item']!r} -> {r['suggest']!r} ({n} listings)")
            else:
                print(f"{r['item'][:38]:38} {r['count']:>4}x   suggest: {sug}")
    elif a.cmd == "match":
        con = db()
        n = rename_item(con, a.ocr_name, a.item)
        add_alias(a.ocr_name, a.item)
        print(f"{a.ocr_name!r} -> {a.item!r}: {n} listings renamed; future reads will match too.")
    elif a.cmd == "forget":
        con = db()
        name = normalize_name(" ".join(a.name), known_items(con))
        print(f"Deleted {forget_item(con, name)} listings of {name!r}.")
    elif a.cmd == "deals":
        deals(cfg, a.hours)
    elif a.cmd == "item":
        item_history(" ".join(a.name), cfg)
    elif a.cmd == "report":
        report(cfg, stamped(a.out))
    elif a.cmd == "import-ref":
        import_ref(a.csv)
    elif a.cmd == "export":
        out = stamped(a.csv)
        n = export_prices(db(), out, cfg, "listings" if a.all else "market")
        print(f"Wrote {n} rows to {out}")
    elif a.cmd == "purge-old":
        con = db()
        # the current parser stores both OCR'd lines joined by " / "; older rows don't
        n = con.execute("DELETE FROM listings WHERE raw IS NULL OR raw NOT LIKE '% / %'").rowcount
        con.commit()
        print(f"Deleted {n} listings logged by the old parser.")
    elif a.cmd == "meowdb":
        r = update_items_meowdb(lambda k: print(f"  {k:,} items…", end="\r"), full=a.full, force=a.force)
        print(r.message + (f" Map list updated ({len(_map_names()):,} maps)." if r.maps_updated else ""))
        try:
            print(update_osms(force=a.force or a.full))
        except Exception as e:
            print(f"Backup source not updated: {e}")
        if a.icons:
            con = db()
            have, total = icon_status(con)
            print(f"Icons: {have:,} of {total:,} here; downloading the rest (Ctrl+C stops, "
                  "the next run continues)…")
            got, bad = download_all_icons(
                con, lambda i, n, r: print(f"  {i:,} / {n:,}  ({r:.1f}/s)   ", end="\r"),
                rate=cfg.get("icon_rate", 10))
            print(f"Downloaded {got:,} icons" + (f" ({bad} failed)" if bad else "") + ".")
    elif a.cmd == "prune":
        days = a.days or cfg.get("keep_days", 30)
        where = (None if a.delete else Path(a.archive) if a.archive else archive_dir(cfg))
        n, path = prune_old(db(), days, where)
        print(f"Removed {plural(n, 'listing')} not seen for {plural(days, 'day')}"
              + (f"; archived to {path}" if path else "") + ".")
    elif a.cmd == "osms":
        print(update_osms(force=a.force))
    elif a.cmd == "fetch-icons":
        con = db()
        items = [r[0] for r in con.execute("SELECT DISTINCT item FROM listings")]
        print(f"Downloaded {fetch_official_icons(con, items)} icons from meowdb.")
    elif a.cmd == "import-items":
        print(f"Imported {import_items(a.file):,} item names from {a.file}")
    elif a.cmd == "export-items":
        out = stamped(a.file)
        print(f"Exported {export_items(out):,} items to {out}")
    elif a.cmd == "import-maps":
        print(f"Imported {import_maps(a.file):,} maps from {a.file}")
    elif a.cmd == "export-maps":
        out = stamped(a.file)
        print(f"Exported {export_maps(out):,} maps to {out}")
    elif a.cmd == "where":
        print(f"data folder: {DATA}\n  prices:   {PRICES_DIR}  (listings + reference prices)\n"
              f"  settings: {SETTINGS_DIR}  (settings, capture areas, watched items and listings, ignore list, matches)\n"
              f"  cache:    {CACHE_DIR}  (item and map database, item icons)\n  backups:  {BACKUP_DIR}")
    if not CONFIG_PATH.exists():
        save_config(cfg)


if __name__ == "__main__":
    main()
