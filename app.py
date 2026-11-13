#!/usr/bin/env python3
"""Navidrome 多服控制台 —— 纯标准库实现。"""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import music_search

# ---------------- 配置 ----------------
PORT = int(os.environ.get("PORT", "3010"))
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "panel.db")
SESSION_TTL = 12 * 3600       # 会话 12 小时
SNAPSHOT_TTL = 30             # 用户快照缓存秒数
MONTHLY_SONG_QUOTA = 50       # 每月求歌上限
MONTHLY_PLAYLIST_QUOTA = 10   # 每月歌单上限

SECRET_FILE = "/run/secrets/panel_secret"

SERVERS = json.loads(os.environ.get("ND_SERVERS", "[]"))
ADMIN_USER = os.environ.get("ND_ADMIN_USER", "botadmin")
ADMIN_PASS_FILE = os.environ.get("ND_ADMIN_PASS_FILE", "/run/secrets/nd_botadmin_pass")
TG_TOKEN_FILE = os.environ.get("TG_TOKEN_FILE", "/run/secrets/tg_token")

# 统计代理：每台 Navidrome 一个只读代理，返回聚合播放数据
STATS_SERVERS = json.loads(os.environ.get("ND_STATS_SERVERS", "[]"))
STATS_TOKEN = ""
_stats_tok_file = "/run/secrets/stats_token"
if os.path.exists(_stats_tok_file):
    with open(_stats_tok_file) as _f:
        STATS_TOKEN = _f.read().strip()
STATS_TTL = int(os.environ.get("STATS_TTL", "60"))

os.makedirs(DATA_DIR, exist_ok=True)


def _load_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            return f.read().strip() or os.urandom(32)
    s = os.environ.get("PANEL_SECRET", "").encode() or hashlib.sha256(b"nd-panel-default").digest()
    return s


SECRET = _load_secret()


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


BOTADMIN_PASS = _read_file(ADMIN_PASS_FILE)
TG_TOKEN = _read_file(TG_TOKEN_FILE)


# ---------------- 数据库 ----------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            actor TEXT,
            action TEXT,
            target TEXT,
            detail TEXT,
            ip TEXT
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            tg_id TEXT,
            username TEXT,
            platform TEXT,
            song_id TEXT,
            title TEXT,
            artist TEXT,
            status TEXT DEFAULT 'pending',
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            tg_id TEXT,
            username TEXT,
            platform TEXT,
            playlist_id TEXT,
            name TEXT,
            status TEXT DEFAULT 'pending',
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_id INTEGER NOT NULL,
            tg_id TEXT NOT NULL,
            ts REAL NOT NULL,
            UNIQUE(req_id, tg_id)
        );
        CREATE TABLE IF NOT EXISTS bindings (
            tg_id TEXT PRIMARY KEY,
            username TEXT,
            name TEXT,
            ts REAL
        );
        """)


init_db()



def init_extra_tables():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS search_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            keyword TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT '',
            n INTEGER NOT NULL DEFAULT 1,
            last_ts REAL NOT NULL
        )""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_hist
                     ON search_history(username, keyword, platform)""")
        c.execute("""CREATE INDEX IF NOT EXISTS ix_hist_user
                     ON search_history(username, last_ts DESC)""")


init_extra_tables()


def month_start_ts(ts=None):
    """当月 1 号 00:00 的时间戳（本地时间）。"""
    t = time.localtime(ts or time.time())
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def usage(username, table):
    """统计本月用量（被拒绝的不占额度）。"""
    with db() as c:
        row = c.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE username=? AND ts>=? AND status!='rejected'",
            (username, month_start_ts())).fetchone()
    return row["n"] if row else 0


def quota_state(username):
    return {
        "song": {"used": usage(username, "requests"), "limit": MONTHLY_SONG_QUOTA},
        "playlist": {"used": usage(username, "playlists"), "limit": MONTHLY_PLAYLIST_QUOTA},
    }


def quota_detail(username):
    """配额明细：本月用量、通过率、历史累计。不泄漏他人数据。"""
    out = {"ok": True}
    try:
        c = db()
        ms = month_start_ts()
        since = time.strftime("%Y-%m-%d", time.localtime(ms))
        for table, key in (("requests", "song"), ("playlists", "playlist")):
            rows = c.execute(
                f"SELECT status, COUNT(*) FROM {table} WHERE username=? AND ts>=? GROUP BY status",
                (username, ms)).fetchall()
            by = {r[0]: r[1] for r in rows}
            used = sum(v for k, v in by.items() if k != "rejected")
            total = c.execute(f"SELECT COUNT(*) FROM {table} WHERE username=?",
                              (username,)).fetchone()[0]
            passed = by.get("approved", 0) + by.get("uploaded", 0)
            decided = passed + by.get("rejected", 0)
            out[key] = {
                "used": used,
                "limit": MONTHLY_SONG_QUOTA if table == "requests" else MONTHLY_PLAYLIST_QUOTA,
                "since": since,
                "by_status": by,
                "total": total,
                "pass_rate": round(passed * 100.0 / decided, 1) if decided else None,
                "remaining": max(0, (MONTHLY_SONG_QUOTA if table == "requests"
                                     else MONTHLY_PLAYLIST_QUOTA) - used),
            }
    except Exception as e:
        out["error"] = str(e)
    return out


