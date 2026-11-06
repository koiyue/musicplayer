# -*- coding: utf-8 -*-
"""网易云音乐音源: 移植自 dashboard.py(扫码登录+手动Cookie+歌单+播放地址), 新增搜索."""
import json
import os
import threading
import time
import urllib.parse

from . import secure_store, song
from .common import http_get, http_post, UA

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
COOKIE_FILE = os.path.join(DATA_DIR, "netease_cookie.json")

NE_HEADERS = {
    "Referer": "https://music.163.com/",
    "Origin": "https://music.163.com",
    "Accept": "application/json, text/plain, */*",
}

_lock = threading.Lock()
_unikey = None
_session = None   # 预热 NMTID, 降低 8821 风控
# UID 缓存绑定账号标识(MUSIC_U): 换号后的迟到响应不会污染新账号的缓存
_uid_cache = {"key": None, "uid": None}


def _account_key(ck):
    for seg in (ck or "").split(";"):
        seg = seg.strip()
        if seg.startswith("MUSIC_U="):
            return seg[len("MUSIC_U="):]
    return None


def _reset_uid_cache():
    with _lock:
        _uid_cache["key"] = None
        _uid_cache["uid"] = None


def _cookie_str():
    ck = secure_store.load_json(COOKIE_FILE)
    if not isinstance(ck, dict):
        return None
    parts = ["%s=%s" % (k, v) for k, v in ck.items() if v]
    return "; ".join(parts) if parts else None


