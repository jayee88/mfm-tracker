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
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps

# --------------------------------------------------------------------------- paths

# Running as the packaged "Free Market Tracker" app (PyInstaller): Python and
# Tesseract are unpacked to a temporary folder (sys._MEIPASS).
FROZEN = bool(getattr(sys, "frozen", False))
SHARE_NEW = False   # a Shared Database is connected: new sightings are added to it too
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
VERSION = "2610.22.002"
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
SHOTS_DB_PATH = CACHE_DIR / "shots.db"   # shop photos: the screen when a listing was logged
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
    "update_check": True,      # look for a new version at launch (and daily)
    "update_skip": "",         # a version you chose to skip
    "channel_prompt": True,    # ask for the channel at launch and after a channel change
    "review_on": True,         # the Review tab (names to match to items); off hides it
    "channel_count": 40,       # channels shown in the channel picker (1-100)
    "hotkeys_on": False,       # the keys below work while the game has focus (off until turned on)
    "hotkeys": {"monitor": "F5", "channel_up": "F4", "channel_down": "F3"},
    "overlay_on": True,        # the widget over the game: monitoring, frames waiting, channel
    "overlay_pos": None,       # where it was left [x, y]; None = top left of the screen
    "inventory_region": None,  # the item inventory window; None = find it automatically
    "chat_region": None,       # the chat messages (for megaphones); None = find it automatically
    "channel_region": None,    # where the Change Channel window shows; None = look on the whole screen
    "capture_window": True,    # read the game's window on its own (still read when it's covered)
    "game_window_title": "MapleStory",   # ... found by this in its title
    "area_modes": {},          # each drawn area: drawn on the game's "window" or the whole "screen"
    "area_sizes": {},          # ... and the size of the picture it was drawn on
    "megaphone_hours": 72,     # megaphones are kept this long, then deleted
    "save_captures": False,    # keep a PNG of every capture for debugging
    "ocr_collect": False,      # keep chat line images (and what was read) for OCR training
    "ocr_collect_stores": False,   # ... and shop titles (store names)
    "ocr_collect_players": False,  # ... and shop owners' names (player names)
    "ocr_share": True,         # ... and send the checked ones to the Shared Database server
    "ocr_workers": 3,          # frames read ahead at once while scrolling (1 = one at a time)
    "tesseract_library": True, # read through Tesseract's library (models loaded once) when it's there
    "shot_days": 7,            # shop photos (the shop window as a row was logged) are kept
                               # this long, to check a misread name; 0 = take none
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

# Captured frames wait in a queue for OCR. While the reading is a few seconds behind,
# only frames that changed a lot (a scroll, another shop) are added, and the queue has
# a ceiling (Monitor.run's capture), so it can't grow for hours.


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text())
        if "confirm_frames" not in saved and saved.get("interval_seconds") == 3:
            saved.pop("interval_seconds")  # old default was too slow for scrolling
        if saved.get("megaphone_hours") == 24:
            saved.pop("megaphone_hours")   # the previous default: use the new one (72)
        if saved.get("channel_count") in (18, 20):
            saved.pop("channel_count")     # previous defaults: use the new one
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


def utc_at(monotonic_ts: float) -> str:
    """UTC timestamp of a moment given as time.monotonic() (e.g. when a frame was captured)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=max(0.0, time.monotonic() - monotonic_ts))
            ).strftime(UTC_FMT)


_NOW = object()   # step(): "use the monitor's current channel" (the default for direct callers)


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


def _wal(con: sqlite3.Connection, *schemas: str) -> None:
    """Write-ahead logging, so the window's reads never wait for the OCR thread's
    writes (and the other way round). In the default rollback mode a commit from
    one thread blocks every query from another, up to the busy timeout: with the
    app recounting its tables while a shop is being logged, that showed as the
    window freezing ("not responding"). The setting sticks to the file; it's
    retried on the next open if another process holds the database right now."""
    for s in schemas:
        try:
            con.execute(f"PRAGMA {s}.journal_mode=WAL")
            con.execute(f"PRAGMA {s}.synchronous=NORMAL")   # safe with WAL, far fewer fsyncs
        except sqlite3.OperationalError:
            pass


def db() -> sqlite3.Connection:
    """Prices database, with the icon cache attached: queries can use `icons` as
    if it were part of it, but it lives in cache/icons.db."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("ATTACH DATABASE ? AS cache", (str(ICONS_DB_PATH),))
    _wal(con, "main", "cache")
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
    # 2610.19: "nicknames" are called aliases now
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nicknames'").fetchone() and \
            not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'aliases'").fetchone():
        con.execute("ALTER TABLE nicknames RENAME TO aliases")
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
        CREATE TABLE IF NOT EXISTS megaphone_offers (
            megaphone_id INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            side TEXT, phrase TEXT, key TEXT, item TEXT, how TEXT,
            price INTEGER, note TEXT, suggest TEXT, score REAL,
            PRIMARY KEY (megaphone_id, idx)
        );
        CREATE INDEX IF NOT EXISTS idx_offers_item ON megaphone_offers(item);
        CREATE TABLE IF NOT EXISTS megaphones_deleted (uid TEXT PRIMARY KEY, ts TEXT);
        CREATE TABLE IF NOT EXISTS aliases (
            key TEXT PRIMARY KEY,
            item TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            added TEXT, by TEXT, updated TEXT,
            shared_sig TEXT
        );
        CREATE TABLE IF NOT EXISTS ocr_lines (      -- chat line images for OCR training
            id INTEGER PRIMARY KEY,
            grp TEXT NOT NULL,                      -- the same line of the same megaphone
            ts TEXT NOT NULL,
            png BLOB NOT NULL,
            read TEXT,                              -- what the OCR read in this image
            text TEXT,                              -- what the line says (agreed, or fixed by you)
            status TEXT NOT NULL DEFAULT 'new',     -- new, auto (reads agreed), unsure, fixed, skip
            sent INTEGER NOT NULL DEFAULT 0,
            uid TEXT,
            alts TEXT,                              -- the same image read by the other models (JSON)
            kind TEXT NOT NULL DEFAULT 'chat'       -- chat (megaphone line), store (shop title), player (shop owner)
        );
        CREATE INDEX IF NOT EXISTS idx_ocr_grp ON ocr_lines(grp);
        CREATE INDEX IF NOT EXISTS idx_ocr_status ON ocr_lines(status);
        CREATE TABLE IF NOT EXISTS reference (
            item TEXT PRIMARY KEY,
            price INTEGER NOT NULL,
            source TEXT,
            updated TEXT
        );
        """
    )
    if "kind" not in {r[1] for r in con.execute("PRAGMA table_info(ocr_lines)")}:
        con.execute("ALTER TABLE ocr_lines ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
    mcols = {r[1] for r in con.execute("PRAGMA table_info(megaphones)")}
    for col, typ in (("color", "TEXT"), ("votes", "TEXT"), ("conf", "REAL"), ("offers_sig", "TEXT"),
                     # Shared Database: id on the server, synced values
                     ("uid", "TEXT"), ("shared_sig", "TEXT")):
        if col not in mcols:
            con.execute(f"ALTER TABLE megaphones ADD COLUMN {col} {typ}")
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
                     ("notes", "TEXT"),
                     # Shared Database (mfm_shared): id in the sheet, state, row, synced values
                     ("uid", "TEXT"), ("shared", "INTEGER"), ("shared_row", "INTEGER"), ("shared_sig", "TEXT")):
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
LAST_SCREEN_ORIGIN: list[int] = [0, 0]   # where its pixel (0, 0) is on screen (a monitor left of the main one is at x < 0)


CAPTURE_CFG: dict = {}             # the settings (capture_window, game_window_title); set by the app
GRAB_MODE = ["screen"]             # what the latest capture was: "window" (the game's) or "screen"


def drawn_area(cfg: dict, key: str, size=None):
    """An area drawn in Settings → Reading → Window detection, if it was drawn on the same kind
    of capture as now (the game's window, or the whole screen): an area drawn on one
    is in the wrong place on the other, so it's left out (found automatically). With
    `size` (the frame's), one drawn on a picture of the same size counts too: a game
    in full screen gives the same picture either way."""
    r = (cfg or {}).get(key)
    if not r:
        return None
    if (cfg.get("area_modes") or {}).get(key, "screen") == GRAB_MODE[0]:
        return r
    drawn_on = (cfg.get("area_sizes") or {}).get(key)
    if size is not None and drawn_on and tuple(drawn_on) == tuple(size):
        return r
    return None


def grab_full_screen() -> Image.Image:
    """What the app reads: the game's window when it can be copied on its own (so it's
    read even when other windows cover it; see game_window_grab), else a screenshot of
    the whole desktop."""
    cfg = CAPTURE_CFG
    if cfg.get("capture_window", True):
        img = game_window_grab(cfg)
        if img is not None:
            GRAB_MODE[0] = "window"
            LAST_SCREEN_SIZE[:] = img.size
            return img
    GRAB_MODE[0] = "screen"
    st = session_type()
    if st in ("windows", "x11", "macos"):
        import mss  # imported lazily so `scan` works without it

        with mss.mss() as sct:
            mon = sct.monitors[0]  # virtual screen spanning all monitors
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            LAST_SCREEN_SIZE[:] = img.size
            LAST_SCREEN_ORIGIN[:] = [mon["left"], mon["top"]]
            return img

    # Wayland, with the game's window not readable on its own: X11 grabbers only see
    # black, so shell out to a native tool (about a second per screenshot). grim only
    # works on wlroots desktops (Sway, Hyprland...): on KDE or GNOME it fails even when
    # it's installed, so the desktop's own tool goes first, and a tool that fails is
    # skipped from then on.
    import threading
    tmp = Path(tempfile.gettempdir()) / f"mfm-tracker-grab-{threading.get_ident()}.png"
    desk = (os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" + os.environ.get("DESKTOP_SESSION", "")).lower()
    tools = {
        "grim": ["grim", str(tmp)],                                    # Hyprland, Sway, river...
        "spectacle": ["spectacle", "-b", "-n", "-f", "-o", str(tmp)],  # KDE Plasma
        "gnome-screenshot": ["gnome-screenshot", "-f", str(tmp)],      # GNOME
    }
    order = (["spectacle"] if "kde" in desk or "plasma" in desk else
             ["gnome-screenshot"] if "gnome" in desk else []) + ["grim", "spectacle", "gnome-screenshot"]
    errors = []
    for name in dict.fromkeys(order):
        if _GRAB_FAILED.get(name, 0) >= 3 or not shutil.which(name):
            continue
        try:
            tmp.unlink(missing_ok=True)
            r = subprocess.run(tools[name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            if r.returncode != 0 or not tmp.exists():
                raise RuntimeError((r.stderr or b"").decode(errors="replace").strip().splitlines()[-1:]
                                   or [f"exit status {r.returncode}"])
            img = Image.open(tmp).convert("RGB")
            img.load()
        except Exception as e:
            msg = e.args[0][0] if isinstance(e, RuntimeError) and e.args and isinstance(e.args[0], list) else str(e)
            errors.append(f"{name}: {msg}")
            _GRAB_FAILED[name] = _GRAB_FAILED.get(name, 0) + 1   # 3 in a row: not on this desktop
            continue
        _GRAB_FAILED.pop(name, None)
        LAST_SCREEN_SIZE[:] = img.size
        LAST_SCREEN_ORIGIN[:] = [0, 0]
        return img
    if errors:
        raise RuntimeError("Couldn't take a screenshot on Wayland (" + "; ".join(errors) + "). On KDE install "
                           "spectacle, on GNOME gnome-screenshot; grim only works on Sway, Hyprland and "
                           "other wlroots desktops. Or log in to an X11 session.")
    sys.exit(
        "No screenshot tool found. Install the one for your desktop:\n"
        "  grim (Hyprland, Sway), spectacle (KDE) or gnome-screenshot (GNOME)"
    )


_GRAB_FAILED: dict = {}   # Wayland screenshot tools: failures in a row here


class _XWindowGrab:
    """Copies the game's window from XWayland (libX11 + libXcomposite through ctypes):
    the window is found by its title (MapleStory, or "game_window_title" in settings),
    redirected off-screen with the Composite extension, and read with XGetImage, so
    even a covered window is read whole. Returns None when there's no such window
    (the game isn't running, or runs as a native Wayland window)."""

    def __init__(self):
        self.ok = None          # None: not set up yet; False: can't be used here
        self.win = None
        self.found_at = 0.0
        self.lock = threading.Lock()

    def _setup(self) -> bool:
        import ctypes
        import ctypes.util

        if not os.environ.get("DISPLAY"):
            return False
        try:
            x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
            xc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xcomposite") or "libXcomposite.so.1")
        except OSError:
            return False
        c = ctypes
        win_t = c.c_ulong

        class XImage(c.Structure):
            _fields_ = [("width", c.c_int), ("height", c.c_int), ("xoffset", c.c_int), ("format", c.c_int),
                        ("data", c.c_void_p), ("byte_order", c.c_int), ("bitmap_unit", c.c_int),
                        ("bitmap_bit_order", c.c_int), ("bitmap_pad", c.c_int), ("depth", c.c_int),
                        ("bytes_per_line", c.c_int), ("bits_per_pixel", c.c_int), ("red_mask", c.c_ulong),
                        ("green_mask", c.c_ulong), ("blue_mask", c.c_ulong)]

        class XWindowAttributes(c.Structure):
            _fields_ = [("x", c.c_int), ("y", c.c_int), ("width", c.c_int), ("height", c.c_int),
                        ("border_width", c.c_int), ("depth", c.c_int), ("visual", c.c_void_p),
                        ("root", win_t), ("class_", c.c_int), ("bit_gravity", c.c_int),
                        ("win_gravity", c.c_int), ("backing_store", c.c_int), ("backing_planes", c.c_ulong),
                        ("backing_pixel", c.c_ulong), ("save_under", c.c_int), ("colormap", c.c_ulong),
                        ("map_installed", c.c_int), ("map_state", c.c_int), ("all_event_masks", c.c_long),
                        ("your_event_mask", c.c_long), ("do_not_propagate_mask", c.c_long),
                        ("override_redirect", c.c_int), ("screen", c.c_void_p)]

        x.XOpenDisplay.restype = c.c_void_p
        x.XOpenDisplay.argtypes = [c.c_char_p]
        x.XDefaultRootWindow.restype = win_t
        x.XDefaultRootWindow.argtypes = [c.c_void_p]
        x.XQueryTree.argtypes = [c.c_void_p, win_t, c.POINTER(win_t), c.POINTER(win_t),
                                 c.POINTER(c.POINTER(win_t)), c.POINTER(c.c_uint)]
        x.XFetchName.argtypes = [c.c_void_p, win_t, c.POINTER(c.c_char_p)]
        x.XGetWindowAttributes.argtypes = [c.c_void_p, win_t, c.POINTER(XWindowAttributes)]
        x.XGetImage.restype = c.POINTER(XImage)
        x.XGetImage.argtypes = [c.c_void_p, win_t, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_ulong, c.c_int]
        x.XDestroyImage.argtypes = [c.POINTER(XImage)]
        x.XFree.argtypes = [c.c_void_p]
        x.XFreePixmap.argtypes = [c.c_void_p, c.c_ulong]
        x.XSync.argtypes = [c.c_void_p, c.c_int]
        x.XInternAtom.restype = c.c_ulong
        x.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
        x.XGetWindowProperty.argtypes = [c.c_void_p, win_t, c.c_ulong, c.c_long, c.c_long, c.c_int, c.c_ulong,
                                         c.POINTER(c.c_ulong), c.POINTER(c.c_int), c.POINTER(c.c_ulong),
                                         c.POINTER(c.c_ulong), c.POINTER(c.c_void_p)]
        xc.XCompositeRedirectWindow.argtypes = [c.c_void_p, win_t, c.c_int]
        xc.XCompositeNameWindowPixmap.restype = c.c_ulong
        xc.XCompositeNameWindowPixmap.argtypes = [c.c_void_p, win_t]
        dpy = x.XOpenDisplay(None)
        if not dpy:
            return False
        # an X error (e.g. the window closed between finding and reading it) must not
        # end the app: Xlib's default handler exits
        handler_t = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_void_p)
        self._errors = [0]

        def on_error(_d, _e):
            self._errors[0] += 1
            return 0
        self._handler = handler_t(on_error)
        x.XSetErrorHandler(self._handler)
        self.c, self.x, self.xc, self.dpy = c, x, xc, dpy
        self.XImage, self.XWindowAttributes, self.win_t = XImage, XWindowAttributes, win_t
        self.root = x.XDefaultRootWindow(dpy)
        self.net_name = x.XInternAtom(dpy, b"_NET_WM_NAME", 0)
        self.utf8 = x.XInternAtom(dpy, b"UTF8_STRING", 0)
        return True

    def _title(self, w) -> str:
        c, x = self.c, self.x
        t, fmt, n, after, data = c.c_ulong(), c.c_int(), c.c_ulong(), c.c_ulong(), c.c_void_p()
        if x.XGetWindowProperty(self.dpy, w, self.net_name, 0, 256, 0, self.utf8, c.byref(t), c.byref(fmt),
                                c.byref(n), c.byref(after), c.byref(data)) == 0 and data.value:
            s = c.string_at(data.value, n.value).decode("utf-8", "replace")
            x.XFree(data)
            if s:
                return s
        name = c.c_char_p()
        if x.XFetchName(self.dpy, w, c.byref(name)) and name.value is not None:
            s = name.value.decode("latin-1", "replace")
            x.XFree(name)
            return s
        return ""

    def _attrs(self, w):
        a = self.XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, w, self.c.byref(a)):
            return None
        return a

    def _find(self, title: str):
        """The biggest mapped window whose title has `title` in it."""
        c, x = self.c, self.x
        best, best_area = None, 0
        stack = [self.root]
        seen = 0
        while stack and seen < 5000:
            w = stack.pop()
            seen += 1
            root, parent, kids, n = self.win_t(), self.win_t(), c.POINTER(self.win_t)(), c.c_uint()
            if x.XQueryTree(self.dpy, w, c.byref(root), c.byref(parent), c.byref(kids), c.byref(n)):
                stack.extend(kids[i] for i in range(n.value))
                if kids:
                    x.XFree(kids)
            if w == self.root or title not in self._title(w).lower():
                continue
            a = self._attrs(w)
            if a and a.map_state == 2 and a.width * a.height > best_area and a.width >= 300 and a.height >= 200:
                best, best_area = w, a.width * a.height
        return best

    def grab(self, title: str = "maplestory"):
        title = title.lower()
        with self.lock:
            if self.ok is None:
                try:
                    self.ok = self._setup()
                except Exception:
                    self.ok = False
            if not self.ok:
                return None
            c, x = self.c, self.x
            if title != getattr(self, "title", None):
                self.title, self.win, self.found_at = title, None, 0.0     # a new title: look again now
            if self.win is None or (self.win and not self._attrs(self.win)):
                if time.monotonic() - self.found_at < 2:
                    return None               # looked a moment ago: don't walk the tree every frame
                self.found_at = time.monotonic()
                self.win = self._find(title.lower())
                if self.win is None:
                    return None
                self.xc.XCompositeRedirectWindow(self.dpy, self.win, 0)   # automatic: still shown
            a = self._attrs(self.win)
            if not a or a.map_state != 2:
                self.win = None
                return None
            self._errors[0] = 0
            pix = self.xc.XCompositeNameWindowPixmap(self.dpy, self.win)
            img_p = x.XGetImage(self.dpy, pix or self.win, 0, 0, a.width, a.height, 0xFFFFFFFF, 2) if pix else None
            if not img_p:
                img_p = x.XGetImage(self.dpy, self.win, 0, 0, a.width, a.height, 0xFFFFFFFF, 2)
            if pix:
                x.XFreePixmap(self.dpy, pix)
            x.XSync(self.dpy, 0)
            if not img_p or self._errors[0]:
                if img_p:
                    x.XDestroyImage(img_p)
                self.win = None
                return None
            im = img_p.contents
            try:
                if im.bits_per_pixel != 32:
                    return None
                raw = c.string_at(im.data, im.bytes_per_line * im.height)
                out = Image.frombuffer("RGB", (im.width, im.height), raw, "raw", "BGRX", im.bytes_per_line, 1)
                return out.copy()
            finally:
                x.XDestroyImage(img_p)


