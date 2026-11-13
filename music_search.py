"""四平台搜索与歌单解析（纯标准库）。"""
import ast
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PLATFORMS = ["netease", "qq", "kugou", "kuwo"]
PLATFORM_NAMES = {"netease": "网易云", "qq": "QQ音乐", "kugou": "酷狗", "kuwo": "酷我"}


def _get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _post(url, data, headers=None, timeout=15):
    body = urllib.parse.urlencode(data).encode()
    hdr = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _jsonp(text):
    t = (text or "").strip()
    if t.startswith("callback(") and t.endswith(")"):
        t = t[9:-1]
    i, j = t.find("("), t.rfind(")")
    if t[:i].isidentifier() and j > i:
        t = t[i + 1:j]
    return json.loads(t)


# ---------------- 搜索 ----------------
def search_netease(kw, limit=20):
    u = ("https://music.163.com/api/search/get?s=%s&type=1&limit=%d"
         % (urllib.parse.quote(kw), limit))
    d = json.loads(_get(u, {"Referer": "https://music.163.com/"}))
    songs = (d.get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        out.append({
            "platform": "netease",
            "song_id": str(s.get("id")),
            "title": s.get("name") or "",
            "artist": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
            "album": (s.get("album") or {}).get("name") or "",
            "duration": s.get("duration") or 0,
        })
    return out


def search_qq(kw, limit=20):
    u = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w=%s&format=json&n=%d&p=1"
         % (urllib.parse.quote(kw), limit))
    d = _jsonp(_get(u, {"Referer": "https://y.qq.com/"}))
    lst = (((d.get("data") or {}).get("song") or {}).get("list") or [])
    out = []
    for s in lst:
        out.append({
            "platform": "qq",
            "song_id": s.get("songmid") or "",
            "title": s.get("songname") or "",
            "artist": "/".join(x.get("name", "") for x in (s.get("singer") or [])),
            "album": s.get("albumname") or "",
            "duration": (s.get("interval") or 0) * 1000,
        })
    return out


def search_kugou(kw, limit=20):
    u = ("http://mobilecdn.kugou.com/api/v3/search/song?keyword=%s&page=1&pagesize=%d"
         % (urllib.parse.quote(kw), limit))
    d = json.loads(_get(u))
    info = ((d.get("data") or {}).get("info") or [])
    out = []
    for s in info:
        out.append({
            "platform": "kugou",
            "song_id": s.get("hash") or "",
            "title": s.get("songname") or "",
            "artist": s.get("singername") or "",
            "album": s.get("album_name") or "",
            "duration": (s.get("duration") or 0) * 1000,
        })
    return out


def search_kuwo(kw, limit=20):
    u = ("http://search.kuwo.cn/r.s?all=%s&ft=music&itemset=web_2013&client=kt"
         "&pn=0&rn=%d&rformat=json&encoding=utf8" % (urllib.parse.quote(kw), limit))
    raw = _get(u)
    try:
        d = json.loads(raw)
    except Exception:
        d = ast.literal_eval(raw)
    lst = d.get("abslist") or []
    out = []
    for s in lst:
        rid = s.get("MUSICRID") or ""
        rid = rid.replace("MUSIC_", "") if rid else ""
        out.append({
            "platform": "kuwo",
            "song_id": rid,
            "title": (s.get("SONGNAME") or "").replace("&nbsp;", " ").strip(),
            "artist": (s.get("ARTIST") or "").replace("&nbsp;", " ").strip(),
            "album": (s.get("ALBUM") or "").replace("&nbsp;", " ").strip(),
            "duration": 0,
        })
    return out


SEARCHERS = {"netease": search_netease, "qq": search_qq,
             "kugou": search_kugou, "kuwo": search_kuwo}


def search(platform, kw, limit=20):
    fn = SEARCHERS.get(platform)
    if not fn:
        raise ValueError("不支持的平台")
    return fn(kw, limit)


# ---------------- 歌单解析 ----------------
def detect_platform(url):
    u = (url or "").lower()
    if "music.163.com" in u or "163cn.tv" in u:
        return "netease"
    if "y.qq.com" in u or "c.y.qq.com" in u:
        return "qq"
    if "kugou.com" in u:
        return "kugou"
    if "kuwo.cn" in u:
        return "kuwo"
    return None


