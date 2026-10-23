#!/usr/bin/env python3
"""Free Market Tracker shared price server.

A small self-hosted database that several copies of Free Market Tracker sync their
price sightings and megaphones with, so a group of players shares one set of prices.
Every player has their own account (a user name and password); everything stored
records which account sent it and from which IP address.

  mfm-server serve                    run the server (what the Docker image does)
  mfm-server add-user NAME            make an account; prints its first password
  mfm-server reset-password NAME      a new password (signs that account out everywhere)
  mfm-server disable-user NAME        block an account (and sign it out); enable-user undoes it
  mfm-server remove-user NAME         delete an account (what it sent stays, with its name)
  mfm-server logout-user NAME         sign an account out everywhere
  mfm-server list-users               accounts, when and from where they were last seen
  mfm-server user-info NAME           an account's IP addresses, sessions and recent events
  mfm-server log [N]                  the last N sign-ins, failed sign-ins and account changes
  mfm-server stats                    how much is stored, and from whom
  mfm-server purge WHAT WHEN [--dry-run] [--yes]
                                      delete stored data. WHAT: prices, megaphones, aliases, ocr
                                      or all (or several: prices,megaphones). WHEN:
                                        --older-than 1d   data older than that (not seen since)
                                        --last 1h         data from the past hour
                                        --all             everything
                                      Times: 30m, 1h, 12h, 1d, 2w. --dry-run only counts.
                                      Connected apps delete the same from their own data.
                                      (ocr: the OCR line images; only on the server.)
  mfm-server backup FOLDER            copy the database files (mfm-server.db, megaphones.db,
                                      aliases.db, ocr-training.db) into FOLDER, while it runs
  mfm-server export-ocr FILE.zip [--fixed | --auto] [--last 7d] [--chat | --stores | --players]
                                      the OCR line images the apps sent, with their text,
                                      for training the app's OCR (fixed: checked by hand;
                                      auto: the app's reads agreed). In the zip: lines/
                                      (chat lines), stores/ (store names), players/
                                      (player names); --chat etc. export only those.

Settings (environment variables):
  MFM_PORT        port to listen on (default 8765)
  MFM_DATA        folder for the database (default /data, or ./data outside Docker)
  MFM_NAME        a name for this server, shown in the app (default "Shared prices")
  MFM_KEEP_DAYS   sightings not seen for this many days are deleted (default 30, 0 = keep)
  MFM_MEGAPHONE_HOURS  megaphones are kept this many hours (default 72)
  MFM_SESSION_DAYS     a sign-in lasts this many days without use (default 90)
  MFM_OCR_LINES   OCR line images kept for training (default 20000)
  MFM_TRUST_PROXY  1 = take the player's IP from X-Forwarded-For (only behind your own
                   reverse proxy, e.g. Caddy)
  MFM_TLS_CERT    with MFM_TLS_KEY: serve HTTPS with this certificate (PEM files)
  MFM_TLS_KEY

API (JSON over HTTP):
  POST /api/v1/login     {"username", "password"} -> {"token", "user": {id, name}, "expires"}
  Every other /api call needs "Authorization: Bearer <token>":
  POST /api/v1/logout                      ends this sign-in
  POST /api/v1/password  {"password", "new_password"}   change your password
  GET  /api/v1/status                      {"name", "version", "sightings", "megaphones", "aliases", "seq",
                                           "purge_id", "you", "keep_days", "megaphone_hours"}
  POST /api/v1/sightings                   {"sightings": [ {uid, ts, item, price, qty, shop,
                                           seller, map, channel, sold, sold_ts, last_seen,
                                           notes}, ... ]}  (at most 500 at a time)
                                           -> {"seq", "same": {your uid: stored uid}}
  GET  /api/v1/sightings?since=SEQ&limit=N -> {"sightings": [... with "seq", "by"], "seq",
                                           "more": bool}
  POST /api/v1/megaphones                  {"megaphones": [ {uid, ts, player, channel, message, kind,
                                           color, conf}, ... ]}  -> {"seq", "same": {...}}
  GET  /api/v1/megaphones?since=SEQ&hours=H -> {"megaphones": [...], "seq", "more"}
  POST /api/v1/aliases                   {"aliases": [ {key, item, deleted, updated}, ... ]}
                                           (item "" = "not an item"; the newest change wins)
                                           -> {"seq", "same": {}}
  GET  /api/v1/aliases?since=SEQ         -> {"aliases": [...], "seq", "more"}
  (/api/v1/nicknames, with "nicknames" for "aliases", still works for apps before 2610.19)
  POST /api/v1/ocr  {"lines": [{uid, png (base64), text, status, read}]}  (at most 100)
                                           chat line images for OCR training -> {"stored"}
  GET  /api/v1/purges?since=ID             -> {"purges": [{id, ts, what, field, start, end}], "last"}
                                           what was purged on the server (mfm-server purge), as
                                           time ranges: the apps delete the same from their data
Every stored or changed sighting (or megaphone) gets the next "seq" number, so a
client asks for everything after the last number it has seen.
"""
from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.8.0"
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_BATCH = 500
MAX_BODY = 2_000_000
MEGA_HOURS = float(os.environ.get("MFM_MEGAPHONE_HOURS", "72"))
SESSION_DAYS = float(os.environ.get("MFM_SESSION_DAYS", "90"))
TRUST_PROXY = os.environ.get("MFM_TRUST_PROXY", "") in ("1", "true", "yes")
PBKDF2_ROUNDS = 200_000
FIELDS = ("ts", "item", "price", "qty", "shop", "seller", "map", "channel", "sold", "sold_ts",
          "last_seen", "notes")


def data_dir() -> Path:
    d = os.environ.get("MFM_DATA") or ("/data" if Path("/.dockerenv").exists() else "data")
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


DB_PATH = data_dir() / "mfm-server.db"            # accounts, sightings, purges, the log
# in files of their own, next to it (attached, so queries use their tables by name)
EXTRA_DBS = (("megadb", data_dir() / "megaphones.db", ("megaphones",)),
             ("aliasdb", data_dir() / "aliases.db", ("aliases",)),
             ("ocrdb", data_dir() / "ocr-training.db", ("ocr_lines",)))