def public_stats(data):
    """普通用户可见的播放统计：去掉线路地址信息，听众只留用户名。"""
    out = dict(data)
    # 服务器只留是否在线，不暴露名称与错误细节
    out["servers"] = {k: {"ok": bool(v.get("ok"))} for k, v in (data.get("servers") or {}).items()}
    # 听众去掉昵称，避免泄露真实姓名
    out["top_listeners"] = [
        {"user": x.get("user", ""), "plays": x.get("plays", 0)}
        for x in (data.get("top_listeners") or [])
    ]
    # 最近播放不显示服务器名
    out["recent"] = [
        {k: v for k, v in x.items() if k != "server"}
        for x in (data.get("recent") or [])
    ]
    return out


def _health_check():
    """一次性自检：线路连通、统计代理、数据库、磁盘。"""
    items = []

    # 线路（Navidrome 本身）
    for c in CLIENTS:
        t0 = time.time()
        try:
            ok = c.ping()
        except Exception:
            ok = False
        items.append({
            "group": "线路", "name": c.name, "ok": ok,
            "ms": int((time.time() - t0) * 1000),
            "detail": "" if ok else "无响应",
        })

    # 统计代理
    st = stats_all(force=True)
    for name, d in st.get("servers", {}).items():
        items.append({
            "group": "统计", "name": name, "ok": bool(d.get("ok")),
            "ms": 0, "detail": "" if d.get("ok") else (d.get("error") or "不可达"),
        })

    # 面板数据库
    try:
        n = db().execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        items.append({"group": "服务", "name": "面板数据库", "ok": True, "ms": 0,
                      "detail": f"{n} 条求歌记录"})
    except Exception as e:
        items.append({"group": "服务", "name": "面板数据库", "ok": False, "ms": 0,
                      "detail": str(e)})

    # Bot 绑定库
    try:
        bm = bind_map(force=True)
        items.append({"group": "服务", "name": "Bot 绑定库", "ok": len(bm) > 0, "ms": 0,
                      "detail": f"{len(bm)} 个已绑定账号"})
    except Exception as e:
        items.append({"group": "服务", "name": "Bot 绑定库", "ok": False, "ms": 0,
                      "detail": str(e)})

    # 磁盘
    try:
        sv = os.statvfs(DATA_DIR)
        free_gb = sv.f_bavail * sv.f_frsize / (1024 ** 3)
        items.append({"group": "服务", "name": "本机磁盘", "ok": free_gb > 1, "ms": 0,
                      "detail": f"剩余 {free_gb:.1f} GB"})
    except Exception as e:
        items.append({"group": "服务", "name": "本机磁盘", "ok": False, "ms": 0,
                      "detail": str(e)})

    ok_n = sum(1 for i in items if i["ok"])
    return {
        "ok": ok_n == len(items),
        "items": items,
        "summary": {"ok": ok_n, "total": len(items)},
        "checked_at": int(time.time()),
    }


def bot_playlist_index():
    """从 Bot 库读歌单导入记录，建 source_id 索引。"""
    cl = _mongo()
    if not cl:
        return {}
    try:
        col = cl.get_database("bot")["playlist_imports"]
        out = {}
        for d in col.find({}, {"source_id": 1, "name": 1, "total": 1,
                               "username": 1, "created_at": 1, "results": 1}):
            sid = str(d.get("source_id") or "")
            if sid:
                out[sid] = {
                    "name": d.get("name", ""),
                    "total": d.get("total", 0),
                    "username": d.get("username", ""),
                    "created_at": str(d.get("created_at", "")),
                }
        return out
    except Exception:
        return {}


def sync_compare():
    """面板求歌/歌单 与 Bot 记录 的对照，只读。"""
    bot_pl = bot_playlist_index()
    out = {"ok": True, "playlists": [], "summary": {}}
    try:
        with db() as c:
            rows = c.execute(
                "SELECT id, ts, username, platform, playlist_id, name, status "
                "FROM playlists ORDER BY ts DESC").fetchall()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    matched = 0
    for r in rows:
        pid = str(r["playlist_id"] or "")
        b = bot_pl.get(pid)
        if b:
            matched += 1
        out["playlists"].append({
            "id": r["id"],
            "name": r["name"],
            "username": r["username"],
            "status": r["status"],
            "ts": r["ts"],
            "in_bot": bool(b),
            "bot": b,
        })
    out["summary"] = {
        "panel": len(rows),
        "bot": len(bot_pl),
        "matched": matched,
        "panel_only": len(rows) - matched,
    }
    return out