class _WinWindowGrab:
    """Windows: the game's window copied with PrintWindow (PW_RENDERFULLCONTENT), which
    draws it into memory whatever covers it on screen. If a game draws in a way
    PrintWindow can't copy, every copy is black: then the screen is read instead."""

    def __init__(self):
        self.hwnd = None
        self.found_at = 0.0
        self.good = False        # a copy of this window has shown something (not all black)
        self.black = 0           # black copies in a row before that
        self.lock = threading.Lock()
        self.ready = False
        self.busy = None         # the copy in progress (see grab)

    def _setup(self):
        import ctypes
        from ctypes import wintypes as wt

        c = ctypes
        u, g = c.windll.user32, c.windll.gdi32
        try:
            c.windll.shcore.SetProcessDpiAwareness(2)      # real pixels on scaled screens
        except Exception:
            pass
        u.GetDC.restype = c.c_void_p
        u.GetDC.argtypes = [c.c_void_p]
        u.ReleaseDC.argtypes = [c.c_void_p, c.c_void_p]
        u.PrintWindow.argtypes = [c.c_void_p, c.c_void_p, c.c_uint]
        u.IsWindow.argtypes = [c.c_void_p]
        u.IsIconic.argtypes = [c.c_void_p]
        u.IsWindowVisible.argtypes = [c.c_void_p]
        u.GetClientRect.argtypes = [c.c_void_p, c.POINTER(wt.RECT)]
        u.GetWindowTextLengthW.argtypes = [c.c_void_p]
        u.GetWindowTextW.argtypes = [c.c_void_p, c.c_wchar_p, c.c_int]
        g.CreateCompatibleDC.restype = c.c_void_p
        g.CreateCompatibleDC.argtypes = [c.c_void_p]
        g.CreateDIBSection.restype = c.c_void_p
        g.CreateDIBSection.argtypes = [c.c_void_p, c.c_void_p, c.c_uint, c.POINTER(c.c_void_p), c.c_void_p,
                                       c.c_uint]
        g.SelectObject.restype = c.c_void_p
        g.SelectObject.argtypes = [c.c_void_p, c.c_void_p]
        g.DeleteObject.argtypes = [c.c_void_p]
        g.DeleteDC.argtypes = [c.c_void_p]
        self.c, self.wt, self.u, self.g = c, wt, u, g
        self.ready = True

    def _find(self, title: str):
        c, u = self.c, self.u
        found = []

        @c.WINFUNCTYPE(c.c_bool, c.c_void_p, c.c_void_p)
        def each(hwnd, _):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n:
                    buf = c.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    if title in buf.value.lower():
                        r = self.wt.RECT()
                        u.GetClientRect(hwnd, c.byref(r))
                        if r.right >= 300 and r.bottom >= 200:
                            found.append((r.right * r.bottom, hwnd))
            return True
        u.EnumWindows(each, 0)
        return max(found)[1] if found else None

    def grab(self, title: str):
        with self.lock:
            if not self.ready:
                self._setup()
            c, u, g = self.c, self.u, self.g
            if title != getattr(self, "title", None):
                self.title, self.hwnd, self.found_at = title, None, 0.0
            if not self.hwnd or not u.IsWindow(self.hwnd):
                if time.monotonic() - self.found_at < 2:
                    return None
                self.found_at = time.monotonic()
                self.hwnd, self.good, self.black = self._find(title), False, 0
                if not self.hwnd:
                    return None
            if self.black >= 30 and not self.good:
                return None                   # this game can't be copied this way
            if u.IsIconic(self.hwnd):
                return None                   # minimized: nothing to copy
            r = self.wt.RECT()
            u.GetClientRect(self.hwnd, c.byref(r))
            w, h = r.right, r.bottom
            if w < 50 or h < 50:
                return None

            class BITMAPINFOHEADER(c.Structure):
                _fields_ = [("biSize", c.c_uint32), ("biWidth", c.c_int32), ("biHeight", c.c_int32),
                            ("biPlanes", c.c_uint16), ("biBitCount", c.c_uint16), ("biCompression", c.c_uint32),
                            ("biSizeImage", c.c_uint32), ("biXPelsPerMeter", c.c_int32),
                            ("biYPelsPerMeter", c.c_int32), ("biClrUsed", c.c_uint32), ("biClrImportant", c.c_uint32)]
            bmi = BITMAPINFOHEADER(c.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            hwnd = self.hwnd
            box = {}

            def copy():
                wdc = u.GetDC(hwnd)
                mdc = g.CreateCompatibleDC(wdc)
                bits = c.c_void_p()
                bmp = g.CreateDIBSection(wdc, c.byref(bmi), 0, c.byref(bits), None, 0)
                old = g.SelectObject(mdc, bmp)
                try:
                    ok = u.PrintWindow(hwnd, mdc, 3)         # PW_CLIENTONLY | PW_RENDERFULLCONTENT
                    box["raw"] = c.string_at(bits, w * h * 4) if ok and bits.value else None
                finally:
                    g.SelectObject(mdc, old)
                    g.DeleteObject(bmp)
                    g.DeleteDC(mdc)
                    u.ReleaseDC(hwnd, wdc)
            # PrintWindow waits for the game to draw: if the game is busy (loading, not
            # responding), that copy is left to finish on its own and the screen is read
            # instead, so the app never waits on the game
            if self.busy is not None and self.busy.is_alive():
                return None
            self.busy = threading.Thread(target=copy, daemon=True, name="mfm-printwindow")
            self.busy.start()
            self.busy.join(0.5)
            if self.busy.is_alive():
                return None
            raw = box.get("raw")
            if raw is None:
                self.black += 1
                return None
            img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1).copy()
            if img.getbbox() is None:         # all black: a fade, or this game can't be copied
                self.black += 1
                return img if self.good else None
            self.good, self.black = True, 0
            return img


class _MacWindowGrab:
    """macOS: the game's window copied with CGWindowListCreateImage, which reads one
    window whatever covers it (needs Screen Recording permission, like any capture)."""

    def __init__(self):
        self.wid = None
        self.found_at = 0.0
        self.lock = threading.Lock()
        self.ready = False

    def _setup(self):
        import ctypes

        c = ctypes
        cg = c.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = c.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

        class CGPoint(c.Structure):
            _fields_ = [("x", c.c_double), ("y", c.c_double)]

        class CGSize(c.Structure):
            _fields_ = [("width", c.c_double), ("height", c.c_double)]

        class CGRect(c.Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]
        vp = c.c_void_p
        cg.CGWindowListCopyWindowInfo.restype = vp
        cg.CGWindowListCopyWindowInfo.argtypes = [c.c_uint32, c.c_uint32]
        cg.CGWindowListCreateImage.restype = vp
        cg.CGWindowListCreateImage.argtypes = [CGRect, c.c_uint32, c.c_uint32, c.c_uint32]
        cg.CGRectMakeWithDictionaryRepresentation.restype = c.c_bool
        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [vp, c.POINTER(CGRect)]
        for f in ("CGImageGetWidth", "CGImageGetHeight", "CGImageGetBytesPerRow", "CGImageGetBitsPerPixel"):
            getattr(cg, f).restype = c.c_size_t
            getattr(cg, f).argtypes = [vp]
        cg.CGImageGetDataProvider.restype = vp
        cg.CGImageGetDataProvider.argtypes = [vp]
        cg.CGDataProviderCopyData.restype = vp
        cg.CGDataProviderCopyData.argtypes = [vp]
        cf.CFArrayGetCount.restype = c.c_long
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, c.c_long]
        cf.CFDictionaryGetValue.restype = vp
        cf.CFDictionaryGetValue.argtypes = [vp, vp]
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, c.c_char_p, c.c_uint32]
        cf.CFStringGetCString.restype = c.c_bool
        cf.CFStringGetCString.argtypes = [vp, c.c_char_p, c.c_long, c.c_uint32]
        cf.CFNumberGetValue.restype = c.c_bool
        cf.CFNumberGetValue.argtypes = [vp, c.c_int, vp]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFDataGetLength.restype = c.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFRelease.argtypes = [vp]
        self.c, self.cg, self.cf, self.CGRect = c, cg, cf, CGRect
        self.keys = {k: cf.CFStringCreateWithCString(None, k.encode(), 0x08000100)
                     for k in ("kCGWindowName", "kCGWindowOwnerName", "kCGWindowNumber", "kCGWindowLayer",
                               "kCGWindowBounds")}
        self.ready = True

    def _str(self, d, key) -> str:
        v = self.cf.CFDictionaryGetValue(d, self.keys[key])
        if not v:
            return ""
        buf = self.c.create_string_buffer(512)
        return buf.value.decode("utf-8", "replace") if self.cf.CFStringGetCString(v, buf, 512, 0x08000100) else ""

    def _num(self, d, key) -> int:
        v = self.cf.CFDictionaryGetValue(d, self.keys[key])
        n = self.c.c_int64()
        return n.value if v and self.cf.CFNumberGetValue(v, 4, self.c.byref(n)) else -1

    def _find(self, title: str):
        c, cg, cf = self.c, self.cg, self.cf
        arr = cg.CGWindowListCopyWindowInfo(16, 0)          # every window but the desktop's
        if not arr:
            return None
        best = (0, None)
        try:
            for i in range(cf.CFArrayGetCount(arr)):
                d = cf.CFArrayGetValueAtIndex(arr, i)
                if self._num(d, "kCGWindowLayer") != 0:
                    continue
                name = (self._str(d, "kCGWindowName") + " " + self._str(d, "kCGWindowOwnerName")).lower()
                if title not in name:
                    continue
                r = self.CGRect()
                b = cf.CFDictionaryGetValue(d, self.keys["kCGWindowBounds"])
                if b and cg.CGRectMakeWithDictionaryRepresentation(b, c.byref(r)):
                    area = r.size.width * r.size.height
                    if r.size.width >= 300 and r.size.height >= 200 and area > best[0]:
                        best = (area, self._num(d, "kCGWindowNumber"))
        finally:
            cf.CFRelease(arr)
        return best[1]

    def grab(self, title: str):
        with self.lock:
            if not self.ready:
                self._setup()
            c, cg, cf = self.c, self.cg, self.cf
            if title != getattr(self, "title", None):
                self.title, self.wid, self.found_at = title, None, 0.0
            if self.wid is None:
                if time.monotonic() - self.found_at < 2:
                    return None
                self.found_at = time.monotonic()
                self.wid = self._find(title)
                if self.wid is None:
                    return None
            null = self.CGRect()
            null.origin.x = null.origin.y = float("inf")              # CGRectNull: the window's own bounds
            img = cg.CGWindowListCreateImage(null, 8, self.wid, 1)    # just this window, without its shadow
            if not img:
                self.wid = None                                       # closed: look for it again
                return None
            try:
                w, h = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img)
                bpr, bpp = cg.CGImageGetBytesPerRow(img), cg.CGImageGetBitsPerPixel(img)
                if w < 50 or h < 50 or bpp != 32:
                    if w <= 1:
                        self.wid = None                               # minimized or gone
                    return None
                data = cg.CGDataProviderCopyData(cg.CGImageGetDataProvider(img))
                try:
                    raw = c.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))
                finally:
                    cf.CFRelease(data)
                return Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", bpr, 1).copy()
            finally:
                cf.CFRelease(img)


_XGRAB = _XWindowGrab()
_WGRAB = _WinWindowGrab()
_MGRAB = _MacWindowGrab()


def game_window_grab(cfg: dict | None = None):
    """The game's window on its own, read even when other windows cover it, or None
    when it can't be (not running, minimized, or not readable this way): Linux
    (X11 or XWayland) through the Composite extension, Windows with PrintWindow,
    macOS with CGWindowListCreateImage. Found by its title: "MapleStory", or
    game_window_title in settings."""
    title = ((cfg or CAPTURE_CFG or {}).get("game_window_title") or "MapleStory").strip().lower()
    st = session_type()
    try:
        if st == "windows":
            return _WGRAB.grab(title)
        if st == "macos":
            return _MGRAB.grab(title)
        return _XGRAB.grab(title)
    except Exception:
        return None


def xwayland_window_grab(cfg: dict | None = None):
    """(Old name.) The game's window, or None."""
    return game_window_grab(cfg)


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
    "channel_region": "Open the game's Change Channel window, then drag a box around all of it: "
                      "the channel buttons and CHANGE CHANNEL / CANCEL. Esc cancels.",
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
    globals()["CAPTURE_CFG"] = cfg          # what grab_full_screen reads (capture_window, the title)
    import pytesseract

    global OCR_IN_PROCESS
    ocr_lang_arg.cfg = cfg
    # one thread per read: Tesseract's own threads gain little on these images and
    # get in the way of reading several frames at once
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    if not cfg.get("tesseract_library", True):
        OCR_IN_PROCESS = False

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


# --------------------------------------------------------------------------- OCR engine
# Tesseract is used through its library when it can be found next to the program
# (libtesseract-5.dll, libtesseract.so, libtesseract.dylib): the models are loaded
# once per thread and each read is a function call. Through the program instead,
# every read starts a new process that loads the models again, ~200 ms with the
# app's own model (a third of a shop read, nearly all of a one-row re-read). The
# library does the same reading and gives the same words. "tesseract_library":
# false in settings goes back to the program; so does a library that can't load.
OCR_IN_PROCESS = True


def app_log(line: str) -> None:
    """A line in the data folder's app.log (problems worth knowing about later)."""
    try:
        with open(DATA / "app.log", "a", encoding="utf-8") as fh:
            fh.write(f"{utc_now()} {line}\n")
    except OSError:
        pass


def _tesseract_library_path() -> str | None:
    """The Tesseract shared library: next to the Tesseract program in use, or the
    system's. None when there isn't one (e.g. the static Linux build)."""
    import ctypes.util
    import pytesseract

    exe = Path(pytesseract.pytesseract.tesseract_cmd)
    exe = exe if exe.is_absolute() else Path(shutil.which(str(exe)) or exe)
    dirs = [exe.parent, exe.parent / "lib", exe.parent.parent / "lib"]
    if sys.platform == "win32":
        pats = ("libtesseract*.dll", "tesseract*.dll")
    elif sys.platform == "darwin":
        pats = ("libtesseract*.dylib",)
        dirs += [Path("/opt/homebrew/lib"), Path("/usr/local/lib")]
    else:
        pats = ("libtesseract.so*",)
    for d in dirs:
        for pat in pats:
            hits = sorted(d.glob(pat)) if d.is_dir() else []
            if hits:
                return str(hits[0])
    found = ctypes.util.find_library("tesseract")
    return found or None


