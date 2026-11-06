# -*- coding: utf-8 -*-
"""哔哩哔哩音源: 搜索取视频, 播放取音频流(dash.audio). 需 WBI 签名 + buvid cookie.
纯标准库实现; 视频可含分P, 加入播放列表时按分P展开为多首."""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import secure_store, song
from .common import http_get, http_post, UA

API = "https://api.bilibili.com"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
COOKIE_FILE = os.path.join(DATA_DIR, "bilibili_cookie.json")
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
             49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55,
             40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57,
             62, 11, 36, 20, 34, 44, 52]

_buvid = None
_wbi_keys = None  # (img_key, sub_key, ts)
_qr_state = None  # (qrcode_key, ts) 当前扫码会话


def _load_cookie():
    """读取登录 Cookie 字典(SESSDATA/bili_jct/DedeUserID 等), 无则返回 {}."""
    d = secure_store.load_json(COOKIE_FILE)
    return d if isinstance(d, dict) else {}


def _cookie_str():
    ck = _load_cookie()
    parts = ["%s=%s" % (k, v) for k, v in ck.items() if v]
    return "; ".join(parts) if parts else None


def _save_cookie(ck):
    if not ck.get("SESSDATA"):
        return False
    keep = {k: ck[k] for k in ("SESSDATA", "bili_jct", "DedeUserID",
                               "DedeUserID__ckMd5", "sid") if ck.get(k)}
    secure_store.save_json(COOKIE_FILE, keep)
    return True


def logout():
    try:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def manual_cookie(raw):
    """手动粘贴 Cookie 登录(备用方案)."""
    ck = {}
    for seg in (raw or "").replace(",", ";").split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            k = k.strip()
            if k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
                ck[k] = v.strip()
    if not ck.get("SESSDATA"):
        return {"ok": False, "error": "Cookie 中未找到 SESSDATA"}
    if not _save_cookie(ck):
        return {"ok": False, "error": "保存失败"}
    st = status()
    if not st.get("logged_in"):
        return {"ok": False, "error": "Cookie 可能无效或已过期"}
    return {"ok": True, "logged_in": True, "uname": st.get("uname", "")}


def status():
    """返回登录状态: {logged_in, uname, vip}. 未登录时 uname 为空."""
    ck = _cookie_str()
    if not ck:
        return {"logged_in": False}
    try:
        j = http_get(API + "/x/web-interface/nav",
                     headers={"Referer": "https://www.bilibili.com/",
                              "User-Agent": UA, "Cookie": ck})
        d = j.get("data") or {}
        if not d.get("isLogin"):
            return {"logged_in": False}
        vip = (d.get("vipStatus") == 1)
        return {"logged_in": True, "uname": d.get("uname", ""),
                "vip": vip, "mid": d.get("mid")}
    except Exception as e:
        return {"logged_in": False, "error": str(e)}


def new_qr():
    """生成登录二维码: 返回 (png_bytes, 网页url). 官方接口直接返回图片."""
    global _qr_state
    j = http_get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                 headers={"Referer": "https://www.bilibili.com/", "User-Agent": UA})
    d = j.get("data") or {}
    url, key = d.get("url"), d.get("qrcode_key")
    if not url or not key:
        raise RuntimeError("获取二维码失败: " + json.dumps(j, ensure_ascii=False))
    _qr_state = (key, time.time())
    png = None
    for tpl in ("https://quickchart.io/qr?size=240&text=%s",
                "https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=%s"):
        try:
            png = http_get(tpl % urllib.parse.quote(url, safe=""), raw=True, timeout=15)
            if png and png[:4] == b"\x89PNG":
                break
            png = None
        except Exception:
            png = None
    return png, url