def remember_search(username, keyword, platform):
    """记一次搜索。同名同平台累加次数。"""
    kw = (keyword or "").strip()
    if not kw or not username:
        return
    try:
        with db() as c:
            c.execute("""INSERT INTO search_history(username, keyword, platform, n, last_ts)
                         VALUES(?,?,?,1,?)
                         ON CONFLICT(username, keyword, platform)
                         DO UPDATE SET n = n + 1, last_ts = excluded.last_ts""",
                      (username, kw, platform or "", time.time()))
    except Exception:
        pass


def search_suggest(username, limit=8):
    """最近搜索 + 常搜，合并去重。"""
    try:
        with db() as c:
            rows = c.execute(
                "SELECT keyword, platform, n, last_ts FROM search_history "
                "WHERE username=? ORDER BY last_ts DESC LIMIT 40", (username,)).fetchall()
    except Exception:
        return []
    seen, out = set(), []
    for r in rows:
        k = (r["keyword"], r["platform"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"keyword": r["keyword"], "platform": r["platform"],
                    "n": r["n"], "ts": r["last_ts"]})
        if len(out) >= limit:
            break
    return out


def audit(actor, action, target="", detail="", ip=""):
    try:
        with db() as c:
            c.execute("INSERT INTO audit(ts,actor,action,target,detail,ip) VALUES(?,?,?,?,?,?)",
                      (time.time(), actor, action, target, str(detail)[:500], ip))
    except Exception:
        pass


# ---------------- Navidrome 客户端 ----------------
# ---------------- 统计代理 ----------------
_stats_cache = {"at": 0, "data": None}
_stats_lock = threading.Lock()