def _headers(cookie=None):
    h = dict(NE_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    return h


def _get(url, cookie=None, timeout=12):
    return http_get(url, headers=_headers(cookie), timeout=timeout)


def _post(url, data, cookie=None, form=True):
    return http_post(url, data=data, headers=_headers(cookie), json_body=not form)


def _parse_cookie(raw):
    """兼容逗号分隔的多条 Set-Cookie 拼接; 只保留网易云登录相关键."""
    ck = {}
    for seg in (raw or "").replace(",", ";").split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            k = k.strip()
            if k in ("MUSIC_U", "NMTID"):
                ck[k] = v.strip()
    return ck


def _save_cookie(ck):
    if not ck.get("MUSIC_U"):
        return False
    secure_store.save_json(COOKIE_FILE, {"MUSIC_U": ck["MUSIC_U"], "NMTID": ck.get("NMTID", "")})
    return True


# ---------- 登录 ----------

def new_qr():
    """返回 (png_bytes, link). 二维码内容=官方Web登录页规则; 图片用第三方服务渲染."""
    global _session, _unikey
    if _session is None:
        try:
            import urllib.request
            req = urllib.request.Request("https://music.163.com/",
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ck = {}
                for line in (resp.headers.get_all("Set-Cookie") or []):
                    ck.update(_parse_cookie(line.split(";")[0]))
                if ck:
                    _session = "; ".join("%s=%s" % (k, v) for k, v in ck.items())
        except Exception:
            pass
    j = _post("https://music.163.com/api/login/qrcode/unikey", {"type": 1}, cookie=_session)
    key = j.get("unikey") or j.get("key")
    if not key:
        raise RuntimeError("获取 unikey 失败: " + json.dumps(j, ensure_ascii=False))
    link = "https://music.163.com/login?codekey=%s" % key
    png = None
    for tpl in ("https://quickchart.io/qr?size=240&text=%s",
                "https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=%s"):
        try:
            png = http_get(tpl % urllib.parse.quote(link, safe=""), raw=True, timeout=15)
            if png and png[:4] == b"\x89PNG":
                break
            png = None
        except Exception:
            png = None
    with _lock:
        _unikey = key
    return png, link


def poll():
    """801 等待 / 802 已扫码 / 800 过期 / 803 成功 / 8821 风控."""
    with _lock:
        key = _unikey
    if not key:
        return {"code": 800}
    j = _get("https://music.163.com/api/login/qrcode/client/login?key=%s&type=1" % key,
             cookie=_session)
    code = j.get("code")
    if code == 8821:
        return {"code": 8821,
                "error": "网易云风控拦截(8821 需行为验证码)，"
                         "请点下方「扫码被风控？在应用内登录」"}
    if code == 803:
        ck = _parse_cookie(j.get("cookie"))
        if not ck.get("MUSIC_U"):
            try:
                j2 = _post("https://music.163.com/api/login/qrcode/client/login",
                           {"key": key, "type": 1})
                ck.update(_parse_cookie(j2.get("cookie")))
            except Exception:
                pass
        if not _save_cookie(ck):
            return {"code": 803, "error": "登录成功但未取到 cookie, 请改用手动粘贴 Cookie"}
        _reset_uid_cache()
    return {"code": code}


def manual_cookie(raw):
    raw = (raw or "").strip()
    if not raw:
        return {"error": "cookie 为空"}
    ck = _parse_cookie(raw)
    if not ck.get("MUSIC_U"):
        if raw.startswith("MUSIC_U="):
            ck["MUSIC_U"] = raw.split("=", 1)[1].strip()
        elif "=" not in raw and len(raw) > 40:
            ck["MUSIC_U"] = raw
    if not ck.get("MUSIC_U"):
        return {"error": "未解析到 MUSIC_U, 请粘贴完整 Cookie 串(含 MUSIC_U=...)"}
    _reset_uid_cache()
    return {"ok": _save_cookie(ck), "logged_in": bool(_cookie_str())}


def status():
    return {"logged_in": bool(_cookie_str())}


def logout():
    _reset_uid_cache()
    try:
        os.remove(COOKIE_FILE)
    except OSError:
        pass
    return {"ok": True}


# ---------- 内容 ----------

def _get_uid(ck):
    key = _account_key(ck)
    if not key:
        return None
    with _lock:
        if _uid_cache["key"] == key and _uid_cache["uid"]:
            return _uid_cache["uid"]
    j = _get("https://music.163.com/api/nuser/account/get", cookie=ck)
    uid = (j.get("account") or {}).get("id")
    if uid:
        with _lock:
            # 仅当当前账号仍是发起请求时的账号才写缓存
            if _account_key(_cookie_str()) == key:
                _uid_cache["key"] = key
                _uid_cache["uid"] = uid
    return uid


def _artists(t):
    arr = t.get("ar") or t.get("artists") or []
    return "/".join((a.get("name") or "") for a in arr if a)


def _cover(t):
    al = t.get("al") or t.get("album") or {}
    return al.get("picUrl") or ""


def _dur(t):
    return (t.get("dt") or t.get("duration") or 0) // 1000


def _extra(t):
    # fee: 1=VIP, 4=付费专辑, 8=免费低音质/会员高音质。
    fee = t.get("fee") or 0
    return {"vip": fee in (1, 4, 8), "fee": fee}


def search(keyword, limit=30, page=1):
    """cloudsearch/pc 表单 POST(实测可用; api/search/pc/get 已 404)."""
    page = max(1, int(page))
    j = _post("https://music.163.com/api/cloudsearch/pc",
              {"s": keyword, "type": 1, "limit": limit, "offset": (page - 1) * limit},
              cookie=_cookie_str())
    result = (j.get("result") or {})
    out = []
    for t in result.get("songs") or []:
        out.append(song("netease", t.get("id"), t.get("name"), _artists(t),
                        (t.get("al") or {}).get("name", ""), _dur(t), _cover(t),
                        extra=_extra(t)))
    total = result.get("songCount") or result.get("total") or None
    total = int(total) if total else None
    has_more = (page * limit < total) if total else (len(out) >= limit)
    return {"songs": out, "page": page, "total": total, "page_size": limit, "has_more": has_more}


def list_my_playlists():
    ck = _cookie_str()
    if not ck:
        return {"error": "未登录"}
    uid = _get_uid(ck)
    if not uid:
        return {"error": "获取账号信息失败, cookie 可能已失效, 请重新登录"}
    j = _get("https://music.163.com/api/user/playlist?uid=%s&limit=1000&offset=0" % uid,
             cookie=ck)
    if j.get("code") not in (200, 201) and not j.get("playlist"):
        return {"error": "获取歌单失败: " + str(j.get("msg") or j.get("code"))}
    out = [{"id": p.get("id"), "name": p.get("name"),
            "cover": p.get("coverImgUrl"), "count": p.get("trackCount")}
           for p in j.get("playlist") or []]
    return {"playlists": out}


def get_playlist(ref):
    ck = _cookie_str()
    j = _get("https://music.163.com/api/v3/playlist/detail?id=%s&n=1000&s=0" % ref,
             cookie=ck)
    pl = j.get("playlist") or {}
    songs = []
    tracks = pl.get("tracks")
    if not tracks:
        ids = [ti.get("id") for ti in (pl.get("trackIds") or []) if ti.get("id")]
        if ids:
            sj = _post("https://music.163.com/api/song/detail",
                       {"ids": ids, "c": "[]"}, cookie=ck)
            tracks = sj.get("songs") or []
    for t in tracks or []:
        songs.append(song("netease", t.get("id"), t.get("name"), _artists(t),
                          (t.get("al") or {}).get("name", ""), _dur(t), _cover(t),
                        extra=_extra(t)))
    return {"songs": songs}


def get_stream(s):
    j = _get("https://music.163.com/api/song/enhance/player/url?ids=[%s]&br=320000"
             % s["id"], cookie=_cookie_str())
    data = j.get("data") or []
    url = data[0].get("url") if data else None
    if not url:
        return {"error": "无版权或需要VIP"}
    return {"url": url, "headers": {"Referer": "https://music.163.com/",
                                    "User-Agent": UA}}


def get_lyrics(s):
    """返回 {"lyric": LRC文本}. 经典歌词接口, 无需登录."""
    try:
        j = _get("https://music.163.com/api/song/lyric?id=%s&lv=-1&kv=-1&tv=-1" % s["id"],
                 cookie=_cookie_str())
        ly = (j.get("lrc") or {}).get("lyric") or ""
        if not ly or "暂无歌词" in ly:
            return {"error": "暂无歌词"}
        return {"lyric": ly}
    except Exception as e:
        return {"error": "获取歌词失败: %s" % e}