def poll_qr():
    """轮询扫码状态. 返回 {code, message} 或登录成功后 {logged_in, uname}."""
    if not _qr_state:
        return {"code": -1, "message": "请先生成二维码"}
    key, ts = _qr_state
    if time.time() - ts > 180:
        return {"code": 86038, "message": "二维码已过期"}
    # 必须在同一次 poll 响应中读取 JSON 状态与 Set-Cookie。
    # 二维码确认后该 poll 凭证可能立即失效；再请求一次会丢失登录 Cookie。
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=" + key
    req = urllib.request.Request(
        url, headers={"Referer": "https://www.bilibili.com/", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = resp.read().decode("utf-8", "replace")
            j = json.loads(payload)
            set_cookies = resp.headers.get_all("Set-Cookie") or []
    except Exception as e:
        return {"code": -1, "message": str(e)}

    d = j.get("data") or {}
    code = d.get("code")
    if code == 0:
        ck = {}
        for line in set_cookies:
            seg = line.split(";", 1)[0]
            if "=" in seg:
                k, v = seg.split("=", 1)
                if k.strip() in ("SESSDATA", "bili_jct", "DedeUserID",
                                 "DedeUserID__ckMd5", "sid"):
                    ck[k.strip()] = v.strip()
        if _save_cookie(ck):
            st = status()
            return {"code": 0, "logged_in": True, "uname": st.get("uname", "")}
        return {"code": 0, "logged_in": False, "message": "登录确认成功，但响应未包含有效 Cookie"}
    return {"code": code, "message": d.get("message") or ""}


def _headers():
    h = {"Referer": "https://www.bilibili.com/", "User-Agent": UA}
    cookies = []
    if _buvid:
        cookies.append("buvid3=%s" % _buvid)
    ck = _cookie_str()
    if ck:
        cookies.append(ck)
    if cookies:
        h["Cookie"] = "; ".join(cookies)
    return h


def _ensure_buvid():
    global _buvid
    if _buvid:
        return
    try:
        req = urllib.request.Request("https://www.bilibili.com/",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            for line in (resp.headers.get_all("Set-Cookie") or []):
                m = re.match(r"(buvid3=[^;]+)", line)
                if m:
                    _buvid = m.group(1).split("=", 1)[1]
                    return
    except Exception:
        pass
    # 兜底: 用 spi 接口取 buvid
    try:
        j = http_get(API + "/x/frontend/finger/spi", headers={"User-Agent": UA})
        _buvid = ((j.get("data") or {}).get("b_3")) or ""
    except Exception:
        _buvid = ""


def _get_wbi_keys():
    """从 nav 接口取 img_key/sub_key(缓存 10 分钟)."""
    global _wbi_keys
    now = time.time()
    if _wbi_keys and now - _wbi_keys[2] < 600:
        return _wbi_keys[0], _wbi_keys[1]
    j = http_get(API + "/x/web-interface/nav", headers=_headers())
    wbi = (j.get("data") or {}).get("wbi_img") or {}
    img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    if not re.fullmatch(r"[0-9a-fA-F]{32}", img) or not re.fullmatch(r"[0-9a-fA-F]{32}", sub):
        raise RuntimeError("B站签名密钥获取失败，请稍后重试")
    _wbi_keys = (img, sub, time.time())
    return img, sub


def _mixin_key(img, sub):
    raw = img + sub
    return "".join(raw[i] for i in MIXIN_TAB)[:32]


def _wbi_sign(params):
    """对参数字典做 WBI 签名, 返回带 wts/w_rid 的 query 串."""
    img, sub = _get_wbi_keys()
    key = _mixin_key(img, sub)
    p = dict(params)
    p["wts"] = int(time.time())
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(p[k]), safe=""))
                 for k in sorted(p))
    w_rid = hashlib.md5((q + key).encode("utf-8")).hexdigest()
    return q + "&w_rid=" + w_rid


def _parse_dur(sec):
    return int(sec) if isinstance(sec, (int, float)) else 0


def search(keyword, limit=30, page=1):
    page = max(1, int(page))
    _ensure_buvid()
    params = {"search_type": "video", "keyword": keyword, "page": page,
              "page_size": min(limit, 32)}
    url = API + "/x/web-interface/wbi/search/type?" + _wbi_sign(params)
    j = http_get(url, headers=_headers())
    data = j.get("data") or {}
    out = []
    for r in data.get("result") or []:
        bvid = r.get("bvid")
        if not bvid:
            continue
        title = re.sub(r"<[^>]+>", "", r.get("title") or "")
        duration = _parse_dur(r.get("duration"))
        # duration 可能是 "03:45" 形式
        if isinstance(r.get("duration"), str) and ":" in r.get("duration"):
            parts = r["duration"].split(":")
            duration = sum(int(x) * (60 ** (len(parts) - 1 - i))
                           for i, x in enumerate(parts) if x.isdigit())
        out.append(song("bilibili", bvid, title, r.get("author", ""),
                        "", duration,
                        "https:" + r.get("pic", "") if r.get("pic", "").startswith("//")
                        else r.get("pic", ""),
                        extra={"aid": r.get("aid")}))
    total = data.get("totalcount") or data.get("numResults") or None
    total = int(total) if total else None
    ps = min(limit, 32)
    has_more = (page * ps < total) if total else (len(out) >= ps)
    return {"songs": out, "page": page, "total": total, "page_size": ps, "has_more": has_more}