def parse_netease(url):
    m = (re.search(r"[?&#]id=(\d+)", url) or re.search(r"/playlist/(\d+)", url)
         or re.search(r"/playlist/(\d+)", url))
    if not m:
        raise ValueError("无法从链接中识别歌单 ID")
    pid = m.group(1)
    d = json.loads(_post("https://music.163.com/api/v6/playlist/detail",
                         {"id": pid},
                         {"Referer": "https://music.163.com/", "Cookie": "os=pc;"}))
    pl = (d.get("playlist") or {})
    ids = [str(t.get("id")) for t in (pl.get("trackIds") or []) if t.get("id")]
    tracks = []
    # 分批拉详情
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        payload = json.dumps([{"id": int(x)} for x in chunk], separators=(",", ":"))
        try:
            dd = json.loads(_post("https://music.163.com/api/v3/song/detail",
                                  {"c": payload},
                                  {"Referer": "https://music.163.com/"}))
            for s in (dd.get("songs") or []):
                tracks.append({"title": s.get("name") or "",
                               "artist": "/".join(a.get("name", "") for a in (s.get("ar") or [])),
                               "album": (s.get("al") or {}).get("name") or ""})
        except Exception:
            continue
    # 详情接口失败时，退回用详情接口已返回的曲目
    if not tracks:
        for s in (pl.get("tracks") or []):
            tracks.append({"title": s.get("name") or "",
                           "artist": "/".join(a.get("name", "") for a in (s.get("ar") or [])),
                           "album": (s.get("al") or {}).get("name") or ""})
    return {"platform": "netease", "playlist_id": pid,
            "name": pl.get("name") or f"歌单{pid}", "tracks": tracks}


def parse_qq(url):
    m = (re.search(r"disstid=(\d+)", url)
         or re.search(r"playlist/(\d+)", url)
         or re.search(r"[?&#]id=(\d+)", url))
    if not m:
        raise ValueError("无法从链接中识别歌单 ID")
    pid = m.group(1)
    u = ("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?"
         "type=1&json=1&utf8=1&onlysong=0&disstid=%s&format=json&g_tk=5381" % pid)
    d = _jsonp(_get(u, {"Referer": f"https://y.qq.com/n/yqq/playlist/{pid}.html"}))
    cd = (d.get("cdlist") or [{}])[0]
    tracks = []
    for s in (cd.get("songlist") or []):
        tracks.append({"title": s.get("songname") or "",
                       "artist": "/".join(x.get("name", "") for x in (s.get("singer") or [])),
                       "album": s.get("albumname") or ""})
    return {"platform": "qq", "playlist_id": pid,
            "name": cd.get("dissname") or f"歌单{pid}", "tracks": tracks}


def parse_kugou(url):
    m = re.search(r"special/single/(\d+)", url) or re.search(r"[?&#]specialid=(\d+)", url)
    if not m:
        raise ValueError("无法从链接中识别歌单 ID")
    sid = m.group(1)
    u = ("http://mobilecdn.kugou.com/api/v3/special/song?specialid=%s&page=1&pagesize=500"
         % sid)
    d = json.loads(_get(u))
    info = ((d.get("data") or {}).get("info") or [])
    tracks = []
    for s in info:
        tracks.append({"title": (s.get("filename") or "").split(" - ")[-1].replace(".mp3", ""),
                       "artist": s.get("singername") or "",
                       "album": s.get("album_name") or ""})
    name = ""
    try:
        di = json.loads(_get("http://mobilecdn.kugou.com/api/v3/special/info?specialid=%s" % sid))
        name = ((di.get("data") or {}).get("specialname") or "")
    except Exception:
        pass
    return {"platform": "kugou", "playlist_id": sid,
            "name": name or f"歌单{sid}", "tracks": tracks}


def parse_kuwo(url):
    m = re.search(r"playlist_detail/(\d+)", url) or re.search(r"[?&#]pid=(\d+)", url)
    if not m:
        raise ValueError("无法从链接中识别歌单 ID")
    pid = m.group(1)
    tracks, name = [], ""
    for page in range(0, 10):
        u = ("http://nplserver.kuwo.cn/pl.svc?op=getlistinfo&pid=%s&pn=%d&rn=100"
             "&encode=utf8&keyset=pl2012&identity=kuwo&pcmp4=1" % (pid, page))
        d = json.loads(_get(u))
        if not name:
            name = d.get("title") or ""
        musiclist = d.get("musiclist") or []
        if not musiclist:
            break
        for s in musiclist:
            tracks.append({"title": s.get("name") or "",
                           "artist": s.get("artist") or "",
                           "album": s.get("album") or ""})
        if len(musiclist) < 100:
            break
    return {"platform": "kuwo", "playlist_id": pid,
            "name": name or f"歌单{pid}", "tracks": tracks}


PARSERS = {"netease": parse_netease, "qq": parse_qq,
           "kugou": parse_kugou, "kuwo": parse_kuwo}


def parse_playlist(url):
    p = detect_platform(url)
    if not p:
        raise ValueError("不支持的链接，只支持网易云、QQ音乐、酷狗、酷我")
    return PARSERS[p](url)
