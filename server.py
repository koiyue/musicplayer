# -*- coding: utf-8 -*-
"""本地 HTTP 服务: 标准库 http.server, 提供 /api/* 音源接口 + /proxy 音频代理 + 静态前端."""
import base64
import copy
import hashlib
import hmac
import inspect
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sources import netease, qq, migu, bilibili
from sources import common
from sources.common import UA
import asr

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
SKIN_DIR = os.path.join(DATA_DIR, "skins")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
DL_DIR = os.path.join(DATA_DIR, "downloads")
DL_FILE = os.path.join(DL_DIR, "index.json")
DL_TASKS_FILE = os.path.join(DL_DIR, "tasks.json")
AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wav")
AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac",
               ".ogg": "audio/ogg", ".aac": "audio/aac", ".wav": "audio/wav"}
SKIN_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".apng": "image/apng", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".avif": "image/avif", ".mp4": "video/mp4", ".webm": "video/webm",
    ".ogg": "video/ogg", ".mov": "video/quicktime", ".m4v": "video/mp4",
}
SKIN_MAX = 256 * 1024 * 1024

# 本地 API 访问令牌: 每次启动随机生成, 注入到 index.html, 前端经请求头/查询参数带回.
# 跨域网页拿不到该令牌(服务不再返回 CORS 头), 无法调用本地 API.
TOKEN = secrets.token_urlsafe(24)
# 图片代理白名单(B站封面防盗链); 只允许这些域名及其子域
IMG_HOSTS = ("hdslb.com", "biliimg.com", "bilibili.com",
             "music.126.net", "p1.music.126.net", "p2.music.126.net",
             "gtimg.cn", "y.gtimg.cn", "qpic.cn", "y.qq.com")
IMG_MAX = 10 * 1024 * 1024

SRC = {
    "netease": netease,
    "qq": qq,
    "migu": migu,
    "bilibili": bilibili,
}

