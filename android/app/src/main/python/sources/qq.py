# -*- coding: utf-8 -*-
"""QQ音乐音源: 免登录搜索/歌单; 播放地址用 vkey.GetVkeyServer.CgiGetVkey(返回 purl).
VIP/付费曲 purl 为空 -> 前端灰显.
登录: 官方扫码接口(ptlogin2)已风控 403, 采用手动粘贴 Cookie(uin+qm36+music_key/skey)."""
import contextvars
import gzip
import hashlib
import html
import json
import os
import random
import re
import threading
import time
import urllib.parse
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import secure_store, song
from .common import http_get, UA

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
GUID_FILE = os.path.join(DATA_DIR, "qq_guid.txt")
COOKIE_FILE = os.path.join(DATA_DIR, "qq_cookie.json")

QQ_HEADERS = {"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"}
# 登录后可携带的 cookie 键(y.qq.com 实际字段: uin + qm_keyst/qqmusic_key 为取流凭证)
_CK_KEYS = ("uin", "euin", "qm_keyst", "qqmusic_key", "music_key", "qm36", "skey",
            "RKD", "RK", "music_ignore_pskey", "psrf_qqaccess_token", "ptcz",
            "login_type", "tmeLoginType")
# 取流必需的密钥键(任一即可)
_KEY_KEYS = ("qqmusic_key", "qm_keyst", "music_key", "skey", "qm36")


def _load_cookie():
    ck = secure_store.load_json(COOKIE_FILE)
    return ck if isinstance(ck, dict) else {}


def _cookie_str():
    parts = ["%s=%s" % (k, v) for k, v in _load_cookie().items() if v]
    return "; ".join(parts) if parts else None