_lock = threading.Lock()          # one writer at a time


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def log(msg: str) -> None:
    print(f"{utc_now()} {msg}", flush=True)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    for schema, path, _ in EXTRA_DBS:
        con.execute(f"ATTACH DATABASE ? AS {schema}", (str(path),))
        con.execute(f"PRAGMA {schema}.journal_mode=WAL")
    # 2.6.0: "nicknames" are called aliases now
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "nicknames" in tables and "aliases" not in tables:
        con.execute("ALTER TABLE nicknames RENAME TO aliases")
        con.execute("UPDATE purges SET what = 'aliases' WHERE what = 'nicknames'") if "purges" in tables else None
        con.commit()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sightings (
            uid TEXT PRIMARY KEY,
            seq INTEGER NOT NULL,
            ts TEXT NOT NULL,
            item TEXT NOT NULL,
            price INTEGER NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            shop TEXT, seller TEXT, map TEXT, channel TEXT,
            sold INTEGER NOT NULL DEFAULT 0,
            sold_ts TEXT, last_seen TEXT, notes TEXT,
            added_by TEXT, updated_by TEXT, updated TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_seq ON sightings(seq);
        CREATE INDEX IF NOT EXISTS idx_item_price ON sightings(item, price);
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,               -- the account's unique id, never reused
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            pw_hash TEXT NOT NULL,
            disabled INTEGER NOT NULL DEFAULT 0,
            created TEXT, last_seen TEXT, last_ip TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created TEXT, created_ip TEXT,
            last_used TEXT, last_ip TEXT,
            expires TEXT
        );
        CREATE TABLE IF NOT EXISTS user_ips (       -- every IP an account has been used from
            user_id TEXT NOT NULL, ip TEXT NOT NULL,
            first_seen TEXT, last_seen TEXT, requests INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, ip)
        );
        CREATE TABLE IF NOT EXISTS events (         -- sign-ins, failed sign-ins, account changes
            id INTEGER PRIMARY KEY,
            ts TEXT, user_id TEXT, username TEXT, ip TEXT, event TEXT, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS megadb.megaphones (
            uid TEXT PRIMARY KEY,
            seq INTEGER NOT NULL,
            ts TEXT NOT NULL,
            player TEXT NOT NULL,
            channel TEXT,
            message TEXT NOT NULL,
            kind TEXT, color TEXT,
            conf REAL NOT NULL DEFAULT 0,
            added_by TEXT, updated TEXT
        );
        CREATE INDEX IF NOT EXISTS megadb.idx_mega_seq ON megaphones(seq);
        CREATE INDEX IF NOT EXISTS megadb.idx_mega_ts ON megaphones(ts);
        CREATE TABLE IF NOT EXISTS aliasdb.aliases (      -- what players call items in megaphones
            key TEXT PRIMARY KEY,
            seq INTEGER NOT NULL,
            item TEXT NOT NULL DEFAULT '',          -- '' = "not an item"
            deleted INTEGER NOT NULL DEFAULT 0,
            updated TEXT,                           -- when the player changed it (newest wins)
            added_by TEXT, added_by_id TEXT, added_ip TEXT, added TEXT,
            updated_by TEXT, updated_by_id TEXT, updated_ip TEXT, stored TEXT
        );
        CREATE INDEX IF NOT EXISTS aliasdb.idx_alias_seq ON aliases(seq);
        CREATE TABLE IF NOT EXISTS ocrdb.ocr_lines (      -- chat line images for training the OCR
            uid TEXT PRIMARY KEY,
            ts TEXT NOT NULL,                       -- when it was stored or last changed
            png BLOB NOT NULL,
            text TEXT NOT NULL,                     -- what the line says
            status TEXT,                            -- auto (the app's reads agreed) or fixed (checked by hand)
            read TEXT,                              -- what the OCR read
            added_by TEXT, added_by_id TEXT, added_ip TEXT,
            kind TEXT NOT NULL DEFAULT 'chat'       -- chat (megaphone line), store (shop title), player (shop owner)
        );
        CREATE TABLE IF NOT EXISTS purges (         -- `mfm-server purge` runs; the apps apply them too
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            what TEXT NOT NULL,                     -- prices, megaphones or aliases
            field TEXT NOT NULL,                    -- seen: last seen / changed; first: first stored
            start TEXT,                             -- rows with field >= start (none: no lower bound)
            "end" TEXT NOT NULL                     -- ... and field < end
        );
    """)
    # who sent each row: the account's id and name, and the IP it came from
    main_tables = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    for table, schemas, cols in (
            ("sightings", ("main",), ("added_by_id", "added_ip", "updated_by_id", "updated_ip")),
            ("megaphones", ("megadb", "main"), ("added_by_id", "added_ip", "updated_by", "updated_by_id",
                                                "updated_ip"))):
        for schema in schemas:
            if schema == "main" and table not in main_tables:
                continue
            have = {r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")}
            for c in cols:
                if c not in have:
                    con.execute(f"ALTER TABLE {schema}.{table} ADD COLUMN {c} TEXT")
    for schema in ("ocrdb", "main"):
        if schema == "main" and "ocr_lines" not in main_tables:
            continue
        if "kind" not in {r[1] for r in con.execute(f"PRAGMA {schema}.table_info(ocr_lines)")}:
            con.execute(f"ALTER TABLE {schema}.ocr_lines ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
    # 2.8.0: megaphones, aliases and OCR training lines moved out of mfm-server.db into
    # files of their own
    moved = False
    for schema, path, tables in EXTRA_DBS:
        for table in tables:
            if table not in main_tables:
                continue
            have = [r[1] for r in con.execute(f"PRAGMA main.table_info({table})")]
            want = {r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")}
            cols = ", ".join(c for c in have if c in want)
            con.execute(f"INSERT OR IGNORE INTO {schema}.{table} ({cols}) SELECT {cols} FROM main.{table}")
            con.execute(f"DROP TABLE main.{table}")
            con.commit()
            log(f"moved {table} out of {DB_PATH.name} into {path.name}")
            moved = True
    con.commit()
    if moved:
        con.execute("VACUUM main")
    return con


# --------------------------------------------------------------------------- accounts

def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, rounds, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def new_password() -> str:
    """Easy to type: four groups of letters and digits, e.g. k7qm-x2dp-9fwa-h3tc."""
    abc = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(abc) for _ in range(4)) for _ in range(4))


def event(con, user: dict | None, ip: str | None, what: str, detail: str = "") -> None:
    con.execute("INSERT INTO events (ts, user_id, username, ip, event, detail) VALUES (?,?,?,?,?,?)",
                (utc_now(), (user or {}).get("id"), (user or {}).get("name"), ip, what, detail))
    con.commit()


def find_user(con, name: str) -> dict | None:
    row = con.execute("SELECT id, name, disabled, pw_hash, created, last_seen, last_ip FROM users WHERE name = ?",
                      (name.strip(),)).fetchone()
    return dict(zip(("id", "name", "disabled", "pw_hash", "created", "last_seen", "last_ip"), row)) if row else None


def add_user(con, name: str, password: str | None = None) -> tuple[dict, str]:
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,32}", name):
        raise ValueError("A user name is 2-32 letters, digits, '_', '.' or '-'.")
    if find_user(con, name):
        raise ValueError(f"There's already a user called {name}.")
    password = password or new_password()
    user = {"id": "u_" + secrets.token_hex(6), "name": name}
    con.execute("INSERT INTO users (id, name, pw_hash, created) VALUES (?,?,?,?)",
                (user["id"], name, hash_password(password), utc_now()))
    con.commit()
    event(con, user, None, "user added")
    return user, password


def end_sessions(con, user_id: str, keep: str | None = None) -> int:
    n = con.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep or "")).rowcount
    con.commit()
    return n


def login(con, name: str, password: str, ip: str) -> tuple[str, dict, str]:
    """A new sign-in token for the account, or raise PermissionError."""
    u = find_user(con, name or "")
    if not u or not check_password(password or "", u["pw_hash"]):
        event(con, u, ip, "sign-in failed", f"user name tried: {name!r}" if not u else "wrong password")
        raise PermissionError("Wrong user name or password.")
    if u["disabled"]:
        event(con, u, ip, "sign-in refused", "account disabled")
        raise PermissionError("This account is disabled.")
    token = "mfs_" + secrets.token_urlsafe(32)
    now = utc_now()
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime(UTC_FMT)
    with _lock:
        con.execute("INSERT INTO sessions (token_hash, user_id, created, created_ip, last_used, last_ip, expires) "
                    "VALUES (?,?,?,?,?,?,?)", (_sha(token), u["id"], now, ip, now, ip, expires))
        con.commit()
    event(con, u, ip, "signed in")
    return token, {"id": u["id"], "name": u["name"]}, expires


def authenticate(con, token: str, ip: str) -> dict | None:
    """The account a token belongs to (also notes the IP it was used from), or None."""
    h = _sha(token)
    row = con.execute("SELECT s.user_id, s.expires, s.last_used, u.name, u.disabled FROM sessions s "
                      "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (h,)).fetchone()
    if not row:
        return None
    uid, expires, last_used, name, disabled = row
    now = utc_now()
    if disabled or (expires and expires < now):
        with _lock:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (h,))
            con.commit()
        return None
    with _lock:
        # a sign-in that's used keeps going; every IP an account is used from is kept
        con.execute("UPDATE sessions SET last_used = ?, last_ip = ?, expires = ? WHERE token_hash = ?",
                    (now, ip, (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime(UTC_FMT), h))
        con.execute("UPDATE users SET last_seen = ?, last_ip = ? WHERE id = ?", (now, ip, uid))
        known = con.execute("SELECT 1 FROM user_ips WHERE user_id = ? AND ip = ?", (uid, ip)).fetchone()
        con.execute("INSERT INTO user_ips (user_id, ip, first_seen, last_seen, requests) VALUES (?,?,?,?,1) "
                    "ON CONFLICT (user_id, ip) DO UPDATE SET last_seen = excluded.last_seen, "
                    "requests = requests + 1", (uid, ip, now, now))
        con.commit()
    user = {"id": uid, "name": name, "ip": ip, "token_hash": h}
    if not known:
        event(con, user, ip, "new IP address")
    return user


class _Throttle:
    """Slows down password guessing: 10 failed sign-ins from one IP in 15 minutes
    blocks that IP for 15 minutes."""

    def __init__(self, limit=10, window=900):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < self.window]
        return len(self.fails[ip]) >= self.limit

    def failed(self, ip: str) -> None:
        self.fails.setdefault(ip, []).append(time.monotonic())


THROTTLE = _Throttle()


def next_seq(con) -> int:
    row = con.execute("SELECT v FROM meta WHERE k = 'seq'").fetchone()
    n = int(row[0]) + 1 if row else 1
    con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('seq', ?)", (str(n),))
    return n


def last_seq(con) -> int:
    row = con.execute("SELECT v FROM meta WHERE k = 'seq'").fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- sightings

def _clean(s: dict) -> dict | None:
    """A sighting from a client, checked and tidied; None if it's not usable."""
    def text(v, n=200):
        v = "" if v is None else str(v).strip()
        return v[:n] or None

    def when(v):
        v = text(v, 30)
        return v if v and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", v) else None
    try:
        price = int(s.get("price"))
        qty = int(s.get("qty") or 1)
    except (TypeError, ValueError):
        return None
    uid, ts, item = text(s.get("uid"), 64), when(s.get("ts")), text(s.get("item"), 120)
    if not uid or not ts or not item or price <= 0 or qty <= 0:
        return None
    return {"uid": uid, "ts": ts, "item": item, "price": price, "qty": qty, "shop": text(s.get("shop")),
            "seller": text(s.get("seller"), 40), "map": text(s.get("map")), "channel": text(s.get("channel"), 8),
            "sold": 1 if s.get("sold") in (1, True, "1", "true") else 0, "sold_ts": when(s.get("sold_ts")),
            "last_seen": when(s.get("last_seen")) or ts, "notes": text(s.get("notes"), 300)}


def _same_shop(a: dict, b: dict) -> bool:
    if a["seller"] and b["seller"]:
        return a["seller"].lower() == b["seller"].lower()
    return bool(a["shop"]) and (a["shop"] or "").lower() == (b["shop"] or "").lower() \
        and (a["map"] or "") == (b["map"] or "")


def _find_same(con, s: dict) -> str | None:
    """A stored sighting of the same listing (same item, price and shop within a day),
    logged by someone else, so it isn't stored twice."""
    t = datetime.strptime(s["ts"], UTC_FMT)
    lo, hi = ((t + timedelta(hours=h)).strftime(UTC_FMT) for h in (-24, 24))
    cols = ("uid",) + FIELDS
    for row in con.execute(f"SELECT {', '.join(cols)} FROM sightings WHERE item = ? AND price = ? "
                           "AND ts BETWEEN ? AND ?", (s["item"], s["price"], lo, hi)):
        r = dict(zip(cols, row))
        if _same_shop(r, s):
            return r["uid"]
    return None


def store(con, sightings: list[dict], by: dict) -> tuple[int, dict]:
    """Add or update sightings; returns (latest seq, {sent uid: stored uid} for ones that
    turned out to be a listing already stored under another uid)."""
    same = {}
    with _lock:
        for raw in sightings:
            s = _clean(raw) if isinstance(raw, dict) else None
            if not s:
                continue
            cur = con.execute("SELECT sold, sold_ts, last_seen, notes FROM sightings WHERE uid = ?",
                              (s["uid"],)).fetchone()
            uid = s["uid"]
            if not cur:
                other = _find_same(con, s)
                if other:
                    same[uid] = other
                    uid = other
                    cur = con.execute("SELECT sold, sold_ts, last_seen, notes FROM sightings WHERE uid = ?",
                                      (uid,)).fetchone()
            if cur:
                # a sighting seen again: it stays sold once sold, keeps its latest
                # last-seen time and its note unless a new one is given
                sold, sold_ts, last_seen, notes = cur
                new = {"sold": max(sold, s["sold"]),
                       "sold_ts": min(x for x in (sold_ts, s["sold_ts"]) if x) if (sold_ts or s["sold_ts"]) else None,
                       "last_seen": max(last_seen or "", s["last_seen"] or ""),
                       "notes": s["notes"] or notes,
                       "channel": s["channel"]}
                if (new["sold"], new["sold_ts"], new["last_seen"], new["notes"]) == (sold, sold_ts, last_seen, notes):
                    continue                    # nothing new
                con.execute("UPDATE sightings SET sold=?, sold_ts=?, last_seen=?, notes=?, "
                            "channel=COALESCE(?, channel), seq=?, updated_by=?, updated_by_id=?, updated_ip=?, "
                            "updated=? WHERE uid=?",
                            (new["sold"], new["sold_ts"], new["last_seen"], new["notes"], new["channel"],
                             next_seq(con), by["name"], by["id"], by["ip"], utc_now(), uid))
            else:
                con.execute("INSERT INTO sightings (uid, seq, ts, item, price, qty, shop, seller, map, channel, sold, "
                            "sold_ts, last_seen, notes, added_by, added_by_id, added_ip, updated_by, updated_by_id, "
                            "updated_ip, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (uid, next_seq(con), s["ts"], s["item"], s["price"], s["qty"], s["shop"], s["seller"],
                             s["map"], s["channel"], s["sold"], s["sold_ts"], s["last_seen"], s["notes"],
                             by["name"], by["id"], by["ip"], by["name"], by["id"], by["ip"], utc_now()))
        con.commit()
        return last_seq(con), same


def changes(con, since: int, limit: int) -> tuple[list[dict], int, bool]:
    cols = ("uid", "seq") + FIELDS + ("added_by",)
    with _lock:                       # not halfway through someone's upload
        rows = con.execute(f"SELECT {', '.join(cols)} FROM sightings WHERE seq > ? ORDER BY seq LIMIT ?",
                           (since, limit + 1)).fetchall()
    more = len(rows) > limit
    out = []
    for row in rows[:limit]:
        r = dict(zip(cols, row))
        r["by"] = r.pop("added_by")
        out.append(r)
    return out, (out[-1]["seq"] if out else since), more


def prune(con, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(UTC_FMT)
    with _lock:
        n = con.execute("DELETE FROM sightings WHERE COALESCE(last_seen, ts) < ?", (cutoff,)).rowcount
        con.commit()
    return n


# --------------------------------------------------------------------------- megaphones
# Several players read the same megaphone off their screens, each with its own OCR
# slips ("Lock1024" / "Locki024"). Reads within two minutes of each other with nearly
# the same message, and the same player (as far as the spelling goes) or channel, are
# one megaphone: only the read with the highest OCR confidence is kept.

_FOLD = str.maketrans({"g": "p", "q": "p", "l": "i", "1": "i", "|": "i", "!": "i", "0": "o",
                       "a": "o", "c": "e", "5": "s", "v": "u", "8": "b"})


def _fold(s: str) -> str:
    return (s or "").lower().replace("rn", "m").replace("vv", "w").translate(_FOLD)


def _alike(a: dict, b: dict) -> bool:
    na, nb = (re.sub(r"\W+", "", (x or "").lower()) for x in (a["message"], b["message"]))
    alike_msg = na == nb or (na and nb and (na.startswith(nb) or nb.startswith(na))) or \
        difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85
    fa, fb = _fold(a["player"]), _fold(b["player"])
    alike_name = fa == fb or difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.75
    same_ch = bool(a["channel"] and b["channel"] and str(a["channel"]) == str(b["channel"]))
    return bool(alike_msg and (alike_name or same_ch))


def _clean_mega(m: dict) -> dict | None:
    def text(v, n):
        v = "" if v is None else str(v).strip()
        return v[:n] or None
    ts = text(m.get("ts"), 30)
    if not ts or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ts):
        return None
    try:
        conf = max(0.0, min(100.0, float(m.get("conf") or 0)))
    except (TypeError, ValueError):
        conf = 0.0
    out = {"uid": text(m.get("uid"), 64), "ts": ts, "player": text(m.get("player"), 20),
           "channel": text(m.get("channel"), 8), "message": text(m.get("message"), 400),
           "kind": text(m.get("kind"), 8), "color": text(m.get("color"), 9), "conf": conf}
    return out if out["uid"] and out["player"] and out["message"] else None


def store_megaphones(con, megaphones: list[dict], by: dict) -> tuple[int, dict]:
    same = {}
    cols = ("uid", "ts", "player", "channel", "message", "kind", "color", "conf")
    with _lock:
        for raw in megaphones:
            m = _clean_mega(raw) if isinstance(raw, dict) else None
            if not m:
                continue
            row = con.execute(f"SELECT {', '.join(cols)} FROM megaphones WHERE uid = ?", (m["uid"],)).fetchone()
            cur = dict(zip(cols, row)) if row else None
            if not cur:
                t = datetime.strptime(m["ts"], UTC_FMT)
                lo, hi = ((t + timedelta(minutes=d)).strftime(UTC_FMT) for d in (-2, 2))
                for r in con.execute(f"SELECT {', '.join(cols)} FROM megaphones WHERE ts BETWEEN ? AND ?", (lo, hi)):
                    r = dict(zip(cols, r))
                    if _alike(r, m):
                        cur = r
                        same[m["uid"]] = r["uid"]
                        break
            if cur:
                if m["conf"] > cur["conf"]:           # a better read: it replaces the stored one
                    con.execute("UPDATE megaphones SET player=?, channel=COALESCE(?, channel), message=?, kind=?, "
                                "color=COALESCE(color, ?), conf=?, seq=?, updated=?, updated_by=?, updated_by_id=?, "
                                "updated_ip=? WHERE uid=?",
                                (m["player"], m["channel"], m["message"], m["kind"], m["color"], m["conf"],
                                 next_seq(con), utc_now(), by["name"], by["id"], by["ip"], cur["uid"]))
                elif m["channel"] and not cur["channel"]:
                    con.execute("UPDATE megaphones SET channel=?, seq=?, updated=?, updated_by=?, updated_by_id=?, "
                                "updated_ip=? WHERE uid=?",
                                (m["channel"], next_seq(con), utc_now(), by["name"], by["id"], by["ip"], cur["uid"]))
                continue
            con.execute("INSERT INTO megaphones (uid, seq, ts, player, channel, message, kind, color, conf, added_by, "
                        "added_by_id, added_ip, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m["uid"], next_seq(con), m["ts"], m["player"], m["channel"], m["message"], m["kind"],
                         m["color"], m["conf"], by["name"], by["id"], by["ip"], utc_now()))
        con.commit()
        return last_seq(con), same


