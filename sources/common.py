# -*- coding: utf-8 -*-
"""纯标准库 HTTP 工具, 供各音源模块复用.

8.6 修复: 增加重试 / 退避 / 总墙钟预算, 并把失败以既有 {"error": str} 形态显式返回,
让上层 `"error" in info` 通道自动生效; 非 JSON 响应在 {"_nonjson": ...} 上做超集扩展,
保留原诊断片段. 成功路径(含 raw=True / JSONP 剥离 / utf-8 replace 解码)逐字节保持原样.
"""
import json
import random
import time
import urllib.error
import urllib.request
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 仅这些 HTTP 状态码触发重试; 其余 4xx / 3xx / 解析失败一律不重试.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# 退避基准(秒): 第 i 次重试前使用 _BACKOFF_BASE[min(i, len-1)] * jitter(0.85, 1.15).
_BACKOFF_BASE = (0.3, 0.9)
_JITTER_LO = 0.85
_JITTER_HI = 1.15


def _backoff_upper(attempts):
    """累计退避上界(未乘抖动前的最大值 * _JITTER_HI), 用于计算默认预算."""
    total = 0.0
    for i in range(max(0, int(attempts) - 1)):
        base = _BACKOFF_BASE[min(i, len(_BACKOFF_BASE) - 1)]
        total += base * _JITTER_HI
    return total


def _do_backoff(i, deadline):
    """按索引 i 睡眠, 受 wall-clock deadline 约束.
    返回 True 表示完成一次退避, False 表示预算耗尽."""
    base = _BACKOFF_BASE[min(i, len(_BACKOFF_BASE) - 1)]
    delay = base * random.uniform(_JITTER_LO, _JITTER_HI)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(delay, remaining))
    return True


def _classify_urlerror(exc):
    """URLError 分流: 只对 timeout / ConnectionError 类做重试, 其他视为不可重试的网络错误."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return "timeout", "请求超时: %s" % (reason if reason is not None else exc,), True
    if isinstance(reason, ConnectionError):
        return "network", "连接错误: %s" % (reason,), True
    if isinstance(exc, TimeoutError):
        return "timeout", "请求超时: %s" % (exc,), True
    if isinstance(exc, ConnectionError):
        return "network", "连接错误: %s" % (exc,), True
    # 非白名单原因(如 unreachable host / ssl verify 失败等)不重试
    return "network", "网络错误: %s" % (reason if reason is not None else exc,), False


def _fetch(req, attempts, timeout, budget):
    """带重试 / 退避 / 预算的 urllib 请求执行器.

    返回 (payload_bytes, n_attempts, None) 表示成功;
    返回 (None, n_attempts, (kind, error_message)) 表示失败, kind ∈ {timeout, http_status, network}.
    """
    try:
        attempts = int(attempts)
    except Exception:
        attempts = 2
    if attempts < 1:
        attempts = 1
    try:
        timeout_f = float(timeout)
    except Exception:
        timeout_f = 12.0
    if budget is None:
        budget_f = timeout_f * attempts + _backoff_upper(attempts)
    else:
        try:
            budget_f = float(budget)
        except Exception:
            budget_f = timeout_f * attempts + _backoff_upper(attempts)
    if budget_f <= 0:
        budget_f = timeout_f if timeout_f > 0 else 12.0

    start = time.monotonic()
    deadline = start + budget_f
    n = 0
    last = ("network", "未知网络错误")
    for i in range(attempts):
        n = i + 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, n, ("timeout",
                             "总预算耗尽(%.2fs): %s" % (budget_f, last[1]))
        per_try = timeout_f if timeout_f < remaining else remaining
        try:
            with urllib.request.urlopen(req, timeout=per_try) as resp:
                payload = resp.read()
            return payload, n, None
        except urllib.error.HTTPError as e:
            code = getattr(e, "code", None)
            msg = "HTTP 状态码 %s" % (code,)
            last = ("http_status", msg)
            retryable = (code in _RETRY_STATUS) and (i + 1 < attempts)
            if retryable and _do_backoff(i, deadline):
                continue
            return None, n, last
        except urllib.error.URLError as e:
            kind, msg, retryable = _classify_urlerror(e)
            last = (kind, msg)
            if retryable and (i + 1 < attempts) and _do_backoff(i, deadline):
                continue
            return None, n, last
        except TimeoutError as e:
            last = ("timeout", "请求超时: %s" % (e,))
            if (i + 1 < attempts) and _do_backoff(i, deadline):
                continue
            return None, n, last
        except ConnectionError as e:
            last = ("network", "连接错误: %s" % (e,))
            if (i + 1 < attempts) and _do_backoff(i, deadline):
                continue
            return None, n, last
        except Exception as e:
            # 未预期的异常: 保留原语义不抛出, 归为 network, 不重试
            last = ("network", "请求异常 %s: %s" % (type(e).__name__, e))
            return None, n, last
    return None, n, last


def _nonjson_result(text, n_attempts):
    """非 JSON 响应: 在既有 {"_nonjson": ...} 形态上做超集扩展, 让 "error" in info 立即生效."""
    head = (text or "").strip()[:120]
    return {
        "error": "响应非 JSON (前 120 字符: %s)" % head,
        "_http_error": "non_json",
        "_attempts": n_attempts,
        "_nonjson": (text or "")[:300],
    }


def _fail_result(err, n_attempts):
    kind, msg = err
    return {"error": msg, "_http_error": kind, "_attempts": n_attempts}


def configure_proxy(cfg):
    """按设置安装全局 urllib opener, 影响所有音源搜索/取流/下载请求.

    mode=system 跟随系统/环境变量, none 强制直连, custom 使用自定义 HTTP 代理。
    本机回环请求(main.py 的 _loopback_opener)自建 opener, 不经此全局设置。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    mode = cfg.get("mode")
    if mode == "custom":
        http = str(cfg.get("http") or "").strip()
        https = str(cfg.get("https") or "").strip() or http
        proxies = {}
        if http:
            proxies["http"] = http
        if https:
            proxies["https"] = https
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    elif mode == "none":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    urllib.request.install_opener(opener)


def http_get(url, headers=None, timeout=12, raw=False, attempts=2, budget=None):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    payload, n, err = _fetch(req, attempts, timeout, budget)
    if err is not None:
        return _fail_result(err, n)
    if raw:
        return payload
    text = payload.decode("utf-8", "replace").lstrip()
    # 部分接口用 jsonp 包裹, 自动剥离
    if text and text[0] not in "{[":
        s, e = text.find("("), text.rfind(")")
        if s != -1 and e != -1:
            text = text[s + 1:e]
    try:
        return json.loads(text)
    except Exception:
        return _nonjson_result(text, n)


def http_post(url, data=None, headers=None, json_body=False, timeout=12,
              attempts=2, budget=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        if json_body:
            body = json.dumps(data).encode("utf-8")
            h["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(data).encode("utf-8")
            h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h)
    payload, n, err = _fetch(req, attempts, timeout, budget)
    if err is not None:
        return _fail_result(err, n)
    text = payload.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except Exception:
        return _nonjson_result(text, n)