def _headers(cookie=None):
    h = dict(QQ_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    return h


def _uin():
    """登录 uin(数字, 去掉前导 u/0); 未登录返回 '0'."""
    u = str(_load_cookie().get("uin") or "")
    u = u.lstrip("u").lstrip("0")
    return u or "0"


def _gtk_from(skey):
    """QQ 标准 g_tk: hash=5381; 逐字符 hash*33+ord; 末尾 &0x7fffffff."""
    h = 5381
    for ch in skey or "":
        h = (h + ((h << 5) & 0xFFFFFFFF) + ord(ch)) & 0xFFFFFFFF
    return h & 0x7fffffff


def _gtk_login():
    """登录态 g_tk 由取流密钥派生(qqmusic_key/qm_keyst/music_key/skey/qm36); 无登录回退匿名值."""
    ck = _load_cookie()
    for k in _KEY_KEYS:
        if ck.get(k):
            return _gtk_from(ck[k])
    return _gtk()


# ---------- 登录 ----------

def _parse_cookie(raw):
    """从粘贴的 Cookie 串中挑出登录相关键."""
    ck = {}
    for seg in (raw or "").replace("\n", ";").replace(",", ";").split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            k = k.strip()
            if k in _CK_KEYS:
                ck[k] = v.strip()
    return ck


def _save_cookie(ck):
    if not (ck.get("uin") and any(ck.get(k) for k in _KEY_KEYS)):
        return False
    secure_store.save_json(COOKIE_FILE, ck)
    return True


def status():
    ck = _cookie_str()
    logged = bool(ck)
    return {"logged_in": logged, "uin": _uin() if logged else ""}


def manual_cookie(raw):
    raw = (raw or "").strip()
    if not raw:
        return {"error": "cookie 为空"}
    ck = _parse_cookie(raw)
    if not ck.get("uin"):
        return {"error": "未解析到 uin, 请粘贴完整 Cookie(含 uin= 与 qqmusic_key=/qm_keyst=)"}
    if not any(ck.get(k) for k in _KEY_KEYS):
        return {"error": "未解析到 qqmusic_key/qm_keyst 等取流密钥, 登录信息不完整"}
    return {"ok": _save_cookie(ck), "logged_in": bool(_cookie_str())}


def logout():
    try:
        os.remove(COOKIE_FILE)
    except OSError:
        pass
    return {"ok": True}


def list_my_playlists():
    """登录歌单: fcg_user_created_diss 需复刻网页精确参数
    (needNewCode=1 + g_tk_new_20200303 + cv/ct + hostuin=自身uin), 否则 disslist 恒空."""
    ck = _cookie_str()
    if not ck:
        return {"error": "未登录"}
    uin = _uin()
    gk = _gtk_login()
    import time as _t
    ts = int(_t.time() * 1000)
    url = ("https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss"
           "?r=%d&_=%d&cv=4747474&ct=24&format=json&inCharset=utf-8&outCharset=utf-8"
           "&notice=0&platform=yqq.json&needNewCode=1&uin=%s"
           "&g_tk_new_20200303=%d&g_tk=%d&hostuin=%s&sin=0&size=50"
           % (ts, ts, uin, gk, gk, uin))
    h = dict(QQ_HEADERS)
    h["Cookie"] = ck
    h["Referer"] = "https://y.qq.com/n/ryqq_v2/profile/create"
    try:
        j = http_get(url, headers=h)
        folders = (j.get("data") or {}).get("disslist") or []
        out = []
        for f in folders:
            tid = f.get("tid")
            if not tid:  # tid=0 为特殊文件夹(QZone背景音乐/本地上传), 无法按 dissid 取内容
                continue
            cover = f.get("diss_cover") or ""
            if cover.startswith("http") is False:
                cover = ""
            out.append({"id": tid,
                        "name": f.get("diss_name"),
                        "cover": cover,
                        "count": f.get("song_cnt")})
        return {"playlists": out}
    except Exception as e:
        return {"error": "获取歌单失败: %s" % e}


def _guid():
    try:
        with open(GUID_FILE, "r", encoding="utf-8") as f:
            g = f.read().strip()
            if g:
                return g
    except Exception:
        pass
    g = str(random.randint(10 ** 9, 10 ** 10 - 1))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(GUID_FILE, "w", encoding="utf-8") as f:
            f.write(g)
    except Exception:
        pass
    return g


def _gtk(seed=5381):
    import hashlib
    m = hashlib.md5()
    m.update((str(seed) + "6726303674571591091").encode("utf-8"))
    return int(m.hexdigest(), 16)


def _cover(albummid):
    return ("https://y.gtimg.cn/music/photo_new/T002R300x300M000%s.jpg" % albummid
            if albummid else "")


def search(keyword, limit=30, page=1):
    page = max(1, int(page))
    url = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&p=%d&n=%d&w=%s"
           "&g_tk=%d&loginUin=0&hostUin=0&inCharset=utf8&outCharset=utf-8&notice=0"
           "&platform=yqq.json&needNewCode=0"
           % (page, limit, urllib.parse.quote(keyword), _gtk()))
    j = http_get(url, headers=QQ_HEADERS)
    song_data = (j.get("data") or {}).get("song") or {}
    lst = song_data.get("list") or []
    out = []
    for t in lst:
        singers = "/".join((s.get("name") or "") for s in t.get("singer") or [])
        pay = t.get("pay") or {}
        vip = bool(pay.get("payplay") or pay.get("payfull") or pay.get("note") not in (0, None))
        out.append(song("qq", t.get("songmid") or t.get("id"), t.get("songname"),
                        singers, t.get("albumname", ""), t.get("interval", 0),
                        _cover(t.get("albummid")),
                        extra={"vip": vip, "media_mid": t.get("media_mid") or ""}))
    total = song_data.get("totalnum") or song_data.get("totalcount") or song_data.get("sum") or None
    total = int(total) if total else None
    has_more = (page * limit < total) if total else (len(out) >= limit)
    return {"songs": out, "page": page, "total": total, "page_size": limit, "has_more": has_more}


_HOME_TTL = 300
_HOME_BUDGET = 2.0
_HOME_CACHE = {}
_HOME_CACHE_LOCK = threading.Lock()
_HOME_DEADLINE = contextvars.ContextVar("qq_home_deadline", default=None)


def _home_timeout():
    default = 1.5
    try:
        deadline = _HOME_DEADLINE.get()
        if deadline is None:
            return default
        left = float(deadline) - time.monotonic()
        return max(0.4, min(default, left))
    except Exception:
        return default


def _home_get(url, headers=None, raw=False):
    """首页专用网络请求: 单次尝试、短超时, 失败直接返回空结果."""
    timeout = _home_timeout()
    args = {"headers": headers, "timeout": timeout, "attempts": 1,
            "budget": timeout}
    if raw:
        args["raw"] = True
    j = http_get(url, **args)
    if isinstance(j, dict) and ("error" in j or "_nonjson" in j):
        return {} if not raw else b""
    return j