def megaphone_changes(con, since: int, hours: float, limit: int) -> tuple[list[dict], int, bool]:
    """Megaphones stored or changed after `since`, from the last `hours` only."""
    cols = ("uid", "seq", "ts", "player", "channel", "message", "kind", "color", "conf")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(UTC_FMT)
    with _lock:                       # not halfway through someone's upload
        newest = last_seq(con)
        rows = con.execute(f"SELECT {', '.join(cols)} FROM megaphones WHERE seq > ? AND ts >= ? ORDER BY seq "
                           "LIMIT ?", (since, cutoff, limit + 1)).fetchall()
    more = len(rows) > limit
    out = [dict(zip(cols, r)) for r in rows[:limit]]
    return out, (out[-1]["seq"] if more else newest), more


def prune_megaphones(con, hours: float) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(UTC_FMT)
    with _lock:
        n = con.execute("DELETE FROM megaphones WHERE ts < ?", (cutoff,)).rowcount
        con.commit()
    return n


# --------------------------------------------------------------------------- aliases
# Players teach their app what a megaphone phrase means ("kumbis" = Kumbi
# Throwing-Stars). Those are shared here so a new player starts with them.

_KEY_RE = re.compile(r"^[a-z0-9% ]{1,80}$")


def store_aliases(con, aliases: list[dict], by: dict) -> tuple[int, dict]:
    now = utc_now()
    with _lock:
        for raw in aliases:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "").strip().lower()
            if not _KEY_RE.match(key):
                continue
            item = str(raw.get("item") or "").strip()[:120]
            deleted = 1 if raw.get("deleted") else 0
            updated = str(raw.get("updated") or now)[:25]
            if updated > now:                       # a clock ahead of ours can't win forever
                updated = now
            row = con.execute("SELECT item, deleted, updated FROM aliases WHERE key = ?", (key,)).fetchone()
            if row is None:
                con.execute("INSERT INTO aliases (key, seq, item, deleted, updated, added_by, added_by_id, "
                            "added_ip, added, updated_by, updated_by_id, updated_ip, stored) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key, next_seq(con), item, deleted, updated, by["name"], by["id"], by["ip"], now,
                             by["name"], by["id"], by["ip"], now))
            elif (row[0], row[1]) != (item, deleted) and updated >= (row[2] or ""):
                con.execute("UPDATE aliases SET seq=?, item=?, deleted=?, updated=?, updated_by=?, "
                            "updated_by_id=?, updated_ip=?, stored=? WHERE key=?",
                            (next_seq(con), item, deleted, updated, by["name"], by["id"], by["ip"], now, key))
        con.commit()
        return last_seq(con), {}