def _fetch_stats_one(srv, timeout=6):
    """拉取单台统计。失败返回 ok=False，不抛出。"""
    url = srv.get("url", "").rstrip("/") + "/stats"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + STATS_TOKEN,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def stats_all(force=False):
    """并发拉三台统计并合并。单台失败降级，不影响其它台。"""
    now = time.time()
    with _stats_lock:
        if not force and _stats_cache["data"] and now - _stats_cache["at"] < STATS_TTL:
            return _stats_cache["data"]

    results = {}
    threads = []

    def work(srv):
        results[srv["name"]] = _fetch_stats_one(srv)

    for srv in STATS_SERVERS:
        t = threading.Thread(target=work, args=(srv,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=8)

    merged = {
        "generated_at": int(now),
        "servers": {},
        "totals": {"songs": 0, "albums": 0, "artists": 0, "plays": 0},
        "top_songs": [], "top_artists": [], "top_listeners": [],
        "top_albums": [], "recent": [],
        "trend": {}, "hourly": [0] * 24,
        "online": 0, "total": len(STATS_SERVERS),
    }

    song_agg, artist_agg, album_agg, listener_agg = {}, {}, {}, {}
    trend_agg, hourly_agg = {}, [0] * 24

    for name, d in results.items():
        ok = bool(d.get("ok"))
        merged["servers"][name] = {
            "ok": ok,
            "error": d.get("error", ""),
            "totals": d.get("totals", {}),
        }
        if not ok:
            continue
        merged["online"] += 1
        for k in ("songs", "albums", "artists", "plays"):
            merged["totals"][k] += d.get("totals", {}).get(k, 0)

        for row in d.get("top_songs", []):
            key = (row.get("title", ""), row.get("artist", ""))
            e = song_agg.setdefault(key, {"title": key[0], "artist": key[1],
                                          "album": row.get("album", ""), "plays": 0})
            e["plays"] += row.get("plays", 0)
        for row in d.get("top_artists", []):
            k = row.get("artist", "")
            if k:
                artist_agg[k] = artist_agg.get(k, 0) + row.get("plays", 0)
        for row in d.get("top_albums", []):
            k = (row.get("album", ""), row.get("artist", ""))
            e = album_agg.setdefault(k, {"album": k[0], "artist": k[1], "plays": 0})
            e["plays"] += row.get("plays", 0)
        for row in d.get("top_listeners", []):
            k = row.get("user", "")
            e = listener_agg.setdefault(k, {"user": k, "name": row.get("name", ""), "plays": 0})
            e["plays"] += row.get("plays", 0)
        for row in d.get("trend", []):
            trend_agg[row["d"]] = trend_agg.get(row["d"], 0) + row.get("n", 0)
        for row in d.get("hourly", []):
            hourly_agg[row["h"]] += row.get("n", 0)
        for row in d.get("recent", []):
            merged["recent"].append({**row, "server": name})

    merged["top_songs"] = sorted(song_agg.values(), key=lambda x: -x["plays"])[:20]
    merged["top_artists"] = sorted(
        [{"artist": k, "plays": v} for k, v in artist_agg.items()],
        key=lambda x: -x["plays"])[:12]
    merged["top_albums"] = sorted(album_agg.values(), key=lambda x: -x["plays"])[:12]
    merged["top_listeners"] = sorted(listener_agg.values(), key=lambda x: -x["plays"])[:15]
    merged["trend"] = [{"d": k, "n": v} for k, v in sorted(trend_agg.items())]
    merged["hourly"] = hourly_agg
    merged["recent"] = sorted(merged["recent"], key=lambda x: -x.get("at", 0))[:12]

    with _stats_lock:
        _stats_cache["at"] = now
        _stats_cache["data"] = merged
    return merged


class NDCall:
    def __init__(self, name, base):
        self.name = name
        self.base = base.rstrip("/")

    def _req(self, path, body=None, token=None, method=None, timeout=25):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
        if token:
            headers["X-ND-Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data is not None else "GET"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return json.loads(raw)
            except Exception:
                return None

    def login(self, user, pwd):
        try:
            d = self._req("/auth/login", {"username": user, "password": pwd})
            if d and d.get("token"):
                return d
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return None
            raise
        except Exception:
            return None
        return None

    def admin_token(self):
        tok = getattr(self, "_tok", None)
        exp = getattr(self, "_tok_exp", 0)
        if tok and time.time() < exp:
            return tok
        d = self.login(ADMIN_USER, BOTADMIN_PASS)
        if d and d.get("token"):
            self._tok = d["token"]
            self._tok_exp = time.time() + 1200   # 20 分钟
            return d["token"]
        return None

    def ping(self):
        try:
            self._req("/ping", timeout=8)
            return True
        except Exception:
            return False

    def users(self, token):
        return self._req("/api/user?_start=0&_end=100000&_sort=userName&_order=ASC", token=token) or []

    def find_user(self, username, token):
        """按用户名定位用户。Navidrome 会忽略 filters 参数，直接拉列表比对（实测 ~30ms）。"""
        try:
            rows = self._req("/api/user?_start=0&_end=100000&_sort=userName&_order=ASC",
                             token=token, timeout=8) or []
        except Exception:
            return None
        low = username.lower()
        for u in rows:
            if (u.get("userName") or "").lower() == low:
                return u
        return None

    def update_user(self, uid, payload, token):
        return self._req(f"/api/user/{uid}", payload, token=token, method="PUT")

    def delete_user(self, uid, token):
        return self._req(f"/api/user/{uid}", None, token=token, method="DELETE")


CLIENTS = [NDCall(s["name"], s["url"]) for s in SERVERS]

_snap_lock = threading.Lock()
_snap = {"ts": 0, "data": None}


def collect_users(force=False):
    """并发抓三台用户，按用户名合并。"""
    now = time.time()
    with _snap_lock:
        if not force and _snap["data"] and now - _snap["ts"] < SNAPSHOT_TTL:
            return _snap["data"], _snap["ts"]

    merged = {}
    server_state = {}
    threads = []
    results = {}

    def work(c):
        try:
            tok = c.admin_token()
            if not tok:
                results[c.name] = {"ok": False, "error": "登录失败", "users": []}
                return
            us = c.users(tok)
            results[c.name] = {"ok": True, "error": None, "users": us}
        except Exception as e:
            results[c.name] = {"ok": False, "error": f"{type(e).__name__}", "users": []}

    for c in CLIENTS:
        t = threading.Thread(target=work, args=(c,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=30)

    for c in CLIENTS:
        r = results.get(c.name, {"ok": False, "error": "超时", "users": []})
        server_state[c.name] = {"online": r["ok"], "error": r["error"], "count": len(r["users"])}
        for u in r["users"]:
            un = u.get("userName") or ""
            ent = merged.setdefault(un, {"username": un, "name": u.get("name") or un,
                                         "isAdmin": False, "servers": {}})
            ent["servers"][c.name] = {"id": u.get("id"), "isAdmin": bool(u.get("isAdmin")),
                                      "lastLoginAt": u.get("lastLoginAt"),
                                      "lastAccessAt": u.get("lastAccessAt")}
            if u.get("isAdmin"):
                ent["isAdmin"] = True

    data = {"users": sorted(merged.values(), key=lambda x: x["username"].lower()),
            "servers": server_state}
    with _snap_lock:
        _snap["data"] = data
        _snap["ts"] = now
    return data, now


def invalidate():
    with _snap_lock:
        _snap["data"] = None
        _snap["ts"] = 0


# ---------------- 会话 ----------------
def make_token(username):
    raw = f"{username}:{time.time()}:{os.urandom(8).hex()}".encode()
    tok = hmac.new(SECRET, raw, hashlib.sha256).hexdigest() + os.urandom(8).hex()
    with db() as c:
        c.execute("INSERT INTO sessions(token, username, created) VALUES(?,?,?)",
                  (tok, username, time.time()))
    return tok


def get_session(tok):
    if not tok:
        return None
    with db() as c:
        r = c.execute("SELECT username, created FROM sessions WHERE token=?", (tok,)).fetchone()
    if not r:
        return None
    if time.time() - r["created"] > SESSION_TTL:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (tok,))
        return None
    return r["username"]


def drop_session(tok):
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (tok,))


# ---------------- Bot 绑定关系（Mongo） ----------------
MONGO_URL = None
_mongo_client = None
_mongo_lock = threading.Lock()
BIND_TTL = 60
_bind_cache = {"ts": 0, "map": {}}


def _mongo():
    """懒连接 Bot 的 Mongo，失败返回 None（面板不因此崩溃）。"""
    global MONGO_URL, _mongo_client
    if _mongo_client is not None:
        return _mongo_client
    with _mongo_lock:
        if _mongo_client is not None:
            return _mongo_client
        path = os.environ.get("MONGO_URL_FILE")
        if not path:
            return None
        try:
            url = open(path).read().strip()
            from pymongo import MongoClient
            cli = MongoClient(url, serverSelectionTimeoutMS=3000,
                              connectTimeoutMS=3000, socketTimeoutMS=5000)
            cli.admin.command("ping")
            MONGO_URL = url
            _mongo_client = cli
            return cli
        except Exception:
            return None


def bind_map(force=False):
    """返回 {小写账号: telegram_id}，缓存 60 秒。"""
    now = time.time()
    if not force and _bind_cache["map"] and now - _bind_cache["ts"] < BIND_TTL:
        return _bind_cache["map"]
    cli = _mongo()
    if cli is None:
        return _bind_cache["map"] or {}
    try:
        db = cli.get_default_database()
        rows = db.users.find({}, {"username": 1, "telegram_id": 1})
        m = {}
        for r in rows:
            un = str(r.get("username") or "").strip().lower()
            tg = r.get("telegram_id")
            if un and tg:
                m[un] = str(tg)
        _bind_cache["map"] = m
        _bind_cache["ts"] = now
        return m
    except Exception:
        return _bind_cache["map"] or {}


def tg_of(username):
    """账号 -> Telegram ID，没有绑定返回 None。"""
    if not username:
        return None
    return bind_map().get(str(username).strip().lower())


# ---------------- Telegram 推送 ----------------
def tg_push(tg_id, text):
    if not TG_TOKEN or not tg_id:
        return False
    try:
        data = json.dumps({"chat_id": tg_id, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------- HTTP 服务 ----------------
STATIC = {"": "index.html", "app.js": "app.js", "style.css": "style.css"}


class Handler(BaseHTTPRequestHandler):
    server_version = "nd-panel/1.2"

    def log_message(self, *a):
        pass

    @staticmethod
    def _review_text(table, title, label, note):
        kind = "求歌" if table == "requests" else "歌单"
        icon = {"已通过": "✅", "已上传": "📀", "已拒绝": "❌", "待审核": "⏳"}.get(label, "🎵")
        txt = f"{icon} 你提交的{kind}《{title}》{label}"
        if label == "已上传":
            txt += "\n可以去服务器上听了。"
        elif label == "已拒绝":
            txt += "\n如需说明请联系管理员。"
        if note:
            txt += f"\n备注：{note}"
        return txt

    # ---- 工具 ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return ""

    def _me(self):
        return get_session(self._token())

    def _ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _need_admin(self):
        u = self._me()
        if not u:
            return None, None
        if not is_admin(u):
            return u, None
        return u, True

    # ---- 路由 ----
    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        q = parse_qs(p.query)

        if path == "/" or path in ("/index.html",):
            return self._static("index.html")
        if path in ("/app.js", "/style.css"):
            return self._static(path[1:])

        if path == "/api/quota":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            return self._json({"ok": True, **quota_state(u), **quota_detail(u)})

        if path == "/api/me":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            return self._json({"ok": True, "username": u, "isAdmin": is_admin(u)})

        if path == "/api/overview":
            if not self._me():
                return self._err("未登录", 401)
            data, ts = collect_users(force=q.get("force") == ["1"])
            users = data["users"]
            online = sum(1 for s in data["servers"].values() if s["online"])
            return self._json({"ok": True, "servers": data["servers"], "serverOnline": online,
                               "serverTotal": len(SERVERS), "userCount": len(users),
                               "adminCount": sum(1 for u in users if u["isAdmin"]),
                               "ts": ts})

        if path == "/api/users":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            data, ts = collect_users(force=q.get("force") == ["1"])
            return self._json({"ok": True, "users": data["users"], "servers": data["servers"], "ts": ts})

        if path == "/api/audit":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            with db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT 200")]
            return self._json({"ok": True, "logs": rows})

        if path == "/api/requests":
            if not self._me():
                return self._err("未登录", 401)
            with db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM requests ORDER BY id DESC LIMIT 300")]
                pls = [dict(r) for r in c.execute(
                    "SELECT * FROM playlists ORDER BY id DESC LIMIT 200")]
                votes = {}
                for v in c.execute("SELECT req_id, COUNT(*) n FROM votes GROUP BY req_id"):
                    votes[v["req_id"]] = v["n"]
            bmap = bind_map()
            for r in rows:
                r["votes"] = votes.get(r["id"], 0)
                r["tg"] = r.get("tg_id") or bmap.get(
                    str(r.get("username") or "").lower())
                r["bound"] = bool(r["tg"])
                if not r.get("tg_id") and r["tg"]:
                    # 顺带回填空缺的 tg_id
                    with db() as c:
                        c.execute("UPDATE requests SET tg_id=? WHERE id=?",
                                  (r["tg"], r["id"]))
            for r in pls:
                r["tg"] = r.get("tg_id") or bmap.get(
                    str(r.get("username") or "").lower())
                r["bound"] = bool(r["tg"])
                if not r.get("tg_id") and r["tg"]:
                    with db() as c:
                        c.execute("UPDATE playlists SET tg_id=? WHERE id=?",
                                  (r["tg"], r["id"]))
            return self._json({"ok": True, "requests": rows, "playlists": pls,
                               "bindings": len(bmap)})

        if path == "/api/search":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            kw = (q.get("kw") or [""])[0].strip()
            plat = (q.get("platform") or ["netease"])[0]
            if not kw:
                return self._err("请输入关键词")
            try:
                res = music_search.search(plat, kw, 20)
            except Exception as e:
                return self._err(f"{music_search.PLATFORM_NAMES.get(plat, plat)}搜索失败：{type(e).__name__}")
            remember_search(u, kw, plat)
            return self._json({"ok": True, "results": res,
                               "platformName": music_search.PLATFORM_NAMES.get(plat, plat)})

        if path == "/api/sync":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            return self._json(sync_compare())

        if path == "/api/suggest":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            return self._json({"ok": True, "items": search_suggest(u)})

        if path == "/api/requests/lookup":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            plat = (q.get("platform") or [""])[0]
            sid = (q.get("song_id") or [""])[0]
            if not plat or not sid:
                return self._err("缺少参数")
            with db() as c:
                row = c.execute(
                    "SELECT id, username, status, ts, note FROM requests "
                    "WHERE platform=? AND song_id=? AND status!=\'rejected\' "
                    "ORDER BY ts ASC LIMIT 1", (plat, sid)).fetchone()
                votes = c.execute(
                    "SELECT COUNT(*) n FROM requests WHERE platform=? AND song_id=? "
                    "AND status!=\'rejected\'", (plat, sid)).fetchone()
            if not row:
                return self._json({"ok": True, "exists": False})
            return self._json({"ok": True, "exists": True, "by": row["username"],
                               "status": row["status"], "ts": row["ts"],
                               "votes": votes["n"] if votes else 1})

        if path == "/api/playlist/parse":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            url = (q.get("url") or [""])[0].strip()
            if not url:
                return self._err("请粘贴歌单链接")
            try:
                pl = music_search.parse_playlist(url)
            except Exception as e:
                return self._err(str(e) if isinstance(e, ValueError) else f"解析失败：{type(e).__name__}")
            return self._json({"ok": True, "playlist": pl,
                               "platformName": music_search.PLATFORM_NAMES.get(pl["platform"], pl["platform"])})

        if path == "/api/stats":
            me = self._me()
            if not me:
                return self._err("未登录", 401)
            try:
                force = parse_qs(urlparse(self.path).query).get("force", ["0"])[0] == "1"
                data = stats_all(force=force)
                if not is_admin(me):
                    data = public_stats(data)
                return self._json(data)
            except Exception as e:
                return self._err("统计获取失败: " + str(e), 503)
        if path == "/api/health":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            return self._json(self._health_check())
        if path == "/api/bindings":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            with db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM bindings ORDER BY ts DESC LIMIT 500")]
            return self._json({"ok": True, "bindings": rows})

        return self._err("not found", 404)

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path
        body = self._body()

        if path == "/api/login":
            user = str(body.get("username") or "").strip()
            pwd = str(body.get("password") or "")
            if not user or not pwd:
                return self._err("请输入用户名和密码")
            ok = _login_any(user, pwd)
            if not ok:
                audit(user, "login_failed", ip=self._ip())
                return self._err("用户名或密码错误", 401)
            tok = make_token(user)
            audit(user, "login", ip=self._ip())
            return self._json({"ok": True, "token": tok, "username": user, "isAdmin": is_admin(user)})

        if path == "/api/logout":
            drop_session(self._token())
            return self._json({"ok": True})

        if path == "/api/users/update":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            username = str(body.get("username") or "")
            newpass = body.get("password")
            name = body.get("name")
            if not username:
                return self._err("缺少用户名")
            return self._json(self._update_user(username, name, newpass, u))

        if path == "/api/users/delete":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            username = str(body.get("username") or "")
            if not username:
                return self._err("缺少用户名")
            if username.lower() in ("botadmin",) :
                return self._err("不能删除内置管理员")
            return self._json(self._delete_user(username, u))

        if path == "/api/review/delete":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            kind = body.get("kind") or "request"
            if kind not in ("request", "playlist"):
                return self._err("类型非法")
            try:
                ids = [int(x) for x in (body.get("ids") or [])][:500]
            except Exception:
                return self._err("参数错误")
            if not ids:
                return self._err("没有选中任何记录")
            table = "requests" if kind == "request" else "playlists"
            done = 0
            with db() as c:
                for rid in ids:
                    cur = c.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
                    done += cur.rowcount
                    if kind == "request":
                        c.execute("DELETE FROM votes WHERE req_id=?", (rid,))
            audit(u, "review_delete", f"{table} x{done}", ",".join(str(i) for i in ids[:20]), self._ip())
            return self._json({"ok": True, "count": done})

        if path == "/api/review/batch":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            kind = body.get("kind") or "request"
            ids = body.get("ids") or []
            status = str(body.get("status") or "")
            note = str(body.get("note") or "")
            if kind not in ("request", "playlist"):
                return self._err("类型非法")
            if status not in ("approved", "rejected", "uploaded", "pending"):
                return self._err("状态非法")
            try:
                ids = [int(x) for x in ids][:500]
            except Exception:
                return self._err("参数错误")
            if not ids:
                return self._err("没有选中任何记录")
            table = "requests" if kind == "request" else "playlists"
            done = 0
            rows = []
            with db() as c:
                for rid in ids:
                    r = c.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
                    if not r:
                        continue
                    c.execute(f"UPDATE {table} SET status=?, note=? WHERE id=?", (status, note, rid))
                    rows.append(dict(r))
                    done += 1
            audit(u, "review_batch", f"{table} x{done}", status, self._ip())

            label = {"approved": "已通过", "rejected": "已拒绝",
                     "uploaded": "已上传", "pending": "待审核"}[status]
            sent = unbound = failed = 0
            for r in rows:
                title = (r.get("title") if table == "requests" else r.get("name")) or r.get("note") or ""
                tg = r.get("tg_id") or tg_of(r.get("username"))
                if not tg:
                    unbound += 1
                    continue
                if tg_push(tg, self._review_text(table, title, label, note)):
                    sent += 1
                else:
                    failed += 1
            audit(u, "notify_batch", f"{table} x{done}",
                  f"发送{sent} 未绑定{unbound} 失败{failed}", self._ip())
            return self._json({"ok": True, "count": done, "sent": sent,
                               "unbound": unbound, "failed": failed})

        if path == "/api/requests/review":
            u, adm = self._need_admin()
            if not u:
                return self._err("未登录", 401)
            if not adm:
                return self._err("需要管理员权限", 403)
            kind = body.get("kind") or "request"
            rid = int(body.get("id") or 0)
            status = str(body.get("status") or "")
            note = str(body.get("note") or "")
            if status not in ("approved", "rejected", "uploaded", "pending"):
                return self._err("状态非法")
            table = "requests" if kind == "request" else "playlists"
            with db() as c:
                row = c.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
                if not row:
                    return self._err("记录不存在", 404)
                c.execute(f"UPDATE {table} SET status=?, note=? WHERE id=?", (status, note, rid))
            audit(u, "review", f"{table}#{rid}", status)

            label = {"approved": "已通过", "rejected": "已拒绝", "uploaded": "已上传", "pending": "待审核"}[status]
            title = (row["title"] if table == "requests" else row["name"]) or ""
            tg = row["tg_id"] or tg_of(row["username"])
            txt = self._review_text(table, title, label, note)
            pushed = tg_push(tg, txt)
            audit(u, "notify", f"{table}#{rid}", "ok" if pushed else ("未绑定TG" if not tg else "失败"))
            return self._json({"ok": True, "pushed": 1 if pushed else 0,
                               "bound": bool(tg), "notified": bool(pushed)})

        if path == "/api/requests/submit":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            used = usage(u, "requests")
            if used >= MONTHLY_SONG_QUOTA:
                return self._err(f"本月求歌额度已用完（{used}/{MONTHLY_SONG_QUOTA}），下月 1 号重置")
            title = str(body.get("title") or "").strip()
            if not title:
                return self._err("歌曲名不能为空")
            platform = str(body.get("platform") or "")
            song_id = str(body.get("song_id") or "")
            # 同平台同歌曲不重复提交
            with db() as c:
                dup = c.execute(
                    "SELECT id, username, status, ts FROM requests "
                    "WHERE platform=? AND song_id=? AND status!='rejected'",
                    (platform, song_id)).fetchone()
            if dup:
                by = dup["username"]
                st = dup["status"]
                when = time.strftime("%m-%d %H:%M", time.localtime(dup["ts"] or 0))
                who = "你" if by == u else by
                label = {"pending": "正在排队", "approved": "已通过，等待上传",
                         "uploaded": "已上传"}.get(st, st)
                return self._err(f"这首歌{who}在 {when} 已经求过，{label}，不用重复求")
            with db() as c:
                c.execute("""INSERT INTO requests(ts,tg_id,username,platform,song_id,title,artist,status)
                             VALUES(?,?,?,?,?,?,?, 'pending')""",
                          (time.time(), tg_of(u), u, platform,
                           song_id, title, body.get("artist")))
            audit(u, "submit_request", title, platform, self._ip())
            return self._json({"ok": True, "quota": quota_state(u)})

        if path == "/api/playlists/submit":
            u = self._me()
            if not u:
                return self._err("未登录", 401)
            used = usage(u, "playlists")
            if used >= MONTHLY_PLAYLIST_QUOTA:
                return self._err(f"本月歌单额度已用完（{used}/{MONTHLY_PLAYLIST_QUOTA}），下月 1 号重置")
            url = str(body.get("url") or "").strip()
            if not url:
                return self._err("请粘贴歌单链接")
            if not (url.startswith("http://") or url.startswith("https://")):
                return self._err("链接格式不对，需要以 http 开头")
            platform = music_search.detect_platform(url) or ""
            pid = ""
            m = re.search(r"(\d{5,})", url)
            if m:
                pid = m.group(1)
            with db() as c:
                dup = c.execute(
                    "SELECT id FROM playlists WHERE playlist_id=? AND playlist_id!='' AND status!='rejected'",
                    (pid,)).fetchone()
            if dup:
                return self._err("这个歌单已经提交过了")
            with db() as c:
                c.execute("""INSERT INTO playlists(ts,tg_id,username,platform,playlist_id,name,status,note)
                             VALUES(?,?,?,?,?,?, 'pending', ?)""",
                          (time.time(), tg_of(u), u, platform, pid,
                           "", url))
            audit(u, "submit_playlist", url, platform, self._ip())
            return self._json({"ok": True, "quota": quota_state(u)})

        return self._err("not found", 404)

    # ---- 业务 ----
    def _health_check(self):
        return _health_check()

    def _update_user(self, username, name, newpass, actor):
        done, failed = [], []
        for c in CLIENTS:
            try:
                tok = c.admin_token()
                if not tok:
                    failed.append(f"{c.name}:登录失败")
                    continue
                u = None
                for x in c.users(tok):
                    if (x.get("userName") or "").lower() == username.lower():
                        u = x
                        break
                if not u:
                    continue
                payload = dict(u)
                if name:
                    payload["name"] = name
                if newpass:
                    payload["changePassword"] = True
                    payload["password"] = newpass
                payload.pop("libraries", None)
                c.update_user(u["id"], payload, tok)
                done.append(c.name)
            except Exception as e:
                failed.append(f"{c.name}:{type(e).__name__}")
        invalidate()
        audit(actor, "update_user", username, f"done={done} failed={failed}", self._ip())
        return {"ok": len(failed) == 0, "done": done, "failed": failed}

    def _delete_user(self, username, actor):
        done, failed = [], []
        for c in CLIENTS:
            try:
                tok = c.admin_token()
                if not tok:
                    failed.append(f"{c.name}:登录失败")
                    continue
                u = None
                for x in c.users(tok):
                    if (x.get("userName") or "").lower() == username.lower():
                        u = x
                        break
                if not u:
                    done.append(f"{c.name}(无)")
                    continue
                c.delete_user(u["id"], tok)
                done.append(c.name)
            except Exception as e:
                failed.append(f"{c.name}:{type(e).__name__}")
        invalidate()
        audit(actor, "delete_user", username, f"done={done} failed={failed}", self._ip())
        return {"ok": len(failed) == 0, "done": done, "failed": failed}

    # ---- 静态文件 ----
    def _static(self, name):
        fname = STATIC.get(name, name)
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        path = os.path.join(base, fname)
        if not os.path.abspath(path).startswith(os.path.abspath(base)) or not os.path.exists(path):
            return self._err("not found", 404)
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8"}.get(fname.rsplit(".", 1)[-1], "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    do_HEAD = do_GET
    do_PUT = do_POST
    do_DELETE = do_POST


_admin_cache = {}
_admin_lock = threading.Lock()
ADMIN_TTL = 300


def _login_any(user, pwd, overall=8):
    """并发验证所有线路，任一成功即可，避免串行等待。"""
    result = {"ok": False}
    done = threading.Event()

    def work(c):
        try:
            if c.login(user, pwd):
                result["ok"] = True
                done.set()
        except Exception:
            pass

    for c in CLIENTS:
        threading.Thread(target=work, args=(c,), daemon=True).start()

    done.wait(timeout=overall)
    return result["ok"]


def is_admin(username, force=False):
    """只查该用户本身，不拉全量列表；结果缓存 5 分钟。"""
    key = username.lower()
    now = time.time()
    with _admin_lock:
        hit = _admin_cache.get(key)
        if not force and hit and now - hit[0] < ADMIN_TTL:
            return hit[1]

    found = []

    def probe(c):
        try:
            tok = c.admin_token()
            if not tok:
                return
            u = c.find_user(username, tok)
            if u:
                found.append(bool(u.get("isAdmin")))
        except Exception:
            pass

    ths = [threading.Thread(target=probe, args=(c,), daemon=True) for c in CLIENTS]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=6)
    admin = any(found)

    with _admin_lock:
        _admin_cache[key] = (now, admin)
    return admin


if __name__ == "__main__":
    print(f"nd-panel listening :{PORT}, servers={[c.name for c in CLIENTS]}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