def _cached_home_part(key, fn, allow_empty=True):
    """首页片段短缓存: 避免切音源/刷新时重复请求; 空结果可下次重试."""
    now = time.monotonic()
    with _HOME_CACHE_LOCK:
        hit = _HOME_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = fn()
    if allow_empty or val:
        with _HOME_CACHE_LOCK:
            _HOME_CACHE[key] = (now + _HOME_TTL, val)
    return val


def _parallel_map(fn, items, limit=8):
    """并行执行首页片段请求; 总截止时间到后不再等待剩余任务, 保留输入顺序."""
    out = [None] * len(items)
    if not items:
        return out
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=min(limit, len(items)))
    try:
        futures = {ctx.run(pool.submit, fn, item): i for i, item in enumerate(items)}
        deadline = _HOME_DEADLINE.get()
        try:
            for f in as_completed(futures, timeout=(
                    max(0.1, deadline - time.monotonic()) if deadline else None)):
                try:
                    out[futures[f]] = f.result()
                except Exception:
                    pass
        except TimeoutError:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def _home_public_playlists(limit=12):
    """公共歌单: 一次请求 fcg_get_diss_by_tag, 附带静态兜底."""
    fallback = [
        {"id": "7707261125", "ref": "7707261125", "title": "甜度爆表 | 旋律说唱狙击少女心",
         "cover": "http://qpic.y.qq.com/music_cover/s4SoBc0IrDEDpHKlDkDflKbCgbic5ldSQrdsEafqvxULdib5CvKhaibww/300?n=1",
         "sub": "855.8万次播放", "tag": "热门歌单", "action": "playlist"},
        {"id": "7729596131", "ref": "7729596131", "title": "耳机里的秘密 | 宝藏女声集合站",
         "cover": "http://qpic.y.qq.com/music_cover/qhuJFHlwiayRp1rhWoJk9HaAkric2uNwiaX5vwuDF1mb4LgEId9z2eExg/300?n=1",
         "sub": "5550.4万次播放", "tag": "热门歌单", "action": "playlist"},
        {"id": "7614366897", "ref": "7614366897", "title": "侠气古风 : 腰间两把刀！断和了",
         "cover": "http://qpic.y.qq.com/music_cover/nKLrO3Sp1ctkpqK9rmuze9wmyUcyQV38GpUdnwYNkAyEUicTQ2dCBoA/300?n=1",
         "sub": "4082.7万次播放", "tag": "热门歌单", "action": "playlist"},
        {"id": "7426529126", "ref": "7426529126", "title": "「片段」你与星河皆不可及",
         "cover": "http://qpic.y.qq.com/music_cover/LQ4zic8aEyNEreHko5rQ2a6S0JjhXjvoxzTWXvqiaZDDvJWlibGcCWa4g/300?n=1",
         "sub": "3944.7万次播放", "tag": "热门歌单", "action": "playlist"},
    ]
    url = ("https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg"
           "?format=json&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json"
           "&needNewCode=0&picmid=1&categoryId=10000000&sortId=3&sin=0&ein=%d"
           "&g_tk=%d" % (max(1, limit - 1), _gtk()))
    j = _home_get(url, headers=QQ_HEADERS)
    rows = ((j.get("data") or {}).get("list") or [])
    out = []
    for p in rows[:limit]:
        ref = p.get("dissid")
        if not ref:
            continue
        out.append({
            "id": ref, "ref": ref, "title": p.get("dissname") or "推荐歌单",
            "cover": p.get("imgurl") or "", "sub": _fmt_count(p.get("listennum")),
            "tag": "热门歌单", "action": "playlist",
        })
    return out[:limit] if out else fallback[:limit]


def _home_page_html():
    """读取 QQ 音乐馆推荐页 HTML(服务端 gzip/deflate 压缩)."""
    if time.monotonic() >= (_HOME_DEADLINE.get() or float("inf")):
        return ""
    payload = _home_get("https://c.y.qq.com/node/musicmac/v6/index.html",
                        headers=_headers(_cookie_str()), raw=True)
    if not isinstance(payload, (bytes, bytearray)):
        return ""
    payload = bytes(payload)
    try:
        text = gzip.decompress(payload).decode("utf-8", "replace")
    except Exception:
        try:
            text = zlib.decompress(payload, -zlib.MAX_WBITS).decode("utf-8", "replace")
        except Exception:
            text = payload.decode("utf-8", "replace")
    return html.unescape(text)


