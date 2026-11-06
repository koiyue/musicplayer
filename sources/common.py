# -*- coding: utf-8 -*-
"""纯标准库 HTTP 工具, 供各音源模块复用."""
import json
import urllib.request
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def http_get(url, headers=None, timeout=12, raw=False):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    if raw:
        return payload
    text = payload.decode("utf-8", "replace")
    # 部分接口用 jsonp 包裹, 自动剥离
    if text and text[0] not in "{[":
        s, e = text.find("("), text.rfind(")")
        if s != -1 and e != -1:
            text = text[s + 1:e]
    try:
        return json.loads(text)
    except Exception:
        return {"_nonjson": text[:300]}


def http_post(url, data=None, headers=None, json_body=False, timeout=12):
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(text)
    except Exception:
        return {"_nonjson": text[:300]}