def _cid(bvid):
    j = http_get(API + "/x/player/pagelist?bvid=%s" % bvid, headers=_headers())
    pages = j.get("data") or []
    return pages


def _to_song(r):
    """将推荐/热门/相关推荐接口的条目转为 song (extra 带观看数/点赞数)."""
    bvid = r.get("bvid")
    if not bvid:
        return None
    title = re.sub(r"<[^>]+>", "", r.get("title") or "")
    owner = r.get("owner") or {}
    stat = r.get("stat") or {}
    cover = r.get("pic") or ""
    if cover.startswith("//"):
        cover = "https:" + cover
    return song("bilibili", bvid, title, owner.get("name", ""),
                "", _parse_dur(r.get("duration")), cover,
                extra={"aid": r.get("aid") or stat.get("aid"),
                       "view": stat.get("view", 0), "like": stat.get("like", 0)})


def recommend(bvid=None, limit=30, pn=1):
    """bvid 为空 -> 热门视频(pn 分页); 否则 -> 该视频的相关推荐."""
    _ensure_buvid()
    out = []
    try:
        if bvid:
            j = http_get(API + "/x/web-interface/archive/related?bvid=%s" % bvid,
                         headers=_headers())
            items = j.get("data") or []
        else:
            j = http_get(API + "/x/web-interface/popular?ps=%d&pn=%d"
                         % (min(limit, 20), max(1, int(pn))), headers=_headers())
            d = j.get("data") or {}
            items = d.get("list") or []
        for r in items:
            s = _to_song(r)
            if s:
                out.append(s)
            if len(out) >= limit:
                break
    except Exception as e:
        return {"songs": [], "error": str(e)}
    nm = False
    if not bvid:
        d = j.get("data") or {}
        nm = bool(d.get("no_more")) if isinstance(d, dict) else False
    return {"songs": out, "no_more": nm}


def get_playlist(ref):
    """ref = bvid, 返回该视频的分P列表."""
    _ensure_buvid()
    pages = _cid(ref)
    out = []
    for i, p in enumerate(pages):
        out.append(song("bilibili", ref, p.get("part") or ("P%d" % (i + 1)),
                        "", "", _parse_dur(p.get("duration")), "",
                        extra={"cid": p.get("cid"), "aid": p.get("aid"), "page": i + 1}))
    return {"songs": out}


def get_stream(s):
    _ensure_buvid()
    bvid = s["id"]
    extra = s.get("extra") or {}
    cid = extra.get("cid")
    if not cid:
        pages = _cid(bvid)
        if not pages:
            return {"error": "无法解析视频分P"}
        cid = pages[0].get("cid")
        extra["cid"] = cid
    params = {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1}
    url = API + "/x/player/wbi/playurl?" + _wbi_sign(params)
    j = http_get(url, headers=_headers())
    data = j.get("data") or {}
    dash = data.get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        durl = data.get("durl") or []
        if durl:
            return {"url": durl[0].get("url"),
                    "headers": {"Referer": "https://www.bilibili.com/",
                                "User-Agent": UA}}
        return {"error": "无可用音频流(可能大会员专属)"}
    # 选码率最高的音频
    best = max(audios, key=lambda a: a.get("bandwidth", 0))
    return {"url": best.get("baseUrl") or best.get("base_url"),
            "headers": {"Referer": "https://www.bilibili.com/", "User-Agent": UA}}


