# -*- coding: utf-8 -*-
"""抖音音源: 对齐其他平台的统一接口.

第一版支持搜索、首页、推荐、歌单、取流、歌词、登录态和分享链接解析。
抖音网页接口可能需要 Cookie / a_bogus 等签名参数; 未配置或风控时返回明确错误,
不把空结果伪装成可用。分享链接解析不依赖签名, 可作为最稳定的入口。
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import secure_store, song
from .common import http_get, UA, data_dir

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = data_dir()
COOKIE_FILE = os.path.join(DATA_DIR, "douyin_cookie.json")
CACHE_FILE = os.path.join(DATA_DIR, "douyin_cache.json")

HEADERS = {
    "Referer": "https://www.douyin.com/",
    "Origin": "https://www.douyin.com",
    "Accept": "application/json, text/plain, */*",
}
MOBILE_HEADERS = {
    "Referer": "https://www.iesdouyin.com/",
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                   "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"),
}
_HOME_TTL = 300
_HOME_CACHE = {}
_STREAM_TTL = 1800
_STREAM_CACHE = {}
_FEED_TTL = 180
_FEED_CACHE = {"songs": [], "ts": 0.0}


def _load_cookie():
    ck = secure_store.load_json(COOKIE_FILE)
    return ck if isinstance(ck, dict) else {}


def _cookie_str():
    parts = ["%s=%s" % (k, v) for k, v in _load_cookie().items() if v]
    return "; ".join(parts) if parts else ""


def _headers(extra=None):
    h = dict(HEADERS)
    ck = _cookie_str()
    if ck:
        h["Cookie"] = ck
    if extra:
        h.update(extra)
    return h


def status():
    ck = _load_cookie()
    return {"logged_in": bool(ck), "uin": str(ck.get("uid_tt") or ck.get("sessionid") or "")[:32]}


def login_cookies():
    """返回已保存的登录 Cookie, 供应用内隐藏 WebView2 注入后由页面自行签名搜索。"""
    return dict(_load_cookie())


def manual_cookie(raw):
    raw = (raw or "").strip()
    if not raw:
        return {"error": "Cookie 为空"}
    ck = {}
    # 不再按白名单过滤: 抖音登录态还依赖 LOGIN_STATUS 等字段, 过滤会丢登录标记
    for seg in raw.replace("\n", ";").replace(",", ";").split(";"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k = k.strip()
        if k and v.strip():
            ck[k] = v.strip()
    if not ck:
        return {"error": "未解析到抖音 Cookie 字段"}
    secure_store.save_json(COOKIE_FILE, ck)
    return {"ok": True, "logged_in": True}


def logout():
    try:
        os.remove(COOKIE_FILE)
    except OSError:
        pass
    _STREAM_CACHE.clear()
    return {"ok": True}


def _err(j, default="抖音接口返回异常"):
    if not isinstance(j, dict):
        return default
    if j.get("error"):
        return str(j.get("error"))
    if j.get("_nonjson"):
        return "抖音接口返回非 JSON（可能需要 Cookie 或签名）"
    return default


def _num(v, default=0):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return default


def _pick_url(obj):
    """从抖音常见的 url_list / play_addr / download_addr 结构中取一个 http(s) URL。"""
    if isinstance(obj, str):
        return obj if obj.startswith("http") else ""
    if not isinstance(obj, dict):
        return ""
    for key in ("url_list", "urls", "url", "src", "uri", "play_addr", "download_addr",
                "bitrateInfo"):
        val = obj.get(key)
        if isinstance(val, list):
            for x in val:
                u = _pick_url(x)
                if u:
                    return u
        elif isinstance(val, str) and val.startswith("http"):
            return val
        elif isinstance(val, dict):
            u = _pick_url(val)
            if u:
                return u
    return ""


def _extract_aweme_id(text):
    text = html.unescape(str(text or "")).strip()
    m = re.search(r"(?:aweme_id|modal_id|item_id|video/|share/video/|share/slide/)[=/]?([0-9]{6,24})", text)
    if m:
        return m.group(1)
    m = re.search(r"https?://[^\s\"']+", text)
    if not m:
        return ""
    url = m.group(0)
    m = re.search(r"/([0-9]{6,24})(?:[/?#]|$)", url)
    if m:
        return m.group(1)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=3) as resp:
            final = resp.geturl()
        m = re.search(r"/(?:video|slide|note)/([0-9]{6,24})", final)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _pick_video_url(video):
    """优先取 /aweme/v1/play/ 跳转地址.

    douyinvod 的签名直链在服务端请求常被防盗链判定为 403, 而 /aweme/v1/play/
    会 302 到当前会话可用的 CDN 地址, 只需要 Referer 即可 206 播放。
    """
    if not isinstance(video, dict):
        return ""
    for key in ("play_addr", "download_addr"):
        addr = video.get(key) or {}
        urls = addr.get("url_list") if isinstance(addr, dict) else None
        for u in urls or []:
            if isinstance(u, str) and "/aweme/v1/play/" in u:
                return u
    return ""


def _aweme_to_song(aweme):
    """把 aweme_detail / aweme_list 中的一项转换为统一 song。"""
    if not isinstance(aweme, dict):
        return None
    aid = str(aweme.get("aweme_id") or aweme.get("id") or "")
    if not aid:
        return None
    author = aweme.get("author") or {}
    music = aweme.get("music") or {}
    video = aweme.get("video") or {}
    cover = _pick_url(aweme.get("cover") or video.get("cover") or video.get("origin_cover")
                      or music.get("cover_hd") or music.get("cover_medium"))
    # 视频 CDN(douyinvod) 直链常返回 403；原声音频(douyinstatic/mp3)可直接播放。
    audio = _pick_url(music.get("play_url") or music.get("play_addr"))
    video_url = (_pick_video_url(video)
                 or _pick_url(video.get("play_addr") or video.get("download_addr")))
    play = audio or video_url
    title = (aweme.get("desc") or music.get("title") or "抖音视频").strip()
    artist = (author.get("nickname") or music.get("author") or "抖音用户").strip()
    duration = _num((music.get("duration") or aweme.get("duration") or video.get("duration")), 0)
    if duration > 10000:
        duration = duration // 1000
    stats = aweme.get("statistics") or {}
    avatar = _pick_url(author.get("avatar_thumb") or author.get("avatar_medium")
                       or author.get("avatar_larger"))
    s = song("douyin", aid, title[:120], artist[:80], "抖音", duration, cover,
             extra={"aweme_id": aid, "play_url": play, "audio_url": audio,
                    "video_url": video_url,
                    "share_url": "https://www.douyin.com/video/" + aid,
                    "music_id": str(music.get("id") or "")})
    # 展示用统计数据放顶层, 不进 extra: extra 参与 songKey 身份计算, 数值变化会造成去重失效
    s["stats"] = {"digg": _num(stats.get("digg_count")),
                  "comment": _num(stats.get("comment_count")),
                  "collect": _num(stats.get("collect_count")),
                  "share": _num(stats.get("share_count")),
                  "create_time": _num(aweme.get("create_time")),
                  "author_avatar": avatar}
    return s


def songs_from_awemes(items, limit=30):
    """把搜索/推荐返回的 aweme 列表转换为统一 song, 去重并限制数量。"""
    out, seen = [], set()
    for aweme in items or []:
        s = _aweme_to_song(aweme)
        if s and s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _music_to_song(m):
    """抖音搜索音乐卡片里的曲目(带 mp3 play_url)转成统一 song。"""
    if not isinstance(m, dict):
        return None
    mid = str(m.get("id_str") or m.get("id") or "")
    if not mid:
        return None
    title = (m.get("title") or "").strip()
    author = (m.get("author") or "").strip() or "抖音音乐"
    if not title:
        return None
    cover = _pick_url(m.get("cover_large") or m.get("cover_hd")
                      or m.get("cover_medium") or m.get("cover_thumb"))
    play = _pick_url(m.get("play_url"))
    duration = _num(m.get("duration"), 0)
    if duration > 10000:
        duration = duration // 1000
    return song("douyin", mid, title[:120], author[:80], (m.get("album") or "抖音")[:60],
                duration, cover,
                extra={"music_id": mid, "play_url": play, "audio_url": play,
                       "share_url": m.get("schema_url") or ""})


def songs_from_music_tracks(tracks, limit=30):
    """把搜索音乐卡片中的曲目转换为 song, 这类结果可直接播放原声 mp3。"""
    out, seen = [], set()
    for m in tracks or []:
        s = _music_to_song(m)
        if s and s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _detail_params(aweme_id):
    return {
        "aweme_id": aweme_id,
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "platform": "PC",
        "downlink": "10",
    }


def _fetch_detail(aweme_id):
    if not aweme_id:
        return {}
    url = ("https://www.douyin.com/aweme/v1/web/aweme/detail/?"
           + urllib.parse.urlencode(_detail_params(aweme_id)))
    j = http_get(url, headers=_headers(), timeout=3, attempts=1)
    if isinstance(j, dict) and isinstance(j.get("aweme_detail"), dict):
        return j["aweme_detail"]
    share = "https://www.iesdouyin.com/share/video/%s/" % aweme_id
    raw = http_get(share, headers=MOBILE_HEADERS, raw=True, timeout=3, attempts=1)
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
        m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", text, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                loader = ((data.get("loaderData") or {}).get("video_(id)/page") or {})
                return loader.get("videoInfoRes", {}).get("item_list", [{}])[0] or {}
            except Exception:
                pass
        m = re.search(r'"aweme_id"\s*:\s*"(\d+)"', text)
        if m:
            return {"aweme_id": m.group(1), "desc": "抖音分享视频"}
    return {}


def _web_search(keyword, limit, offset):
    """尝试抖音网页通用搜索; 需要 Cookie/签名时可自然失败。"""
    params = {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "search_channel": "aweme_general", "sort_type": "general", "publish_time": "0",
        "keyword": keyword, "search_source": "normal_search", "query_correct_type": "1",
        "is_filter_search": "0", "offset": offset, "count": limit,
        "version_code": "170400", "version_name": "17.4.0", "cookie_enabled": "true",
        "platform": "PC", "downlink": "10",
    }
    url = "https://www.douyin.com/aweme/v1/web/general/search/single/?" + urllib.parse.urlencode(params)
    j = http_get(url, headers=_headers({"Referer": "https://www.douyin.com/search/" + urllib.parse.quote(keyword)}),
                 timeout=8, attempts=1)
    items = []
    if isinstance(j, dict):
        for row in j.get("data") or []:
            aweme = ((row.get("aweme_info") or row.get("aweme_detail") or {}) if isinstance(row, dict) else {})
            if aweme:
                items.append(aweme)
            elif isinstance(row, dict) and row.get("aweme_list"):
                items.extend(row["aweme_list"])
    return items, j


def _suggest_search(keyword, limit):
    """无需签名的公开联想词接口; 结果仅作为可搜索词条。"""
    url = ("https://www.douyin.com/aweme/v1/web/api/suggest_words/?"
           + urllib.parse.urlencode({"keyword": keyword, "aid": "6383", "device_platform": "webapp"}))
    j = http_get(url, headers=_headers(), timeout=5, attempts=1)
    words = []
    if isinstance(j, dict):
        for group in j.get("data") or j.get("suggest_words") or []:
            if isinstance(group, dict):
                for w in group.get("words") or []:
                    word = w.get("word") if isinstance(w, dict) else str(w)
                    if word:
                        words.append(word)
    return words[:limit]


def search(keyword, limit=30, page=1):
    page = max(1, int(page))
    limit = min(max(1, int(limit)), 50)
    keyword = (keyword or "").strip()
    if not keyword:
        return {"songs": [], "page": page, "total": 0, "page_size": limit, "has_more": False}
    aid = _extract_aweme_id(keyword)
    linked = _fetch_detail(aid) if aid else {}
    linked_song = _aweme_to_song(linked) if linked else None
    items, raw = _web_search(keyword, limit, (page - 1) * limit)
    out = []
    if linked_song:
        out.append(linked_song)
    for aweme in items:
        s = _aweme_to_song(aweme)
        if s and s["id"] != (linked_song or {}).get("id"):
            out.append(s)
        if len(out) >= limit:
            break
    if not out:
        # 搜索接口风控时回退到已可访问的推荐流，只保留与关键词相关的结果。
        feed = _feed_songs(max(limit * 2, 30), page)
        needle = keyword.lower()
        matched = [s for s in feed
                   if needle in s.get("title", "").lower()
                   or needle in s.get("artist", "").lower()]
        if matched:
            return {"songs": matched[:limit], "page": page, "total": len(matched),
                    "page_size": limit, "has_more": len(matched) >= limit,
                    "note": "搜索接口触发风控，已展示推荐流中的相关内容"}
        words = _suggest_search(keyword, limit)
        if words:
            return {"songs": [], "page": page, "total": 0, "page_size": limit,
                    "has_more": False, "suggestions": words,
                    "error": "抖音搜索需要 Cookie/签名；可粘贴分享链接，或尝试联想词"}
        return {"songs": [], "page": page, "total": 0, "page_size": limit,
                "has_more": False,
                "error": "抖音搜索未返回结果；请粘贴分享链接或在账号设置中配置 Cookie"}
    return {"songs": out, "page": page, "total": None, "page_size": limit,
            "has_more": len(out) >= limit}


def _cached(key, ttl, fn):
    now = time.time()
    rec = _HOME_CACHE.get(key)
    if rec and now - rec[0] < ttl:
        return rec[1]
    val = fn()
    _HOME_CACHE[key] = (now, val)
    return val


def _recommend_params(refresh_index=1):
    return {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0",
        "cookie_enabled": "true", "platform": "PC", "downlink": "10",
        "count": 20, "refresh_index": refresh_index, "fresh_idx": refresh_index,
        "fresh_id": "", "feed_style": "1", "pull_type": "2",
        "last_ad_show_interval": "-1", "need_personalized": "true",
        "filter_warn": "0", "is_vcd": "0", "category": "0",
    }


def _fetch_feed(refresh_index=1, limit=30):
    """并发拉取几页推荐流，只等待一次网络往返时间。"""
    def one(idx):
        url = ("https://www.douyin.com/aweme/v1/web/tab/feed/?"
               + urllib.parse.urlencode(_recommend_params(idx)))
        j = http_get(url, headers=_headers({"Referer": "https://www.douyin.com/?recommend=1"}),
                     timeout=4, attempts=1)
        batch = []
        if isinstance(j, dict):
            batch = j.get("aweme_list") or j.get("data") or []
            if not isinstance(batch, list):
                batch = []
            if not batch and isinstance(j.get("data"), dict):
                batch = j["data"].get("aweme_list") or []
            return batch, j
        return [], j

    start = max(1, refresh_index)
    items, raw = [], {}
    pages = 8
    with ThreadPoolExecutor(max_workers=pages) as pool:
        futures = [pool.submit(one, i) for i in range(start, start + pages)]
        for fut in as_completed(futures):
            try:
                batch, j = fut.result()
            except Exception:
                continue
            if isinstance(j, dict):
                raw = j
            items.extend(batch)
    return items[:max(limit * 3, 80)], raw


def _fetch_feed_page(refresh_index):
    """拉取单页推荐流。"""
    url = ("https://www.douyin.com/aweme/v1/web/tab/feed/?"
           + urllib.parse.urlencode(_recommend_params(max(1, refresh_index))))
    j = http_get(url, headers=_headers({"Referer": "https://www.douyin.com/?recommend=1"}),
                 timeout=6, attempts=1)
    items = []
    if isinstance(j, dict):
        items = j.get("aweme_list") or j.get("data") or []
        if not isinstance(items, list):
            items = []
        if not items and isinstance(j.get("data"), dict):
            items = j["data"].get("aweme_list") or []
    return items


def feed_page(limit=20, index=1):
    """抖音推荐流分页: 返回当前页歌曲与下一页游标, 供前端无限下拉。"""
    limit = min(max(1, int(limit or 20)), 40)
    index = max(1, int(index or 1))
    pages = 4 if index == 1 else 3
    items = []
    with ThreadPoolExecutor(max_workers=pages) as pool:
        futures = [pool.submit(_fetch_feed_page, i) for i in range(index, index + pages)]
        for fut in as_completed(futures):
            try:
                items.extend(fut.result())
            except Exception:
                continue
    songs = songs_from_awemes(items, limit)
    return {"songs": songs, "next_index": index + pages, "has_more": bool(songs)}


def _static_cards():
    return {
        "quick": [
            {"title": "抖音推荐", "sub": "调用网页首页推荐流", "cover": "", "tag": "个性化",
             "action": "songs", "mode": "feed"},
            {"title": "粘贴分享链接", "sub": "解析抖音视频/音乐", "cover": "", "tag": "稳定入口",
             "action": "search", "keyword": "https://v.douyin.com/"},
            {"title": "抖音热歌", "sub": "热门音乐搜索", "cover": "", "tag": "热歌",
             "action": "search", "keyword": "抖音热歌"},
        ],
        "playlists": [
            {"id": "feed", "ref": "feed", "title": "抖音推荐流", "cover": "", "sub": "网页首页推荐", "tag": "推荐", "count": 20, "action": "songs", "mode": "feed"},
            {"id": "hot", "ref": "hot", "title": "抖音热歌", "cover": "", "sub": "关键词精选", "tag": "歌单", "count": 30, "action": "songs", "mode": "hot"},
        ],
        "charts": [
            {"id": "hot", "ref": "hot", "topid": "hot", "title": "抖音热歌榜", "cover": "", "sub": "需要 Cookie/签名", "tag": "榜单", "action": "toplist"},
        ],
        "activities": [
            {"id": "cookie", "ref": "cookie", "title": "配置抖音登录", "cover": "", "sub": "扫码/应用内登录", "tag": "账号", "action": "search", "keyword": "请在账号设置中配置抖音登录"},
        ],
        "songs": [], "source": "douyin",
    }


def home(limit=30):
    data = dict(_cached("home", _HOME_TTL, lambda: _static_cards()))
    data["songs"] = _feed_songs(min(max(1, int(limit or 30)), 30))
    return data


def _feed_songs(limit=30, start=1):
    """缓存推荐流；缓存不足一页时重新拉取，避免永远只返回少量结果。"""
    now = time.time()
    cached = _FEED_CACHE["songs"]
    fresh = cached and now - _FEED_CACHE["ts"] < _FEED_TTL
    if fresh and len(cached) >= limit:
        return cached[:limit]
    items, _ = _fetch_feed(start, max(limit, 30))
    base = cached if fresh else []
    out = list(base)
    seen = {s.get("id") for s in base}
    for aweme in items:
        s = _aweme_to_song(aweme)
        if s and s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
    if out:
        _FEED_CACHE.update({"songs": out, "ts": now})
    return out[:limit]


def recommend(limit=30, mode=None, topid=None, albumid=None):
    limit = min(max(1, int(limit or 30)), 50)
    mode = mode or topid or "feed"
    if mode == "feed":
        out = _feed_songs(limit, 1)
        return {"songs": out, "has_more": bool(out)} if out else {
            "songs": [], "has_more": False,
            "error": "抖音推荐需要 Cookie/签名；可粘贴分享链接播放"}
    word = {"hot": "抖音热歌", "new": "抖音新歌", "toplist": "抖音热歌榜"}.get(mode, str(mode))
    items, raw = _web_search(word, limit, 0)
    out = []
    for aweme in items:
        s = _aweme_to_song(aweme)
        if s:
            out.append(s)
    if not out:
        # 搜索接口风控时仍展示推荐流，避免首页/热歌入口完全空白。
        out = _feed_songs(limit, 2)
    if not out:
        return {"songs": [], "has_more": False,
                "error": "抖音推荐需要 Cookie/签名；可粘贴分享链接播放"}
    return {"songs": out[:limit], "has_more": len(out) >= limit}


def get_playlist(ref):
    ref = str(ref or "").strip()
    if ref.startswith("http") or re.search(r"[0-9]{6,24}", ref):
        aid = _extract_aweme_id(ref)
        detail = _fetch_detail(aid)
        s = _aweme_to_song(detail)
        if s:
            return {"songs": [s]}
    if ref in ("hot", "share", "cookie"):
        return recommend(limit=30, mode="hot")
    items, raw = _web_search(ref, 30, 0)
    out = []
    for aweme in items:
        s = _aweme_to_song(aweme)
        if s:
            out.append(s)
    if out:
        return {"songs": out}
    return {"songs": [], "error": "歌单为空；抖音网页歌单需要登录态，可粘贴分享链接"}


def list_my_playlists():
    ck = _cookie_str()
    if not ck:
        return {"playlists": [], "error": "未登录抖音账号"}
    # 网页端收藏/歌单接口签名随版本变化，稳定返回空列表而非错误数据。
    return {"playlists": [], "error": "抖音网页歌单暂未开放"}


def get_stream(s):
    extra = s.get("extra") or {}
    aid = str(extra.get("aweme_id") or s.get("id") or "")
    url = str(extra.get("audio_url") or "")
    if not url:
        play = str(extra.get("play_url") or "")
        # 视频 CDN 常 403，只有音频 CDN 可直接播放。
        url = "" if "douyinvod.com" in play else play
    key = aid or url
    now = time.time()
    rec = _STREAM_CACHE.get(key)
    if rec and now - rec[0] < _STREAM_TTL and rec[1]:
        return {"url": rec[1], "headers": {"Referer": "https://www.douyin.com/", "User-Agent": UA}}
    if not url:
        detail = _fetch_detail(_extract_aweme_id(str(s.get("id") or "")) or aid)
        if detail:
            fresh = _aweme_to_song(detail)
            if fresh:
                url = (fresh.get("extra") or {}).get("audio_url") or ""
    if not url:
        return {"error": "未取得播放地址；请粘贴分享链接重试，或配置抖音 Cookie"}
    _STREAM_CACHE[key] = (now, url)
    return {"url": url, "headers": {"Referer": "https://www.douyin.com/", "User-Agent": UA}}


def get_video(s, qn=None):
    """返回抖音视频流地址(供 /proxy?kind=video 代理播放)."""
    extra = s.get("extra") or {}
    aid = str(extra.get("aweme_id") or s.get("id") or "")
    url = str(extra.get("video_url") or "")
    # 旧数据里可能是 douyinvod 直链(服务端 403), 需要重新解析
    if url and "douyinvod.com" in url:
        url = ""
    if not url:
        detail = _fetch_detail(_extract_aweme_id(str(s.get("id") or "")) or aid)
        if detail:
            fresh = _aweme_to_song(detail)
            if fresh:
                url = str((fresh.get("extra") or {}).get("video_url") or "")
    if not url:
        return {"error": "未取得视频地址"}
    return {"url": url,
            "headers": {"Referer": "https://www.douyin.com/", "User-Agent": UA}}


def comments(aweme_id, cursor=0, count=20):
    """网页版评论列表(支持分页).

    抖音网关当前只校验 a_bogus 参数是否存在, 不校验内容, 因此这里自己拼一个
    64 位随机串即可, 不需要引入第三方签名库或后台浏览器。
    """
    aid = str(aweme_id or "").strip()
    if not aid:
        return {"comments": [], "cursor": 0, "has_more": False, "total": 0,
                "error": "缺少视频 ID"}
    params = {
        "aweme_id": aid,
        "cursor": str(max(0, int(cursor or 0))),
        "count": str(max(1, min(int(count or 20), 50))),
        "item_type": "0",
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "platform": "PC",
        "downlink": "10",
        "a_bogus": os.urandom(32).hex(),
    }
    ms = _load_cookie().get("msToken")
    if ms:
        params["msToken"] = ms
    url = "https://www.douyin.com/aweme/v1/web/comment/list/?" + urllib.parse.urlencode(params)
    headers = {
        "Referer": "https://www.douyin.com/video/%s" % aid,
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "x-tt-argus": "1",
    }
    ck = _cookie_str()
    if ck:
        headers["Cookie"] = ck
    j = http_get(url, headers=headers, timeout=6, attempts=1)
    if not isinstance(j, dict) or j.get("error"):
        return {"comments": [], "cursor": 0, "has_more": False, "total": 0,
                "error": "评论接口无响应"}
    out = []
    for c in j.get("comments") or []:
        if not isinstance(c, dict):
            continue
        user = c.get("user") or {}
        out.append({
            "cid": str(c.get("cid") or ""),
            "text": c.get("text") or "",
            "user": user.get("nickname") or "抖音用户",
            "avatar": _pick_url(user.get("avatar_thumb") or user.get("avatar_medium")),
            "like": _num(c.get("digg_count")),
            "time": _num(c.get("create_time") or c.get("createTime")),
            "reply": _num(c.get("reply_comment_total")),
            "ip": c.get("ip_label") or "",
        })
    return {"comments": out,
            "cursor": _num(j.get("cursor"), max(0, int(cursor or 0)) + len(out)),
            "has_more": bool(j.get("has_more")),
            "total": _num(j.get("total"), len(out))}


def get_lyrics(s):
    extra = s.get("extra") or {}
    lyric = extra.get("lyric") or ""
    if lyric:
        return {"lyric": str(lyric)}
    return {"lyric": "[00:00.00]该抖音内容暂无歌词\n[00:04.00]可粘贴分享链接播放视频原声"}


def resolve_share(text):
    aid = _extract_aweme_id(text)
    detail = _fetch_detail(aid) if aid else {}
    s = _aweme_to_song(detail)
    if not s:
        return {"error": "无法解析该分享链接；请确认链接完整，或配置 Cookie"}
    return {"songs": [s]}