class _TessLib:
    """ctypes bindings for the handful of TessBaseAPI functions used."""
    lib = None
    path = None
    tried = None            # the Tesseract program the library was last looked for next to
    lock = threading.Lock()

    @classmethod
    def load(cls):
        """The library, loaded on first use; looked for again whenever the Tesseract
        program in use changes. A read before setup_tesseract() had run (the GUI's
        channel-dialog check, before any shop is read) used to find no program, so
        no library, and that stood for the rest of the session: every read ran the
        program, and frames piled up behind the reading."""
        import pytesseract

        with cls.lock:
            if cls.lib is not None:
                return cls.lib
            exe = pytesseract.pytesseract.tesseract_cmd
            if cls.tried == exe:
                return None
            cls.tried = exe
            path = _tesseract_library_path()
            if not path:
                if Path(exe).is_absolute() or shutil.which(exe):
                    app_log(f"tesseract library not found next to the program ({exe}): using the program")
                return None
            try:
                import ctypes

                if sys.platform == "win32":
                    os.add_dll_directory(str(Path(path).parent))
                lib = ctypes.CDLL(path)
                vp, cp, ci = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
                lib.TessVersion.restype = cp
                lib.TessBaseAPICreate.restype = vp
                lib.TessBaseAPIDelete.argtypes = [vp]
                lib.TessBaseAPIEnd.argtypes = [vp]
                lib.TessBaseAPIInit4.restype = ci
                lib.TessBaseAPIInit4.argtypes = [vp, cp, cp, ci, ctypes.POINTER(cp), ci, ctypes.POINTER(cp),
                                                 ctypes.POINTER(cp), ctypes.c_size_t, ctypes.c_bool]
                lib.TessBaseAPISetVariable.restype = ctypes.c_bool
                lib.TessBaseAPISetVariable.argtypes = [vp, cp, cp]
                lib.TessBaseAPISetPageSegMode.argtypes = [vp, ci]
                lib.TessBaseAPISetImage.argtypes = [vp, vp, ci, ci, ci, ci]
                lib.TessBaseAPIGetTsvText.restype = vp
                lib.TessBaseAPIGetTsvText.argtypes = [vp, ci]
                lib.TessBaseAPIGetUTF8Text.restype = vp
                lib.TessBaseAPIGetUTF8Text.argtypes = [vp]
                lib.TessBaseAPIClear.argtypes = [vp]
                lib.TessDeleteText.argtypes = [vp]
                lib.TessVersion()          # a call that fails here fails loudly, not mid-read
                cls.lib, cls.path = lib, path
            except (OSError, AttributeError) as e:
                app_log(f"tesseract library not used ({path}): {type(e).__name__}: {e}")
                cls.lib = None
            return cls.lib


class _TessEngine:
    """One initialised Tesseract (one model set). Engines live in a pool shared by
    every thread (see _EnginePool): a read takes one, uses it, gives it back. One
    engine is used by one thread at a time, which is what Tesseract asks for;
    they were per thread before, and the megaphone reader makes a new thread for
    every read, so engines were initialised once a second and never freed."""
    _init_lock = threading.Lock()   # one initialisation at a time: they share model caches

    def __init__(self, lib, datapath: str | None, lang: str, words_file: str | None):
        import ctypes

        self.lib, self.vars, self.dead = lib, {}, False
        self.handle = lib.TessBaseAPICreate()
        keys, vals = [], []
        if words_file:
            keys.append(b"user_words_file")
            vals.append(words_file.encode())
        karr = (ctypes.c_char_p * max(1, len(keys)))(*keys) if keys else None
        varr = (ctypes.c_char_p * max(1, len(vals)))(*vals) if vals else None
        with _TessEngine._init_lock:
            rc = lib.TessBaseAPIInit4(self.handle, datapath.encode() if datapath else None, lang.encode(), 3,
                                      None, 0, karr, varr, len(keys), False)
        if rc == 0:
            lib.TessBaseAPISetVariable(self.handle, b"debug_file", os.devnull.encode())   # its warnings: not on the console
        if rc != 0:
            lib.TessBaseAPIDelete(self.handle)
            self.handle = None
            raise OSError(f"Tesseract couldn't load '{lang}'" + (f" from {datapath}" if datapath else ""))

    def set_vars(self, wanted: dict):
        """Runtime variables (a character whitelist): set, and cleared again when a
        later read on this engine doesn't want them."""
        for k in set(self.vars) | set(wanted):
            v = wanted.get(k, "")
            if self.vars.get(k, "") != v:
                self.lib.TessBaseAPISetVariable(self.handle, k.encode(), v.encode())
        self.vars = dict(wanted)

    def read(self, img: Image.Image, psm: int, tsv: bool) -> str:
        import ctypes

        if img.width < 3 or img.height < 3:
            return ""                  # nothing to read; the program says "Image too small" and reads nothing
        if img.mode not in ("L", "RGB"):
            img = img.convert("L")
        bpp = 1 if img.mode == "L" else 3
        data = img.tobytes()
        lib, h = self.lib, self.handle
        lib.TessBaseAPISetPageSegMode(h, psm)
        lib.TessBaseAPISetImage(h, data, img.width, img.height, bpp, img.width * bpp)
        p = lib.TessBaseAPIGetTsvText(h, 0) if tsv else lib.TessBaseAPIGetUTF8Text(h)
        try:
            return ctypes.string_at(p).decode("utf-8", "replace") if p else ""
        finally:
            if p:
                lib.TessDeleteText(p)
            lib.TessBaseAPIClear(h)

    def close(self):
        """Free an engine the pool doesn't keep. Never at exit: after a fault in the
        library, TessBaseAPIEnd never returned and the process outlived its window
        by hours. The OS frees everything at exit."""
        if self.handle and not self.dead:
            self.lib.TessBaseAPIEnd(self.handle)
            self.lib.TessBaseAPIDelete(self.handle)
            self.handle = None


class _EnginePool:
    """Initialised engines by model set, shared by every thread. A read takes one
    (making it if none is free), gives it back after; a few are kept per model set,
    enough for the frames read at once plus the megaphone and channel readers."""
    free: dict = {}
    lock = threading.Lock()
    KEEP = 6

    @classmethod
    def acquire(cls, key) -> "_TessEngine":
        with cls.lock:
            lst = cls.free.setdefault(key, [])
            if lst:
                return lst.pop()
        return _TessEngine(_TessLib.load(), *key)

    @classmethod
    def release(cls, key, engine) -> None:
        if engine.dead:
            return
        with cls.lock:
            lst = cls.free.setdefault(key, [])
            if len(lst) < cls.KEEP:
                lst.append(engine)
                return
        engine.close()




def _parse_ocr_config(config: str) -> tuple[int, str | None, str, str | None, dict]:
    """The pieces of a Tesseract command line: psm, tessdata dir, languages,
    user-words file and -c variables."""
    import shlex

    # split like the shell does: a path with spaces comes quoted (see _short_path)
    try:
        toks = shlex.split(config, posix=(sys.platform != "win32"))
    except ValueError:
        toks = config.split()
    toks = [t.strip('"') for t in toks]

    def after(flag):
        return toks[toks.index(flag) + 1] if flag in toks and toks.index(flag) + 1 < len(toks) else None
    psm = int(after("--psm") or 3)
    datapath = after("--tessdata-dir")
    lang = after("-l") or "eng"
    words = after("--user-words")
    cvars = dict(t.split("=", 1) for i, t in enumerate(toks) if i and toks[i - 1] == "-c" and "=" in t)
    if datapath is None:
        import pytesseract

        d = Path(pytesseract.pytesseract.tesseract_cmd).parent / "tessdata"
        datapath = os.environ.get("TESSDATA_PREFIX") or (str(d) if (d / "eng.traineddata").exists() else None)
    return psm, datapath, lang, words, cvars


def _ocr_in_process(img: Image.Image, config: str, tsv: bool) -> str | None:
    """Read through the library; None when it isn't available (the caller then
    runs the program, as before)."""
    global OCR_IN_PROCESS
    if not OCR_IN_PROCESS or _TessLib.load() is None:
        return None
    psm, datapath, lang, words, cvars = _parse_ocr_config(config)
    key = (datapath, lang, words)
    eng = None
    try:
        eng = _EnginePool.acquire(key)
        eng.set_vars(cvars)
        return eng.read(img, psm, tsv)
    except OSError as e:
        # an access violation inside the library (ctypes turns it into OSError on
        # Windows): that engine is never used again, and the program takes over
        if eng is not None:
            eng.dead = True
        OCR_IN_PROCESS = False
        app_log(f"tesseract library gave up, using the program: {e} | thread {threading.current_thread().name}, "
                f"image {img.size} {img.mode}, config {config.strip()[:80]}")
        return None
    finally:
        if eng is not None:
            _EnginePool.release(key, eng)


def ocr_data(img: Image.Image, config: str) -> dict:
    """Words with positions and confidences, like pytesseract.image_to_data(...,
    output_type=DICT): lists under "text", "conf", "left", "top", "width",
    "height", "block_num", "par_num", "line_num" (and the rest of the TSV)."""
    text = _ocr_in_process(img, config, tsv=True)
    if text is None:
        import pytesseract

        return pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
    # the library's TSV has no header line (the program's does)
    header = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
              "left", "top", "width", "height", "conf", "text"]
    rows = [r.split("\t") for r in text.splitlines()]
    if rows and rows[0][:1] == ["level"]:
        rows = rows[1:]
    out: dict = {k: [] for k in header}
    for r in rows:
        if not any(x.strip() for x in r):
            continue
        if len(r) < len(header):
            r += [""] * (len(header) - len(r))
        for k, v in zip(header, r):
            if k == "text":
                out[k].append(v)
            elif k == "conf":
                out[k].append((float(v) if "." in v else int(v)) if v.strip() else -1)
            else:
                out[k].append(int(v) if v.strip() else 0)
    return out


def ocr_text(img: Image.Image, config: str) -> str:
    """The text read, like pytesseract.image_to_string."""
    text = _ocr_in_process(img, config, tsv=False)
    if text is None:
        import pytesseract

        return pytesseract.image_to_string(img, config=config)
    return text


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


OCR_WORDS_PATH = CACHE_DIR / "ocr_words.txt"
_EXTRA_OCR_WORDS = ("for", "mesos", "Scroll", "Greater", "Intermediate", "Lesser", "Chaos")


def _short_path(p: Path) -> str | None:
    """The path as Tesseract's command line (via pytesseract) can take it: quoted on
    macOS and Linux; on Windows the 8.3 short name if it has spaces (pytesseract
    keeps quotes there), None if there's no such form."""
    s = str(p)
    if os.name != "nt":
        import shlex
        return shlex.quote(s)
    if " " not in s:
        return s
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024) and " " not in buf.value:
                return buf.value
        except Exception:
            pass
    return None