def get_danmaku(s):
    """拉取视频弹幕(XML), 解析为 [{time, mode, color, size, text}]. 无需登录."""
    _ensure_buvid()
    bvid = s["id"]
    extra = s.get("extra") or {}
    cid = extra.get("cid")
    if not cid:
        pages = _cid(bvid)
        if not pages:
            return {"danmaku": []}
        cid = pages[0].get("cid")
    try:
        raw = http_get("https://comment.bilibili.com/%s.xml" % cid,
                       headers=_headers(), raw=True, timeout=15)
    except Exception:
        return {"danmaku": []}
    if not raw:
        return {"danmaku": []}
    # comment.bilibili.com 常返回 deflate/gzip 压缩
    if raw[:1] != b"<":
        import zlib
        for wb in (zlib.MAX_WBITS | 16, -zlib.MAX_WBITS, zlib.MAX_WBITS):
            try:
                raw = zlib.decompress(raw, wb)
                break
            except Exception:
                continue
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
    except Exception:
        return {"danmaku": []}
    out = []
    for m in re.finditer(r'<d p="([^"]*)">((?:(?!</d>).)*)</d>', raw):
        parts = (m.group(1) or "").split(",")
        try:
            t = float(parts[0])
            mode = int(parts[1]) if len(parts) > 1 else 1
            size = int(parts[2]) if len(parts) > 2 else 25
            color = int(parts[3]) if len(parts) > 3 else 16777215
        except Exception:
            continue
        text = (m.group(2) or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if text:
            out.append({"time": round(t, 2), "mode": mode, "size": size,
                        "color": color, "text": text})
    out.sort(key=lambda d: d["time"])
    return {"danmaku": out}


def send_danmaku(s, text, mode=1, color=16777215, fontsize=25, progress=0.0, length=0):
    """发送弹幕(需登录: SESSDATA + bili_jct). progress 为视频时间点(秒)."""
    ck = _load_cookie()
    if not ck.get("SESSDATA") or not ck.get("bili_jct"):
        return {"ok": False, "error": "请先登录B站账号"}
    text = (text or "").strip()[:100]
    if not text:
        return {"ok": False, "error": "弹幕内容不能为空"}
    _ensure_buvid()
    bvid = s["id"]
    extra = s.get("extra") or {}
    cid = extra.get("cid")
    if not cid:
        pages = _cid(bvid)
        if not pages:
            return {"ok": False, "error": "无法解析视频分P"}
        cid = pages[0].get("cid")
    sig = hashlib.md5(("1c11824d06a718b9b2203f0ff6da4a1d" + text).encode("utf-8")).hexdigest()
    data = {
        "oid": cid, "bvid": bvid, "msg": text,
        "progress": int(float(progress or 0) * 1000),
        "mode": int(mode), "fontsize": int(fontsize), "color": int(color),
        "bottom": "", "top": "", "pool": 0, "type": 1,
        "length": int(float(length or 0)),
        "signature": sig, "csrf": ck["bili_jct"],
    }
    try:
        j = http_post(API + "/x/v2/dm/post", data=data, headers={
            "Referer": "https://www.bilibili.com/", "User-Agent": UA,
            "Cookie": _cookie_str()})
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if j.get("code") == 0:
        return {"ok": True}
    return {"ok": False, "error": j.get("message") or ("code=" + str(j.get("code")))}


# ---------------- 点赞 / 投币 / 评论区 ----------------
def _view_info(s):
    """获取视频基本信息: 返回 (aid, cid, stat) 或 (None, None, None)."""
    _ensure_buvid()
    bvid = s["id"]
    try:
        j = http_get(API + "/x/web-interface/view?bvid=" + bvid, headers=_headers())
    except Exception:
        return None, None, None
    d = j.get("data") or {}
    aid = d.get("aid")
    cid = d.get("cid")
    if not cid:
        cid = (s.get("extra") or {}).get("cid")
    stat = d.get("stat") or {}
    return aid, cid, stat


def get_stats(s):
    """视频统计(播放/点赞/投币/收藏/评论) + 当前用户已执行动作. 无需登录."""
    aid, cid, stat = _view_info(s)
    if not aid:
        return {"error": "无法获取视频信息"}
    out = {"aid": aid, "view": stat.get("view", 0), "like": stat.get("like", 0),
           "coin": stat.get("coin", 0), "favorite": stat.get("favorite", 0),
           "reply": stat.get("reply", 0), "liked": False, "coined": 0}
    # 登录态下查询用户动作: has/like(近期点赞), archive/coins(投币枚数)
    if _cookie_str():
        try:
            j = http_get(API + "/x/web-interface/archive/has/like?bvid=" + s["id"],
                         headers=_headers())
            out["liked"] = (j.get("data") == 1)
        except Exception:
            pass
        try:
            j = http_get(API + "/x/web-interface/archive/coins?bvid=" + s["id"],
                         headers=_headers())
            out["coined"] = int((j.get("data") or {}).get("multiply") or 0)
        except Exception:
            pass
    return out


def like_video(s):
    """点赞(需登录)."""
    ck = _load_cookie()
    if not ck.get("SESSDATA") or not ck.get("bili_jct"):
        return {"ok": False, "error": "请先登录B站账号"}
    aid, cid, _ = _view_info(s)
    if not aid:
        return {"ok": False, "error": "无法获取视频信息"}
    try:
        j = http_post(API + "/x/web-interface/archive/like", data={
            "bvid": s["id"], "like": 1, "csrf": ck["bili_jct"]}, headers={
            "Referer": "https://www.bilibili.com/", "User-Agent": UA,
            "Cookie": _cookie_str()})
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if j.get("code") == 0:
        return {"ok": True, "message": (j.get("data") or {}).get("message") or "点赞成功"}
    return {"ok": False, "error": j.get("message") or ("code=" + str(j.get("code")))}


def coin_video(s, multiply=1):
    """投币(需登录), multiply=1 或 2 枚. 投币风控要求 buvid3 + 视频页 Referer."""
    ck = _load_cookie()
    if not ck.get("SESSDATA") or not ck.get("bili_jct"):
        return {"ok": False, "error": "请先登录B站账号"}
    _ensure_buvid()
    aid, cid, _ = _view_info(s)
    if not aid:
        return {"ok": False, "error": "无法获取视频信息"}
    h = _headers()  # 含 buvid3 + 登录 Cookie
    h["Referer"] = "https://www.bilibili.com/video/" + s["id"] + "/"
    h["Origin"] = "https://www.bilibili.com"
    try:
        j = http_post(API + "/x/web-interface/coin/add", data={
            "aid": aid, "bvid": s["id"], "multiply": int(multiply),
            "select_like": 0, "csrf": ck["bili_jct"]}, headers=h)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if j.get("code") == 0:
        return {"ok": True}
    return {"ok": False, "error": j.get("message") or ("code=" + str(j.get("code")))}


def get_comments(s, page=1, size=20, sort=2):
    """拉取评论区一级评论. sort: 0=最新(按时间) 2=最热(按点赞). 无需登录."""
    _ensure_buvid()
    aid, cid, _ = _view_info(s)
    if not aid:
        return {"comments": [], "error": "无法获取视频信息"}
    base = {"type": 1, "oid": aid, "pn": int(page), "ps": int(size),
            "three_version": "1", "spmid": "333.1007.0.0"}
    # 实测: 最新用 mode=2(时间倒序), 热好用 sort=2; sort=0/mode=3 不生效
    if int(sort) == 0:
        base["mode"] = 2
    else:
        base["sort"] = 2
    params = urllib.parse.urlencode(base)
    try:
        j = http_get(API + "/x/v2/reply/main?" + params, headers=_headers())
    except Exception as e:
        return {"comments": [], "error": str(e)}
    d = j.get("data") or {}
    out = []
    my_mid = str((_load_cookie().get("DedeUserID") or ""))
    for r in (d.get("replies") or []):
        c = r.get("content") or {}
        m = r.get("member") or {}
        out.append({
            "rpid": r.get("rpid"),
            "name": m.get("uname", ""),
            "face": m.get("avatar", ""),
            "mid": str(m.get("mid", "")),
            "msg": c.get("message", ""),
            "like": r.get("like", 0),
            "liked": bool((m.get("action") or 0) & 4),
            "reply": r.get("rcount", 0),
            "time": r.get("ctime", 0),
        })
    return {"comments": out, "aid": aid, "my_mid": my_mid,
            "cursor": (d.get("cursor") or {}).get("is_end", True)}


def delete_comment(s, rpid):
    """删除自己的评论(需登录). 仅能删除本人发布的评论."""
    ck = _load_cookie()
    if not ck.get("SESSDATA") or not ck.get("bili_jct"):
        return {"ok": False, "error": "请先登录B站账号"}
    _ensure_buvid()
    aid, cid, _ = _view_info(s)
    if not aid:
        return {"ok": False, "error": "无法获取视频信息"}
    h = _headers()
    h["Referer"] = "https://www.bilibili.com/video/" + s["id"] + "/"
    h["Origin"] = "https://www.bilibili.com"
    try:
        j = http_post(API + "/x/v2/reply/del", data={
            "oid": aid, "type": 1, "rpid": rpid,
            "csrf": ck["bili_jct"]}, headers=h)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if j.get("code") == 0:
        return {"ok": True}
    return {"ok": False, "error": j.get("message") or ("code=" + str(j.get("code")))}


def like_comment(s, rpid, like=1):
    """点赞/取消点赞评论(需登录). like=1 赞, 0 取消."""
    ck = _load_cookie()
    if not ck.get("SESSDATA") or not ck.get("bili_jct"):
        return {"ok": False, "error": "请先登录B站账号"}
    _ensure_buvid()
    aid, cid, _ = _view_info(s)
    if not aid:
        return {"ok": False, "error": "无法获取视频信息"}
    h = _headers()  # 含 buvid3 + 登录 Cookie
    h["Referer"] = "https://www.bilibili.com/video/" + s["id"] + "/"
    h["Origin"] = "https://www.bilibili.com"
    try:
        j = http_post(API + "/x/v2/reply/action", data={
            "type": 1, "oid": aid, "rpid": rpid,
            "action": 1 if like else 2, "csrf": ck["bili_jct"]}, headers=h)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if j.get("code") == 0:
        return {"ok": True}
    return {"ok": False, "error": j.get("message") or ("code=" + str(j.get("code")))}


def post_comment(s, text, root=0, parent=0):
    """发表评论/回复评论(需登录). root=parent=0 为直接评论, 否则为回复."""
    ck = _load_cookie()
    if not ck.get("SESSDATA") or not ck.get("bili_jct"):
        return {"ok": False, "error": "请先登录B站账号"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "评论内容不能为空"}
    _ensure_buvid()
    aid, cid, _ = _view_info(s)
    if not aid:
        return {"ok": False, "error": "无法获取视频信息"}
    h = _headers()
    h["Referer"] = "https://www.bilibili.com/video/" + s["id"] + "/"
    h["Origin"] = "https://www.bilibili.com"
    try:
        j = http_post(API + "/x/v2/reply/add", data={
            "oid": aid, "type": 1, "message": text,
            "root": int(root or 0), "parent": int(parent or 0),
            "plat": 1, "csrf": ck["bili_jct"]}, headers=h)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if j.get("code") == 0:
        d = j.get("data") or {}
        return {"ok": True, "rpid": d.get("rpid") or d.get("comment_id")}
    return {"ok": False, "error": j.get("message") or ("code=" + str(j.get("code")))}


# 分辨率档位: qn -> 名称(与B站官方一致)
QUALITIES = [(127, "8K"), (126, "杜比视界"), (125, "HDR"), (120, "4K"),
             (116, "1080P 60帧"), (112, "1080P 高码率"), (80, "1080P"),
             (64, "720P"), (32, "480P"), (16, "360P")]


def get_video(s, qn=None):
    """返回视频流地址(用于歌词页显示画面); 与音频分离, 前端静音播放并跟随音频进度.
    qn: 指定分辨率档位(如 80=1080P), 取可用流中不超过该档位的最高清晰度."""
    _ensure_buvid()
    bvid = s["id"]
    extra = s.get("extra") or {}
    cid = extra.get("cid")
    if not cid:
        pages = _cid(bvid)
        if not pages:
            return {"error": "无法解析视频分P"}
        cid = pages[0].get("cid")
    params = {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1}
    if qn:
        params["qn"] = int(qn)
    url = API + "/x/player/wbi/playurl?" + _wbi_sign(params)
    j = http_get(url, headers=_headers())
    data = j.get("data") or {}
    vids = (data.get("dash") or {}).get("video") or []
    ids = {v.get("id") for v in vids}
    support = [{"qn": q, "desc": d} for q, d in QUALITIES if q in ids]
    if not vids:
        durl = data.get("durl") or []
        if durl:
            return {"url": durl[0].get("url"),
                    "qualities": support,
                    "headers": {"Referer": "https://www.bilibili.com/", "User-Agent": UA}}
        return {"error": "无可用视频流", "qualities": support}
    # 选不超过 qn 档位的最高清晰度流(默认 720p 省带宽)
    cap = int(qn) if qn else 64
    cands = [v for v in vids if (v.get("id") or 0) <= cap]
    if not cands:
        cands = [min(vids, key=lambda v: v.get("id") or 0)]
    pick = max(cands, key=lambda v: (v.get("id") or 0, v.get("bandwidth") or 0))
    return {"url": pick.get("baseUrl") or pick.get("base_url"),
            "qualities": support, "picked": pick.get("id"),
            "headers": {"Referer": "https://www.bilibili.com/", "User-Agent": UA}}