OCR_MAX = int(os.environ.get("MFM_OCR_LINES", "20000"))
# what an OCR line image shows, and its folder in `export-ocr`
OCR_KINDS = {"chat": "lines", "store": "stores", "player": "players"}


def store_ocr_lines(con, lines: list[dict], by: dict) -> tuple[int, dict]:
    """Line images from the apps, with their text: kept for training the OCR (see
    `mfm-server export-ocr`). A line sent again replaces the old one (e.g. checked
    by hand since); status "skip" removes it."""
    import base64

    now = utc_now()
    n = 0
    with _lock:
        for raw in lines:
            if not isinstance(raw, dict):
                continue
            uid = str(raw.get("uid") or "")[:64]
            if not re.fullmatch(r"[0-9a-f]{16,64}", uid):
                continue
            status = str(raw.get("status") or "")
            if status == "skip":
                con.execute("DELETE FROM ocr_lines WHERE uid = ?", (uid,))
                continue
            text = " ".join(str(raw.get("text") or "").split())[:400]
            try:
                png = base64.b64decode(str(raw.get("png") or ""), validate=True)
            except ValueError:
                continue
            if not text or not png.startswith(b"\x89PNG") or len(png) > 200_000:
                continue
            kind = str(raw.get("kind") or "chat")
            con.execute("INSERT OR REPLACE INTO ocr_lines (uid, ts, png, text, status, read, added_by, "
                        "added_by_id, added_ip, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (uid, now, png, text, status if status in ("auto", "fixed") else "auto",
                         str(raw.get("read") or "")[:400], by["name"], by["id"], by["ip"],
                         kind if kind in OCR_KINDS else "chat"))
            n += 1
        total = con.execute("SELECT COUNT(*) FROM ocr_lines").fetchone()[0]
        if total > OCR_MAX:     # the oldest agreed-on ones go first; checked ones last
            con.execute("DELETE FROM ocr_lines WHERE uid IN (SELECT uid FROM ocr_lines ORDER BY "
                        "status = 'fixed', ts LIMIT ?)", (total - OCR_MAX,))
        con.commit()
    return n, {}