def _home_banners(limit=5):
    """音乐馆推荐页运营 Banner: data-type=10002 为专辑, 点击进入专辑歌曲列表."""
    s = _home_page_html()
    pat = (r'<p class="focus__pic[^>]*>\s*<a[^>]*data-rid="(\d+)"'
           r'[^>]*data-type="(\d+)"[^>]*>\s*<picture>\s*<img[^>]*src="([^"]+)')

    def banner_card(m):
        ref, typ, cover = m.group(1), m.group(2), m.group(3)
        if typ != "10002":
            return None
        return {"id": ref, "ref": ref, "albumid": ref,
                "title": "QQ音乐精选专辑", "cover": cover,
                "sub": "QQ音乐 · 精选活动", "tag": "精选活动", "action": "album"}

    cards = _parallel_map(banner_card, list(re.finditer(pat, s)), limit=5)
    out, seen = [], set()
    for c in cards:
        if not c or c["ref"] in seen:
            continue
        seen.add(c["ref"])
        out.append(c)
        if len(out) >= limit:
            break
    return out


_HOME_TOPLIST_META = {
    26: ("巅峰榜·热歌", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174743.jpg"),
    27: ("巅峰榜·新歌", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174831.jpg"),
    4:  ("巅峰榜·流行指数", "https://y.gtimg.cn/music/common/upload/test_order_channel_hitlist_con/1847681.png"),
    3:  ("巅峰榜·欧美", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150122151215.jpg"),
    5:  ("巅峰榜·内地", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174759.jpg"),
    62: ("飙升榜", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174903.jpg"),
    64: ("巅峰榜·香港", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174920.jpg"),
    65: ("巅峰榜·台湾", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174937.jpg"),
    17: ("巅峰榜·日本", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820174954.jpg"),
    52: ("巅峰榜·韩国", "https://y.gtimg.cn/music/common/upload/iphone_order_channel/20150820175011.jpg"),
}


def _home_toplist_info(topid):
    """巅峰榜卡片: 静态元数据, 点击时再请求真实榜单内容."""
    title, cover = _HOME_TOPLIST_META.get(int(topid), ("巅峰榜", ""))
    return {"title": title, "cover": cover, "sub": "QQ音乐 · 巅峰榜",
            "topid": int(topid), "action": "toplist"}


def _fmt_count(v):
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return ""
    return "%.1f万次播放" % (n / 10000.0) if n >= 10000 else "%d次播放" % n


def _home_song(t):
    """首页/榜单里的 QQ 歌曲条目 -> 统一 song 结构."""
    singers = "/".join((s.get("name") or "") for s in (t.get("singer") or []))
    pay = t.get("pay") or {}
    vip = bool(pay.get("pay_play") or pay.get("payplay") or
               pay.get("pay_status") or pay.get("paystatus") or
               pay.get("pay_month") or pay.get("paymonth"))
    return song("qq", t.get("songmid") or t.get("mid") or t.get("songid"),
                t.get("songname") or t.get("name") or t.get("title") or "",
                singers, t.get("albumname") or "", t.get("interval") or 0,
                _cover(t.get("albummid")),
                extra={"vip": vip, "media_mid": t.get("strMediaMid") or ""})


def _home_new_songs(limit=30):
    """首页快捷入口用的新歌, 与 recommend 共享解析但使用首页短超时."""
    data = {
        "comm": {"ct": 24, "cv": 4747474},
        "new_song": {
            "module": "QQMusic.MusichallServer",
            "method": "GetNewSong",
            "param": {"type": 0},
        },
    }
    url = ("https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&data=" +
           urllib.parse.quote(json.dumps(data, separators=(",", ":"))))
    j = _home_get(url, headers=QQ_HEADERS)
    payload = (j.get("new_song") or {}).get("data") or {}
    rows = payload.get("song_list") or []
    out = []
    for t in rows[:limit]:
        singers = "/".join((s.get("name") or "") for s in t.get("singer") or [])
        album = t.get("album") or {}
        pay = t.get("pay") or {}
        vip = bool(pay.get("pay_play") or pay.get("payplay") or
                   pay.get("pay_status") or pay.get("paystatus") or
                   pay.get("pay_month") or pay.get("paymonth") or
                   pay.get("note") not in (0, None))
        media = ((t.get("file") or {}).get("media_mid") or t.get("media_mid") or "")
        mid = t.get("mid") or t.get("songmid") or t.get("id")
        if not mid:
            continue
        out.append(song("qq", mid, t.get("name") or t.get("title") or "",
                        singers, album.get("name", ""), t.get("interval", 0),
                        _cover(album.get("mid")),
                        extra={"vip": vip, "media_mid": media}))
    return out


def home(limit=30):
    """QQ 首页: 各区块并行抓取, 单次请求短超时且总预算内返回."""
    token = _HOME_DEADLINE.set(time.monotonic() + _HOME_BUDGET)
    try:
        def task_playlists():
            return _cached_home_part("playlists", lambda: _home_public_playlists(limit=12),
                                     allow_empty=False)

        def task_charts():
            return _cached_home_part("charts", lambda: [
                _home_toplist_info(tid) for tid in _HOME_TOPLIST_META],
                                     allow_empty=False)

        def task_activities():
            def load():
                cards = _home_banners(limit=4)
                return cards if cards else [
                    {"id": "67421058", "ref": "67421058", "albumid": "67421058",
                     "title": "QQ音乐精选专辑",
                     "cover": "https://y.qq.com/music/common/upload/MUSIC_FOCUS/7032830.png?max_age=2592000",
                     "sub": "QQ音乐 · 精选活动", "tag": "精选活动", "action": "album"},
                    {"id": "67445700", "ref": "67445700", "albumid": "67445700",
                     "title": "QQ音乐新碟",
                     "cover": "https://y.qq.com/music/common/upload/ocs/a784ddef0b4632a2c9fa822a16f5a6a6.jpg?max_age=2592000",
                     "sub": "QQ音乐 · 精选活动", "tag": "精选活动", "action": "album"},
                ]
            return _cached_home_part("activities", load, allow_empty=False)

        tasks = [task_playlists, task_charts, task_activities]
        results = _parallel_map(lambda fn: fn(), tasks, limit=3)
        new_songs = []
        playlists = (results[0] or [])[:12]
        charts = results[1] or []
        activities = results[2] or []
        quick = [
            {"title": "每日推荐", "sub": "QQ音乐新歌速递", "cover": "", "tag": "每日更新",
             "action": "songs", "mode": "new"},
            {"title": "新歌速递", "sub": "最新上架歌曲", "cover": "", "tag": "新歌",
             "action": "songs", "mode": "new"},
            {"title": "巅峰榜·热歌", "sub": "站内播放热度", "cover": "", "tag": "榜单",
             "action": "toplist", "topid": 26},
            {"title": "私人推荐", "sub": "根据常听风格推荐",
             "cover": playlists[0].get("cover") if playlists else "", "tag": "个性化",
             "action": "playlist", "ref": playlists[0].get("ref") if playlists else ""},
            {"title": "经典专区", "sub": "热门怀旧金曲", "cover": "", "tag": "精选",
             "action": "search", "keyword": "经典老歌"},
            {"title": "听·周杰伦", "sub": "艺人歌曲精选", "cover": "", "tag": "艺人",
             "action": "search", "keyword": "周杰伦"},
        ]
        return {"quick": quick, "playlists": playlists, "charts": charts,
                "activities": activities, "songs": new_songs, "source": "qq"}
    finally:
        _HOME_DEADLINE.reset(token)


def recommend(limit=30, mode=None, topid=None, albumid=None):
    """QQ 推荐: 默认新歌; topid 指定巅峰榜, albumid 指定精选专辑."""
    if albumid:
        try:
            j = http_get("https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
                         "?albumid=%s&format=json" % urllib.parse.quote(str(albumid)),
                         headers=_headers(_cookie_str()))
        except Exception as e:
            return {"songs": [], "error": str(e)}
        rows = (j.get("data") or {}).get("list") or []
        out = []
        for t in rows[:limit]:
            if not (t.get("songmid") or t.get("songid")):
                continue
            singers = "/".join((s.get("name") or "") for s in (t.get("singer") or []))
            pay = t.get("pay") or {}
            out.append(song("qq", t.get("songmid") or t.get("songid"),
                            t.get("songname") or t.get("songorig") or "",
                            singers, t.get("albumname") or "", t.get("interval") or 0,
                            _cover(t.get("albummid") or t.get("strMediaMid")),
                            extra={"vip": bool(pay.get("payplay") or pay.get("payalbum")),
                                   "media_mid": t.get("strMediaMid") or ""}))
        return {"songs": out, "has_more": False}
    if topid:
        try:
            j = http_get("https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg"
                         "?topid=%s&format=json" % urllib.parse.quote(str(topid)),
                         headers=_headers(_cookie_str()))
        except Exception as e:
            return {"songs": [], "error": str(e)}
        rows = []
        for r in (j.get("songlist") or [])[:limit]:
            t = r.get("data") or {}
            if t.get("songmid"):
                rows.append(_home_song(t))
        return {"songs": rows, "has_more": False}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(100, limit))
    return {"songs": _home_new_songs(limit), "has_more": False}


def get_playlist(ref, quick=False):
    """歌单内容: 经典端点 fcg_ucc_getcdinfo_byids_cp (cdlist[0].songlist),
    登录 Cookie 下可完整读取"我喜欢"等私有歌单. quick=True 供首页可用性探测,
    使用短超时且不重试, 避免阻塞首页加载."""
    ref = ("%s" % ref).strip()
    ck = _cookie_str()
    uin = _uin()
    gk = _gtk_login() if ck else _gtk()
    url = ("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
           "?type=1&json=1&utf8=1&onlysong=0&disstid=%s&g_tk=%d&loginUin=%s"
           "&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0"
           "&platform=yqq&needNewCode=0" % (urllib.parse.quote(ref), gk, uin if ck else "0"))
    try:
        if quick:
            j = _home_get(url, headers=_headers(ck) if ck else QQ_HEADERS)
        else:
            j = http_get(url, headers=_headers(ck) if ck else QQ_HEADERS)
    except Exception as e:
        return {"error": "获取歌单失败: %s" % e}
    cds = j.get("cdlist") or []
    vec = (cds[0].get("songlist") or []) if cds else []
    if not vec:
        return {"error": "歌单为空或无权限"}
    out = []
    for t in vec:
        singers = "/".join((s.get("name") or "") for s in t.get("singer") or [])
        pay = t.get("pay") or {}
        vip = bool(pay.get("payplay") or pay.get("payfull") or pay.get("note") not in (0, None))
        out.append(song("qq", t.get("songmid") or t.get("id"), t.get("songname"),
                        singers, t.get("albumname", ""), t.get("interval", 0),
                        _cover(t.get("albummid")),
                        extra={"vip": vip, "media_mid": t.get("media_mid") or ""}))
    return {"songs": out}


def get_stream(s):
    """CgiGetVkey: 传 songmid, 返回 midurlinfo[0].purl(相对路径, 拼 sip[0] 得完整地址).
    登录 Cookie 有效时可解锁 VIP 曲; 未登录/无版权 purl 空."""
    mid = s["id"]
    guid = _guid()
    ck = _cookie_str()
    uin = _uin()
    gk = _gtk_login() if ck else _gtk()
    data = {"req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                      "param": {"guid": guid, "songmid": [mid], "songtype": [0],
                                "uin": uin, "loginflag": 1, "platform": "20"}},
            "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0}}
    url = ("https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&g_tk=%d&loginUin=%s"
           "&hostUin=0&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json"
           "&needNewCode=0&data=%s" % (gk, uin, urllib.parse.quote(json.dumps(data))))
    try:
        j = http_get(url, headers=_headers(ck))
        d = (j.get("req_0") or {}).get("data") or {}
        items = d.get("midurlinfo") or []
        purl = items[0].get("purl") if items else ""
        if purl:
            sip = d.get("sip") or []
            base = sip[0] if sip and sip[0] else "http://dl.stream.qqmusic.qq.com/"
            h = {"Referer": "https://y.qq.com/", "User-Agent": UA}
            return {"url": base + purl, "headers": h}
        return {"error": "该曲目需要VIP或付费" + ("(Cookie 可能失效)" if ck else "")}
    except Exception as e:
        return {"error": "获取播放地址失败: %s" % e}


def get_lyrics(s):
    """经典歌词端点 fcg_query_lyric_new, 返回 base64 编码的 LRC, 需解码."""
    import base64
    mid = s["id"]
    gk = _gtk_login()
    url = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
           "?songmid=%s&g_tk=%d&loginUin=%s&hostUin=0&format=json&inCharset=utf8"
           "&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0"
           % (urllib.parse.quote(str(mid)), gk, _uin()))
    h = dict(QQ_HEADERS)
    h["Referer"] = "https://y.qq.com/portal/player.html"
    try:
        j = http_get(url, headers=h)
        raw = j.get("lyric") or ""
        if not raw:
            return {"error": "暂无歌词"}
        try:
            ly = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            ly = raw
        if not ly.strip():
            return {"error": "暂无歌词"}
        return {"lyric": ly}
    except Exception as e:
        return {"error": "获取歌词失败: %s" % e}