def ocr_words_arg() -> str:
    """" --user-words FILE": every word of every item name, so Tesseract leans towards
    real item words ("Cutlus", "Sylvia") when a letter is unclear. Made from the item
    list (again whenever it changes); "" when there's no item list."""
    st = ocr_words_arg.state
    try:
        src = ITEMS_PATH.stat().st_mtime
    except OSError:
        return ""
    if st.get("src") != src:
        st["src"], st["arg"] = src, ""
        try:
            names = ITEMS_PATH.read_text(encoding="utf-8").splitlines()
            words = sorted({w for n in names for w in re.findall(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]|[A-Za-z]", n)}
                           | set(_EXTRA_OCR_WORDS))
            OCR_WORDS_PATH.write_text("\n".join(words) + "\n", encoding="utf-8")
            short = _short_path(OCR_WORDS_PATH)
            st["arg"] = f" --user-words {short}" if short else ""
        except OSError:
            pass
    return st["arg"]


ocr_words_arg.state = {}


# The app's own OCR model: Tesseract's English model fine-tuned on lines from the
# game's chat and shops (ocr/mfm.traineddata, trained by .github/workflows/ocr-train.yml).
# Used together with the standard English one ("mfm+eng"): on lines it wasn't trained
# on it got 1.6% of chat characters wrong instead of 5.1%, and 2.8% of shop characters
# instead of 4.4%.
OCR_MODEL = "mfm"
OCR_TESSDATA = CACHE_DIR / "tessdata"


def _tessdata_dir() -> Path | None:
    """A folder with both mfm.traineddata and eng.traineddata, or None."""
    shipped = BUNDLE / "tesseract" / "tessdata"
    if (shipped / f"{OCR_MODEL}.traineddata").exists() and (shipped / "eng.traineddata").exists():
        return shipped                      # the packaged app
    model = BUNDLE / "ocr" / f"{OCR_MODEL}.traineddata"
    if not model.exists():
        return None
    # running from source: put the model next to a copy of Tesseract's own English data
    import pytesseract

    try:
        out = subprocess.run([pytesseract.pytesseract.tesseract_cmd, "--list-langs"], capture_output=True,
                             text=True, timeout=20)
        m = re.search(r'languages in "([^"]+)"', out.stdout + out.stderr)
        eng = Path(m.group(1)) / "eng.traineddata" if m else None
        if not eng or not eng.exists():
            return None
        OCR_TESSDATA.mkdir(parents=True, exist_ok=True)
        for src in (model, eng):
            dst = OCR_TESSDATA / src.name
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
        return OCR_TESSDATA
    except (OSError, subprocess.SubprocessError):
        return None


def ocr_lang_arg(cfg: dict | None = None) -> str:
    """" --tessdata-dir DIR -l mfm+eng" when the app's own OCR model can be used (and
    isn't turned off with "trained_ocr": false in settings), else ""."""
    if cfg is None:
        cfg = ocr_lang_arg.cfg
    if not cfg.get("trained_ocr", True):
        return ""
    st = ocr_lang_arg.state
    if "arg" not in st:
        st["arg"] = ""
        d = _tessdata_dir()
        p = _short_path(d) if d else None
        if p:
            import pytesseract

            try:
                out = subprocess.run([pytesseract.pytesseract.tesseract_cmd, "--tessdata-dir", str(d), "--list-langs"],
                                     capture_output=True, text=True, timeout=20)
                if re.search(rf"^{OCR_MODEL}$", out.stdout, re.M) and re.search(r"^eng$", out.stdout, re.M):
                    st["arg"] = f" --tessdata-dir {p} -l {OCR_MODEL}+eng"
            except (OSError, subprocess.SubprocessError):
                pass
    return st["arg"]


ocr_lang_arg.state = {}
ocr_lang_arg.cfg = {}            # the settings last given to setup_tesseract


def ocr_words(img: Image.Image, cfg: dict, mode: str = "gray") -> list[list[Word]]:
    """OCR into lines of positioned words (coordinates in the original image)."""
    import pytesseract

    setup_tesseract(cfg)
    up = cfg.get("upscale", 3)
    d = ocr_data(preprocess(img, up, mode), "--psm 6" + ocr_lang_arg(cfg) + ocr_words_arg())
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
    ui_pause()
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


MAP_EVERY = 3.0                 # seconds between looks at the minimap while monitoring
MAX_PRICE = 10_000_000_000      # no price is this high (the game caps mesos far below it)


def parse_price(s: str) -> int | None:
    s = s.strip().lower().replace(" ", "")
    if s and s[-1] in "kmb":
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[s[-1]]
        try:
            n = int(float(s[:-1]) * mult)
        except (ValueError, OverflowError):
            return None
        return n if n <= MAX_PRICE else None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits and int(digits) <= MAX_PRICE else None


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
        # the quantity word ("12" or "12for"); the quantity is optional in the pattern,
        # so a line where it wasn't read only counts if a word starts with digits + "for"
        qty = m.group(1)
        q = next((w for w in ws if (qty and w.text.startswith(qty))
                  or re.match(r"\d*[il|]?f[o0ce]r" if qty else r"\d+[il|]?f[o0ce]r", w.text, re.I)), None)
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
    ui_pause()
    import pytesseract

    up = 4 if mode == "gray4" else 3
    g = ImageOps.grayscale(box).resize((box.width * up, box.height * up), Image.LANCZOS)
    if mode in ("gray", "gray4"):
        g = ImageOps.autocontrast(g)
    else:
        cut = 120 if mode == "graybin120" else 150
        g = ImageOps.autocontrast(g, cutoff=1).point(lambda p, c=cut: 0 if p < c else 255)
    setup_tesseract(cfg)
    d = ocr_data(g, "--psm 6" + ocr_lang_arg(cfg) + ocr_words_arg())
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
    g = ImageOps.autocontrast(g)
    setup_tesseract(cfg)
    text = ocr_text(ImageOps.expand(g, border=15, fill=255), "--psm 7").strip()
    _collect_name(cfg, "player", g, text)
    return re.sub(r"^[\W_]+|[\W_]+$", "", text.split()[0]) if text.split() else ""


def _collect_name(cfg: dict, kind: str, img: Image.Image, text: str) -> None:
    """With Settings → Reading → OCR training → Collect store / player names on: keep
    the picture the OCR just read, with what it read. The same shop gives the same
    picture, so it's kept once (grouped by the picture itself)."""
    OCR_COLLECT.set_from(cfg)
    if OCR_COLLECT.wants(kind):
        OCR_COLLECT.add(f"{kind}|{hashlib_md5(img.tobytes())}|0", img, text, kind)


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
    # ... and the thin line after it, when it's in there: it reads as "~" or "|"
    bp = ImageOps.grayscale(bar).load()
    for x in range(min(bar.width // 8, 40)):
        if sum(1 for y in range(bar.height) if bp[x, y] < 215) >= 0.7 * bar.height:
            bar = bar.crop((x + 2, 0, bar.width, bar.height))
            break
    up = 3
    g = ImageOps.autocontrast(ImageOps.grayscale(bar).resize((bar.width * up, bar.height * up),
                                                             Image.LANCZOS))
    setup_tesseract(cfg)
    text = ocr_text(g, "--psm 7").strip()
    _collect_name(cfg, "store", g, text)
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
    quick = difflib.SequenceMatcher(None, "", fh)   # quick_ratio never undershoots ratio
    for n in d.graded_scrolls:
        fb = d.scroll_base_fold[n]
        # whole "... Scroll" visible: compare with the whole base name; cut off
        # earlier ("Two-handed Blunt Wea…"): with the same-length start of it
        ref = fb if m else fb[:len(fh)]
        if ref == fh:
            scored.append((1.0, n))
            continue
        quick.set_seq1(ref)
        if quick.quick_ratio() < 0.9:
            continue   # can't reach 0.9 on the full comparison either
        r = difflib.SequenceMatcher(None, fh, ref).ratio()
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
    ui_pause()
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
        # a bit of the icon (its stack count) read as part of the name: "4 Meba", "Z Blue
        # Sauna Robe": a short, unsure word well to the left of the rest
        while len(name_words) > 1:
            first, nxt = name_words[0], name_words[1]
            gap = nxt.left - (first.left + first.width)
            if len(first.text) <= 3 and first.conf < 85 and gap >= 12:
                name_words = name_words[1:]
            else:
                break
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


def find_minimap(full: Image.Image, store=None, info: dict | None = None) -> list | None:
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
            # the MINI MAP strip, but no name panel: the map is switched to the view
            # without its name. (Only a WORLD-sized button in the top half: the Change
            # Channel window's CANCEL also sits under a white strip, and is bigger.)
            if info is not None and bh <= 20 and ys < H / 2:
                info["no_name"] = True
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


def _join_row_blobs(boxes: list) -> list:
    """Join boxes that sit side by side on the same row with a small gap (one button
    cut in two by something drawn over it)."""
    boxes = sorted(boxes)
    out: list = []
    for b in boxes:
        for i, a in enumerate(out):
            h = max(a[3] - a[1], b[3] - b[1])
            if (abs(a[1] - b[1]) <= 0.2 * h and abs(a[3] - b[3]) <= 0.2 * h
                    and -2 <= b[0] - a[2] <= 0.9 * h):
                out[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                break
        else:
            out.append(tuple(b))
    return out


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
    oranges = _join_row_blobs(oranges)       # the mouse pointer over CANCEL can split it in two
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


def _grey_button_mask(img: Image.Image) -> Image.Image:
    """The flat grey of the change-channel dialog's channel buttons (CH.1 ...)."""
    from PIL import ImageChops

    r, g, b = img.convert("RGB").split()
    spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b),
                                 ImageChops.darker(ImageChops.darker(r, g), b))
    lum = img.convert("L")
    return ImageChops.multiply(lum.point(lambda v: 255 if 140 <= v <= 200 else 0),
                               spread.point(lambda v: 255 if v <= 12 else 0))


def read_dialog_channel(full: Image.Image, pair: list | None = None) -> str | None:
    """The channel highlighted (blue) in the game's change-channel dialog: read from
    the button ("CH.19"), or else worked out from where it sits in the grid of
    channel buttons (8 to a row). None when it can't be told."""
    import pytesseract

    pair = pair or find_channel_dialog(full)
    if not pair:
        return None
    img = full.convert("RGB")
    px0, py0, pw, ph = pair
    # the grid of channel buttons is above the CHANGE CHANNEL / CANCEL pair
    area = (max(0, int(px0 + pw - 3.6 * pw)), max(0, int(py0 - 1.1 * pw)), px0 + pw, py0 - 2)
    if area[3] - area[1] < 2 * ph:
        return None
    sub = img.crop(area)
    blues = [b for b in _blobs(_blue_button_mask(sub), min_w=max(6, int(0.15 * pw)), min_h=max(4, int(0.5 * ph)))
             if 0.6 * ph <= b[3] - b[1] <= 1.4 * ph and 2.0 <= (b[2] - b[0]) / (b[3] - b[1]) <= 6]
    if len(blues) != 1:
        return None
    bx = blues[0]
    # 1. read it: white lettering on blue
    inset = max(2, (bx[3] - bx[1]) // 6)     # the button's light edge isn't lettering
    btn = sub.crop((bx[0] + inset, bx[1] + inset, bx[2] - inset, bx[3] - inset))
    white = btn.convert("L").point(lambda v: 0 if v >= 200 else 255)
    box = ImageOps.invert(white).getbbox()
    if box:
        white = white.crop(box)
    up = white.resize((white.width * 4, white.height * 4), Image.LANCZOS)
    try:
        text = ocr_text(ImageOps.expand(up, border=10, fill=255),
                        "--psm 7 -c tessedit_char_whitelist=CHch.0123456789").strip()
    except Exception:
        text = ""
    m = re.search(r"(\d{1,3})\s*$", text.replace(" ", ""))
    if m and 1 <= int(m.group(1)) <= 100:
        return str(int(m.group(1)))
    # 2. its place among the grey buttons: about the same size, in rows of 8
    bw_, bh_ = bx[2] - bx[0], bx[3] - bx[1]
    greys = [b for b in _blobs(_grey_button_mask(sub), min_w=max(6, int(0.15 * pw)), min_h=max(4, int(0.5 * ph)))
             if 0.75 <= (b[3] - b[1]) / bh_ <= 1.35 and 0.85 <= (b[2] - b[0]) / bw_ <= 1.2]
    if len(greys) < 3:
        return None
    centers = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in greys + [bx]]
    xs = sorted({round(c[0]) for c in centers})
    ys = sorted({round(c[1]) for c in centers})
    steps_x = [b - a for a, b in zip(xs, xs[1:]) if b - a > 0.5 * bw_]
    steps_y = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5 * bh_]
    if not steps_x:
        return None
    sx = min(steps_x)
    sy = min(steps_y) if steps_y else bh_ * 1.25
    x0_, y0_ = min(c[0] for c in centers), min(c[1] for c in centers)
    cols = {round((c[0] - x0_) / sx) for c in centers}
    if max(cols) + 1 != 8:
        return None                      # not the whole grid in view
    cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
    return str(round((cy - y0_) / sy) * 8 + round((cx - x0_) / sx) + 1)


def faded_to_black(full: Image.Image, box) -> bool:
    """Has the game faded to black here? (It does while it changes channel.)"""
    l, t, r, b = (int(v) for v in box)
    l, t = max(0, l), max(0, t)
    r, b = min(full.width, r), min(full.height, b)
    if r - l < 8 or b - t < 8:
        return False
    px = full.crop((l, t, r, b)).convert("RGB").resize((48, 24)).getdata()
    dark = sum(1 for p in px if max(p) <= 30)
    return dark >= 0.92 * len(px)


class ChannelWatch:
    """Notices a channel change: the change-channel dialog (find_channel_dialog)
    was on screen and went away, and the game then faded to black within
    FADE_SECONDS (it does when it changes channel). Closing the dialog any other
    way (CANCEL, Esc, or a change the game refuses: a restricted map, in combat)
    has no fade, so it changes nothing. One frame without the dialog doesn't end
    it: it counts as gone after `misses` checks in a row (about half a second)."""

    FADE_SECONDS = 3.0

    def __init__(self, misses: int = 2, every: float | None = None, cfg: dict | None = None):
        # how often to look: `every` seconds, or the Capture every (seconds) setting
        self.misses, self._every, self.cfg = misses, every, cfg
        self._read_memo: dict = {}
        self.seen = False
        self.gone = 0
        self.gone_at = 0.0                  # when the dialog was last seen
        self.waiting = False                # gone: waiting up to FADE_SECONDS for the fade
        self.box = None                     # where the dialog was (checked for the fade)
        self.last = 0.0
        self.picked: str | None = None      # the channel highlighted while the dialog was open
        self.channel: str | None = None     # ... when it closed: the channel you changed to
        self.cancelled = False              # set when a dialog closed with no fade; see take_cancelled()

    def take_cancelled(self) -> bool:
        """True once after a Change Channel window closed without changing channel."""
        c, self.cancelled = self.cancelled, False
        return c

    def _reset(self) -> None:
        self.seen, self.gone, self.waiting = False, 0, False

    paused = False

    def pause(self) -> None:
        """Stop noticing channel changes (e.g. while the app's channel picker is open)."""
        self.paused = True
        self._reset()

    def resume(self) -> None:
        self.paused = False
        self._reset()

    quiet_until = 0.0

    def quiet(self, seconds: float) -> None:
        """Ignore channel changes for a while (e.g. right after you picked your
        channel at launch, while the game's Change Channel window may still be open)."""
        self.quiet_until = time.monotonic() + seconds
        self._reset()

    def _off(self) -> bool:
        return self.paused or time.monotonic() < self.quiet_until

    @property
    def every(self) -> float:
        if self._every is not None:
            return self._every
        try:
            return max(0.05, float((self.cfg or {}).get("interval_seconds", 0.1)))
        except (TypeError, ValueError):
            return 0.1

    def due(self) -> bool:
        return not self._off() and time.monotonic() - self.last >= self.every

    def _changed(self) -> bool:
        self._reset()
        self.channel, self.picked = self.picked, None
        return True

    def feed(self, full: Image.Image) -> bool:
        """Check one screenshot; True once the dialog has gone and the game faded
        to black (self.channel: the channel highlighted in it, or None)."""
        now = self.last = time.monotonic()
        if self._off():
            self._reset()
            return False
        area = drawn_area(self.cfg, "channel_region", full.size)
        if area:
            try:
                full = crop(full, area)        # only where you said the window shows
            except Exception:
                pass
        pair = find_channel_dialog(full)
        if pair:
            if not self.seen:
                self.picked = None
            self.seen, self.gone, self.waiting, self.gone_at = True, 0, False, now
            x0, y0, w, h = pair
            # the dialog: the buttons' row and the channel grid above it
            self.box = (x0 + w - 3.2 * w, y0 - 8 * h, x0 + w, y0 + h)
            key = (tuple(pair), hashlib_md5(full.crop((max(0, pair[0] - 3 * pair[2]), max(0, pair[1] - pair[2]),
                                                       pair[0] + pair[2], pair[1])).resize((96, 32)).tobytes()))
            if key in self._read_memo:
                ch = self._read_memo[key]
            else:
                try:
                    if self.cfg:
                        setup_tesseract(self.cfg)   # this may be the first read of the session
                    ch = read_dialog_channel(full, pair)
                except Exception:
                    ch = None
                if len(self._read_memo) > 64:
                    self._read_memo.clear()
                self._read_memo[key] = ch
            if ch:
                self.picked = ch
            return False
        if not self.seen:
            return False
        if self.box and faded_to_black(full, self.box):
            return self._changed()            # gone, and the game faded out: a new channel
        self.gone += 1
        if now - self.gone_at > self.FADE_SECONDS and self.gone >= self.misses:
            self._reset()                     # gone with no fade: cancelled, or the game said no
            self.picked = None
            self.cancelled = True
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
        text = ocr_text(white_text_mask(part, up=4, thr=215), "--psm 7 -c tessedit_char_whitelist=0123456789")
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
    con = sqlite3.connect(INVENTORY_DB_PATH, timeout=10)
    _wal(con, "main")
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
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # everything into the main file first
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
    return re.sub(r"<([^<>]{1,3})>", _fix_map_number, ln)


_DIGIT_LOOKALIKES = str.maketrans({"/": "7", "\\": "7", "?": "7", "|": "1", "l": "1", "I": "1", "!": "1",
                                   "O": "0", "o": "0", "S": "5", "B": "8", "Z": "2", "z": "2", "g": "9"})


def _fix_map_number(m: re.Match) -> str:
    """The room number in "<7>" as the OCR gets it: "</>" (the 7's bar), "<l>", "<O>".
    Only when every character is a digit or a look-alike, so "<Area 1>" is left alone."""
    inner = re.sub(r"\s+", "", m.group(1))      # "<l O>": the camel-case split above added the space
    fixed = inner.translate(_DIGIT_LOOKALIKES)
    return f"<{fixed}>" if fixed.isdigit() else m.group(0)


def map_number_unread(name: str) -> bool:
    """True when the name has a <...> part without a number: the room or area number
    wasn't read, so the name isn't trusted (it would snap to a different map)."""
    m = re.search(r"<([^<>]*)>", name or "")
    return bool(m) and not re.search(r"\d", m.group(1))


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
    text = ocr_text(white_text_mask(img), "--psm 6")
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
    if map_number_unread(name):
        return ""        # "Free Market <?>": better no map this frame than the wrong room
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


# --------------------------------------------------------------------------- updates
# The packaged app looks for a newer release on GitHub, and can install it: the new
# app is downloaded next to this one, checked against GitHub's SHA-256, and swapped in
# (the running app is renamed to "... .old", which every system allows, and deleted
# by the new one when it starts). Then the new app is started and this one closes.

UPDATE_REPO = "jayee88/mfm-tracker"
UPDATE_CHECK_EVERY = timedelta(hours=24)


def version_tuple(v: str) -> tuple:
    """'v2610.19.004' -> (2610, 19, 4); anything unreadable -> ()."""
    try:
        return tuple(int(p) for p in str(v).strip().lstrip("vV").split("."))
    except ValueError:
        return ()


def update_platform() -> str:
    return "Windows" if sys.platform == "win32" else "macOS" if sys.platform == "darwin" else "Linux"


def latest_release(timeout: int = 15) -> dict | None:
    """The newest published release: {"version", "notes", "page", "asset"...}, or
    None when there is none. Raises on network errors."""
    raw = _get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", timeout=timeout)
    rel = json.loads(raw)
    ver = str(rel.get("tag_name", "")).lstrip("vV")
    if not version_tuple(ver):
        return None
    body = rel.get("body") or ""
    m = re.search(r"## What's new\s*(.*?)(?=^## |\Z)", body, re.S | re.M)
    notes = (m.group(1) if m else "").strip()
    notes = re.sub(r"\*\*(.+?)\*\*", r"\1", notes)
    notes = re.sub(r"^###\s*", "", notes, flags=re.M)
    ending = "-Linux.tar.gz" if update_platform() == "Linux" else f"-{update_platform()}.zip"
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").endswith(ending)), None)
    return {"version": ver, "notes": notes, "page": rel.get("html_url") or
            f"https://github.com/{UPDATE_REPO}/releases/latest",
            "asset_url": asset and asset.get("browser_download_url"),
            "asset_name": asset and asset.get("name"), "size": (asset or {}).get("size") or 0,
            "digest": (asset or {}).get("digest") or ""}


def update_newer(info: dict | None) -> bool:
    return bool(info) and version_tuple(info["version"]) > version_tuple(VERSION)


def update_target() -> Path | None:
    """What an update replaces: the .exe (Windows), the program (Linux) or the .app
    (macOS). None when running from the Python source."""
    if not FROZEN:
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for p in exe.parents:
            if p.suffix == ".app":
                return p
        return None
    return exe


def update_blocker() -> str | None:
    """Why this copy of the app can't update itself (None: it can)."""
    target = update_target()
    if target is None:
        return ("You're running the app from its Python source. Update it with git pull, "
                "or download the app from the release page.")
    if "AppTranslocation" in str(target):
        return ("macOS is running the app from a temporary read-only copy. Move "
                "Free Market Tracker to your Applications folder, open it from there, "
                "then update.")
    if not os.access(target.parent, os.W_OK):
        return (f"The app can't write to its folder ({target.parent}). Download the new "
                "version from the release page instead.")
    return None


def _old_path(target: Path) -> Path:
    return target.with_name(target.name + ".old")


def cleanup_old_update() -> None:
    """Delete the app an update replaced (left as "... .old")."""
    target = update_target()
    if target is None:
        return
    old = _old_path(target)
    try:
        if old.is_dir():
            shutil.rmtree(old)
        elif old.exists():
            old.unlink()
    except OSError:
        pass             # still in use (it's closing): next time


def download_update(info: dict, progress=None, cancel=None) -> Path:
    """Download the release file next to the app (so the swap is a rename on the
    same disk). progress(done, total) is called as it goes; cancel() -> True stops."""
    import hashlib
    import urllib.request

    target = update_target()
    work = target.parent / ".mfm-update"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    dest = work / info["asset_name"]
    req = urllib.request.Request(info["asset_url"], headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        while True:
            if cancel and cancel():
                raise InterruptedError("cancelled")
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    want = info.get("digest", "")
    if want.startswith("sha256:") and h.hexdigest() != want.split(":", 1)[1].lower():
        raise ValueError("the download doesn't match the release (SHA-256 differs); try again")
    if total and done != total:
        raise ValueError(f"the download was cut short ({done:,} of {total:,} bytes); try again")
    return dest


def install_update(archive: Path) -> list:
    """Unpack the downloaded release and swap it in for this app. Returns the command
    that starts the new app."""
    target = update_target()
    work = archive.parent
    new = work / "new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    if sys.platform == "darwin":
        # ditto keeps the app bundle's links, permissions and signature
        subprocess.run(["ditto", "-x", "-k", str(archive), str(new)], check=True,
                       capture_output=True, timeout=300)
        apps = [p for p in new.iterdir() if p.suffix == ".app"]
        if not apps:
            raise ValueError("no app in the download")
        fresh = apps[0]
    elif sys.platform == "win32":
        import zipfile
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".exe")]
            if not names:
                raise ValueError("no .exe in the download")
            fresh = new / target.name
            with z.open(names[0]) as src, open(fresh, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            m = next((m for m in t.getmembers() if m.isfile() and
                      Path(m.name).name == "Free Market Tracker"), None)
            if m is None:
                raise ValueError("no app in the download")
            fresh = new / target.name
            with t.extractfile(m) as src, open(fresh, "wb") as dst:
                shutil.copyfileobj(src, dst)
        fresh.chmod(0o755)
    old = _old_path(target)
    cleanup_old_update()
    if old.exists():
        raise OSError(f"{old} is in the way; delete it and try again")
    target.rename(old)                  # allowed while it runs
    try:
        fresh.rename(target)
    except OSError:
        old.rename(target)              # put this app back
        raise
    shutil.rmtree(work, ignore_errors=True)
    return ["open", "-n", str(target)] if sys.platform == "darwin" else [str(target)]


def start_new_app(cmd: list) -> None:
    """Start the updated app, on its own (this one is about to close)."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"    # a fresh app, not a child of this one
    for k in [k for k in env if k.startswith("_PYI_") or k == "_MEIPASS2"]:
        env.pop(k)
    kw: dict = {"env": env, "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **kw)


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
        # folded spellings, worked out once: the fuzzy scans below compare every
        # name with what was read, and re-folding 5,000 names per lookup was most
        # of their cost
        self.norm_of = {n: _norm(n) for n in names}
        self.fold_lower = [(n, fl, _counts(fl)) for n, fl in ((n, fold(n.lower())) for n in names)]
        self.fold_norm = [(n, fn, len(self.norm_of[n].split()), _counts(fn))
                          for n, fn in ((n, fold(self.norm_of[n])) for n in names)]
        self.folded = {n: f for n, f, _, _ in self.fold_norm}   # name -> fold(_norm(name))
        self.memo: dict[str, str | None] = {}
        self.suggest_memo: dict[tuple, list[str]] = {}
        import hashlib
        self.key = hashlib.sha1("\n".join(names).encode("utf-8")).hexdigest()[:16]   # which list this is
        best_match.memo = _load_review_memo(self.key)
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
        pool.update(_quick_scan(fa, ((n, f, c) for n, f, nw, c in self.fold_norm
                                     if nw == len(ta) and abs(len(n) - len(a)) <= 2), 0.7))
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
            cands.update(_quick_scan(f, ((n, fl, c) for n, fl, c in self.fold_lower
                                         if abs(len(n) - len(low)) <= 3), 0.85))
        return list(cands)


def _counts(s: str) -> dict:
    """Letter counts of a (folded) name, kept with it for _quick_scan."""
    c: dict = {}
    for ch in s:
        c[ch] = c.get(ch, 0) + 1
    return c


def _quick_scan(query: str, triples, threshold: float) -> list[str]:
    """Names whose folded spelling is at least `threshold` alike to the folded
    `query` by difflib's quick_ratio, from (name, folded, letter counts) triples.

    quick_ratio is 2 × (letters the two have in common, with repeats) / (both
    lengths), so it's computed here straight from the letter counts worked out
    once per name when the dictionary loads, instead of difflib indexing each
    name again on every lookup; names too long or short to possibly reach the
    threshold are skipped before even that. Same results as
    SequenceMatcher(None, query, folded).quick_ratio() >= threshold."""
    q = list(_counts(query).items())
    lq = len(query)
    out = []
    for n, f, counts in triples:
        lf = len(f)
        if not lf and not lq:
            out.append(n)     # difflib calls two empty strings identical
            continue
        if 2.0 * min(lf, lq) / (lf + lq) < threshold:   # even all letters in common wouldn't do
            continue
        m = 0
        for ch, k in q:
            c = counts.get(ch)
            if c:
                m += c if c < k else k
        if 2.0 * m / (lf + lq) >= threshold:
            out.append(n)
    return out


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
    key = (text, limit)
    hit = d.suggest_memo.get(key)
    if hit is not None:
        return list(hit)
    low = _norm(text.rstrip("…"))
    words = [w for w in re.findall(r"[a-z0-9']+", low) if len(w) >= 2]
    norm_of = d.norm_of
    pool = [n for n in d.names if all(w in norm_of[n] for w in words)] if words else []
    if len(pool) < limit:
        pool += [n for n in d._candidates(low) if n not in pool]
    f = fold(low)
    if len(pool) < 5:
        have = set(pool)
        pool += [n for n in _quick_scan(f, ((n, fn, c) for n, fn, _, c in d.fold_norm), 0.7)
                 if n not in have]
    folded = d.folded
    pool.sort(key=lambda n: -difflib.SequenceMatcher(None, f, folded[n][:len(f) + 8]).ratio())
    if len(d.suggest_memo) > 2000:
        d.suggest_memo.clear()
    d.suggest_memo[key] = pool[:limit]
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
best_match.saved = 0          # how many answers the file holds
REVIEW_MEMO_PATH = CACHE_DIR / "review_matches.json"


def _load_review_memo(key: str) -> dict:
    """best_match's answers from the last run, when the item list is the same one.
    Working them out for every name takes half a minute on a big database, and the
    window dragged while that ran in the background at every launch."""
    best_match.saved = 0
    try:
        d = json.loads(REVIEW_MEMO_PATH.read_text(encoding="utf-8"))
        if d.get("key") != key:
            return {}
        memo = {n: (p, float(s)) for n, (p, s) in d["memo"].items()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    best_match.saved = len(memo)
    return memo


def save_review_memo() -> None:
    """Keep best_match's answers for the next run. Truncated scroll names are left
    out: their answer comes from the stored icon, which can arrive later."""
    d = _Dictionary.cache
    if d is None or len(best_match.memo) == best_match.saved:
        return
    memo = {n: [p, s] for n, (p, s) in best_match.memo.items()
            if not (n.endswith("\u2026") or looks_like_partial_scroll(n))}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        REVIEW_MEMO_PATH.write_text(json.dumps({"key": d.key, "memo": memo}), encoding="utf-8")
        best_match.saved = len(best_match.memo)
    except OSError:
        pass


def unmatched_items(con, include_kept: bool = False) -> list[dict]:
    """Every logged item name that isn't in the meowdb list, most-seen first, with a
    suggested match: what the Review tab shows."""
    if not has_item_list():
        return []
    kept = load_kept()
    out = []
    for item, n, sold, last, lid in con.execute(
            "SELECT item, COUNT(*), SUM(COALESCE(sold, 0)), MAX(COALESCE(last_seen, ts)), "
            "(SELECT id FROM listings l2 WHERE l2.item = l1.item ORDER BY ts DESC LIMIT 1) "
            "FROM listings l1 GROUP BY item"):
        if is_known_item(item) or (item in kept and not include_kept):
            continue
        ui_pause()
        # the newest listing with this name: where it was and for how much
        raw, price, qty, shop, seller, channel = con.execute(
            "SELECT raw, price, qty, shop, seller, channel FROM listings WHERE id = ?", (lid,)).fetchone()
        pick, score = best_match(con, item)
        out.append({"item": item, "count": n, "sold": sold or 0, "last": last or "",
                    "raw": (raw or "").replace(" (slot re-read)", ""), "suggest": pick,
                    "score": score, "kept": item in kept, "price": price, "qty": qty or 1,
                    "shop": shop or "", "seller": seller or "", "channel": channel or ""})
    save_review_memo()
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
# and watched words: {"item": the words, "type": "word"} (megaphones whose message has them)
PING_PATH = CACHE_DIR / "ping.wav"


def load_watchlist() -> list[dict]:
    return _load_json(WATCH_PATH, [])


def save_watchlist(watch: list[dict]) -> None:
    WATCH_PATH.write_text(json.dumps(watch, indent=1, ensure_ascii=False), encoding="utf-8")


def watch_is_word(w: dict) -> bool:
    """A watched word (type "word"): found in megaphone messages, for things that
    can't be matched to an item. Otherwise a watched item."""
    return w.get("type") == "word"


def watched_words(watch: list[dict]) -> list[str]:
    return [w["item"].lower() for w in watch if watch_is_word(w) and w.get("item")]


def message_has_word(message: str, words: list[str]) -> bool:
    """Does a megaphone's message have one of these watched words (any case)?"""
    low = " ".join((message or "").lower().split())
    return any(w in low for w in words)


def watch_match(watch: list[dict], item: str, each: float) -> dict | None:
    """The watchlist entry this listing satisfies (item matches and, if a price cap
    is set, the per-item price is at or under it). Watched words don't count here."""
    for w in watch:
        if watch_is_word(w):
            continue
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


def table_data(con, cfg: dict, hours: int = 24, show_sold: bool = False,
               show_kept: bool = False, review: bool = True) -> dict:
    """What the app's Deals, Items and Review tables show, as plain data.

    The app works this out on a background thread (with its own connection) and
    only puts the rows on screen from the window's thread: with a thousand items
    it is thousands of queries plus the fuzzy name search for every unmatched
    name, which used to freeze the window while it ran.

    "deals": one tuple per listing seen in the last `hours` hours (sold ones only
      with `show_sold`), sorted best deal first:
      (sold, pct or 999, item, qty, each, market, verdict, (shop, seller, map,
       channel), last_seen, price, shop, seller, notes). Outliers, ignored items
      and non-deals are all still in: the app filters them as its checkboxes say.
    "items": every item ever logged (or seen in megaphones), A-Z: (item, market, source, price_stats,
      cheapest-for-sale tuple or None, npc buy price, npc sell price).
    "review": unmatched_items(con, show_kept)."""
    since = utc_ago(hours=hours)
    sold_filter = "" if show_sold else "AND COALESCE(sold, 0) = 0"
    rows = con.execute(
        "SELECT item, price, qty, shop, seller, map, channel, COALESCE(sold, 0), "
        "MAX(COALESCE(last_seen, ts)), MAX(COALESCE(notes, '')) FROM listings "
        "WHERE COALESCE(last_seen, ts) >= ? "
        f"{sold_filter} GROUP BY item, price, qty, shop, seller, map, sold",
        (since,)).fetchall()
    scored = []
    bounds: dict[str, object] = {}
    rates: dict[str, tuple] = {}     # an item's market rate, once, not once per listing
    for n_, (item, price, qty, shop, seller, mp, channel, sold, ts, notes) in enumerate(rows):
        if n_ % 50 == 0:
            ui_pause()
        each = price
        if item not in rates:
            rates[item] = market_rate(con, item, cfg)
            bounds[item] = outlier_bounds(con, item, cfg)
        mkt = rates[item][0]
        v, pct = verdict(each, mkt, cfg["deal_threshold_pct"], bounds[item])
        scored.append((sold, pct if pct is not None else 999, item, qty, each, mkt, v,
                       (shop or "", seller or "", mp or "", channel or ""), ts, price, shop, seller,
                       notes))
    # best deal first; only the fields that can be ordered (a shop or seller saved as ""
    # in one row and missing in another, or no market price, made plain sort() fail)
    scored.sort(key=lambda r: (r[0], r[1], r[2].casefold(), r[3], r[4],
                               -1 if r[5] is None else r[5], r[6] or "", r[7], r[8] or ""))
    best = cheapest(con, 24, cfg)
    items = []
    try:
        mega = megaphone_prices(con, float(cfg.get("megaphone_hours", 72) or 72))
    except sqlite3.Error:
        mega = {}
    names = {r[0] for r in con.execute("SELECT DISTINCT item FROM listings")} | set(mega)
    for item in sorted(names, key=str.casefold):
        ui_pause()
        mkt, src = rates.get(item) or market_rate(con, item, cfg)
        info = item_info(item) or {}
        st = price_stats(con, item, cfg, bounds.get(item, "auto"))
        items.append((item, mkt, src, st, best.get(item), info.get("npcBuy"), info.get("npcSell"),
                      mega.get(item) or {}))
    return {"deals": scored, "items": items,
            "review": unmatched_items(con, include_kept=show_kept) if review else []}


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
        "sold, last_seen, sold_ts, notes, shared) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, l.item, l.price, l.qty, title, seller, mp, channel, l.raw, session,
         int(l.sold), ts, ts if l.sold else None, carried_notes(con, l.item, title, seller, l.price),
         0 if SHARE_NEW else None))
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