def export_ocr(con, path: str, status: str = "all", since: timedelta | None = None,
               kind: str = "all") -> int:
    """The stored line images as a zip: <folder>/<status>_<uid>.png and .gt.txt (the
    text), in lines/ (chat lines), stores/ (store names) and players/ (player names).
    Hand it to whoever trains the app's OCR model."""
    import zipfile

    where, args = "1", []
    if status in ("auto", "fixed"):
        where, args = "status = ?", [status]
    if kind in OCR_KINDS:
        where += " AND kind = ?"
        args.append(kind)
    if since is not None:
        where += " AND ts >= ?"
        args.append((datetime.now(timezone.utc) - since).strftime(UTC_FMT))
    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for uid, png, text, st, k in con.execute(f"SELECT uid, png, text, status, kind FROM ocr_lines "
                                                 f"WHERE {where} ORDER BY ts", args):
            name = f"{OCR_KINDS.get(k, 'lines')}/{st or 'auto'}_{uid}"
            z.writestr(name + ".png", png)
            z.writestr(name + ".gt.txt", text + "\n")
            n += 1
    return n


def alias_changes(con, since: int, limit: int) -> tuple[list[dict], int, bool]:
    cols = ("key", "seq", "item", "deleted", "updated", "updated_by")
    with _lock:
        newest = last_seq(con)
        rows = con.execute(f"SELECT {', '.join(cols)} FROM aliases WHERE seq > ? ORDER BY seq LIMIT ?",
                           (since, limit + 1)).fetchall()
    more = len(rows) > limit
    out = [dict(zip(cols, r)) for r in rows[:limit]]
    for r in out:
        r["by"] = r.pop("updated_by")
    return out, (out[-1]["seq"] if more else newest), more


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"mfm-server/{VERSION}"
    con: sqlite3.Connection = None   # set in serve()

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ip(self) -> str:
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.headers.get("X-Real-IP", "")
            if fwd:
                return fwd[:64]
        return self.client_address[0]

    def _user(self) -> dict | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return authenticate(self.con, auth[7:].strip(), self._ip())

    def _body(self) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(n))
            return body if isinstance(body, dict) else None
        except ValueError:
            return None

    def log_message(self, fmt, *args):
        pass                          # quiet; sign-ins and errors are logged below

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/health"):
            body = (f"Free Market Tracker server {VERSION} is running.\n"
                    "Connect to it from the app: Shared Database.\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        user = self._user()
        if user is None:
            return self._send(401, {"error": "Please sign in."})
        if url.path == "/api/v1/status":
            n = self.con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
            m = self.con.execute("SELECT COUNT(*) FROM megaphones").fetchone()[0]
            k = self.con.execute("SELECT COUNT(*) FROM aliases WHERE deleted = 0").fetchone()[0]
            return self._send(200, {"name": os.environ.get("MFM_NAME", "Shared prices"), "version": VERSION,
                                    "sightings": n, "megaphones": m, "aliases": k, "nicknames": k,
                                    "megaphone_hours": MEGA_HOURS,
                                    "keep_days": int(os.environ.get("MFM_KEEP_DAYS", "30")),
                                    "seq": last_seq(self.con), "purge_id": last_purge(self.con),
                                    "ocr_lines": self.con.execute("SELECT COUNT(*) FROM ocr_lines").fetchone()[0],
                                    "you": {"id": user["id"], "name": user["name"]}})
        if url.path == "/api/v1/sightings":
            q = parse_qs(url.query)
            try:
                since = int(q.get("since", ["0"])[0])
                limit = max(1, min(5000, int(q.get("limit", ["2000"])[0])))
            except ValueError:
                return self._send(400, {"error": "since and limit must be numbers"})
            rows, seq, more = changes(self.con, since, limit)
            return self._send(200, {"sightings": rows, "seq": seq, "more": more})
        if url.path == "/api/v1/megaphones":
            q = parse_qs(url.query)
            try:
                since = int(q.get("since", ["0"])[0])
                hours = max(0.1, min(MEGA_HOURS, float(q.get("hours", [str(MEGA_HOURS)])[0])))
                limit = max(1, min(5000, int(q.get("limit", ["2000"])[0])))
            except ValueError:
                return self._send(400, {"error": "since, hours and limit must be numbers"})
            rows, seq, more = megaphone_changes(self.con, since, hours, limit)
            return self._send(200, {"megaphones": rows, "seq": seq, "more": more})
        if url.path == "/api/v1/purges":
            q = parse_qs(url.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with _lock:
                rows = self.con.execute('SELECT id, ts, what, field, start, "end" FROM purges WHERE id > ? '
                                        "ORDER BY id LIMIT 1000", (since,)).fetchall()
            cols = ("id", "ts", "what", "field", "start", "end")
            return self._send(200, {"purges": [dict(zip(cols, r)) for r in rows], "last": last_purge(self.con)})
        if url.path in ("/api/v1/aliases", "/api/v1/nicknames"):   # (apps before 2610.19 say nicknames)
            q = parse_qs(url.query)
            try:
                since = int(q.get("since", ["0"])[0])
                limit = max(1, min(5000, int(q.get("limit", ["5000"])[0])))
            except ValueError:
                return self._send(400, {"error": "since and limit must be numbers"})
            rows, seq, more = alias_changes(self.con, since, limit)
            return self._send(200, {url.path.rsplit("/", 1)[1]: rows, "seq": seq, "more": more})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        ip = self._ip()
        if path == "/api/v1/login":
            if THROTTLE.blocked(ip):
                return self._send(429, {"error": "Too many failed sign-ins: try again in 15 minutes."})
            body = self._body() or {}
            try:
                token, user, expires = login(self.con, str(body.get("username") or ""),
                                             str(body.get("password") or ""), ip)
            except PermissionError as e:
                THROTTLE.failed(ip)
                log(f"sign-in failed for {str(body.get('username'))[:40]!r} from {ip}")
                return self._send(401, {"error": str(e)})
            log(f"{user['name']} signed in from {ip}")
            return self._send(200, {"token": token, "user": user, "expires": expires})
        user = self._user()
        if user is None:
            return self._send(401, {"error": "Please sign in."})
        if path == "/api/v1/logout":
            with _lock:
                self.con.execute("DELETE FROM sessions WHERE token_hash = ?", (user["token_hash"],))
                self.con.commit()
            event(self.con, user, ip, "signed out")
            return self._send(200, {"ok": True})
        if path == "/api/v1/password":
            body = self._body() or {}
            u = find_user(self.con, user["name"])
            new = str(body.get("new_password") or "")
            if not u or not check_password(str(body.get("password") or ""), u["pw_hash"]):
                event(self.con, user, ip, "password change failed", "wrong current password")
                return self._send(403, {"error": "The current password is wrong."})
            if len(new) < 8:
                return self._send(400, {"error": "The new password needs at least 8 characters."})
            with _lock:
                self.con.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(new), user["id"]))
                self.con.commit()
            end_sessions(self.con, user["id"], keep=user["token_hash"])   # other devices sign in again
            event(self.con, user, ip, "password changed")
            return self._send(200, {"ok": True})
        if path == "/api/v1/ocr":
            lines = (self._body() or {}).get("lines")
            if not isinstance(lines, list) or len(lines) > 100:
                return self._send(400, {"error": 'send {"lines": [...]} with at most 100'})
            try:
                n, _ = store_ocr_lines(self.con, lines, user)
            except Exception as e:
                log(f"store failed: {type(e).__name__}: {e}")
                return self._send(500, {"error": "server error"})
            return self._send(200, {"stored": n})
        if path not in ("/api/v1/sightings", "/api/v1/megaphones", "/api/v1/aliases", "/api/v1/nicknames"):
            return self._send(404, {"error": "not found"})
        what = path.rsplit("/", 1)[1]
        body = self._body()
        items = (body or {}).get(what)
        if not isinstance(items, list) or len(items) > MAX_BATCH:
            return self._send(400, {"error": f'send {{"{what}": [...]}} with at most {MAX_BATCH}'})
        try:
            fn = {"sightings": store, "megaphones": store_megaphones, "aliases": store_aliases,
                  "nicknames": store_aliases}[what]
            seq, same = fn(self.con, items, user)
        except Exception as e:
            log(f"store failed: {type(e).__name__}: {e}")
            return self._send(500, {"error": "server error"})
        self._send(200, {"seq": seq, "same": same})


