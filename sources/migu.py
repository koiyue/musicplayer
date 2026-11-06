# -*- coding: utf-8 -*-
"""咪咕音乐音源: 免登录搜索(c.musicapp.migu.cn search_all.do), 播放地址需解密 listen-url token."""
import json
import urllib.parse
import urllib.request

from . import song
from .common import http_get, UA

HEADERS = {"Accept": "application/json", "Referer": "https://y.migu.cn/"}


def _cover(item):
    """imgItems 里取一张封面."""
    for it in item.get("imgItems") or []:
        url = it.get("imgUrl") or it.get("url")
        if url:
            return url
    return ""


def search(keyword, limit=20, page=1):
    page = max(1, int(page))
    limit = min(max(1, int(limit)), 20)  # 接口单页上限 20
    sw = json.dumps({"song": 1, "album": 0, "singer": 0,
                     "tagSong": 1, "mvSong": 0, "bestShow": 1},
                    separators=(",", ":"))
    url = ("https://c.musicapp.migu.cn/v1.0/content/search_all.do"
           "?text=%s&pageNo=%d&pageSize=%d&isCopyright=1&sort=1&searchSwitch=%s"
           % (urllib.parse.quote(keyword), page, limit, urllib.parse.quote(sw)))
    j = http_get(url, headers=HEADERS)
    if j.get("code") != "000000":
        return {"songs": [], "error": "咪咕搜索返回异常: %s" % j.get("message")}
    song_data = j.get("songResultData") or {}
    out = []
    for t in song_data.get("result") or []:
        artists = " / ".join((a.get("name") or "") for a in t.get("singers") or [])
        album = ""
        albums = t.get("albums") or []
        if albums:
            album = albums[0].get("name") or ""
        # 搜索接口不返回时长, 用 LQ(64kbps) 文件大小估算
        dur = 0
        for rf in t.get("rateFormats") or []:
            if rf.get("formatType") == "LQ" and rf.get("size"):
                try:
                    dur = int(int(rf["size"]) * 8 / 64000)
                except Exception:
                    dur = 0
                break
        out.append(song("migu", t.get("contentId") or t.get("id"),
                        t.get("name", ""), artists, album, dur, _cover(t),
                        extra={"contentId": t.get("contentId") or "",
                               "copyrightId": t.get("copyrightId") or "",
                               "lyricUrl": t.get("lyricUrl") or ""}))
    total = (song_data.get("totalCount") or song_data.get("totalRecord")
             or song_data.get("recordCount") or song_data.get("total") or None)
    total = int(total) if total else None
    has_more = (page * limit < total) if total else (len(out) >= limit)
    return {"songs": out, "page": page, "total": total, "page_size": limit, "has_more": has_more}


def get_playlist(ref):
    return {"songs": [], "error": "咪咕暂不支持歌单浏览"}


def get_stream(s):
    extra = s.get("extra") or {}
    cid = extra.get("contentId") or s.get("id")
    crid = extra.get("copyrightId")
    if not cid or not crid:
        return {"error": "咪咕缺少播放参数"}
    # 注意: 正确路径无 /v2.0 前缀; 响应为二进制密文
    url = ("https://c.musicapp.migu.cn/strategy/listen-url/h5/v2.4"
           "?contentId=%s&copyrightId=%s&resourceType=2&netType=01"
           "&toneFlag=PQ&scene=&lowerQualityContentId=%s" % (cid, crid, cid))
    hdr = dict(HEADERS)
    hdr.update({"birth": "h5page", "channel": "014X031", "subchannel": "014X031"})
    try:
        raw = http_get(url, headers=hdr, raw=True)
    except Exception as e:
        return {"error": "咪咕取播放地址失败: %s" % e}
    real = _decrypt_token(raw)
    if not real:
        return {"error": "咪咕该曲目无免费音源"}
    # 尝试升级到 320k(路径替换 + HEAD 探测), 失败保持 128k
    hq = _upgrade_quality(real)
    return {"url": hq or real, "headers": {"Referer": "https://y.migu.cn/"}}


def get_lyrics(s):
    """搜索接口返回的 lyricUrl 直接指向 LRC 文本文件."""
    extra = s.get("extra") or {}
    lu = extra.get("lyricUrl")
    if not lu:
        return {"error": "该曲目无歌词地址(请从搜索/歌单重新加入)"}
    try:
        raw = http_get(lu, headers={"Referer": "https://y.migu.cn/"}, raw=True)
        ly = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if not ly.strip():
            return {"error": "暂无歌词"}
        return {"lyric": ly}
    except Exception as e:
        return {"error": "获取歌词失败: %s" % e}


# 固定密钥滚动异或(非 AES): plain[j] = (cipher[j] + seed - key[j % 32]) mod 256
_XOR_KEY = b"Jk8qzuePiJ1qE3mDYhLQ3T73DtDoAhLP"


def _decrypt_token(raw):
    """解密 listen-url 二进制响应 -> 播放 URL 字符串; 失败返回 None."""
    try:
        if len(raw) < 5 or raw[:3] != bytes.fromhex("abcd01"):
            return None
        seed = raw[3]
        out = bytearray((raw[i] + seed - _XOR_KEY[(i - 4) % len(_XOR_KEY)]) & 0xFF
                        for i in range(4, len(raw)))
        j = json.loads(out.decode("utf-8"))
        if j.get("code") != "000000":
            return None
        return ((j.get("data") or {}).get("url")) or None
    except Exception:
        return None


_TONE_PQ = urllib.parse.quote("标清高清/MP3_128_16_Stero")
_TONE_HQ = urllib.parse.quote("标清高清/MP3_320_16_Stero")


def _upgrade_quality(url128):
    """128k -> 320k: 替换 CDN 路径段, HEAD 探测可用才返回."""
    try:
        base = url128.split("?")[0]
        if _TONE_PQ not in base:
            return None
        hq = base.replace(_TONE_PQ, _TONE_HQ)
        req = urllib.request.Request(hq, method="HEAD",
                                     headers={"User-Agent": UA,
                                              "Referer": "https://y.migu.cn/"})
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 200:
                return hq
    except Exception:
        pass
    return None