# --------------------------------------------------------------------------- shop photos
# The shop window as it was on screen when a listing was logged, so a misread shop,
# player or item name can be checked against the real thing later. A visit keeps its
# first frame plus the ones where further rows appeared (scrolling), a few per visit.
# They're a cache (cache/shots.db): lossy WebP, ~20 KB each, purged after `shot_days`.
SHOT_QUALITY = 60
SHOTS_PER_VISIT = 4


def shots_db():
    SHOTS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SHOTS_DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS shots (session TEXT NOT NULL, ts TEXT NOT NULL, "
                "webp BLOB NOT NULL, PRIMARY KEY (session, ts))")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shots_ts ON shots(ts)")
    return con


def _webp_ok() -> bool:
    if not hasattr(_webp_ok, "ok"):
        try:
            from PIL import features
            _webp_ok.ok = bool(features.check("webp"))
        except Exception:
            _webp_ok.ok = False
    return _webp_ok.ok


def save_shot(con, session: str, ts: str, img: Image.Image) -> bool:
    """Keep the frame `img` of visit `session`, captured at `ts` (a listing's ts)."""
    import io

    n = con.execute("SELECT COUNT(*) FROM shots WHERE session = ?", (session,)).fetchone()[0]
    if n >= SHOTS_PER_VISIT:
        return False
    buf = io.BytesIO()
    if _webp_ok():
        img.convert("RGB").save(buf, "WEBP", quality=SHOT_QUALITY, method=4)
    else:   # a Pillow built without WebP (the Linux app's): JPEG, a little bigger
        img.convert("RGB").save(buf, "JPEG", quality=75, optimize=True)
    con.execute("INSERT OR IGNORE INTO shots (session, ts, webp) VALUES (?,?,?)", (session, ts, buf.getvalue()))
    con.commit()
    return True