def serve() -> None:
    port = int(os.environ.get("MFM_PORT", "8765"))
    con = db()
    if not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        user, pw = add_user(con, "admin")
        log("No accounts yet: made one called 'admin'. Make one per player with: mfm-server add-user NAME")
        log(f"Account 'admin', password: {pw}")
    Handler.con = con
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    cert, key = os.environ.get("MFM_TLS_CERT"), os.environ.get("MFM_TLS_KEY")
    scheme = "http"
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    keep = int(os.environ.get("MFM_KEEP_DAYS", "30"))

    def housekeeping():
        while True:
            try:
                n = prune(con, keep)
                if n:
                    log(f"deleted {n} sightings not seen for {keep} days")
                n = prune_megaphones(con, MEGA_HOURS)
                if n:
                    log(f"deleted {n} megaphones older than {MEGA_HOURS:g} hours")
                with _lock:
                    con.execute("DELETE FROM sessions WHERE expires < ?", (utc_now(),))
                    con.commit()
            except Exception as e:
                log(f"clean-up failed: {e}")
            time.sleep(600)
    threading.Thread(target=housekeeping, daemon=True).start()
    log(f"Free Market Tracker server {VERSION} on {scheme}://0.0.0.0:{port} (data: {DB_PATH})"
        + ("; player IPs from X-Forwarded-For" if TRUST_PROXY else ""))
    httpd.serve_forever()


