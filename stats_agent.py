#!/usr/bin/env python3
"""Navidrome 只读统计代理。

只执行固定的聚合查询，不接受外部传入 SQL，不写库。
以 mode=ro + immutable 之外的方式打开，保证与 Navidrome 并存不锁库。
"""
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("ND_DB", "/opt/music-stack/navidrome/data/navidrome.db")
PORT = int(os.environ.get("PORT", "3999"))
TOKEN = os.environ.get("STATS_TOKEN", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "45"))

_cache = {"at": 0, "data": None}


def q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def collect(days=30):
    """一次采集全部指标。任何单段失败都降级为空，不整体 500。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    out = {"ok": True, "generated_at": int(time.time())}
    try:
        # 总量
        out["totals"] = {
            "songs": conn.execute("SELECT COUNT(*) FROM media_file WHERE missing=0").fetchone()[0],
            "albums": conn.execute("SELECT COUNT(*) FROM album").fetchone()[0],
            "artists": conn.execute("SELECT COUNT(*) FROM artist").fetchone()[0],
            "users": conn.execute("SELECT COUNT(*) FROM user").fetchone()[0],
            "plays": conn.execute("SELECT COUNT(*) FROM scrobbles").fetchone()[0],
        }

        since = int(time.time()) - days * 86400

        # 热门歌曲（窗口内）
        out["top_songs"] = q(conn, """
            SELECT mf.title AS title, mf.artist AS artist, mf.album AS album,
                   COUNT(*) AS plays
            FROM scrobbles s JOIN media_file mf ON mf.id = s.media_file_id
            WHERE s.submission_time >= ?
            GROUP BY s.media_file_id
            ORDER BY plays DESC, title ASC
            LIMIT 20
        """, (since,))

        # 热门专辑
        out["top_albums"] = q(conn, """
            SELECT mf.album AS album, mf.album_artist AS artist, COUNT(*) AS plays
            FROM scrobbles s JOIN media_file mf ON mf.id = s.media_file_id
            WHERE s.submission_time >= ? AND mf.album <> ''
            GROUP BY mf.album, mf.album_artist
            ORDER BY plays DESC
            LIMIT 12
        """, (since,))

        # 热门艺人
        out["top_artists"] = q(conn, """
            SELECT mf.artist AS artist, COUNT(*) AS plays
            FROM scrobbles s JOIN media_file mf ON mf.id = s.media_file_id
            WHERE s.submission_time >= ? AND mf.artist <> ''
            GROUP BY mf.artist
            ORDER BY plays DESC
            LIMIT 12
        """, (since,))

        # 活跃听众
        out["top_listeners"] = q(conn, """
            SELECT u.user_name AS user, u.name AS name, COUNT(*) AS plays
            FROM scrobbles s JOIN user u ON u.id = s.user_id
            WHERE s.submission_time >= ?
            GROUP BY s.user_id
            ORDER BY plays DESC
            LIMIT 15
        """, (since,))

        # 每日趋势（最近 N 天，含 0 值补全）
        rows = q(conn, """
            SELECT date(submission_time,'unixepoch','localtime') AS d, COUNT(*) AS n
            FROM scrobbles WHERE submission_time >= ?
            GROUP BY d
        """, (since,))
        got = {r["d"]: r["n"] for r in rows}
        today = time.time()
        trend = []
        for i in range(days - 1, -1, -1):
            key = time.strftime("%Y-%m-%d", time.localtime(today - i * 86400))
            trend.append({"d": key, "n": got.get(key, 0)})
        out["trend"] = trend

        # 近 24 小时活跃度（按小时）
        hrows = q(conn, """
            SELECT CAST(strftime('%H', submission_time,'unixepoch','localtime') AS INTEGER) AS h,
                   COUNT(*) AS n
            FROM scrobbles WHERE submission_time >= ?
            GROUP BY h
        """, (int(time.time()) - 7 * 86400,))
        hg = {r["h"]: r["n"] for r in hrows}
        out["hourly"] = [{"h": h, "n": hg.get(h, 0)} for h in range(24)]

        # 最近播放
        out["recent"] = q(conn, """
            SELECT mf.title AS title, mf.artist AS artist, u.user_name AS user,
                   s.submission_time AS at
            FROM scrobbles s
            JOIN media_file mf ON mf.id = s.media_file_id
            LEFT JOIN user u ON u.id = s.user_id
            ORDER BY s.submission_time DESC
            LIMIT 12
        """)
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True})
        if path != "/stats":
            return self._send(404, {"error": "not found"})
        if TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer " + TOKEN:
                return self._send(401, {"error": "unauthorized"})
        now = time.time()
        if _cache["data"] and now - _cache["at"] < CACHE_TTL:
            return self._send(200, _cache["data"])
        data = collect()
        if data.get("ok"):
            _cache["at"] = now
            _cache["data"] = data
        return self._send(200 if data.get("ok") else 503, data)


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print("DB 不存在:", DB_PATH, file=sys.stderr)
        sys.exit(1)
    host = os.environ.get("BIND", "127.0.0.1")
    srv = ThreadingHTTPServer((host, PORT), H)
    print(f"stats agent on {host}:{PORT} db={DB_PATH}")
    srv.serve_forever()