def load_shot(session: str | None, *stamps: str | None) -> bytes | None:
    """The photo for a listing: the frame it was logged (or last seen / sold) on, by
    timestamp; otherwise the nearest earlier frame of its visit, or the visit's first."""
    if not SHOTS_DB_PATH.exists():
        return None
    con = sqlite3.connect(f"file:{SHOTS_DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        for ts in stamps:
            if ts:
                r = con.execute("SELECT webp FROM shots WHERE ts = ? ORDER BY session = ? DESC LIMIT 1",
                                (ts, session or "")).fetchone()
                if r:
                    return r[0]
        if not session:
            return None
        ts = next((t for t in stamps if t), None) or "9"
        r = con.execute("SELECT webp FROM shots WHERE session = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                        (session, ts)).fetchone() \
            or con.execute("SELECT webp FROM shots WHERE session = ? ORDER BY ts LIMIT 1", (session,)).fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def purge_shots(days: float) -> int:
    """Delete shop photos older than `days` (all of them when days <= 0)."""
    if not SHOTS_DB_PATH.exists():
        return 0
    con = shots_db()
    try:
        cur = (con.execute("DELETE FROM shots WHERE ts < ?", (utc_ago(days=days),)) if days > 0
               else con.execute("DELETE FROM shots"))
        con.commit()
        if cur.rowcount:
            con.execute("VACUUM")     # give the space back to the disk
        return cur.rowcount
    finally:
        con.close()


def shots_size() -> int:
    """Bytes used by the shop photos (with their write-ahead log, if any)."""
    return sum(p.stat().st_size for p in (SHOTS_DB_PATH, SHOTS_DB_PATH.with_name(SHOTS_DB_PATH.name + "-wal"))
               if p.exists())


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
    """Channel badges in a crop of the chat: white boxes with red text, top to bottom.
    The box is translucent, so it takes a tint from the line it's on (over a teal
    line it's about (231, 249, 246), over a yellow one (248, 240, 216)): white-ish is
    enough, the red text inside tells a badge from anything else that light."""
    white = _rgb_mask(img, (205, 205, 205), (255, 255, 255))
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


_DIGITISH = str.maketrans({"/": "7", "o": "0", "O": "0", "D": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                           "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"})


def parse_chat_line(text: str) -> tuple[str | None, str, str] | None:
    """'[04:01] Player : message' -> ("04:01", "Player", "message"). A chat without
    times (the game can show them or not) gives 'Player : message' -> (None, ...)."""
    # (the trained OCR model sometimes reads a 7 as "/", or a stray digit before the "]")
    m = re.search(r"([0-9oOlIDSZz|/]{2})\s*[:;.\-,]\s*([0-9oOlIDSZz|/]{2})[0-9/]?\s*[\]\)}|lI1J]?\s*(.+)$", text)
    if m:
        hh, mm = (g.translate(_DIGITISH) for g in m.group(1, 2))
        if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
            return None
        time, rest = f"{hh}:{mm}", m.group(3)
    else:
        # no time: a player name (letters and digits, one word) then the ':'
        m = re.match(r"\s*([A-Za-z0-9][\w().'\-]{1,15})\s*:(.+)$", text)
        if not m:
            return None
        time, rest = None, m.group(1) + ":" + m.group(2)
    if ":" not in rest:
        return None
    name, msg = rest.split(":", 1)
    # the ':' after the badge, read twice, and specks around it
    msg = re.sub(r"^\s*(?:[^\w\s\[@#$(]{1,3}\s+)*[^\w\s\[@#$(]*:[^\w\s\[@#$(]*\s*", "", msg)
    # player names have no spaces, but OCR sometimes splits one ("OT COT C")
    name = re.sub(r"[^\w().'\-]+", "", "".join(name.split()))
    if name.endswith(")") and "(" not in name:
        name = name[:-1]
    return time, name, msg.strip()


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
# "S>" and "B>" as the OCR sometimes reads them too: the S as a 5, the B as an 8
SELL_TERMS = re.compile(r"(?<![A-Za-z])(S\s?>|(?<![0-9])5\s?>|S=(?=\S)|C\s?/\s?O\b|WTS\b|sell(?:s|ing)?\b|have\b|trad(?:e|es|ing)\b)", re.I)
BUY_TERMS = re.compile(r"(?<![A-Za-z])(B\s?>|(?<![0-9])8\s?>|B=(?=\S)|WTB\b|buy(?:s|ing)?\b|looking\s+for\b|LF\b|need\b|want\b)",
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
    d = ocr_data(ImageOps.expand(img, border=border, fill=255), "--psm 7" + ocr_lang_arg() + ocr_words_arg())
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
    text = ocr_text(ImageOps.expand(left.crop((0, 0, end, H)), border=12, fill=255),
                    "--psm 7 -c tessedit_char_whitelist=0123456789:[]").strip()
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


LINE_PAD_BELOW = 0.0   # a chat line is cut this much (of a line's height) below its own
                       # band, so letters that hang below it (g, y, p, q) aren't cut off


def read_megaphones(chat: Image.Image, cfg: dict, memo: dict | None = None, con=None, unit: float | None = None,
                    channel: str | None = None, marks: list | None = None, stats: dict | None = None) -> list[dict]:
    """Megaphones in a crop of the chat messages: [{time, player, channel, message,
    color}]. `unit` is about one line's height (from find_chat); `channel` is used for
    megaphones without a channel badge (the ones in your own channel). The boxes of
    the lines read are added to `marks` as (box, color name)."""
    ui_pause()
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
    OCR_COLLECT.set_from(cfg)

    # the lines on screen, top to bottom
    lines = []
    k = -1
    while True:
        y0 = int(round(phase + k * pitch))
        y1 = y0 + int(round(pitch)) + int(round(LINE_PAD_BELOW * pitch))
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
            # megaphones without "[hh:mm]" at the start: the game's chat timestamps are off
            if stats is not None:
                k = "no_time" if m.pop("untimed", False) else "timed"
                stats[k] = stats.get(k, 0) + 1
            else:
                m.pop("untimed", None)
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
        confs = [c for w, x, c in words if c >= 0]
        split = left.width + gap // 2
        head = " ".join(w for w, x, c in words if x < split)
        tail = _drop_junk([(w, c) for w, x, c in words if x >= split])
        parsed = parse_chat_line(head + " : " + tail)
    else:
        left = _chat_text(_text_band(row.crop((lx0 + 1, 0, lx1, row.height))))
        words = _ocr_words(left)
        confs = [c for w, x, c in words if c >= 0]
        parsed = parse_chat_line(_drop_junk([(w, c) for w, x, c in words]))
    if not parsed:
        return None
    hhmm, name, message = parsed
    # the time: read again from the picture; a chat without times (the game's chat
    # timestamps are off) gets the time now
    hhmm = _read_chat_time(left, u * 3) or hhmm
    untimed = not hhmm
    hhmm = hhmm or datetime.now(timezone.utc).strftime("%H:%M")
    # names are letters and digits only; the font's g is often read as q
    name = re.sub(r"q(?!u)", "g", re.sub(r"[^A-Za-z0-9]", "", name))
    grp = f"{datetime.now():%Y%m%d}|{hhmm}|{fold(name)}"
    OCR_COLLECT.add(grp + "|0", line if b else left, " ".join(w for w, x, c in words))
    for n, im in enumerate(extra, 1):
        txt_img = _chat_text(im)
        ws = _ocr_words(txt_img)
        OCR_COLLECT.add(f"{grp}|{n}", txt_img, " ".join(w for w, x, c in ws))
        confs += [c for w, x, c in ws if c >= 0]
        t = _drop_junk([(w, c) for w, x, c in ws])
        message = _join_wrapped(message, t)
    # how sure the OCR was of this read (0-100): the average of its words
    conf = round(sum(confs) / len(confs), 1) if confs else 0.0
    return {"time": hhmm, "player": name, "channel": badge_channel(chat, b) if b else None,
            "message": fix_megaphone_words(clean_megaphone(message), con), "conf": conf,
            "untimed": untimed}


# --------------------------------------------------------------------------- OCR training lines
# With "Collect chat lines for OCR training" on, every megaphone line image the OCR reads
# is kept with what was read (table ocr_lines); "Collect store names" and "Collect player
# names" do the same for shop titles and shop owners' names (kind store / player). The same line is read again on every
# frame it's on screen (and the chat is see-through, so each image differs a little):
# when those reads agree, that's the line's text ("auto"); when they don't, it waits in
# Settings → Reading → OCR training → Check lines for you to fix ("unsure"). Checked lines go to
# the Shared Database server (mfm-server export-ocr), for training the app's OCR model.

OCR_PER_LINE = 8          # images kept per line while it's being read
OCR_KEEP_PER_LINE = 3     # ... and once settled (the misread ones first: they teach the most)
OCR_LINES_MAX = 2500      # images kept here in all (about 45 MB)
OCR_SETTLE_SECONDS = 120  # a line is settled this long after it was last read
OCR_AGREE = 3             # reads that must agree (and 3 in 4 of all reads)


class _OcrCollector:
    """Takes line images from the OCR thread and saves them (its own connection)."""

    def __init__(self):
        self.on = False           # chat lines (megaphones)
        self.stores = False       # shop titles
        self.players = False      # shop owners' names
        self._local = threading.local()

    def wants(self, kind: str) -> bool:
        return {"chat": self.on, "store": self.stores, "player": self.players}.get(kind, False)

    def set_from(self, cfg: dict) -> None:
        self.on = bool(cfg.get("ocr_collect"))
        self.stores = bool(cfg.get("ocr_collect_stores"))
        self.players = bool(cfg.get("ocr_collect_players"))

    def add(self, grp: str, img: Image.Image, read: str, kind: str = "chat") -> None:
        """Hand a picture over to be saved, on a thread of its own: reading it again with
        the other models takes two more OCR runs, which mustn't slow the reading down.
        When it falls behind, pictures are dropped (there'll be others)."""
        if not self.wants(kind) or img is None or img.width < 20 or not read.strip():
            return
        import queue as _q
        if getattr(self, "_queue", None) is None:
            with _OCR_COLLECT_LOCK:
                if getattr(self, "_queue", None) is None:
                    self._queue = _q.Queue(maxsize=100)
                    threading.Thread(target=self._worker, daemon=True, name="mfm-ocr-collect").start()
        try:
            self._queue.put_nowait((grp, img, read, kind))
        except _q.Full:
            pass

    def flush(self) -> None:
        """Wait until every picture handed over is saved."""
        if getattr(self, "_queue", None) is not None:
            self._queue.join()

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._save(*job)
            finally:
                self._queue.task_done()

    def _save(self, grp: str, img: Image.Image, read: str, kind: str) -> None:
        try:
            con = getattr(self._local, "con", None)
            if con is None:
                con = self._local.con = db()
            n = con.execute("SELECT COUNT(*) FROM ocr_lines WHERE grp = ?", (grp,)).fetchone()[0]
            status = con.execute("SELECT status FROM ocr_lines WHERE grp = ? LIMIT 1", (grp,)).fetchone()
            # a store or player name is the same picture every time (the shop window isn't
            # see-through, unlike the chat): one image of it is enough
            if n >= (OCR_PER_LINE if kind == "chat" else 1) or (status and status[0] != "new"):
                return
            import io
            buf = io.BytesIO()
            img.convert("L").save(buf, "PNG", optimize=True)
            con.execute("INSERT INTO ocr_lines (grp, ts, png, read, alts, kind) VALUES (?,?,?,?,?,?)",
                        (grp, utc_now(), buf.getvalue(), " ".join(read.split()), json.dumps(self._alts(img)),
                         kind))
            con.commit()
        except Exception:
            pass


    @staticmethod
    def _alts(img: Image.Image) -> list[str]:
        """The same image read by the trained model alone and by the standard English
        one: reads that don't share all their mistakes, so when the standard one
        agrees with a trained one, that's very likely the line's text."""
        import pytesseract

        arg = ocr_lang_arg()
        if f"-l {OCR_MODEL}+eng" not in arg:
            return []
        out = []
        for lang in (OCR_MODEL, "eng"):
            try:
                t = ocr_text(ImageOps.expand(img, border=12, fill=255),
                             "--psm 7" + arg.replace(f"{OCR_MODEL}+eng", lang))
            except Exception:
                t = ""
            out.append(" ".join(t.split()))
        return out


_OCR_COLLECT_LOCK = threading.Lock()
OCR_COLLECT = _OcrCollector()


def plausible_ocr_text(text: str) -> bool:
    """False for a read with the OCR's usual slips in it, even when the models agree
    on it (they share some): g read as q ("smeqa"), a stray "|" or "?", "=" for ">"
    ("B= ..."), or an item in [brackets] that isn't on the item list."""
    if re.search(r"q(?!u)", text, re.I) or "|" in text:
        return False
    if re.search(r"(^|\s)[?!.,:;]+(\s|$)", text.replace(" : ", " ")):
        return False
    if re.search(r"(^|[\s:])[SBTsbt]\s?=", text):
        return False
    if has_item_list():
        for name in re.findall(r"\[([^\]]+)\]", text):
            if not is_known_item(name):
                return False
    return True


def settle_ocr_lines(con, now_iso: str | None = None) -> dict:
    """Lines no longer being read get their text: the read most images agree on
    ("auto"), or the likeliest one, to be checked ("unsure"). Extra images are
    dropped, and the oldest beyond OCR_LINES_MAX. Returns counts by status."""
    from collections import Counter

    if now_iso is not None:
        OCR_COLLECT.flush()              # (tests: everything read so far, saved)
    cutoff = utc_ago(seconds=OCR_SETTLE_SECONDS) if now_iso is None else now_iso
    grps = [g for (g,) in con.execute("SELECT grp FROM ocr_lines WHERE status = 'new' GROUP BY grp "
                                      "HAVING MAX(ts) < ?", (cutoff,))]
    for g in grps:
        rows = con.execute("SELECT id, read, alts FROM ocr_lines WHERE grp = ? ORDER BY id", (g,)).fetchall()
        votes = Counter(r for _, r, _ in rows if r)
        if not votes:
            con.execute("DELETE FROM ocr_lines WHERE grp = ?", (g,))
            continue
        top, n = votes.most_common(1)[0]
        status = "auto" if n >= OCR_AGREE and n >= 0.75 * len(rows) else "unsure"
        if status == "unsure":
            # or: the standard model read an image exactly as a trained one did (not for
            # a short wrapped piece of a megaphone, like "olls dm": too little to tell)
            for _, r, alts in rows:
                try:
                    trained_alone, standard = (json.loads(alts or "[]") + ["", ""])[:2]
                except ValueError:
                    continue
                if standard and standard in (r, trained_alone) and (g.endswith("|0") or len(standard) >= 12):
                    top, status = standard, "auto"
                    break
        if status == "unsure" and g.startswith("player|") and re.fullmatch(r"[A-Za-z0-9]{3,13}", top) \
                and con.execute("SELECT 1 FROM megaphones WHERE player = ? LIMIT 1", (top,)).fetchone():
            status = "auto"     # the same name was read in a megaphone (another picture, another font size)
        if status == "auto" and not plausible_ocr_text(top):
            status = "unsure"
        rows = [(i, r) for i, r, _ in rows]
        keep = sorted(rows, key=lambda r: (r[1] == top, r[0]))[:OCR_KEEP_PER_LINE]
        ids = {i for i, _ in keep}
        con.executemany("DELETE FROM ocr_lines WHERE id = ?", [(i,) for i, _ in rows if i not in ids])
        con.execute("UPDATE ocr_lines SET text = ?, status = ? WHERE grp = ?", (top, status, g))
    total = con.execute("SELECT COUNT(*) FROM ocr_lines").fetchone()[0]
    if total > OCR_LINES_MAX:
        # the oldest go first: skipped, then sent, then the rest
        con.execute("DELETE FROM ocr_lines WHERE id IN (SELECT id FROM ocr_lines ORDER BY "
                    "CASE WHEN status = 'skip' THEN 0 WHEN sent = 1 THEN 1 ELSE 2 END, id LIMIT ?)",
                    (total - OCR_LINES_MAX,))
    con.commit()
    return ocr_line_counts(con)


def ocr_line_counts(con) -> dict:
    """Lines (not images) by status, and how many images were sent."""
    out = {s: n for s, n in con.execute("SELECT status, COUNT(DISTINCT grp) FROM ocr_lines GROUP BY status")}
    out["sent"] = con.execute("SELECT COUNT(*) FROM ocr_lines WHERE sent = 1").fetchone()[0]
    return out


def set_ocr_line(con, grp: str, text: str | None, status: str) -> None:
    """You checked a line: its text (status "fixed"), or "skip" to leave it out."""
    if text is not None:
        con.execute("UPDATE ocr_lines SET text = ?, status = ?, sent = 0 WHERE grp = ?",
                    (" ".join(text.split()), status, grp))
    else:
        con.execute("UPDATE ocr_lines SET status = ?, sent = 0 WHERE grp = ?", (status, grp))
    con.commit()


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


def megaphones_alike(a_player, a_channel, a_msg, b_player, b_channel, b_msg) -> bool:
    """Two reads of the same megaphone: nearly the same message, and the same player
    (as far as the spelling goes) or channel. (Times are compared by the caller.)"""
    na, nb = re.sub(r"\W+", "", (a_msg or "").lower()), re.sub(r"\W+", "", (b_msg or "").lower())
    alike_msg = na == nb or (na and nb and (na.startswith(nb) or nb.startswith(na))) or \
        difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85
    fa, fb = fold(a_player or ""), fold(b_player or "")
    alike_name = fa == fb or difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.75
    same_ch = bool(a_channel and b_channel and str(a_channel) == str(b_channel))
    return bool(alike_msg and (alike_name or same_ch))


def find_megaphone(con, ts: str, player: str, channel, message: str, minutes: int = 2):
    """A stored megaphone that is this one: (id, player, channel, message, conf, uid) or None."""
    t = datetime.strptime(ts, UTC_FMT)
    lo, hi = ((t + timedelta(minutes=d)).strftime(UTC_FMT) for d in (-minutes, minutes))
    for row in con.execute("SELECT id, player, channel, message, conf, uid FROM megaphones "
                           "WHERE ts BETWEEN ? AND ?", (lo, hi)).fetchall():
        if megaphones_alike(row[1], row[2], row[3], player, channel, message):
            return row
    return None


def save_megaphones(con, found: list[dict], now: datetime | None = None) -> int:
    """Store megaphones that aren't stored yet; returns how many are new.

    The same megaphone is read again every second while it's on screen (and moves up
    as the chat scrolls), sometimes with a letter read differently. A read within two
    minutes of a stored one, with nearly the same message and the same player (as
    far as the spelling goes) or channel, is that one: only the read the OCR was most
    sure of (its confidence) is kept."""
    new = 0
    seen = utc_now()
    names = _player_names(con)
    for f in found:
        player = re.sub(r"[^A-Za-z0-9]", "", f.get("player") or "")
        if len(player) < 2 or not f.get("message"):
            continue
        player = names.get(fold(player), player)
        ts = chat_time_utc(f["time"], now)
        conf = float(f.get("conf") or 0)
        same = find_megaphone(con, ts, player, f.get("channel"), f["message"])
        if same:
            mid, p0, ch0, msg0, conf0, uid = same
            if conf > (conf0 or 0):
                con.execute("UPDATE megaphones SET player = ?, message = ?, kind = ?, conf = ?, "
                            "channel = COALESCE(?, channel) WHERE id = ?",
                            (player, f["message"], megaphone_kind(f["message"]), conf, f.get("channel"), mid))
            elif f.get("channel") and not ch0:
                con.execute("UPDATE megaphones SET channel = ? WHERE id = ?", (f["channel"], mid))
            continue
        con.execute("INSERT INTO megaphones (ts, seen, player, channel, message, kind, color, conf) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ts, seen, player, f.get("channel"), f["message"], megaphone_kind(f["message"]),
                     f.get("color"), conf))
        new += 1
    con.commit()
    return new


def delete_megaphones(con, ids) -> int:
    """Delete megaphones (and what was read from them). Shared ones are remembered, so
    they aren't copied back from the Shared Database."""
    ids = list(ids)
    for i in ids:
        row = con.execute("SELECT uid, ts FROM megaphones WHERE id = ?", (i,)).fetchone()
        if row and row[0]:
            con.execute("INSERT OR IGNORE INTO megaphones_deleted (uid, ts) VALUES (?, ?)", row)
    con.executemany("DELETE FROM megaphones WHERE id = ?", [(i,) for i in ids])
    con.executemany("DELETE FROM megaphone_offers WHERE megaphone_id = ?", [(i,) for i in ids])
    con.commit()
    return len(ids)


def apply_purge(con, what: str, field: str, start: str | None, end: str, when: str | None = None) -> int:
    """Delete what the Shared Database's server purged (mfm-server purge) from this
    app's own data too: `what` (prices, megaphones or aliases) whose time `field`
    (seen: last seen / changed, first: first stored) is >= start (if given) and < end.
    Aliases are marked deleted, like on the server. Returns how many."""
    if what == "prices":
        col = "COALESCE(last_seen, ts)" if field == "seen" else "ts"
        table = "listings"
    elif what == "megaphones":
        col, table = "ts", "megaphones"
    elif what in ("aliases", "nicknames"):       # (servers before 2.6.0 say nicknames)
        col = "COALESCE(updated, added)" if field == "seen" else "COALESCE(added, updated)"
        table = "aliases"
    else:
        return 0
    where, args = f"{col} < ?", [end]
    if start:
        where, args = where + f" AND {col} >= ?", args + [start]
    if what in ("aliases", "nicknames"):
        rows = con.execute(f"SELECT key, item FROM aliases WHERE COALESCE(deleted, 0) = 0 AND {where}",
                           args).fetchall()
        for key, item in rows:
            con.execute("UPDATE aliases SET deleted = 1, updated = ?, by = 'server admin', shared_sig = ? "
                        "WHERE key = ?", (when or utc_now(), json.dumps([item or "", 1]), key))
        n = len(rows)
    else:
        n = con.execute(f"DELETE FROM {table} WHERE {where}", args).rowcount
        if what == "megaphones":
            con.execute("DELETE FROM megaphone_offers WHERE megaphone_id NOT IN (SELECT id FROM megaphones)")
    con.commit()
    return n


def purge_megaphones(con, hours: float) -> int:
    """Delete megaphones older than `hours`."""
    n = con.execute("DELETE FROM megaphones WHERE ts < ?", (utc_ago(hours=hours),)).rowcount
    con.execute("DELETE FROM megaphones_deleted WHERE ts < ?", (utc_ago(hours=max(hours, 96)),))
    con.execute("DELETE FROM megaphone_offers WHERE megaphone_id NOT IN (SELECT id FROM megaphones)")
    con.commit()
    return n


# --------------------------------------------------------------------------- megaphone offers
# What a megaphone sells or buys: "S> Black cat beanie 520k, B> Fondue Scepter" is two
# offers, each with an item (when it can be told) and a price (when one is given).
# Players' shorthand ("kumbis") is learned in Review as aliases, which the Shared
# Database shares, so the whole group only teaches each one once.

def alias_key(phrase: str) -> str:
    """How a phrase is compared: lower case, letters and digits only."""
    return " ".join(re.sub(r"[^a-z0-9%]+", " ", (phrase or "").lower()).split())


def alias_version(con) -> str:
    """Changes whenever any alias does (so megaphones get read again)."""
    rows = con.execute("SELECT key, item FROM aliases WHERE COALESCE(deleted, 0) = 0 ORDER BY key").fetchall()
    return hashlib_md5("\n".join(f"{k}={i or ''}" for k, i in rows).encode())[:12]


def load_aliases(con) -> dict[str, str]:
    """{phrase key: item} (item "" = not an item)."""
    return {k: (i or "") for k, i in con.execute("SELECT key, item FROM aliases WHERE COALESCE(deleted, 0) = 0")}


def set_alias(con, phrase: str, item: str | None, by: str | None = None) -> str:
    """Teach an alias: `item` None or "" = the phrase isn't an item. Returns its key."""
    key = alias_key(phrase)
    if not key:
        return key
    con.execute("INSERT INTO aliases (key, item, deleted, added, by, updated) VALUES (?,?,0,?,?,?) "
                "ON CONFLICT (key) DO UPDATE SET item = excluded.item, deleted = 0, updated = excluded.updated",
                (key, item or "", utc_now(), by, utc_now()))
    con.commit()
    return key


def delete_alias(con, key: str) -> None:
    con.execute("UPDATE aliases SET deleted = 1, updated = ? WHERE key = ?", (utc_now(), key))
    con.commit()


def _item_index() -> dict[str, str]:
    """Item names by their phrase key ("ink and wash thought bubble ring" -> name)."""
    d = _dict()
    if not d or not d.names:
        return {}
    idx = getattr(d, "offer_index", None)
    if idx is None:
        idx = {}
        for n in d.names:
            idx.setdefault(alias_key(n), n)
        d.offer_index = idx
    return idx


_SIDE_RE = re.compile(r"(?<![A-Za-z])(?:(?P<s>S\s?[>=]+|WTS\b|sell(?:s|ing)?\b)"
                      r"|(?P<b>B\s?[>=]+|WTB\b|buy(?:s|ing)?\b|LF\b|looking\s+for\b))", re.I)
_PRICE_RE = re.compile(r"(?<![\w.])(\d{1,4}(?:[.,]\d{1,3})?)\s*(k|m|mil|mill|b|bil)(?![a-z])"
                       r"|(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\w%])", re.I)
_NO_PRICE_RE = re.compile(r"\b(pmo|pm me|pm|offers?|best offer|highest bid|bid)\b", re.I)
# words that aren't part of an item's name
_FILLER_RE = re.compile(
    r"(?<![A-Za-z])(?:c\s?/\s?o|cfo|ea|each|per|for|pmo|pm|me|offers?|price|best|highest|bid|win|cheap|"
    r"fm\d*|ch\s?\d+|channel\s?\d+|x\s?\d+|\d+\s?x|@+|nx|obo|or|trade|trading|the|a|an|my|your|"
    r"plz|pls|please|anyone|all|below|average|selling|buying|have|need|want)(?![A-Za-z])", re.I)
_STAT_RE = re.compile(r"(?<![A-Za-z])(?:lv\.?\s?\d+|lvl\s?\d+|\+\d+|att\s?\d+(?![\d%])(?:\(\S*\))?|"
                      r"\d+\s?(?:str|dex|int|luk|att|matk|acc|avoid|speed|jump|slots?))(?![A-Za-z])", re.I)


def _price_of(text: str) -> tuple[int | None, str]:
    """The price in a piece of a megaphone, and a note ("c/o", "each", "offer")."""
    note = []
    if re.search(r"\b(c\s?/\s?o|cfo)\b", text, re.I):
        note.append("c/o")
    if re.search(r"\b(ea|each|per)\b", text, re.I):
        note.append("each")
    price = None
    for m in _PRICE_RE.finditer(text):
        if m.group(1):
            n = float(m.group(1).replace(",", "."))
            mult = {"k": 1e3, "m": 1e6, "mil": 1e6, "mill": 1e6, "b": 1e9, "bil": 1e9}[m.group(2).lower()]
            price = int(round(n * mult))
        else:
            price = int(m.group(3).replace(",", ""))
        if price > MAX_PRICE:
            price = None              # a misread run of digits, not a price (and too big to store)
    if price is None and _NO_PRICE_RE.search(text):
        note.append("offer")
    return price, ", ".join(note)


WORDS_PREFIX = "="          # an alias whose value starts with this stands for words, not an item
_OPTIONAL_WORDS = {"scroll", "for", "of", "the", "a", "an"}


def _plain(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _token_index() -> dict[str, list[tuple[str, frozenset]]]:
    """Item names by each of their words: {"gloves": [(name, its words), ...]}."""
    d = _dict()
    if not d or not d.names:
        return {}
    idx = getattr(d, "token_index", None)
    if idx is None:
        idx = {}
        for n in d.names:
            words = frozenset(_plain(w) for w in alias_key(n).split()) - _OPTIONAL_WORDS
            if len(words) >= 2:
                for w in words:
                    idx.setdefault(w, []).append((n, words))
        d.token_index = idx
    return idx


# Common shorthand in megaphones, put in once as word aliases (they can be changed or
# removed in Edit aliases like any other; see seed_default_words).
DEFAULT_WORDS = {
    "att": "attack", "atk": "attack", "attk": "attack",
    "matk": "magic attack", "matt": "magic attack", "m att": "magic attack", "m atk": "magic attack",
    "acc": "accuracy", "avoid": "evasion", "avoi": "evasion", "avo": "evasion", "eva": "evasion",
    "spd": "speed", "mdef": "magic def", "m def": "magic def", "wdef": "weapon def", "w def": "weapon def",
    "overall": "overall armor", "ovr": "overall armor", "top": "topwear", "bottom": "bottomwear",
    "bottoms": "bottomwear", "btm": "bottomwear", "pants": "bottomwear", "helm": "hat", "helmet": "hat",
    "1h": "one handed", "2h": "two handed", "ohs": "one handed sword", "ths": "two handed sword",
    "oha": "one handed axe", "tha": "two handed axe", "ohbw": "one handed blunt weapon",
    "thbw": "two handed blunt weapon", "bw": "blunt weapon", "xbow": "crossbow", "cbow": "crossbow",
}
_SEED_TIME = "2000-01-01T00:00:00Z"     # older than any real change, so those always win


def seed_default_words(con) -> int:
    """Put in DEFAULT_WORDS that aren't there (or weren't removed) yet."""
    have = {r[0] for r in con.execute("SELECT key FROM aliases")}
    rows = [(k, WORDS_PREFIX + v, _SEED_TIME, "app", _SEED_TIME) for k, v in DEFAULT_WORDS.items() if k not in have]
    if rows:
        con.executemany("INSERT INTO aliases (key, item, deleted, added, by, updated) VALUES (?,?,0,?,?,?)", rows)
        con.commit()
    return len(rows)


def split_aliases(aliases: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """({phrase: item}, {phrase: words}): aliases for items, and ones that stand for words
    ("60%" = "intermediate", "att" = "attack") used to read any item's name."""
    items, words = {}, {}
    for k, v in aliases.items():
        if v.startswith(WORDS_PREFIX):
            words[k] = alias_key(v[len(WORDS_PREFIX):])
        else:
            items[k] = v
    return items, words


def apply_words(key: str, words: dict[str, str]) -> str:
    """A phrase key with the word aliases put in ("60% gloves att" -> "intermediate
    gloves attack"); the longest match wins."""
    if not words:
        return key
    toks, out, i = key.split(), [], 0
    longest = max(len(k.split()) for k in words)
    while i < len(toks):
        for n in range(min(longest, len(toks) - i), 0, -1):
            sub = " ".join(toks[i:i + n])
            if sub in words:
                if words[sub]:
                    out.append(words[sub])
                i += n
                break
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out)


def _by_words(key: str) -> str | None:
    """The item whose every word is in the phrase, in any order ("intermediate gloves
    attack" -> "Gloves Attack Scroll: Intermediate"); the one that uses the most words,
    and only if there's exactly one such."""
    have = {_plain(w) for w in key.split()}
    idx = _token_index()
    best, best_n, tie = None, 0, False
    seen = set()
    for w in have:
        for name, words in idx.get(w, ()):
            if name in seen:
                continue
            seen.add(name)
            if words <= have:
                if len(words) > best_n:
                    best, best_n, tie = name, len(words), False
                elif len(words) == best_n and name != best:
                    tie = True
    return None if tie else best


def _find_item(phrase: str, links: list[str], aliases: dict[str, str],
               words: dict[str, str] | None = None) -> tuple[str | None, str]:
    """(item, how) for a phrase: how = "link", "alias", "name", "words", "ignored" or ""."""
    key = alias_key(phrase)
    for link in links:
        lk = alias_key(link)
        if lk and (lk in key or key in lk or not key):
            idx = _item_index()
            item = idx.get(lk)
            if item is None and has_item_list():
                sugg = suggest_items(link, 1)
                item = sugg[0] if sugg and difflib.SequenceMatcher(
                    None, fold(alias_key(sugg[0])), fold(lk)).ratio() >= 0.8 else None
            return (item or link), "link"
    if not key:
        return None, ""
    if key in aliases:
        return (aliases[key] or None), ("alias" if aliases[key] else "ignored")
    idx = _item_index()
    # as written, then with the word aliases put in
    keys = [key]
    swapped = apply_words(key, words or {})
    if swapped != key:
        keys.append(swapped)
    for k0 in keys:
        how = "name" if k0 == key else "words"
        if k0 != key and k0 in aliases:
            return (aliases[k0] or None), ("alias" if aliases[k0] else "ignored")
        for k in (k0, " ".join(_plain(w) for w in k0.split())):
            if k in idx:
                return idx[k], how                     # "mithril polearms" -> Mithril Polearm
        ws = k0.split()
        # the longest run of words that is an alias or an item's name
        for n in range(min(len(ws), 8), 0, -1):
            for i in range(len(ws) - n + 1):
                sub = " ".join(ws[i:i + n])
                if sub in aliases and aliases[sub] and (n > 1 or len(sub) >= 3):
                    return aliases[sub], "alias"
                if sub in idx and (n > 1 or len(sub) >= 4):
                    return idx[sub], how
                single = " ".join(_plain(w) for w in ws[i:i + n])
                if single != sub and single in idx and (n > 1 or len(single) >= 4):
                    return idx[single], how
    # every word of one item's name, in any order (after the word aliases)
    item = _by_words(keys[-1])
    if item:
        return item, "words"
    return None, ""


def parse_offers(message: str, kind: str | None, aliases: dict[str, str]) -> list[dict]:
    """The offers in a megaphone: [{side, phrase, key, item, how, price, note}]."""
    aliases, words = split_aliases(aliases)
    links = re.findall(r"\[([^\[\]]{2,60})\]", message or "")
    text = re.sub(r"\[([^\[\]]{2,60})\]", r" \1 ", message or "")
    text = re.sub(r"\bc\s?/\s?o\b", "c_o", text, flags=re.I)        # don't split "c/o"
    default = kind if kind in ("sell", "buy") else "sell"
    # pieces by side: "S> ... B> ..." (no marker: the megaphone's own kind)
    pieces, pos, side = [], 0, default
    for m in _SIDE_RE.finditer(text):
        if text[pos:m.start()].strip():
            pieces.append((side, text[pos:m.start()]))
        side = "sell" if m.group("s") else "buy"
        pos = m.end()
    if text[pos:].strip():
        pieces.append((side, text[pos:]))
    offers = []
    for side, piece in pieces:
        chunks = [c for c in re.split(r",\s+|;\s*|\s+/\s+|(?<=[A-Za-z]{2})/(?=[A-Za-z]{2})|\s+&\s+|\s+and\s+|"
                                      r"\s*\|\s*|\s+\+\s+", piece, flags=re.I) if c and c.strip()]
        parsed = []
        for c in chunks:
            c = c.replace("c_o", "c/o")
            # scroll percentages: "60 %", "60 percent", "60°/o" -> "60%"; "60%atk" -> "60% atk"
            c = re.sub(r"(?<![\d.])(\d{1,3})\s*(?:%|percent\b|pct\b|°/o|o/o)", r"\1%", c, flags=re.I)
            c = re.sub(r"(\d%)(?=[A-Za-z])", r"\1 ", c)
            price, note = _price_of(c)
            phrase = _PRICE_RE.sub(" ", c)
            phrase = _STAT_RE.sub(" ", phrase)
            phrase = _FILLER_RE.sub(" ", phrase)
            phrase = re.sub(r"[^\w%'\- ]+", " ", phrase)
            phrase = " ".join(w for w in phrase.split() if not w.isdigit()).strip(" -'")
            parsed.append([phrase, price, note])
        # "Angelwings and Slime shirt 50k each": a piece without a price takes the next one's
        for i in range(len(parsed) - 2, -1, -1):
            if parsed[i][1] is None and parsed[i + 1][1] is not None and "each" in parsed[i + 1][2]:
                parsed[i][1], parsed[i][2] = parsed[i + 1][1], parsed[i + 1][2]
        for phrase, price, note in parsed:
            if len(re.sub(r"[^A-Za-z]", "", phrase)) < 3:
                if price is not None and offers and offers[-1]["side"] == side and offers[-1]["price"] is None:
                    offers[-1]["price"], offers[-1]["note"] = price, note   # "Item / 200k"
                continue
            item, how = _find_item(phrase, links, aliases, words)
            offers.append({"side": side, "phrase": phrase, "key": alias_key(phrase), "item": item, "how": how,
                           "price": price, "note": note})
    # "[Korean Fan] S>Korean Fan 90k": the same item twice on one side is one offer
    merged: list[dict] = []
    for o in offers:
        twin = next((m for m in merged if o["item"] and m["item"] == o["item"] and m["side"] == o["side"]), None)
        if twin is None:
            merged.append(o)
        elif twin["price"] is None and o["price"] is not None:
            twin.update(price=o["price"], note=o["note"])
    return merged[:12]


def update_offers(con, force: bool = False) -> int:
    """Work out the offers of every stored megaphone whose message (or the aliases)
    changed since; returns how many megaphones were (re)read.

    Reading runs with no lock on the database; only the short write at the end does,
    so the app and the other threads aren't kept waiting ("database is locked")."""
    with _OFFERS_LOCK:                 # one at a time: a second run would redo the same work
        ver = alias_version(con)
        todo = []
        for mid, msg, kind, sig in con.execute("SELECT id, message, kind, offers_sig FROM megaphones").fetchall():
            want = hashlib_md5(f"{OFFERS_VERSION}|{ver}|{kind}|{msg}".encode())
            if sig != want or force:
                todo.append((mid, msg, kind, want))
        if not todo:
            return 0
        aliases = load_aliases(con)
        results = []
        for mid, msg, kind, want in todo:
            ui_pause()
            rows = []
            # (suggested items for Review are worked out only for the rows it shows)
            for i, o in enumerate(parse_offers(msg, kind, aliases)):
                rows.append((mid, i, o["side"], o["phrase"], o["key"], o["item"], o["how"], o["price"],
                             o["note"], None, 0.0))
            results.append((mid, msg, want, rows))
        # now the quick write, in batches so nobody waits long
        n = 0
        for b in range(0, len(results), 200):
            for mid, msg, want, rows in results[b:b + 200]:
                # skip it if it was deleted or its message changed meanwhile (read next time)
                if not con.execute("UPDATE megaphones SET offers_sig = ? WHERE id = ? AND message = ?",
                                   (want, mid, msg)).rowcount:
                    continue
                con.execute("DELETE FROM megaphone_offers WHERE megaphone_id = ?", (mid,))
                con.executemany("INSERT INTO megaphone_offers (megaphone_id, idx, side, phrase, key, item, how, "
                                "price, note, suggest, score) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                n += 1
            con.commit()
        return n


_OFFERS_LOCK = threading.Lock()
OFFERS_VERSION = 4          # bump when parse_offers changes: megaphones are read again

# While the window is redrawing a big table, background threads doing pure-Python work
# pause: otherwise every one of the window's thousands of small Tk calls waits for its
# turn at Python's global lock, and a redraw that takes 0.2 s takes minutes ("Not
# responding"). Long loops call ui_pause(); the window clears UI_FREE while redrawing.
UI_FREE = threading.Event()
UI_FREE.set()


def ui_pause() -> None:
    if not UI_FREE.is_set() and threading.current_thread() is not threading.main_thread():
        UI_FREE.wait(5)


def megaphone_prices(con, hours: float = 72) -> dict[str, dict]:
    """Per item, the median asking price in selling and buying megaphones:
    {item: {"sell": median or None, "buy": ..., "sell_n": count, "buy_n": count}}."""
    out: dict[str, dict] = {}
    for item, side, price in con.execute(
            "SELECT o.item, o.side, o.price FROM megaphone_offers o JOIN megaphones m ON m.id = o.megaphone_id "
            "WHERE o.item IS NOT NULL AND o.price IS NOT NULL AND m.ts >= ?", (utc_ago(hours=hours),)):
        out.setdefault(item, {"sell": [], "buy": []})[side].append(price)
    return {item: {"sell": statistics.median(v["sell"]) if v["sell"] else None,
                   "buy": statistics.median(v["buy"]) if v["buy"] else None,
                   "sell_n": len(v["sell"]), "buy_n": len(v["buy"])} for item, v in out.items()}


def unmatched_offers(con, hours: float = 72, limit: int | None = None,
                     suggest: bool = True) -> list[dict]:
    """Megaphone phrases not matched to an item yet (what Review → Megaphones shows),
    most-seen first: {key, phrase, sides, price, suggest, score, count, example, last}.
    With `limit`, only that many (the suggested items are the slow part)."""
    rows = {}
    for key, phrase, side, price, msg, ts in con.execute(
            "SELECT o.key, o.phrase, o.side, o.price, m.message, m.ts FROM megaphone_offers o "
            "JOIN megaphones m ON m.id = o.megaphone_id WHERE o.item IS NULL AND COALESCE(o.how, '') != 'ignored' "
            "AND m.ts >= ? ORDER BY m.ts", (utc_ago(hours=hours),)):
        r = rows.get(key)
        if r is None:
            r = rows[key] = {"key": key, "phrase": phrase, "sides": set(), "price": None, "suggest": None,
                             "score": 0.0, "count": 0, "example": msg, "last": ts}
        r["sides"].add(side)
        r["count"] += 1
        r["example"], r["last"] = msg, ts
        if price is not None:
            r["price"] = price
    out = sorted(rows.values(), key=lambda r: (-r["count"], r["key"]))
    if limit is not None:
        out = out[:limit]
    if suggest and has_item_list():
        for r in out:
            ui_pause()
            try:
                r["suggest"], r["score"] = best_match(con, r["phrase"].title())
            except Exception:
                pass
    return out


def count_unmatched_offers(con, hours: float = 72) -> int:
    return con.execute(
        "SELECT COUNT(DISTINCT o.key) FROM megaphone_offers o JOIN megaphones m ON m.id = o.megaphone_id "
        "WHERE o.item IS NULL AND COALESCE(o.how, '') != 'ignored' AND m.ts >= ?",
        (utc_ago(hours=hours),)).fetchone()[0]


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
        self.no_time = False      # megaphones are on screen without "[hh:mm]": timestamps are off
        self._untimed = 0         # reads in a row that saw only megaphones without a time

    STUCK = 30.0              # a read taking longer than this is logged, and reading goes on

    def due(self) -> bool:
        if self.busy.is_set() and time.monotonic() - self.last >= self.STUCK:
            # a read that never finished stopped every read after it: say where it
            # is in app.log, and start reading again
            self._log_stuck()
            self.busy.clear()
        return not self.busy.is_set() and time.monotonic() - self.last >= self.every

    def _log_stuck(self) -> None:
        import sys as _sys
        import traceback

        th = getattr(self, "_thread", None)
        frame = _sys._current_frames().get(th.ident) if th is not None and th.ident else None
        where = "".join(traceback.format_stack(frame)[-8:]) if frame is not None else "(finished)"
        app_log(f"megaphone read stuck for {self.STUCK:.0f} s, reading again. Where it was:\n{where}")
        self.on_event("error", f"megaphone reading was stuck for {self.STUCK:.0f} s and was restarted "
                               "(details in app.log)")

    def offer(self, full: Image.Image) -> None:
        import threading

        self.last = time.monotonic()
        self.busy.set()
        self._thread = threading.Thread(target=self._work, args=(full, self.channel()), daemon=True)
        self._thread.start()

    def _work(self, full, channel):
        ui_pause()
        try:
            region, unit = drawn_area(self.cfg, "chat_region", full.size), None
            if not region or is_auto(region, full.size):
                region = find_chat(full)
                unit = getattr(find_chat, "unit", None)
                drawn = self.cfg.get("chat_region")
                if region is None and drawn and not is_auto(drawn, full.size) \
                        and drawn[0] + drawn[2] <= full.width and drawn[1] + drawn[3] <= full.height:
                    # not found, but an area was drawn (on the other kind of capture):
                    # better than nothing, and right when the game fills the screen
                    region = drawn
            found = region is not None
            if found != self.found:
                self.found = found
                self.on_event("chat", {"found": found, "mode": GRAB_MODE[0], "size": full.size})
            if not region:
                return
            img = crop(full, region)
            marks: list = []
            stats: dict = {}
            con = db()
            try:
                got = read_megaphones(img, self.cfg, self.memo, con, unit=unit, channel=channel,
                                      marks=marks, stats=stats)
                # megaphones without a time at the start, read after read (and none with
                # one): the game's chat timestamps are off, so megaphones get the time
                # they were read instead of the time they were sent
                if stats.get("timed"):
                    self._untimed = 0
                elif stats.get("no_time", 0) >= 2:
                    self._untimed += 1
                no_time = self._untimed >= 3
                if no_time != self.no_time:
                    self.no_time = no_time
                    self.on_event("chat_hint", no_time)
                n = save_megaphones(con, got) if got else 0
                purge_megaphones(con, self.cfg.get("megaphone_hours", 72))
                if got:
                    update_offers(con)        # items and prices in the new megaphones
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
        self.channel = channel          # the channel now (the GUI's Channel box)
        self.frame_channel = channel    # the channel the frame being read was captured on
        # the app's own windows, (left, top, width, height) on screen: painted over
        # before a frame is looked at, or a shop photo open in the app (a store
        # window at its real size) is found and read as a shop open in the game
        self.own_windows = lambda: ()
        self._frame = None              # the frame being read, for the shop photo
        self._prefetched: dict = {}     # id(frame) -> (frame, future or None, hash, geom used)
        self._pf = None                 # the entry for the frame in step()
        self._ocr_warm = False          # one read done on this thread: caches are filled
        self.shots_con = None           # cache/shots.db, opened on the first photo
        self._shots_left = 0            # photos this visit may still take
        self._shot_hashes: set = set()  # frames of this visit already photographed
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
        self.chan_watch = ChannelWatch(cfg=cfg)   # the GUI may hand in its own (shared with its idle check)
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
        self._shots_left, self._shot_hashes = SHOTS_PER_VISIT, set()
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
                                  "map": self.map, "channel": self.frame_channel,
                                  "started": datetime.now()})

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
            # logged, or an OCR variant of a logged row: no more re-reads for it,
            # or every unchanged frame of the visit would go through the full OCR
            self.pending_unmatched.pop(key, None)
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

    def _prefetch(self, pool, pending) -> None:
        """Frames waiting behind the one being read are OCR'd ahead by the pool,
        several at once, so the reading keeps up with a scroll. A frame is read
        ahead when it looks like a shop and differs from the frame before it (the
        same checks step() makes), with the geometry and names known now; step()
        takes the result if its geometry is still the one in use."""
        if not self._ocr_warm:
            return                   # the first read fills caches: on this thread, alone
        in_flight = sum(1 for _, f, _, _ in self._prefetched.values() if f is not None and not f.done())
        limit = pool._max_workers * 2
        prev_h, names = self.last_hash, None
        for img, _map, _ts, _chan in pending:
            key = id(img)
            if key in self._prefetched:
                prev_h = self._prefetched[key][2]
                continue
            h = frame_hash(img)
            changed = prev_h is None or bin(h ^ prev_h).count("1") > 8
            prev_h = h
            if not changed or not looks_like_shop(img):
                self._prefetched[key] = (img, None, h, None)
                continue
            if in_flight >= limit:
                break
            if names is None:
                names = known_items(self.con) + [s.item for s in self.seen.values()]
            fut = pool.submit(read_shop, img, self.cfg, names, False, self.geom)
            self._prefetched[key] = (img, fut, h, self.geom)
            in_flight += 1

    def _shoot(self, ts: str) -> None:
        """Keep the frame a listing was just logged from, once per distinct frame and
        a few times per visit (see SHOTS_PER_VISIT)."""
        if not self.cfg.get("shot_days", 7) or self._frame is None or self._shots_left <= 0 \
                or self.last_hash in self._shot_hashes or not self.session_id:
            return
        try:
            if self.shots_con is None:
                self.shots_con = shots_db()
            if save_shot(self.shots_con, self.session_id, ts, self._frame):
                self._shots_left -= 1
                self._shot_hashes.add(self.last_hash)
        except Exception as e:
            self._shots_left = 0      # don't keep failing for the rest of the visit
            self.on_event("error", f"shop photo not saved: {type(e).__name__}: {e}")

    def _log(self, key: tuple, listing: Listing):
        each = listing.price
        mkt, _ = market_rate(self.con, listing.item, self.cfg)
        v, pct = verdict(each, mkt, self.cfg["deal_threshold_pct"],
                         outlier_bounds(self.con, listing.item, self.cfg))
        ts = utc_at(self._now)          # when the frame was captured, not when it was read
        info: dict = {}
        status = log_listing(self.con, listing, self.title, self.seller, self.map, self.frame_channel,
                             self.session_id, ts, info)
        save_icon(self.con, listing.item, listing.icon, listing.sold)
        self._shoot(ts)
        s = SeenListing(listing.item, listing.price, listing.qty, each, mkt,
                        "SOLD" if listing.sold else v, pct, ts, self.shop_name(), self.map,
                        listing.sold, status, self.frame_channel, self.title or "", self.seller or "",
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
             ts: float | None = None, channel=_NOW) -> None:
        if self.con is None:
            self.con = db()
        self._now = time.monotonic() if ts is None else ts
        # the channel (and clock) the frame was captured on: with a queue of frames
        # behind the screen, "now" may be a channel change and many seconds later
        self.frame_channel = self.channel if channel is _NOW else channel
        self._pf = self._prefetched.pop(id(img), None)
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
        self._frame = img
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
        frame = None
        if self._pf and self._pf[1] is not None and self._pf[3] == self.geom:
            try:
                frame = self._pf[1].result()   # read ahead by a worker while earlier frames were read
            except Exception as e:
                self.on_event("error", f"read-ahead failed, reading again: {type(e).__name__}: {e}")
        if frame is None:
            names = known_items(self.con) + [s.item for s in self.seen.values()]
            frame = read_shop(img, self.cfg, names, with_title=False, geom=self.geom)
            self._ocr_warm = True
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
            if not self.map and self.map_img is not None:
                try:                 # the map's number may read on a later frame
                    self.map = read_map(self.map_img, self.cfg, known_maps(self.con))
                except Exception:
                    pass
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

    def _hide_own_windows(self, full: Image.Image) -> None:
        """Black out the app's own windows (own_windows) in a screenshot of the desktop.
        A copy of the game's window alone (GRAB_MODE "window") never shows them, and
        has its own coordinates: left as it is."""
        if GRAB_MODE[0] != "screen":
            return
        ox, oy = LAST_SCREEN_ORIGIN
        for l, t, w, h in self.own_windows():
            x0, y0 = max(0, l - ox), max(0, t - oy)
            x1, y1 = min(full.width, l - ox + w), min(full.height, t - oy + h)
            if x1 > x0 and y1 > y0:
                full.paste((0, 0, 0), (x0, y0, x1, y1))

    def regions_for(self, full: Image.Image) -> tuple[list | None, list | None]:
        """Shop and map areas for this frame: the calibrated ones, or found
        automatically (re-located only when the cached spot stops looking like
        a store, e.g. you closed it or moved the game window)."""
        shop_r, map_r = drawn_area(self.cfg, "region", full.size), drawn_area(self.cfg, "map_region", full.size)
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

        No frame is ever dropped, however far behind the OCR falls: a frame may
        be the only one showing a row. On stop the queued frames are still read,
        so a sweep made just before pressing Stop isn't lost; setting `abort` as
        well throws them away.
        """
        import queue as _queue
        import threading

        try:
            self.con = db()
        except Exception as e:
            self.on_event("error", f"can't open the price database: {type(e).__name__}: {e}")
            return
        blank = Image.new("RGB", (8, 8), (0, 0, 0))
        frames: _queue.Queue = _queue.Queue()   # unbounded: no frame is ever dropped
        latest = {"full": None}     # the newest screenshot, for the map watcher

        def map_watch() -> None:
            """The map you're on, from the minimap, every few seconds (also with no
            shop open): sends a "map" event when it changes."""
            spot, last_sig, last_name = None, None, None
            hinted = False
            con = None
            while not stop_event.wait(MAP_EVERY):
                full = latest["full"]
                if full is None:
                    continue
                try:
                    area = drawn_area(self.cfg, "map_region", full.size)
                    if is_auto(area, full.size):
                        # where the minimap is: looked for again when it can't be read there
                        if spot is None:
                            info: dict = {}
                            spot = find_minimap(full, self.layout.store if self.layout else None, info)
                            hint = bool(info.get("no_name")) and spot is None
                            if hint != hinted:
                                hinted = hint
                                self.on_event("map_hint", hint)   # "switch the minimap to show its name"
                        area = spot
                    if not area:
                        continue
                    img = crop(full, area)
                    sig = frame_hash(img)
                    if last_sig is not None and bin(sig ^ last_sig).count("1") <= 4:
                        continue                  # the same picture: the same map
                    con = con or db()     # its own connection: this is its own thread
                    name = read_map(img, self.cfg, known_maps(con))
                    if not name:
                        spot = None               # not a minimap (any more): find it again
                        continue
                    last_sig = sig
                    if name != last_name:
                        last_name = name
                        self.on_event("map", name)
                except Exception:
                    spot = None
        threading.Thread(target=map_watch, daemon=True, name="mfm-map").start()

        BACKLOG_SOFT = max(20, int(3 / max(self.cfg["interval_seconds"], 0.02)))  # ~3 s of frames
        BACKLOG_MAX = BACKLOG_SOFT * 4
        BACKLOG_BIG_CHANGE = 40          # of the 576 bits of frame_hash: a scroll or another shop

        def capture() -> None:
            last_h, last_imgs, same = None, None, 0
            repeats = max(self.cfg["confirm_frames"], self.cfg["close_frames"]) + 2
            while not stop_event.is_set():
                t0 = time.perf_counter()
                try:
                    full = grab_full_screen()
                    self._hide_own_windows(full)
                    latest["full"] = full
                    if self.cfg.get("channel_prompt") and self.chan_watch.due():
                        try:
                            if self.chan_watch.feed(full):
                                self.on_event("channel_change", self.chan_watch.channel)
                            elif self.chan_watch.take_cancelled():
                                self.on_event("channel_cancel", None)
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
                    skip = False
                    if last_h is not None and bin(h ^ last_h).count("1") <= 8 and last_imgs:
                        imgs = last_imgs   # nothing moved: requeue the same image object
                        # ... but while frames are still waiting to be read, only enough
                        # copies in a row to confirm rows / a close: more add nothing but
                        # backlog (a shop left open piled up tens of thousands of them)
                        skip = same >= repeats and frames.qsize() + len(pending) > 0
                        same += not skip
                    else:
                        # Reading behind by more than a few seconds (a cursor or tooltip
                        # over the rows makes every frame a full read, animations make
                        # frames differ): only frames that changed a lot (a scroll,
                        # another shop) are queued, and none past BACKLOG_MAX. A burst
                        # (a fast scroll) is still read in full; a slow screen just
                        # isn't read 10 times a second.
                        waiting = frames.qsize() + len(pending)
                        if waiting >= BACKLOG_MAX or (
                                waiting >= BACKLOG_SOFT and last_h is not None
                                and bin(h ^ last_h).count("1") <= BACKLOG_BIG_CHANGE):
                            skip = True
                        else:
                            last_h, last_imgs, same = h, imgs, 1
                    # stamped with the channel now, so a frame still queued when you
                    # change channel is logged under the one it was captured on
                    if not skip:
                        frames.put((imgs[0], imgs[1], time.monotonic(), self.channel))
                except Exception as e:
                    self.on_event("error", f"{type(e).__name__}: {e}")
                wait = self.cfg["interval_seconds"] - (time.perf_counter() - t0)
                stop_event.wait(max(wait, 0.02))

        # frames behind the one being read are OCR'd ahead by a pool (see _prefetch)
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor
        pending: deque = deque()
        grabber = threading.Thread(target=capture, daemon=True, name="mfm-capture")
        grabber.start()
        workers = int(self.cfg.get("ocr_workers", 3) or 0)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mfm-ocr") if workers > 1 else None
        lookahead = workers * 2 if pool else 0
        last_backlog = 0
        try:
            while True:
                while pool and len(pending) < lookahead:
                    try:
                        pending.append(frames.get_nowait())
                    except _queue.Empty:
                        break
                if not pending:
                    try:
                        pending.append(frames.get(timeout=0.2))
                    except _queue.Empty:
                        if stop_event.is_set():
                            break    # every captured frame was read: done
                        continue
                if stop_event.is_set() and self.abort:
                    break        # don't read what's left in the queue
                if pool:
                    try:
                        self._prefetch(pool, pending)
                    except Exception as e:
                        self.on_event("error", f"read-ahead: {type(e).__name__}: {e}")
                img, map_img, ts, chan = pending.popleft()
                try:
                    self.step(img, map_img, ts, chan)
                except Exception as e:  # keep monitoring through transient failures
                    self.on_event("error", f"{type(e).__name__}: {e}")
                n = frames.qsize() + len(pending)
                if (n == 0) != (last_backlog == 0) or abs(n - last_backlog) >= 5:
                    last_backlog = n   # how far the OCR is behind the screen, for the GUI
                    self.on_event("backlog", {"frames": n})
        finally:
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)
            self._prefetched.clear()
            self.end_session()
            if self.shots_con is not None:
                self.shots_con.close()
                self.shots_con = None
            self.on_event("stopped", {})


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
    got = ocr_text(img, "--psm 7").strip()
    print(f"read:      {got!r}  ({'library ' + str(_TessLib.path) if _TessLib.load() else 'program'})")
    ok = "12,345" in got or "Market" in got
    print("OCR OK" if ok else "OCR FAILED")
    # the hotkeys: pynput and its backend for this system, packed in the app
    import importlib.util
    backend = {"win32": "_win32", "darwin": "_darwin"}.get(sys.platform, "_xorg")
    try:
        hot = importlib.util.find_spec(f"pynput.keyboard.{backend}") is not None
    except (ImportError, ValueError):
        hot = False
    print(f"hotkeys:   {'pynput ' + backend if hot else 'pynput missing'}")
    if FROZEN and not hot and sys.platform in ("win32", "darwin"):   # (Linux: needs a display to load)
        return 1
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
        cfg["channel_region"] = None
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