_queue_lock = threading.Lock()
_pl_lock = threading.Lock()
_settings_lock = threading.Lock()

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# ---------- 统一歌曲身份 ----------
def _song_identity(source, sid, extra):
    """歌曲唯一身份键. 必须与前端 JSON.stringify 口径一致:
    紧凑分隔符(默认分隔符是 ", " / ": ", 不显式指定就对不上)、非 ASCII 原样、键递归排序;
    %s 渲染 sid 等价于前端 String(id)(整数不带引号)."""
    return "%s:%s:%s" % (source, sid,
                         json.dumps(extra or {}, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")))


def _load_queue():
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"songs": [], "index": -1}


def _save_queue(obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, QUEUE_FILE)


def _load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _save_settings(obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with _settings_lock:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj if isinstance(obj, dict) else {}, f, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)


def _load_pls():
    try:
        with open(PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("lists"), list):
            return d
    except Exception:
        pass
    return {"lists": []}


def _save_pls(obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PLAYLISTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, PLAYLISTS_FILE)


# ---------- 写接口业务校验 ----------
_QUEUE_SOURCES = ("netease", "qq", "migu", "bilibili", "local")
_MAX_QUEUE_SONGS = 2000
_MAX_SONG_ID_LEN = 300
_MAX_PL_NAME_LEN = 120


def _is_number(v):
    """int/float 且排除 bool(Python 里 isinstance(True, int) 为 True)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_queue(obj):
    """校验 POST /api/queue 请求体. 合法返回 None, 非法返回 "<字段>:<原因码>".
    只做校验、不改数据; 未知字段(如 vip)原样保留, 不做白名单裁剪."""
    songs = obj.get("songs")
    if not isinstance(songs, list):
        return "songs:songs_not_array"
    if len(songs) > _MAX_QUEUE_SONGS:
        return "songs:songs_too_many"
    for i, s in enumerate(songs):
        if not isinstance(s, dict):
            return "song[%d]:song_not_object" % i
        src = s.get("source")
        if not isinstance(src, str) or src not in _QUEUE_SOURCES:
            return "song[%d].source:song_source" % i
        sid = s.get("id")
        if isinstance(sid, bool) or not isinstance(sid, (str, int)):
            return "song[%d].id:song_id" % i
        sid = str(sid)
        if not sid or len(sid) > _MAX_SONG_ID_LEN:
            return "song[%d].id:song_id" % i
        if "extra" in s and s["extra"] is not None:
            ex = s["extra"]
            if isinstance(ex, bool) or not isinstance(ex, dict):
                return "song[%d].extra:song_extra" % i
        for k in ("title", "artist", "cover"):
            if k in s and not isinstance(s[k], str):
                return "song[%d].%s:song_field_type" % (i, k)
        if "duration" in s:
            d = s["duration"]
            if not _is_number(d) or d < 0:
                return "song[%d].duration:song_field_type" % i
        if "file" in s:
            f = s["file"]
            if not isinstance(f, str):
                return "song[%d].file:song_field_type" % i
            # 与 _local_file 的 basename 口径一致, 拒绝路径穿越
            if os.path.basename(f) != f:
                return "song[%d].file:song_file_path" % i
    idx = obj.get("index", -1)
    if isinstance(idx, bool) or not isinstance(idx, int):
        return "index:index_type"
    if not songs:
        if idx != -1:
            return "index:index_on_empty_queue" if idx > -1 else "index:index_range"
    if idx < -1 or idx > len(songs) - 1:
        return "index:index_range"
    return None


FONT_FAMILIES = ("Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "SimSun",
                 "KaiTi", "DengXian", "PingFang SC", "Segoe UI", "Arial", "Consolas")


def _validate_proxy(v):
    """代理配置: 只接受 http/https 代理地址(本机回环不受代理影响)."""
    if not isinstance(v, dict):
        return "proxy:proxy_shape"
    if v.get("mode") not in ("system", "none", "custom"):
        return "proxy:proxy_mode"
    for key in ("http", "https"):
        s = v.get(key, "")
        if s is None:
            s = ""
        if not isinstance(s, str) or len(s) > 300:
            return "proxy:%s_length" % key
        s = s.strip()
        if s:
            u = urllib.parse.urlparse(s)
            if u.scheme not in ("http", "https") or not u.hostname or " " in s:
                return "proxy:%s_url" % key
    if v.get("mode") == "custom" and not (v.get("http") or "").strip():
        return "proxy:proxy_missing"
    return None


def _validate_font(v):
    if not isinstance(v, dict):
        return "font:font_shape"
    for key in ("ui_family", "lyrics_family"):
        s = v.get(key)
        if not isinstance(s, str) or not s.strip() or len(s.strip()) > 60:
            return "font:%s_length" % key
        if any(c in s for c in "\"'{};<>\\\n\r"):
            return "font:%s_chars" % key
        if key == "lyrics_family" and len(s.strip()) >= 32:  # LOGFONT.lfFaceName
            return "font:%s_length" % key
    scale = v.get("ui_scale")
    if not _is_number(scale) or not 0.8 <= scale <= 1.5:
        return "font:ui_scale_range"
    size = v.get("lyrics_size")
    if isinstance(size, bool) or not isinstance(size, int) or not 16 <= size <= 72:
        return "font:lyrics_size_range"
    return None


def _validate_skin(v):
    """校验皮肤元数据；媒体本体通过 /api/skin 原始二进制接口上传。"""
    if not isinstance(v, dict):
        return "skin:skin_shape"
    if "enabled" in v and not isinstance(v.get("enabled"), bool):
        return "skin:skin_enabled"
    typ = v.get("type", "")
    if typ not in ("", "image", "video"):
        return "skin:skin_type"
    fit = v.get("fit", "cover")
    if fit not in ("fill", "contain", "cover"):
        return "skin:skin_fit"
    name = v.get("name", "")
    if not isinstance(name, str) or len(name) > 180:
        return "skin:skin_name"
    data = v.get("data", "")
    if not isinstance(data, str) or len(data) > 512:
        # 旧版 Data URL 仍允许在 16MB JSON 上限内使用，避免拒绝升级前的设置。
        if not (isinstance(data, str) and len(data) <= 16 * 1024 * 1024):
            return "skin:skin_data"
    if data:
        if data.startswith("/skin"):
            parsed = None
            try:
                parsed = urllib.parse.urlparse(data)
                stored = (urllib.parse.parse_qs(parsed.query).get("name") or [""])[0]
            except Exception:
                stored = ""
            if (not parsed or parsed.scheme or parsed.netloc or parsed.fragment or
                    parsed.path != "/skin" or
                    not stored or
                    os.path.basename(stored) != stored):
                return "skin:skin_path"
        elif not re.match(r"^data:(?:image|video)/[a-z0-9.+-]+(?:;[^,]*)?,", data, re.I):
            return "skin:skin_data"
    url = v.get("url", "")
    if not isinstance(url, str) or len(url) > 512:
        return "skin:skin_url"
    if url:
        try:
            parsed_url = urllib.parse.urlparse(url)
            stored = (urllib.parse.parse_qs(parsed_url.query).get("name") or [""])[0]
        except Exception:
            stored = ""
            parsed_url = None
        if (not parsed_url or parsed_url.scheme or parsed_url.netloc or
                parsed_url.fragment or
                parsed_url.path != "/skin" or
                os.path.basename(stored) != stored or not stored):
            return "skin:skin_url"
    size = v.get("size", 0)
    if isinstance(size, bool) or not isinstance(size, (int, float)) or size < 0 or size > SKIN_MAX:
        return "skin:skin_size"
    return None


def _validate_settings(obj):
    """校验 POST /api/settings 请求体(JSON 对象已由 _json_body 保证).
    合法返回 None, 非法返回 "<字段>:<原因码>"; 未知字段原样保留."""
    if "proxy" in obj:
        err = _validate_proxy(obj["proxy"])
        if err:
            return err
    if "font" in obj:
        err = _validate_font(obj["font"])
        if err:
            return err
    if "skin" in obj:
        err = _validate_skin(obj["skin"])
        if err:
            return err
    if "volume" in obj:
        v = obj["volume"]
        if not _is_number(v):
            return "volume:volume_type"
        if not 0 <= v <= 1:
            return "volume:volume_range"
    if "rate" in obj:
        v = obj["rate"]
        if not _is_number(v):
            return "rate:rate_type"
        if not 0.5 <= v <= 2:
            return "rate:rate_range"
    if "qn" in obj:
        v = obj["qn"]
        if isinstance(v, bool) or not isinstance(v, int):
            return "qn:qn_type"
        if v < 16:
            return "qn:qn_range"
    if "pos" in obj and obj["pos"] is not None:
        p = obj["pos"]
        if not isinstance(p, dict):
            return "pos:pos_shape"
        if "key" in p:
            k = p["key"]
            if not isinstance(k, str) or not k:
                return "pos:pos_key"
        if "time" in p:
            t = p["time"]
            if not _is_number(t) or t < 0:
                return "pos:pos_time"
    return None


def _validate_pl_name(raw):
    """歌单名: 非 str(且非 None) = 类型违规(HTTP 400); str 但空白 = 既有业务错误(200), 保持不变.
    返回 (name, http_status, error); http_status 为 None 表示合法."""
    if raw is None:
        return "", None, None
    if not isinstance(raw, str):
        return None, 400, "name:name_type"
    name = raw.strip()
    if not name:
        return "", None, None
    if len(name) > _MAX_PL_NAME_LEN:
        return None, 400, "name:name_type"
    return name, None, None


def _pl_id_ok(raw):
    """歌单 id: 缺失保持既有语义(查不到歌单), 传了非 str 则类型违规."""
    return raw is None or isinstance(raw, str)


def _validate_icon(raw):
    """seticon 的 icon: 缺失/None 视为清空; 非空必须是 http:/https:/data:image/ 开头."""
    if raw is None:
        return "", None, None
    if not isinstance(raw, str):
        return None, 400, "icon:icon_type"
    if raw and not raw.startswith(("http:", "https:", "data:image/")):
        return None, 400, "icon:icon_type"
    return raw, None, None


# ---------- 下载管理器 ----------
_dl_lock = threading.Lock()
_dl_state = {}          # task_id -> task dict
_dl_queue = []          # 待执行 task_id
_dl_worker = None
_SAFE = lambda x: re.sub(r'[\\/:*?"<>|]', "_", x).strip() or "song"
_DL_KEEP = ("pending", "downloading", "error", "cancelled")  # 需要写盘的任务状态


class _Cancelled(Exception):
    pass


def _dl_load():
    try:
        with open(DL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return d
    except Exception:
        pass
    return []


def _dl_save(items):
    """写下载索引; 读、修改与写入全过程由调用方持有 _dl_lock."""
    os.makedirs(DL_DIR, exist_ok=True)
    tmp = DL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, DL_FILE)


def _dl_persist():
    """把未完成的任务写盘, 重启后可续传/重试. 调用方需持有 _dl_lock."""
    os.makedirs(DL_DIR, exist_ok=True)
    keep = [dict(t) for t in _dl_state.values() if t.get("status") in _DL_KEEP]
    tmp = DL_TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False)
    os.replace(tmp, DL_TASKS_FILE)


def _dl_set(t, **kw):
    with _dl_lock:
        t.update(kw)
        _dl_persist()


def _dl_kick():
    """确保工作线程在运行. 调用方需持有 _dl_lock."""
    global _dl_worker
    if _dl_worker is None or not _dl_worker.is_alive():
        _dl_worker = threading.Thread(target=_dl_run, daemon=True)
        try:
            _dl_worker.start()
        except Exception:
            _dl_worker = None
            raise


def _dl_unique_name(title, artist, ext):
    base = "%s - %s" % (_SAFE(title), _SAFE(artist)) if artist else _SAFE(title)
    name = base + ext
    n = 1
    while os.path.exists(os.path.join(DL_DIR, name)):
        name = "%s (%d)%s" % (base, n, ext)
        n += 1
    return name


def _sniff_ext(head):
    """按文件头识别真实音频格式; 无法识别返回 None(多半是错误页或损坏数据)."""
    if head[:3] == b"ID3":
        return ".mp3"
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # 帧同步字: layer 位为 00 是 AAC(ADTS), 否则是 MPEG 音频
        return ".aac" if (head[1] & 0x06) == 0 else ".mp3"
    return None


def _dl_drop_part(t):
    """清理分片与进度. 调用方需持有 _dl_lock."""
    if t.get("part"):
        try:
            os.remove(os.path.join(DL_DIR, t["part"]))
        except FileNotFoundError:
            pass
    t.update(part="", received=0)


def _dl_run():
    """后台工作线程: 串行下载队列中的任务."""
    global _dl_worker
    worker = threading.current_thread()
    try:
        while True:
            with _dl_lock:
                if not _dl_queue:
                    return
                tid = _dl_queue.pop(0)
                t = _dl_state.get(tid)
                if not t or t.get("status") != "pending":
                    continue
                t.update(status="downloading", error="")
                t.pop("cancel", None)
            try:
                with _dl_lock:
                    _dl_persist()
                _dl_fetch(t)
            except Exception as e:
                with _dl_lock:
                    if isinstance(e, _Cancelled) or t.get("cancel"):
                        error = ""
                        try:
                            _dl_drop_part(t)
                        except OSError as cleanup_error:
                            error = "残片清理失败: %s" % cleanup_error
                        t.update(status="cancelled", size=0, error=error)
                    else:
                        # 保留 .part, 重试时按已收字节续传
                        t.update(status="error", error=str(e))
                    try:
                        _dl_persist()
                    except OSError as persist_error:
                        t["error"] = (t.get("error") + "; " if t.get("error") else "") + \
                            "任务状态保存失败: %s" % persist_error
    finally:
        with _dl_lock:
            if _dl_worker is worker:
                _dl_worker = None
                if _dl_queue:
                    _dl_kick()


def _dl_validator(t):
    """续传用的实体校验器: 优先强 ETag, 其次 Last-Modified; 都没有则不可安全续传."""
    etag = (t.get("etag") or "").strip()
    if etag and not etag.lower().startswith("w/"):
        return etag
    return (t.get("last_modified") or "").strip() or None


def _dl_fetch(t):
    """下载单个任务: 写 .part(Range 续传+实体校验) -> 校验 -> 原子改名为最终文件."""
    if t.get("cancel"):
        raise _Cancelled()
    src = SRC.get(t["source"])
    if not src:
        raise RuntimeError("未知音源")
    info = src.get_stream({"id": t["id"], "extra": t.get("extra") or {}})
    if t.get("cancel"):
        raise _Cancelled()
    if "error" in info or not info.get("url"):
        raise RuntimeError((info or {}).get("error") or "无法获取播放地址")
    headers = dict(info.get("headers") or {})
    headers.setdefault("User-Agent", UA)
    os.makedirs(DL_DIR, exist_ok=True)
    part = t.get("part") or (hashlib.md5(t["task_id"].encode("utf-8")).hexdigest()[:16] + ".part")
    pp = os.path.join(DL_DIR, part)
    have = os.path.getsize(pp) if os.path.isfile(pp) else 0
    validator = _dl_validator(t)
    if have and not validator:
        # 旧任务没有实体标识, 无法确认 .part 属于同一资源, 从头下载
        have = 0
        with _dl_lock:
            _dl_drop_part(t)
    resp = None
    for _attempt in range(2):
        hdrs = dict(headers)
        if have:
            hdrs["Range"] = "bytes=%d-" % have
            hdrs["If-Range"] = validator
        try:
            resp = urllib.request.urlopen(urllib.request.Request(info["url"], headers=hdrs),
                                          timeout=30)
        except urllib.error.HTTPError as e:
            if e.code != 416 or not have:
                raise
            resp = None  # 416: .part 已不匹配上游, 丢弃后从头下载
        if resp is not None:
            code = resp.getcode()
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)$",
                         (resp.headers.get("Content-Range") or "").strip())
            bad = False
            if have:
                if code != 206 or not m:
                    bad = True  # 上游忽略 Range/实体已变: 全量响应不能追加到旧分片
                else:
                    r_start, r_end, r_total = (int(x) for x in m.groups())
                    if r_start != have or r_end < r_start or r_end + 1 > r_total:
                        bad = True  # 响应范围与本地分片不一致
                    elif t.get("size") and r_total != t["size"]:
                        bad = True  # 总长度与已记录任务不一致
            if not bad:
                break
            resp.close()
            resp = None
        have = 0
        with _dl_lock:
            _dl_drop_part(t)
    if resp is None:
        raise RuntimeError("续传校验失败")
    with resp:
        if t.get("cancel"):
            raise _Cancelled()
        code = resp.getcode()
        if code != 206:
            have = 0  # 上游不支持续传, 从头写
        total = 0
        m = re.match(r"bytes \d+-\d+/(\d+)", resp.headers.get("Content-Range") or "")
        if m:
            total = int(m.group(1))
        elif resp.headers.get("Content-Length"):
            total = have + int(resp.headers["Content-Length"])
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype.startswith(("text/", "application/json")):
            raise RuntimeError("上游返回的不是音频(%s)" % ctype)
        _dl_set(t, part=part, received=have, size=total,
                etag=(resp.headers.get("ETag") or "").strip(),
                last_modified=(resp.headers.get("Last-Modified") or "").strip())
        last = time.time()
        with open(pp, "ab" if have else "wb") as f:
            while True:
                if t.get("cancel"):
                    raise _Cancelled()
                chunk = resp.read(64 * 1024)
                if t.get("cancel"):
                    raise _Cancelled()
                if not chunk:
                    break
                f.write(chunk)
                t["received"] += len(chunk)
                if time.time() - last > 2:
                    last = time.time()
                    with _dl_lock:
                        _dl_persist()
    got = os.path.getsize(pp)
    if not got:
        raise RuntimeError("下载内容为空")
    if total and got != total:
        raise RuntimeError("下载不完整(%d/%d 字节), 可点重试续传" % (got, total))
    with open(pp, "rb") as f:
        ext = _sniff_ext(f.read(16))
    if not ext:
        with _dl_lock:
            _dl_drop_part(t)
        raise RuntimeError("下载内容不是可识别的音频格式")
    with _dl_lock:
        if t.get("cancel"):
            raise _Cancelled()
        fname = _dl_unique_name(t["title"], t["artist"], ext)
        fp = os.path.join(DL_DIR, fname)
        done_at = time.time()
        items = _dl_load()
        items.append({
            "id": fname, "file": fname, "title": t["title"], "artist": t["artist"],
            "source": t["source"], "size": got, "time": done_at,
        })
        os.replace(pp, fp)
        try:
            _dl_save(items)
        except OSError:
            os.replace(fp, pp)  # 索引未登记成功, 恢复分片供重试
            raise
        t.update(status="done", file=fname, part="", size=got, received=got, done_at=done_at)
        try:
            _dl_persist()
        except OSError as e:
            # 文件和索引已经提交, 状态保存失败不能把成功下载伪标为失败
            t["error"] = "任务状态保存失败: %s" % e


def _dl_enqueue(song):
    tid = _song_identity(song.get("source"), song.get("id"), song.get("extra"))
    with _dl_lock:
        exist = _dl_state.get(tid)
        if exist and exist.get("status") in ("pending", "downloading"):
            _dl_kick()
            return tid
        if exist and exist.get("status") == "error":
            # 失败任务再次添加 = 重试(保留 .part 续传)
            task = exist
            task.update(status="pending", error="")
        else:
            task = {
                "task_id": tid, "source": song.get("source"), "id": song.get("id"),
                "extra": song.get("extra") or {}, "title": song.get("title") or "",
                "artist": song.get("artist") or "", "status": "pending",
                "received": 0, "size": 0, "part": "", "queued_at": time.time(),
            }
            _dl_state[tid] = task
        _dl_queue.append(tid)
        try:
            _dl_persist()
        except OSError as e:
            task["error"] = "任务状态保存失败: %s" % e
        _dl_kick()
    return tid


def _dl_retry(tid):
    with _dl_lock:
        t = _dl_state.get(tid)
        if not t or t.get("status") not in ("error", "cancelled"):
            return False
        t.update(status="pending", error="")
        _dl_queue.append(tid)
        try:
            _dl_persist()
        except OSError as e:
            t["error"] = "任务状态保存失败: %s" % e
        _dl_kick()
    return True


def _dl_cancel(tid):
    """取消排队/下载中的任务, 删除 .part; 对失败任务则直接丢弃."""
    with _dl_lock:
        t = _dl_state.get(tid)
        if not t:
            return False
        st = t.get("status")
        if st == "downloading":
            t["cancel"] = True  # 工作线程在下一个分块检查后退出并清理
            try:
                _dl_persist()  # 立即落盘取消意图, 防止进程退出后重启"复活"
            except OSError as e:
                t["error"] = "任务状态保存失败: %s" % e
            return True
        if st not in ("pending", "error"):
            return False
        if tid in _dl_queue:
            _dl_queue.remove(tid)
        error = ""
        try:
            _dl_drop_part(t)
        except OSError as e:
            error = "残片清理失败: %s" % e
        t.update(status="cancelled", size=0, error=error)
        try:
            _dl_persist()
        except OSError as e:
            t["error"] = (error + "; " if error else "") + "任务状态保存失败: %s" % e
    return True


def _dl_restore():
    """启动时恢复上次未完成的任务: 排队/下载中的重新排队续传, 失败的保留以便重试."""
    try:
        with open(DL_TASKS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        return
    if not isinstance(saved, list):
        return
    with _dl_lock:
        for t in saved:
            if not isinstance(t, dict) or not t.get("task_id"):
                continue
            if t.get("status") == "cancelled":
                continue
            if t.get("cancel"):
                # 上次进程退出前已持久化的取消意图: 不重新排队, 落为终态并清理分片
                t.update(status="cancelled", size=0, error="")
                t.pop("cancel", None)
                if t.get("part"):
                    try:
                        os.remove(os.path.join(DL_DIR, t["part"]))
                    except FileNotFoundError:
                        pass
                    t.update(part="", received=0)
                _dl_state[t["task_id"]] = t
                continue
            t.pop("cancel", None)
            if t.get("status") in ("pending", "downloading"):
                t["status"] = "pending"
                _dl_queue.append(t["task_id"])
            _dl_state[t["task_id"]] = t
        try:
            _dl_persist()
        except OSError as e:
            for t in _dl_state.values():
                t["error"] = "任务状态保存失败: %s" % e
        if _dl_queue:
            _dl_kick()


def _dl_tasks():
    with _dl_lock:
        return sorted((dict(t) for t in _dl_state.values()),
                      key=lambda t: t.get("queued_at", 0), reverse=True)


def _dl_clear_finished():
    with _dl_lock:
        for tid in [k for k, v in _dl_state.items()
                    if v.get("status") in ("done", "error", "cancelled")]:
            t = _dl_state.pop(tid)
            if t.get("part"):
                try:
                    os.remove(os.path.join(DL_DIR, t["part"]))
                except OSError:
                    pass
        _dl_persist()


# ---------- P2P 一起听: 权威房间状态 + 独立 LAN 监听 ----------
ROOM_MAX_MEMBERS = 3
ROOM_PORT_FIRST = 8797
ROOM_PORT_LAST = 8806
ROOM_GRANT_TTL = 600
ROOM_FIREWALL_HINT = ("若局域网设备无法连接，请在 Windows 防火墙中允许 Python 的专用网络入站连接，"
                      "或临时关闭防火墙；本程序不会自动修改防火墙规则。")
_ROOM_PROPOSAL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_ROOM_RFC1918 = tuple(ipaddress.ip_network(x) for x in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

_room_cond = threading.Condition(threading.RLock())
_room_state = None
_room_stopping = []


class RoomState:
    def __init__(self, epoch=1):
        self.room_id = secrets.token_urlsafe(12)
        self.host_id = secrets.token_urlsafe(9)
        self.epoch = epoch
        self.room_secret = secrets.token_bytes(32)
        self.max_members = ROOM_MAX_MEMBERS
        self.seq = 0
        self.rev = 0
        self.generation = 0
        self.transport = "http"
        self.state = {}
        self.members = []
        self.chat_tail = []
        self.events = []
        self.proposals = []
        self.proposal_seq = 0
        self.nonces = {}
        self.revoked_nonces = set()
        self.used_ops = {}
        self.rate = {}
        self.slots = []
        self.lan_ips = []
        self.lan_available = False
        self.firewall_hint = ROOM_FIREWALL_HINT
        self.room_port = 0
        self.bound_ips = ["127.0.0.1"]
        self.servers = []
        self.fallback_urls = []
        self.loopback_url = ""
        self.start_error = ""


def _room_refresh_slot(room, slot):
    """签发短期 HMAC grant; nonce 为一次性凭据, 轮换/重置后旧凭据立即撤销."""
    old = slot.get("nonce")
    if old:
        room.revoked_nonces.add(old)
        room.nonces.pop(old, None)
    nonce = secrets.token_urlsafe(12)
    exp = int(time.time()) + ROOM_GRANT_TTL
    payload = {"room_id": room.room_id, "member_id": slot["member_id"],
               "epoch": room.epoch, "scope": "member", "exp": exp,
               "nonce": nonce, "slot": slot["slot"]}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(
        hmac.new(room.room_secret, b64.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    slot["grant"] = b64 + "." + sig
    slot["exp"] = exp
    slot["nonce"] = nonce
    room.nonces[nonce] = {"exp": exp, "member_id": slot["member_id"],
                          "epoch": room.epoch}


def _room_new_slots(room):
    room.slots = []
    for i in range(1, ROOM_MAX_MEMBERS + 1):
        slot = {"slot": i, "member_id": secrets.token_urlsafe(9),
                "grant": "", "exp": 0, "nonce": ""}
        room.slots.append(slot)
        _room_refresh_slot(room, slot)


class _RoomServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, addr, bound_ips):
        self.room_bound_ips = tuple(bound_ips)
        super().__init__(addr, RoomHandler)


def _room_lan_ips():
    """默认路由探测 + 主机名解析, 排除无效地址, RFC1918 优先, 最多 3 个."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_DGRAM):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except Exception:
        pass
    out = []
    for ip in found:
        try:
            a = ipaddress.IPv4Address(ip)
        except Exception:
            continue
        if (a.is_loopback or a.is_link_local or a.is_reserved or
                a.is_multicast or a.is_unspecified or ip in out):
            continue
        out.append(ip)
    out.sort(key=lambda ip: 0 if any(ipaddress.IPv4Address(ip) in n
                                     for n in _ROOM_RFC1918) else 1)
    usable = []
    for ip in out[:3]:
        try:
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                t.bind((ip, 0))
            finally:
                t.close()
            usable.append(ip)
        except OSError:
            continue
    return usable[:3]


def _room_stop_servers(servers, port):
    """后台 shutdown + server_close, 不阻塞请求线程."""
    if not servers:
        return
    ev = threading.Event()
    with _room_cond:
        _room_stopping.append({"port": port, "event": ev})

    def _run():
        for httpd, _ in servers:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        ev.set()
        with _room_cond:
            for d in list(_room_stopping):
                if d["event"] is ev:
                    _room_stopping.remove(d)

    threading.Thread(target=_run, daemon=True).start()


def _room_start_locked(room):
    lan = _room_lan_ips()
    room.lan_ips = lan
    room.lan_available = bool(lan)
    ips = ["127.0.0.1"] + [ip for ip in lan if ip != "127.0.0.1"]
    busy = {d["port"] for d in _room_stopping if not d["event"].is_set()}
    servers = []
    chosen = 0
    last = None
    for port in range(ROOM_PORT_FIRST, ROOM_PORT_LAST + 1):
        if port in busy:
            continue
        try:
            for ip in ips:
                servers.append([_RoomServer((ip, port), ips), None])
            chosen = port
            break
        except OSError as e:
            last = e
            for srv, _ in servers:
                try:
                    srv.server_close()
                except Exception:
                    pass
            servers = []
    if not chosen:
        room.start_error = str(last or "端口均被占用")
        return False
    room.room_port = chosen
    room.bound_ips = ips
    room.loopback_url = "http://127.0.0.1:%d" % chosen
    room.fallback_urls = (["http://%s:%d" % (ip, chosen) for ip in lan]
                          if lan else ["http://127.0.0.1:%d" % chosen])
    for item in servers:
        t = threading.Thread(target=item[0].serve_forever,
                             kwargs={"poll_interval": 0.2}, daemon=True)
        t.start()
        item[1] = t
    room.servers = [tuple(x) for x in servers]
    return True


def _room_local(reset):
    global _room_state
    with _room_cond:
        room = _room_state
        if room is None:
            room = RoomState(epoch=1)
            _room_state = room
            _room_new_slots(room)
            if not _room_start_locked(room):
                _room_state = None
                return {"ok": False, "error": "无法启动房间监听: " + room.start_error}
        elif reset:
            old_room = room
            room = RoomState(epoch=old_room.epoch + 1)
            room.room_port = old_room.room_port
            room.loopback_url = old_room.loopback_url
            room.bound_ips = old_room.bound_ips
            room.servers = old_room.servers
            room.lan_ips = old_room.lan_ips
            room.lan_available = old_room.lan_available
            room.fallback_urls = old_room.fallback_urls
            _room_state = room
            _room_new_slots(room)
            _room_cond.notify_all()
        now = int(time.time())
        for slot in room.slots:
            if slot["exp"] - now < 30:
                _room_refresh_slot(room, slot)
        return {"ok": True, "room_id": room.room_id, "host_id": room.host_id,
                "epoch": room.epoch, "max_members": room.max_members,
                "room_port": room.room_port, "loopback_url": room.loopback_url,
                "fallback_urls": list(room.fallback_urls),
                "lan_available": bool(room.lan_available),
                "firewall_hint": room.firewall_hint,
                "slots": [{"slot": s["slot"], "member_id": s["member_id"],
                           "grant": s["grant"]} for s in room.slots]}


def _room_grant_slot(num):
    with _room_cond:
        room = _room_state
        if room is None:
            return {"ok": False, "error": "房间不存在"}
        slot = next((s for s in room.slots if s["slot"] == num), None)
        if not slot:
            return {"ok": False, "error": "slot 无效"}
        _room_refresh_slot(room, slot)
        return {"ok": True, "member_id": slot["member_id"],
                "grant": slot["grant"], "firewall_hint": room.firewall_hint}


def _room_proposal_view(p):
    return {"id": p["id"], "member_id": p["member_id"], "type": p["type"],
            "payload": copy.deepcopy(p["payload"]), "actor": p["actor"],
            "ts": p["ts"], "op_id": p["op_id"]}


def _room_proposals(since, wait):
    try:
        deadline = time.monotonic() + min(max(float(wait or 0), 0.0), 5.0)
    except (TypeError, ValueError):
        deadline = time.monotonic()
    with _room_cond:
        while True:
            room = _room_state
            if room is None:
                return {"ok": True, "proposals": [], "seq": 0}
            items = [_room_proposal_view(p) for p in room.proposals
                     if p["_seq"] > since]
            if items or time.monotonic() >= deadline:
                return {"ok": True, "proposals": items, "seq": room.proposal_seq}
            rem = deadline - time.monotonic()
            if rem > 0:
                _room_cond.wait(rem)


def _room_commit(body):
    ev_in, state_in = body.get("event"), body.get("state")
    if not isinstance(ev_in, dict) or not isinstance(state_in, dict):
        return {"ok": False, "error": "请求体无效"}
    q, fq = state_in.get("queue") or [], state_in.get("feedQueue") or []
    if not isinstance(q, list) or not isinstance(fq, list):
        return {"ok": False, "error": "queue 无效"}
    err = _validate_queue({"songs": q, "index": -1})
    if err:
        return {"ok": False, "error": "queue 数据无效: " + err}
    err = _validate_queue({"songs": fq, "index": -1})
    if err:
        return {"ok": False, "error": "feedQueue 数据无效: " + err}
    state = copy.deepcopy(state_in)
    song = state.get("song")
    if (isinstance(song, dict) and song.get("source") and
            song.get("id") is not None and not state.get("song_key")):
        try:
            state["song_key"] = _song_identity(song.get("source"),
                                               str(song.get("id")),
                                               song.get("extra"))
        except Exception:
            pass
    members = body.get("members") if isinstance(body.get("members"), list) else []
    chat = body.get("chat_tail") if isinstance(body.get("chat_tail"), list) else []
    try:
        gen = int(body.get("generation") or 0)
    except (TypeError, ValueError):
        gen = 0
    transport = body.get("transport") if body.get("transport") in ("webrtc", "http") else "http"
    with _room_cond:
        room = _room_state
        if room is None:
            return {"ok": False, "error": "房间不存在"}
        try:
            want = int(ev_in.get("seq") or 0)
        except (TypeError, ValueError):
            want = 0
        if ev_in.get("type") == "anchor":
            # 心跳只刷新权威快照, 不增加事件序号, 否则 WebRTC/HTTP 成员会不断请求 resync。
            ev = copy.deepcopy(ev_in)
            ev["seq"] = room.seq
            ev["rev"] = room.rev
            ev["room_id"] = room.room_id
            ev.setdefault("v", 1)
            ev.setdefault("actor", room.host_id)
            ev.setdefault("ts", time.time())
            ev["transport"] = ev.get("transport") if ev.get("transport") in ("webrtc", "http") else transport
            room.state = state
            room.members = copy.deepcopy(members[:ROOM_MAX_MEMBERS + 1])
            room.chat_tail = copy.deepcopy(chat[-50:])
            room.generation = gen
            room.transport = transport
            _room_cond.notify_all()
            return {"ok": True, "event": ev, "seq": room.seq, "rev": room.rev}
        seq = max(want, room.seq + 1)
        ev = copy.deepcopy(ev_in)
        ev["seq"] = seq
        ev["rev"] = seq
        ev["room_id"] = room.room_id
        ev.setdefault("v", 1)
        ev.setdefault("type", "commit")
        ev.setdefault("actor", "host")
        ev.setdefault("ts", time.time())
        ev["transport"] = ev.get("transport") if ev.get("transport") in ("webrtc", "http") else transport
        room.events.append(ev)
        if len(room.events) > 500:
            del room.events[:-500]
        room.seq = room.rev = seq
        room.state = state
        room.members = copy.deepcopy(members[:ROOM_MAX_MEMBERS + 1])
        room.chat_tail = copy.deepcopy(chat[-50:])
        room.generation = gen
        room.transport = transport
        _room_cond.notify_all()
    return {"ok": True, "event": ev, "seq": seq, "rev": seq}


def _room_debug_state():
    with _room_cond:
        room = _room_state
        if room is None:
            return {"ok": True, "active": False, "events": [], "seq": 0, "rev": 0}
        return {"ok": True, "active": True, "room_id": room.room_id,
                "host_id": room.host_id, "epoch": room.epoch,
                "seq": room.seq, "rev": room.rev,
                "generation": room.generation, "transport": room.transport,
                "state": copy.deepcopy(room.state),
                "members": copy.deepcopy(room.members),
                "chat_tail": copy.deepcopy(room.chat_tail),
                "events": copy.deepcopy(room.events[-100:]),
                "event_count": len(room.events),
                "proposal_count": len(room.proposals),
                "room_port": room.room_port,
                "lan_available": room.lan_available,
                "server_time": time.time()}


def _room_close():
    global _room_state
    with _room_cond:
        room = _room_state
        _room_state = None
        if room is not None:
            _room_cond.notify_all()
            _room_stop_servers(room.servers, room.room_port)
        return {"ok": True}


def _room_verify_locked(room, token):
    try:
        b64, sig = (token or "").split(".", 1)
        want = hmac.new(room.room_secret, b64.encode("ascii"), hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        if not hmac.compare_digest(want, got):
            return None
        p = json.loads(base64.urlsafe_b64decode(
            b64 + "=" * (-len(b64) % 4)).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(p, dict):
        return None
    if p.get("room_id") != room.room_id or p.get("epoch") != room.epoch:
        return None
    if p.get("scope") not in ("member", "guest"):
        return None
    try:
        exp = int(p.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if exp <= int(time.time()):
        return None
    nonce = p.get("nonce")
    if not isinstance(nonce, str) or not nonce or nonce in room.revoked_nonces:
        return None
    rec = room.nonces.get(nonce)
    if (not rec or rec["exp"] != exp or rec["epoch"] != room.epoch or
            rec["member_id"] != p.get("member_id")):
        return None
    return p


def _room_auth(token):
    with _room_cond:
        room = _room_state
        if room is None:
            return None
        p = _room_verify_locked(room, token)
        if not p:
            return None
        return room, p


def _room_replay_locked(room, p, key):
    """nonce + 操作键的一次性防重放; 只针对写请求."""
    now = time.time()
    for k, v in list(room.used_ops.items()):
        if v <= now:
            del room.used_ops[k]
    k = (p["nonce"],) + tuple(key)
    if k in room.used_ops:
        return False
    room.used_ops[k] = now + 60
    return True


def _room_append_proposal(room, p, body, typ, payload):
    now = time.time()
    times = [t for t in room.rate.get(p["member_id"], []) if now - t < 60]
    if len(times) >= 20:
        return False, "请求过于频繁"
    times.append(now)
    room.rate[p["member_id"]] = times
    room.proposal_seq += 1
    room.proposals.append({
        "id": secrets.token_urlsafe(9), "member_id": p["member_id"],
        "type": typ, "payload": copy.deepcopy(payload),
        "actor": body.get("actor"), "ts": body.get("ts"),
        "op_id": body.get("op_id"), "_seq": room.proposal_seq})
    if len(room.proposals) > 200:
        del room.proposals[:-200]
    _room_cond.notify_all()
    return True, None


class RoomHandler(BaseHTTPRequestHandler):
    """独立 LAN 房间接口: 仅 /room/*, 不复用主服务令牌与路由."""
    protocol_version = "HTTP/1.1"
    server_version = "MusicHubRoom/1.0"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        allowed = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        for ip in getattr(self.server, "room_bound_ips", ()):
            allowed.add(("%s:%d" % (ip, port)).lower())
        return host in allowed

    def _origin_ok(self, origin):
        o = origin.strip().rstrip("/")
        if re.match(r"^http://(?:127\.0\.0\.1|localhost):\d+$", o, re.I):
            return True
        port = self.server.server_address[1]
        for ip in getattr(self.server, "room_bound_ips", ("127.0.0.1",)):
            if o.lower() == ("http://%s:%d" % (ip, port)).lower():
                return True
        return False

    def _cors(self):
        h = {}
        origin = self.headers.get("Origin")
        if not origin or not self._origin_ok(origin):
            return h
        h["Access-Control-Allow-Origin"] = origin
        h["Vary"] = "Origin"
        h["Access-Control-Allow-Headers"] = "X-Room-Grant, Content-Type"
        h["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        if (self.headers.get("Access-Control-Request-Private-Network") or
                "").strip().lower() == "true":
            h["Access-Control-Allow-Private-Network"] = "true"
        return h

    def _common(self):
        h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        h.update(self._cors())
        return h

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for k, v in self._common().items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, OSError):
                pass

    def _begin_stream(self, code, ctype, extra, length=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for k, v in self._common().items():
            self.send_header(k, v)
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def _grant_token(self, qs):
        return (qs.get("grant") or [""])[0] or self.headers.get("X-Room-Grant") or ""

    def do_OPTIONS(self):
        if not self._host_ok():
            return self._send(403, {"ok": False, "error": "forbidden"})
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/room/"):
            return self._send(403, {"ok": False, "error": "forbidden"})
        return self._send(204, b"", ctype=None)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"ok": False, "error": "forbidden"})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        g = lambda k, d="": (qs.get(k) or [d])[0]
        if path == "/room/":
            with _room_cond:
                rid = _room_state.room_id if _room_state else "-"
            return self._send(200, "ok\nroom_id: %s\n" % rid,
                              "text/plain; charset=utf-8")
        if not path.startswith("/room/"):
            return self._send(404, {"ok": False, "error": "not found"})
        auth = _room_auth(self._grant_token(qs))
        if not auth:
            return self._send(403, {"ok": False, "error": "grant 无效或已过期"})
        room, p = auth
        with _room_cond:
            if room is not _room_state:
                return self._send(403, {"ok": False, "error": "房间已重置"})
        if path == "/room/ping":
            return self._send(200, {"ok": True, "room_id": room.room_id,
                                    "member_id": p["member_id"],
                                    "epoch": room.epoch,
                                    "server_time": time.time(),
                                    "transport": "http"})
        if path == "/room/state":
            with _room_cond:
                if room is not _room_state:
                    return self._send(403, {"ok": False, "error": "房间已重置"})
                out = {"ok": True, "room_id": room.room_id,
                       "state": copy.deepcopy(room.state),
                       "members": copy.deepcopy(room.members),
                       "chat_tail": copy.deepcopy(room.chat_tail),
                       "seq": room.seq, "rev": room.rev,
                       "transport": room.transport,
                       "generation": room.generation,
                       "server_time": time.time()}
            return self._send(200, out)
        if path == "/room/events":
            try:
                since = int(g("since", "0") or 0)
            except ValueError:
                since = 0
            try:
                wait = min(max(float(g("wait", "0") or 0), 0.0), 5.0)
            except ValueError:
                wait = 0.0
            deadline = time.monotonic() + wait
            with _room_cond:
                while True:
                    if room is not _room_state:
                        return self._send(403, {"ok": False, "error": "房间已重置"})
                    evs = [e for e in room.events if (e.get("seq") or 0) > since]
                    if evs or time.monotonic() >= deadline:
                        out = {"ok": True, "events": copy.deepcopy(evs),
                               "seq": room.seq, "rev": room.rev,
                               "state": copy.deepcopy(room.state),
                               "members": copy.deepcopy(room.members),
                               "chat_tail": copy.deepcopy(room.chat_tail)}
                        break
                    rem = deadline - time.monotonic()
                    if rem > 0:
                        _room_cond.wait(rem)
            return self._send(200, out)
        if path == "/room/stream":
            return self._room_stream(g("kind"), room)
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"ok": False, "error": "forbidden"})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path not in ("/room/propose", "/room/chat"):
            return self._send(404, {"ok": False, "error": "not found"})
        raw = b""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            self.close_connection = True
            return self._send(413, {"ok": False, "error": "请求体无效或过大"})
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._send(400, {"ok": False, "error": "请求体无效"})
        if not isinstance(body, dict):
            return self._send(400, {"ok": False, "error": "请求体无效"})
        auth = _room_auth(self._grant_token(qs))
        if not auth:
            return self._send(403, {"ok": False, "error": "grant 无效或已过期"})
        room, p = auth
        if body.get("member_id") != p["member_id"]:
            return self._send(403, {"ok": False, "error": "member_id 不匹配"})
        if path == "/room/chat":
            typ = "chat"
            text = str(body.get("text") or "")[:200]
            payload = {"text": text,
                       "nickname": str(body.get("nickname") or "")[:40]}
        else:
            typ = body.get("type")
            payload = body.get("payload")
            if not isinstance(typ, str) or not _ROOM_PROPOSAL_RE.match(typ):
                return self._send(400, {"ok": False, "error": "type 无效"})
            if not isinstance(payload, dict):
                return self._send(400, {"ok": False, "error": "payload 无效"})
            try:
                if len(json.dumps(payload, ensure_ascii=False)) > 2048:
                    return self._send(400, {"ok": False, "error": "payload 过大"})
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "error": "payload 无效"})
        op_id = str(body.get("op_id") or "")[:80]
        replay = ("POST", path, op_id or hashlib.sha256(raw).hexdigest())
        with _room_cond:
            if room is not _room_state:
                return self._send(403, {"ok": False, "error": "房间已重置"})
            if not _room_replay_locked(room, p, replay):
                return self._send(409, {"ok": False, "error": "重复请求"})
            ok, err = _room_append_proposal(room, p, body, typ, payload)
        if not ok:
            return self._send(429, {"ok": False, "error": err})
        return self._send(200, {"ok": True})

    def _room_local_file(self, name):
        fname = os.path.basename(name or "")
        fp = os.path.join(DL_DIR, fname)
        root = os.path.realpath(DL_DIR)
        real = os.path.realpath(fp)
        if (not fname or not real.startswith(root + os.sep) or
                not os.path.isfile(real)):
            return self._send(404, {"ok": False, "error": "not found"})
        size = os.path.getsize(real)
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        bad = False
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    suffix = int(m.group(2))
                    if suffix <= 0:
                        bad = True
                    else:
                        start, end = max(0, size - suffix), size - 1
                code = 206
        if bad or start > end or start >= size:
            return self._send(416, {"ok": False, "error": "range not satisfiable"},
                              extra={"Content-Range": "bytes */%d" % size})
        ext = os.path.splitext(fname)[1].lower()
        ctype = AUDIO_TYPES.get(ext, "application/octet-stream")
        extra = {"Accept-Ranges": "bytes"}
        if code == 206:
            extra["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        self._begin_stream(code, ctype, extra, length=end - start + 1)
        try:
            with open(real, "rb") as f:
                f.seek(start)
                remain = end - start + 1
                while remain > 0:
                    chunk = f.read(min(64 * 1024, remain))
                    if not chunk:
                        break
                    remain -= len(chunk)
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            pass

    def _room_stream(self, kind, room):
        if kind not in ("audio", "video"):
            return self._send(400, {"ok": False, "error": "kind 无效"})
        with _room_cond:
            if room is not _room_state:
                return self._send(403, {"ok": False, "error": "房间已重置"})
            state = copy.deepcopy(room.state)
        song = state.get("song") if isinstance(state, dict) else None
        if not isinstance(song, dict) or not song.get("source") or song.get("id") is None:
            return self._send(502, {"ok": False, "error": "当前无歌曲"})
        source = song.get("source")
        if source == "local":
            return self._room_local_file(song.get("file") or song.get("id"))
        src = SRC.get(source)
        if not src:
            return self._send(502, {"ok": False, "error": "未知音源"})
        s = {"id": str(song.get("id")), "extra": song.get("extra") or {}}
        try:
            qn = int(state.get("qn") or 16)
        except (TypeError, ValueError):
            qn = 16
        try:
            if kind == "video" and hasattr(src, "get_video"):
                args = inspect.signature(src.get_video).parameters
                if "room" in args:
                    info = src.get_video(s, qn=qn, room=True)
                else:
                    info = src.get_video(s, qn=qn)
            else:
                info = src.get_stream(s)
        except Exception as e:
            return self._send(502, {"ok": False, "error": "取流失败: %s" % e})
        if (not isinstance(info, dict) or info.get("error") or
                not info.get("url")):
            err = info.get("error") if isinstance(info, dict) else "无可用流"
            return self._send(502, {"ok": False,
                                    "error": str(err or "无可用流")})
        url = info["url"]
        segs = [u for u in (info.get("segments") or []) if u and u != url]
        headers = dict(info.get("headers") or {})
        headers.setdefault("User-Agent", UA)
        for u in [url] + segs:
            if urllib.parse.urlparse(u).scheme not in ("http", "https"):
                return self._send(502, {"ok": False, "error": "上游地址无效"})
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-", rng or "")
        multi = bool(segs) and (not rng or (m and int(m.group(1)) == 0))
        if rng and not multi:
            headers["Range"] = rng if rng.lower().startswith("bytes=") else "bytes=" + rng.strip()
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=20)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                cr = e.headers.get("Content-Range") if e.headers else None
                return self._send(416, {"ok": False, "error": "range not satisfiable"},
                                  extra={"Content-Range": cr or "bytes */0"})
            return self._send(502, {"ok": False, "error": "上游取流失败: %s" % e})
        except Exception as e:
            return self._send(502, {"ok": False, "error": "上游取流失败: %s" % e})
        code = 200 if multi else resp.getcode()
        default_type = "video/mp4" if kind == "video" else "audio/mpeg"
        ctype = (resp.headers.get("Content-Type") or default_type).split(";")[0].strip()
        extra = {}
        if multi:
            extra["Accept-Ranges"] = "none"
            length = None
        else:
            extra["Accept-Ranges"] = resp.headers.get("Accept-Ranges") or "bytes"
            for k in ("Content-Range", "Content-Encoding"):
                v = resp.headers.get(k)
                if v:
                    extra[k] = v
            try:
                length = int(resp.headers.get("Content-Length"))
            except (TypeError, ValueError):
                length = None
        self._begin_stream(code, ctype, extra, length=length)

        def _pump(r):
            try:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        return True
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, OSError):
                return False
            finally:
                try:
                    r.close()
                except Exception:
                    pass

        if not _pump(resp):
            return
        if multi:
            seg_headers = dict(headers)
            seg_headers.pop("Range", None)
            for u in segs:
                try:
                    r2 = urllib.request.urlopen(
                        urllib.request.Request(u, headers=seg_headers), timeout=20)
                except Exception:
                    break
                if not _pump(r2):
                    break


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicHub/1.0"

    def log_message(self, *a):
        pass

    # ---------- 响应工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    _MAX_BODY = 16 * 1024 * 1024  # 歌单封面 Data URL 最大约 12MB 图片裁切后远小于此

    def _json_body(self):
        """解析 JSON 请求体; 非法/超限返回 None."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0:
            return {}
        if n > self._MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    def _expected_hosts(self):
        """当前监听端口上合法的回环 Host 与对应 Origin 集合(DNS rebinding 防线)."""
        port = self.server.server_address[1]
        hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port)
        return hosts, tuple("http://" + h for h in hosts)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self._expected_hosts()[0]

    def _authorized(self, qs):
        """校验回环 Host + 来源 Origin + 本地访问令牌. 令牌只注入到合法首页中."""
        if not self._host_ok():
            return False
        origin = (self.headers.get("Origin") or "").strip().lower().rstrip("/")
        if origin and origin not in self._expected_hosts()[1]:
            return False
        tok = self.headers.get("X-MH-Token") or (qs.get("t") or [""])[0]
        return hmac.compare_digest(tok.encode("utf-8"), TOKEN.encode("utf-8"))

    def _index(self):
        # 非预期 Host(如 DNS rebinding)不返回首页, 更不注入令牌
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        fp = os.path.join(WEB_DIR, "index.html")
        with open(fp, "r", encoding="utf-8") as f:
            html = f.read()
        tag = '<meta name="mh-token" content="%s">' % TOKEN
        html = html.replace("<head>", "<head>\n" + tag, 1)
        return self._send(200, html, "text/html; charset=utf-8",
                          {"Cache-Control": "no-store"})

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        g = lambda k, d="": (qs.get(k) or [d])[0]

        if path in ("/", "/index.html"):
            return self._index()
        if path.startswith("/web/"):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            return self._static(path[5:])
        if not self._authorized(qs):
            return self._send(403, {"error": "forbidden"})
        if path == "/api/room/local":
            try:
                out = _room_local(g("reset", "0") == "1")
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
            return self._send(200 if out.get("ok") else 500, out)
        if path == "/api/room/grant":
            try:
                slot = int(g("slot", "1") or 1)
            except ValueError:
                slot = 0
            out = _room_grant_slot(slot)
            return self._send(200 if out.get("ok") else 400, out)
        if path == "/api/room/proposals":
            try:
                since = int(g("since", "0") or 0)
            except ValueError:
                since = 0
            return self._send(200, _room_proposals(since, g("wait", "0")))
        if path == "/api/room/state":
            return self._send(200, _room_debug_state())
        if path == "/api/sources":
            return self._send(200, {"sources": list(SRC.keys())})
        if path == "/api/fonts":
            return self._send(200, {"families": list(FONT_FAMILIES),
                                    "lyrics_min": 16, "lyrics_max": 72})
        if path == "/api/search":
            return self._search(g)
        if path == "/api/recommend":
            return self._recommend(g)
        if path == "/api/home":
            return self._home(g)
        if path == "/api/playlist":
            return self._playlist(g)
        if path == "/api/mylists":
            return self._mylists(g)
        if path == "/api/stream":
            return self._stream_api(g)
        if path == "/api/lyrics":
            return self._lyrics_api(g)
        if path == "/api/subtitle":
            return self._subtitle_api(g)
        if path == "/api/asr/poll":
            return self._send(200, asr.poll(g("after")))
        if path == "/api/queue":
            with _queue_lock:
                return self._send(200, _load_queue())
        if path == "/api/settings":
            return self._send(200, _load_settings())
        if path == "/api/locallists":
            with _pl_lock:
                d = _load_pls()
            return self._send(200, {"lists": [
                {"id": l.get("id"), "name": l.get("name"),
                 "icon": l.get("icon") or "",
                 "count": len(l.get("songs") or [])}
                for l in d["lists"]]})
        if path == "/api/localplaylist":
            with _pl_lock:
                d = _load_pls()
            pl = next((l for l in d["lists"] if l.get("id") == g("pid")), None)
            if not pl:
                return self._send(200, {"error": "歌单不存在"})
            return self._send(200, {"id": pl.get("id"), "name": pl.get("name"),
                                    "icon": pl.get("icon") or "",
                                    "songs": pl.get("songs") or []})
        if path == "/api/downloads":
            with _dl_lock:
                items = _dl_load()
                known = {it.get("file") for it in items}
                # 收编目录中未登记的音频(如旧版直接下载的文件); 下载中的 .part 不会被收编
                if os.path.isdir(DL_DIR):
                    for fn in os.listdir(DL_DIR):
                        stem, ext = os.path.splitext(fn)
                        if ext.lower() not in AUDIO_EXTS or fn in known:
                            continue
                        fp = os.path.join(DL_DIR, fn)
                        if not os.path.isfile(fp):
                            continue
                        if " - " in stem:
                            title, artist = stem.split(" - ", 1)
                        else:
                            title, artist = stem, ""
                        items.append({"id": fn, "file": fn, "title": title, "artist": artist,
                                      "source": "", "size": os.path.getsize(fp),
                                      "time": os.path.getmtime(fp)})
                out = []
                for it in items:
                    fp = os.path.join(DL_DIR, it.get("file") or "")
                    if it.get("file") and os.path.isfile(fp):
                        it = dict(it)
                        it["size"] = os.path.getsize(fp)
                        out.append(it)
                out.sort(key=lambda x: x.get("time") or 0, reverse=True)
                _dl_save(out)  # 清理已被手动删除的文件记录
            return self._send(200, {"items": out})
        if path == "/api/dl/tasks":
            return self._send(200, {"tasks": _dl_tasks()})
        if path == "/file":
            return self._local_file(g("name"))
        if path == "/proxy":
            return self._proxy(g)
        if path == "/imgproxy":
            return self._imgproxy(g)
        if path == "/skin":
            return self._skin_file(g("name"))
        # 网易云登录
        if path == "/api/netease/status":
            return self._send(200, netease.status())
        if path == "/api/netease/qr":
            try:
                png, link = netease.new_qr()
                if png:
                    return self._send(200, png, "image/png", {"X-QR-Link": link})
                return self._send(200, {"image_error": True, "link": link})
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if path == "/api/netease/poll":
            try:
                return self._send(200, netease.poll())
            except Exception as e:
                return self._send(200, {"error": str(e)})
        # QQ音乐登录
        if path == "/api/qq/status":
            return self._send(200, qq.status())
        # B站登录 + 视频清晰度
        if path == "/api/bili/status":
            return self._send(200, bilibili.status())
        if path == "/api/bili/qr":
            try:
                png, link = bilibili.new_qr()
                if png:
                    return self._send(200, png, "image/png", {"X-QR-Link": link})
                return self._send(200, {"image_error": True, "link": link})
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if path == "/api/bili/poll":
            try:
                return self._send(200, bilibili.poll_qr())
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if path == "/api/bili/playinfo":
            try:
                s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
                info = bilibili.get_video(s, qn=g("qn") or None)
                return self._send(200, {"qualities": info.get("qualities") or [],
                                        "picked": info.get("picked"),
                                        "error": info.get("error")})
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if path == "/api/bili/danmaku":
            try:
                s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
                return self._send(200, bilibili.get_danmaku(s))
            except Exception as e:
                return self._send(200, {"danmaku": [], "error": str(e)})
        if path == "/api/bili/recommend":
            try:
                return self._send(200, bilibili.recommend(
                    g("bvid") or None, int(g("limit", "30") or 30),
                    int(g("pn", "1") or 1)))
            except Exception as e:
                return self._send(200, {"songs": [], "error": str(e)})
        if path == "/api/bili/stats":
            try:
                s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
                return self._send(200, bilibili.get_stats(s))
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if path == "/api/bili/comments":
            try:
                s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
                return self._send(200, bilibili.get_comments(
                    s, page=int(g("page", "1") or 1), size=int(g("size", "20") or 20),
                    sort=int(g("sort", "2") or 2)))
            except Exception as e:
                return self._send(200, {"comments": [], "error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._authorized(urllib.parse.parse_qs(parsed.query)):
            return self._send(403, {"error": "forbidden"})
        # ASR 控制接口无请求体, 放在 JSON 体校验之前
        if path == "/api/asr/start":
            return self._send(200, asr.start())
        if path == "/api/asr/stop":
            return self._send(200, asr.stop())
        if path == "/api/room/close":
            return self._send(200, _room_close())
        # 自定义皮肤以原始二进制上传，避免把视频编码成巨大的 JSON/Data URL。
        if path == "/api/skin":
            return self._skin_upload(urllib.parse.parse_qs(parsed.query))
        body = self._json_body()
        if body is None:
            return self._send(400, {"error": "请求体无效或过大"})
        if path == "/api/room/commit":
            out = _room_commit(body)
            return self._send(200 if out.get("ok") else 400, out)
        if path == "/api/queue":
            err = _validate_queue(body)
            if err:
                return self._send(400, {"error": "队列数据无效: " + err})
            with _queue_lock:
                _save_queue(body)
            return self._send(200, {"ok": True})
        if path == "/api/settings":
            err = _validate_settings(body)
            if err:
                return self._send(400, {"error": "设置数据无效: " + err})
            _save_settings(body)
            if "proxy" in body:
                try:
                    common.configure_proxy(body["proxy"])
                except Exception as e:
                    return self._send(400, {"error": "代理配置失败: %s" % e})
            return self._send(200, {"ok": True})
        if path == "/api/netease/manual":
            return self._send(200, netease.manual_cookie(body.get("cookie", "")))
        if path == "/api/netease/logout":
            return self._send(200, netease.logout())
        if path == "/api/qq/manual":
            return self._send(200, qq.manual_cookie(body.get("cookie", "")))
        if path == "/api/qq/logout":
            return self._send(200, qq.logout())
        if path == "/api/bili/logout":
            return self._send(200, bilibili.logout())
        if path == "/api/bili/manual":
            return self._send(200, bilibili.manual_cookie(body.get("cookie", "")))
        if path == "/api/bili/send_dm":
            try:
                s = {"id": body.get("id"), "extra": body.get("extra") or {}}
                return self._send(200, bilibili.send_danmaku(
                    s, body.get("text", ""), body.get("mode", 1),
                    body.get("color", 16777215), body.get("fontsize", 25),
                    body.get("progress", 0), body.get("length", 0)))
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/bili/like":
            try:
                s = {"id": body.get("id"), "extra": body.get("extra") or {}}
                return self._send(200, bilibili.like_video(s))
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/bili/coin":
            try:
                s = {"id": body.get("id"), "extra": body.get("extra") or {}}
                return self._send(200, bilibili.coin_video(s, body.get("multiply", 1)))
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/bili/cmt_like":
            try:
                s = {"id": body.get("id"), "extra": body.get("extra") or {}}
                return self._send(200, bilibili.like_comment(
                    s, body.get("rpid"), body.get("like", 1)))
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/bili/cmt_post":
            try:
                s = {"id": body.get("id"), "extra": body.get("extra") or {}}
                return self._send(200, bilibili.post_comment(
                    s, body.get("text", ""), body.get("root", 0), body.get("parent", 0)))
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/bili/cmt_del":
            try:
                s = {"id": body.get("id"), "extra": body.get("extra") or {}}
                return self._send(200, bilibili.delete_comment(
                    s, body.get("rpid")))
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if path == "/api/dl/add":
            song = body.get("song") or {}
            if not song.get("source") or not song.get("id"):
                return self._send(200, {"error": "歌曲信息无效"})
            tid = _dl_enqueue(song)
            return self._send(200, {"ok": True, "task_id": tid})
        if path == "/api/dl/clear":
            _dl_clear_finished()
            return self._send(200, {"ok": True})
        if path == "/api/dl/retry":
            ok = _dl_retry(body.get("task_id") or "")
            return self._send(200, {"ok": True} if ok else {"error": "任务不存在或无需重试"})
        if path == "/api/dl/cancel":
            ok = _dl_cancel(body.get("task_id") or "")
            return self._send(200, {"ok": True} if ok else {"error": "任务不存在或已结束"})
        if path == "/api/dl/delete":
            fname = os.path.basename(body.get("file") or "")
            if not fname:
                return self._send(200, {"error": "参数无效"})
            fp = os.path.join(DL_DIR, fname)
            try:
                with _dl_lock:
                    if os.path.isfile(fp):
                        os.remove(fp)
                    _dl_save([it for it in _dl_load() if it.get("file") != fname])
            except Exception as e:
                return self._send(200, {"error": "删除失败: %s" % e})
            return self._send(200, {"ok": True})
        if path.startswith("/api/localplaylist/"):
            return self._local_pl_action(path.split("/")[-1], body)
        return self._send(404, {"error": "not found"})

    def do_OPTIONS(self):
        # 不允许跨域预检: 前端与服务同源, 不需要 CORS
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 业务 ----------
    def _static(self, rel):
        # 用真实路径包含关系替代字符串前缀, 拒绝 .. 与兄弟目录(如 web_backup)越界
        root = os.path.realpath(WEB_DIR)
        fp = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not (fp == root or fp.startswith(root + os.sep)) or not os.path.isfile(fp):
            return self._send(404, "not found", "text/plain")
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            return self._send(200, f.read(), STATIC_TYPES.get(ext, "application/octet-stream"))

    def _search(self, g):
        src = SRC.get(g("source"))
        kw = g("keyword").strip()
        if not src:
            return self._send(200, {"error": "未知音源"})
        if not kw:
            return self._send(200, {"songs": []})
        try:
            return self._send(200, src.search(kw, limit=int(g("limit", "30") or 30),
                                              page=int(g("page", "1") or 1)))
        except Exception as e:
            return self._send(200, {"error": "搜索失败: %s" % e})

    def _recommend(self, g):
        src = SRC.get(g("source"))
        fn = getattr(src, "recommend", None) if src else None
        if not src:
            return self._send(200, {"songs": [], "error": "未知音源"})
        if not fn:
            return self._send(200, {"songs": [], "error": "该音源暂无推荐"})
        try:
            limit = max(1, min(int(g("limit", "30") or 30), 50))
            kwargs = {"limit": limit, "mode": g("mode") or None,
                      "topid": g("topid") or None,
                      "albumid": g("albumid") or None}
            return self._send(200, fn(**kwargs))
        except Exception as e:
            return self._send(200, {"songs": [], "error": "获取推荐失败: %s" % e})

    def _home(self, g):
        src = SRC.get(g("source"))
        fn = getattr(src, "home", None) if src else None
        if not fn:
            return self._send(200, {"quick": [], "playlists": [], "charts": [],
                                    "activities": [], "error": "该音源暂无首页"})
        try:
            limit = max(1, min(int(g("limit", "30") or 30), 50))
            return self._send(200, fn(limit=limit))
        except Exception as e:
            return self._send(200, {"quick": [], "playlists": [], "charts": [],
                                    "activities": [], "error": "获取首页失败: %s" % e})

    def _playlist(self, g):
        src = SRC.get(g("source"))
        if not src:
            return self._send(200, {"error": "未知音源"})
        try:
            return self._send(200, src.get_playlist(g("ref")))
        except Exception as e:
            return self._send(200, {"error": "获取歌单失败: %s" % e})

    def _mylists(self, g):
        src = SRC.get(g("source"))
        fn = getattr(src, "list_my_playlists", None) if src else None
        if not fn:
            return self._send(200, {"playlists": [], "error": "该音源不支持我的歌单"})
        try:
            return self._send(200, fn())
        except Exception as e:
            return self._send(200, {"error": str(e)})

    # ---------- 本地歌单 ----------
    def _local_pl_action(self, action, body):
        import time as _t
        # 类型违规一律在任何读写盘之前返回 HTTP 400; 业务错误仍保持既有 200 + {"error": ...} 约定
        if action in ("create", "rename"):
            name, code, err = _validate_pl_name(body.get("name"))
            if code:
                return self._send(code, {"error": "歌单参数无效: " + err})
        if action in ("delete", "rename", "seticon", "add", "remove"):
            if not _pl_id_ok(body.get("id")):
                return self._send(400, {"error": "歌单参数无效: id:id_type"})
        if action == "seticon":
            icon, code, err = _validate_icon(body.get("icon"))
            if code:
                return self._send(code, {"error": "歌单参数无效: " + err})
        if action in ("add", "remove") and "song" in body and not isinstance(body["song"], dict):
            return self._send(400, {"error": "歌单参数无效: song:song_not_object"})
        with _pl_lock:
            d = _load_pls()
            lists = d["lists"]
            if action == "create":
                if not name:
                    return self._send(200, {"error": "歌单名不能为空"})
                pid = "pl%d" % int(_t.time() * 1000)
                lists.append({"id": pid, "name": name, "created": int(_t.time()), "songs": []})
                _save_pls(d)
                return self._send(200, {"ok": True, "id": pid})
            if action == "delete":
                pid = body.get("id")
                d["lists"] = [l for l in lists if l.get("id") != pid]
                _save_pls(d)
                return self._send(200, {"ok": True})
            if action == "rename":
                pid = body.get("id")
                pl = next((l for l in lists if l.get("id") == pid), None)
                if not pl or not name:
                    return self._send(200, {"error": "参数无效"})
                pl["name"] = name
                _save_pls(d)
                return self._send(200, {"ok": True})
            if action == "seticon":
                pid = body.get("id")
                pl = next((l for l in lists if l.get("id") == pid), None)
                if not pl:
                    return self._send(200, {"error": "歌单不存在"})
                pl["icon"] = icon
                _save_pls(d)
                return self._send(200, {"ok": True})
            if action in ("add", "remove"):
                pid = body.get("id")
                s = body.get("song") or {}
                pl = next((l for l in lists if l.get("id") == pid), None)
                if not pl:
                    return self._send(200, {"error": "歌单不存在"})

                # 与 /api/dl 的任务身份键共用 _song_identity: 键序无关、中文值不再因
                # ensure_ascii 差异而分裂
                def key(x):
                    return _song_identity(x.get("source"), x.get("id"), x.get("extra"))
                k = key(s)
                songs = pl.setdefault("songs", [])
                if action == "add":
                    if not s.get("source") or s.get("id") is None:
                        return self._send(200, {"error": "歌曲信息无效"})
                    if any(key(x) == k for x in songs):
                        return self._send(200, {"ok": True, "existed": True})
                    songs.append(s)
                else:
                    pl["songs"] = [x for x in songs if key(x) != k]
                _save_pls(d)
                return self._send(200, {"ok": True})
        return self._send(200, {"error": "未知操作"})

    # ---------- 自定义皮肤文件 ----------
    def _skin_file(self, name):
        """以受保护的本地静态资源方式返回已上传的皮肤，支持视频 Range 请求。"""
        fname = os.path.basename(urllib.parse.unquote(name or ""))
        fp = os.path.join(SKIN_DIR, fname)
        if not fname or not os.path.isfile(fp):
            return self._send(404, "not found", "text/plain")
        size = os.path.getsize(fp)
        if size <= 0:
            return self._send(404, "not found", "text/plain")
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        unsatisfiable = False
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    suffix = int(m.group(2))
                    if suffix <= 0:
                        unsatisfiable = True
                    else:
                        start, end = max(0, size - suffix), size - 1
                code = 206
        if unsatisfiable or start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        ext = os.path.splitext(fname)[1].lower()
        self.send_response(code)
        self.send_header("Content-Type", SKIN_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                remain = end - start + 1
                while remain > 0:
                    chunk = f.read(min(64 * 1024, remain))
                    if not chunk:
                        break
                    remain -= len(chunk)
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _skin_upload(self, qs):
        """保存一份图片/GIF/视频皮肤并返回随机文件名。请求体为原始二进制。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return self._send(400, {"error": "皮肤文件为空"})
        if n > SKIN_MAX:
            return self._send(413, {"error": "皮肤文件不能超过 256MB"})
        original = (qs.get("name") or [""])[0]
        original = os.path.basename(urllib.parse.unquote(original))
        ext = os.path.splitext(original)[1].lower()
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ext not in SKIN_TYPES:
            mime_ext = next((e for e, m in SKIN_TYPES.items() if m == ctype), "")
            ext = mime_ext
        if ext not in SKIN_TYPES:
            return self._send(415, {"error": "仅支持图片、GIF 或视频皮肤"})
        # 只使用服务端生成的名称，设置文件中的 name 不会形成路径。
        fname = secrets.token_hex(16) + ext
        os.makedirs(SKIN_DIR, exist_ok=True)
        fp = os.path.join(SKIN_DIR, fname)
        tmp = fp + ".tmp"
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < n:
                    chunk = self.rfile.read(min(1024 * 1024, n - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    f.write(chunk)
            if got != n:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return self._send(400, {"error": "皮肤文件上传不完整"})
            os.replace(tmp, fp)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._send(500, {"error": "保存皮肤失败: %s" % e})
        return self._send(200, {"ok": True, "name": fname,
                                "mime": SKIN_TYPES[ext], "size": got})

    # ---------- 本地文件(已下载歌曲) ----------
    def _local_file(self, name):
        fname = os.path.basename(name or "")
        fp = os.path.join(DL_DIR, fname)
        if not fname or not os.path.isfile(fp):
            return self._send(404, "not found", "text/plain")
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        unsatisfiable = False
        if rng:
            # 只接受"单区间 bytes"范围头(单位大小写敏感, 保持现状); 语法非法/未知单位/多区间
            # 按 RFC 属于不可解析的 Range -> 忽略并返回 200 全文, 不是 416.
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    # 后缀区间 bytes=-N: 取末尾 N 字节; bytes=-0 不可满足
                    suffix = int(m.group(2))
                    if suffix <= 0:
                        unsatisfiable = True
                    else:
                        start = max(0, size - suffix)
                        end = size - 1
                code = 206
        if unsatisfiable or start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        self.send_response(code)
        ext = os.path.splitext(fname)[1].lower()
        self.send_header("Content-Type", AUDIO_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                remain = end - start + 1
                while remain > 0:
                    chunk = f.read(min(64 * 1024, remain))
                    if not chunk:
                        break
                    remain -= len(chunk)
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _stream_api(self, g):
        """返回播放地址(供前端调试); 实际播放走 /proxy."""
        src = SRC.get(g("source"))
        if not src:
            return self._send(200, {"error": "未知音源"})
        try:
            s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
            return self._send(200, src.get_stream(s))
        except Exception as e:
            return self._send(200, {"error": str(e)})

    def _lyrics_api(self, g):
        """返回当前歌曲歌词: {"lyric": LRC文本} 或 {"error": str}."""
        src = SRC.get(g("source"))
        if not src:
            return self._send(200, {"error": "未知音源"})
        if not hasattr(src, "get_lyrics"):
            return self._send(200, {"error": "该音源暂不支持歌词"})
        try:
            s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
            return self._send(200, src.get_lyrics(s))
        except Exception as e:
            return self._send(200, {"error": str(e)})

    def _subtitle_api(self, g):
        """视频字幕(B站官方/AI 字幕): {"lines":[{t,text}], ai} 或 {"error": str}."""
        src = SRC.get(g("source"))
        if not src or not hasattr(src, "get_subtitles"):
            return self._send(200, {"error": "该音源暂不支持字幕"})
        try:
            return self._send(200, src.get_subtitles(g("id"), g("cid") or None))
        except Exception as e:
            return self._send(200, {"error": str(e)})

    def _imgproxy(self, g):
        """图片代理: 带 Referer 拉取防盗链图片(如B站封面)并转发."""
        url = g("url") or ""
        if url.startswith("//"):
            url = "https:" + url
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https") or not any(
                host == h or host.endswith("." + h) for h in IMG_HOSTS):
            return self._send(400, "bad url", "text/plain")
        referer = "https://www.bilibili.com/"
        if "music.126.net" in host:
            referer = "https://music.163.com/"
        elif "gtimg.cn" in host or "qpic.cn" in host or host == "y.qq.com":
            referer = "https://y.qq.com/"
        headers = {"User-Agent": UA, "Referer": referer}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    return self._send(415, "not an image", "text/plain")
                data = resp.read(IMG_MAX + 1)
        except Exception as e:
            return self._send(502, {"error": str(e)})
        if len(data) > IMG_MAX:
            return self._send(413, "image too large", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _proxy(self, g):
        """音频统一代理: 后端带 Referer/Cookie 请求真实源, 流式转发, 支持 Range."""
        src = SRC.get(g("source"))
        if not src:
            return self._send(400, "unknown source", "text/plain")
        try:
            s = {"id": g("id"), "extra": json.loads(g("extra", "{}") or "{}")}
            if g("kind") == "video" and hasattr(src, "get_video"):
                info = src.get_video(s, qn=g("qn") or None)
            else:
                info = src.get_stream(s)
        except Exception as e:
            return self._send(502, {"error": str(e)})
        if "error" in info or not info.get("url"):
            return self._send(404, info)
        headers = dict(info.get("headers") or {})
        headers.setdefault("User-Agent", UA)
        rng = self.headers.get("Range")
        # B站旧版 durl 会把一个视频拆成多个顺序分片: 顺序接力成一条流。
        # 只有从 0 开始(或无 Range)的请求能安全接力; 非零 Range 仍只代理首片。
        segs = [u for u in (info.get("segments") or []) if u and u != info["url"]]
        m = re.match(r"bytes=(\d+)-", rng or "")
        multi = bool(segs) and (not rng or (m and int(m.group(1)) == 0))
        if rng and not multi:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(info["url"], headers=headers)
            resp = urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            return self._send(502, {"error": "上游取流失败: %s" % e})
        code = resp.getcode()
        self.send_response(code if not multi else 200)
        if not multi:
            self.send_header("Accept-Ranges", "bytes")
            for h in ("Content-Type", "Content-Length", "Content-Range"):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
        else:
            ctype = resp.headers.get("Content-Type")
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "none")
        self.end_headers()

        def _pump(r):
            try:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        return True
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return False
            finally:
                try:
                    r.close()
                except Exception:
                    pass

        if not _pump(resp):
            return
        if multi:
            seg_headers = dict(headers)
            seg_headers.pop("Range", None)
            for u in segs:
                try:
                    r2 = urllib.request.urlopen(
                        urllib.request.Request(u, headers=seg_headers), timeout=20)
                except Exception:
                    break
                if not _pump(r2):
                    break


def free_port(preferred=8787):
    for p in (preferred, 0):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return 0


def serve(port):
    os.makedirs(DATA_DIR, exist_ok=True)
    common.configure_proxy(_load_settings().get("proxy"))
    _dl_restore()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
