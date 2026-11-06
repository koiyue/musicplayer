# -*- coding: utf-8 -*-
"""QQ音乐音源: 免登录搜索/歌单; 播放地址用 vkey.GetVkeyServer.CgiGetVkey(返回 purl).
VIP/付费曲 purl 为空 -> 前端灰显.
登录: 官方扫码接口(ptlogin2)已风控 403, 采用手动粘贴 Cookie(uin+qm36+music_key/skey)."""
import hashlib
import json
import os
import random
import urllib.parse

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


def get_playlist(ref):
    """歌单内容: 经典端点 fcg_ucc_getcdinfo_byids_cp (cdlist[0].songlist),
    登录 Cookie 下可完整读取"我喜欢"等私有歌单."""
    ref = ("%s" % ref).strip()
    ck = _cookie_str()
    uin = _uin()
    gk = _gtk_login() if ck else _gtk()
    url = ("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
           "?type=1&json=1&utf8=1&onlysong=0&disstid=%s&g_tk=%d&loginUin=%s"
           "&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0"
           "&platform=yqq&needNewCode=0" % (urllib.parse.quote(ref), gk, uin if ck else "0"))
    try:
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