_PURGE_WHAT = ("prices", "megaphones", "aliases", "ocr")
# per kind: the table, when a row was last seen/changed (for --older-than), when it was
# first stored (for --last)
_PURGE_COLS = {"prices": ("sightings", "COALESCE(last_seen, ts)", "ts"),
               "megaphones": ("megaphones", "ts", "ts"),
               "aliases": ("aliases", "COALESCE(updated, stored)", "COALESCE(added, stored)"),
               "ocr": ("ocr_lines", "ts", "ts")}


def last_purge(con) -> int:
    return con.execute("SELECT COALESCE(MAX(id), 0) FROM purges").fetchone()[0]


def parse_duration(text: str) -> timedelta:
    """'30m', '1h', '1.5h', '1d', '2w' (or '1 day', '2 hours')."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)\s*",
                     str(text).lower())
    if not m:
        raise ValueError(f"Can't read the time {text!r}: use e.g. 30m, 1h, 1d or 2w.")
    n, unit = float(m.group(1)), m.group(2)[0]
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" \
        else timedelta(days=n) if unit == "d" else timedelta(weeks=n)


def purge(con, what: list[str], older_than: timedelta | None = None, last: timedelta | None = None,
          dry_run: bool = False) -> dict:
    """Delete stored prices / megaphones / aliases: older than `older_than`, from the
    `last` while, or (neither given) all of them. Aliases aren't removed outright but
    marked deleted. Each purge is recorded (table purges), and the apps delete the same
    range from their own data on their next sync. Returns counts."""
    now = datetime.now(timezone.utc)
    stamp = now.strftime(UTC_FMT)
    # as a fixed time range, so each app deletes the same rows from its own data
    if older_than is not None:
        field, start, end = "seen", None, (now - older_than).strftime(UTC_FMT)
    elif last is not None:
        field, start, end = "first", (now - last).strftime(UTC_FMT), "9999"
    else:
        field, start, end = "first", None, "9999"
    out = {}
    with _lock:
        if not dry_run:
            if con.in_transaction:
                con.commit()
            con.execute("BEGIN IMMEDIATE")      # the server may be running: hold the write lock
        for w in what:
            table, seen_col, first_col = _PURGE_COLS[w]
            col = seen_col if field == "seen" else first_col
            where, args = f"{col} < ?", [end]
            if start:
                where, args = where + f" AND {col} >= ?", args + [start]
            if not dry_run:
                # "everything" and "the past hour" end now: newer data an app has isn't touched
                con.execute('INSERT INTO purges (ts, what, field, start, "end") VALUES (?,?,?,?,?)',
                            (stamp, w, field, start, end if end != "9999" else stamp))
            if w == "aliases":
                where += " AND deleted = 0"
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
            if n and not dry_run:
                if w == "aliases":
                    for (key,) in con.execute(f"SELECT key FROM aliases WHERE {where}", args).fetchall():
                        con.execute("UPDATE aliases SET seq=?, deleted=1, updated=?, updated_by=?, "
                                    "updated_by_id=NULL, updated_ip=NULL, stored=? WHERE key=?",
                                    (next_seq(con), stamp, "server admin", stamp, key))
                else:
                    con.execute(f"DELETE FROM {table} WHERE {where}", args)
            out[w] = n
        if not dry_run:
            con.commit()
    return out


def _purge_cmd(con, args: list[str]) -> int:
    usage = ("Usage: mfm-server purge prices|megaphones|aliases|ocr|all  --older-than 1d | --last 1h | --all"
             "  [--dry-run] [--yes]")
    what, older, last, everything, dry, yes = None, None, None, False, False, False
    i = 0
    try:
        while i < len(args):
            a = args[i]
            if a in ("--older-than", "--last") or a.startswith(("--older-than=", "--last=")):
                if "=" in a:
                    a, val = a.split("=", 1)
                else:
                    i += 1
                    if i >= len(args):
                        raise ValueError(f"{a} needs a time, e.g. 1h or 1d.")
                    val = args[i]
                if a == "--older-than":
                    older = parse_duration(val)
                else:
                    last = parse_duration(val)
            elif a == "--all":
                everything = True
            elif a in ("--dry-run", "-n"):
                dry = True
            elif a in ("--yes", "-y"):
                yes = True
            elif what is None and not a.startswith("-"):
                what = a.lower()
            else:
                raise ValueError(f"Unknown option {a}.")
            i += 1
    except ValueError as e:
        print(e)
        print(usage)
        return 2
    kinds = list(_PURGE_WHAT) if what == "all" else \
        [{"price": "prices", "sightings": "prices", "alias": "aliases", "nickname": "aliases",
          "nicknames": "aliases", "megaphone": "megaphones"}.get(w, w)
         for w in (what or "").split(",") if w]
    if not kinds or any(k not in _PURGE_WHAT for k in kinds):
        print("Say what to delete: prices, megaphones, aliases, ocr or all.")
        print(usage)
        return 2
    if sum(x is not None and x is not False for x in (older, last, everything or None)) != 1:
        print("Say which data: --older-than TIME, --last TIME or --all (one of them).")
        print(usage)
        return 2
    when = (f"older than {args_time(older)}" if older is not None else
            f"from the past {args_time(last)}" if last is not None else "all of it")
    counts = purge(con, kinds, older, last, dry_run=True)
    print(f"To delete ({when}): " + ", ".join(f"{n} {k if n != 1 else k[:-1]}" for k, n in counts.items())
          + " on the server" + ("; the connected apps delete the same from their own data."
                                if set(kinds) - {"ocr"} else "."))
    if dry:
        return 0
    if not yes:
        if not sys.stdin.isatty():
            print("Add --yes to delete them.")
            return 1
        if input("Delete them? Type yes: ").strip().lower() not in ("yes", "y"):
            print("Nothing deleted.")
            return 1
    done = purge(con, kinds, older, last)
    print("Deleted: " + ", ".join(f"{n} {k if n != 1 else k[:-1]}" for k, n in done.items())
          + (". The apps delete the same on their next sync." if set(kinds) - {"ocr"} else "."))
    event(con, None, None, "data purged", f"{when}: " + ", ".join(f"{n} {k if n != 1 else k[:-1]}" for k, n in done.items()))
    return 0


def args_time(d: timedelta) -> str:
    m = d.total_seconds() / 60
    for size, name in ((10080, "week"), (1440, "day"), (60, "hour"), (1, "minute")):
        if m >= size:
            n = m / size
            n = int(n) if n == int(n) else round(n, 1)
            return f"{n} {name}{'' if n == 1 else 's'}"
    return f"{d.total_seconds():g} seconds"


def _table(rows: list[tuple], head: tuple) -> None:
    rows = [tuple("" if v is None else str(v) for v in r) for r in rows]
    w = [max(len(str(h)), *(len(r[i]) for r in rows)) if rows else len(str(h)) for i, h in enumerate(head)]
    print("  ".join(str(h).ljust(w[i]) for i, h in enumerate(head)))
    for r in rows:
        print("  ".join(v.ljust(w[i]) for i, v in enumerate(r)))


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        serve()
        return 0
    con = db()
    arg = argv[2] if len(argv) > 2 else None
    needs_name = {"add-user", "reset-password", "disable-user", "enable-user", "remove-user", "logout-user",
                  "user-info"}
    if cmd in needs_name and not arg:
        print(f"Usage: mfm-server {cmd} NAME")
        return 2
    u = find_user(con, arg) if arg and cmd in needs_name - {"add-user"} else None
    if cmd in needs_name - {"add-user"} and not u:
        print(f"No user called {arg}. See: mfm-server list-users")
        return 1
    if cmd == "add-user":
        try:
            user, pw = add_user(con, arg)
        except ValueError as e:
            print(e)
            return 1
        print(f"Made user {user['name']} (id {user['id']}).")
        print(f"Password: {pw}")
        print("Give them the server address, their user name and this password. They sign in under "
              "Shared Database in the app, and can change the password there.")
        return 0
    if cmd == "reset-password":
        pw = new_password()
        con.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(pw), u["id"]))
        con.commit()
        n = end_sessions(con, u["id"])
        event(con, u, None, "password reset", "by the server admin")
        print(f"New password for {u['name']}: {pw}  (signed out of {n} device(s))")
        return 0
    if cmd in ("disable-user", "enable-user"):
        off = cmd == "disable-user"
        con.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(off), u["id"]))
        con.commit()
        n = end_sessions(con, u["id"]) if off else 0
        event(con, u, None, "user disabled" if off else "user enabled", "by the server admin")
        print(f"{u['name']} is {'disabled' if off else 'enabled'}." + (f" Signed out of {n} device(s)." if off else ""))
        return 0
    if cmd == "remove-user":
        end_sessions(con, u["id"])
        con.execute("DELETE FROM users WHERE id = ?", (u["id"],))
        con.commit()
        event(con, u, None, "user removed", "by the server admin")
        print(f"Removed {u['name']}. What they sent stays, marked with their name and id ({u['id']}).")
        return 0
    if cmd == "logout-user":
        n = end_sessions(con, u["id"])
        event(con, u, None, "signed out everywhere", "by the server admin")
        print(f"Signed {u['name']} out of {n} device(s).")
        return 0
    if cmd == "list-users":
        rows = con.execute("SELECT u.name, u.id, CASE u.disabled WHEN 1 THEN 'disabled' ELSE 'active' END, "
                           "u.created, u.last_seen, u.last_ip, (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id), "
                           "(SELECT COUNT(*) FROM user_ips i WHERE i.user_id = u.id) FROM users u ORDER BY u.name "
                           "COLLATE NOCASE").fetchall()
        _table(rows, ("user", "id", "status", "created (UTC)", "last seen (UTC)", "last IP", "signed in", "IPs"))
        return 0
    if cmd == "user-info":
        print(f"{u['name']}  id {u['id']}  {'DISABLED' if u['disabled'] else 'active'}  created {u['created']}")
        print("\nIP addresses:")
        _table(con.execute("SELECT ip, first_seen, last_seen, requests FROM user_ips WHERE user_id = ? "
                           "ORDER BY last_seen DESC", (u["id"],)).fetchall(),
               ("IP", "first seen (UTC)", "last seen (UTC)", "requests"))
        print("\nSigned in on:")
        _table(con.execute("SELECT created, created_ip, last_used, last_ip, expires FROM sessions WHERE user_id = ? "
                           "ORDER BY last_used DESC", (u["id"],)).fetchall(),
               ("since (UTC)", "from IP", "last used (UTC)", "last IP", "expires (UTC)"))
        print("\nRecent events:")
        _table(con.execute("SELECT ts, ip, event, detail FROM events WHERE user_id = ? ORDER BY id DESC LIMIT 25",
                           (u["id"],)).fetchall(), ("time (UTC)", "IP", "event", "detail"))
        n = con.execute("SELECT COUNT(*) FROM sightings WHERE added_by_id = ?", (u["id"],)).fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM megaphones WHERE added_by_id = ?", (u["id"],)).fetchone()[0]
        print(f"\nSent: {n} sightings, {m} megaphones (still stored).")
        return 0
    if cmd == "log":
        try:
            n = int(arg or 50)
        except ValueError:
            n = 50
        rows = con.execute("SELECT ts, username, ip, event, detail FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        _table(rows[::-1], ("time (UTC)", "user", "IP", "event", "detail"))
        return 0
    if cmd == "purge":
        return _purge_cmd(con, argv[2:])
    if cmd == "export-ocr":
        args = argv[2:]
        if not args or args[0].startswith("-"):
            print("Usage: mfm-server export-ocr FILE.zip [--fixed | --auto] [--last TIME] "
                  "[--chat | --stores | --players]")
            return 2
        status = "fixed" if "--fixed" in args else "auto" if "--auto" in args else "all"
        since = None
        if "--last" in args:
            try:
                since = parse_duration(args[args.index("--last") + 1])
            except (IndexError, ValueError) as e:
                print(e if isinstance(e, ValueError) else "--last needs a time, e.g. 7d.")
                return 2
        kind = "chat" if "--chat" in args else "store" if "--stores" in args else \
            "player" if "--players" in args else "all"
        n = export_ocr(con, args[0], status, since, kind)
        print(f"Wrote {n} line images with their text to {args[0]}.")
        return 0
    if cmd == "backup":
        if len(argv) < 3:
            print("Usage: mfm-server backup FOLDER")
            return 2
        out = Path(argv[2])
        out.mkdir(parents=True, exist_ok=True)
        for schema, path in [("main", DB_PATH)] + [(s_, p) for s_, p, _ in EXTRA_DBS]:
            dst = sqlite3.connect(out / path.name)
            con.backup(dst, name=schema)       # a consistent copy, while the server runs
            dst.close()
            print(f"  {out / path.name}")
        print(f"Backed up the {1 + len(EXTRA_DBS)} database files to {out}.")
        return 0
    if cmd == "stats":
        n = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
        items = con.execute("SELECT COUNT(DISTINCT item) FROM sightings").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM megaphones").fetchone()[0]
        k = con.execute("SELECT COUNT(*) FROM aliases WHERE deleted = 0").fetchone()[0]
        o = con.execute("SELECT COUNT(*) FROM ocr_lines").fetchone()[0]
        by = dict(con.execute("SELECT kind, COUNT(*) FROM ocr_lines GROUP BY kind").fetchall())
        o = (f"{o} OCR line images ({by.get('chat', 0)} chat, {by.get('store', 0)} store names, "
             f"{by.get('player', 0)} player names)")
        print(f"{n} sightings of {items} items, {m} megaphones, {k} aliases, {o}")
        for path in [DB_PATH] + [p for _, p, _ in EXTRA_DBS]:
            print(f"  {path.name}: {path.stat().st_size / 1e6:.1f} MB" if path.exists() else f"  {path.name}: -")
        _table(con.execute("SELECT added_by, added_by_id, COUNT(*) FROM sightings GROUP BY added_by_id "
                           "ORDER BY 3 DESC").fetchall(), ("user", "id", "sightings"))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
