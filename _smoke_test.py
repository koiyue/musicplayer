# -*- coding: utf-8 -*-
"""后端 API 冒烟测试: 四源搜索 + 播放地址 + 代理取流 + Range + 队列读写 + 登录状态.

默认在进程内启动隔离服务: 临时 DATA_DIR/DL_DIR + 随机端口, 绝不写真实
queue/settings/downloads; 测试结束后关闭服务并清理临时目录.

用法:
    python _smoke_test.py                # 隔离实例(默认, 推荐)
    python _smoke_test.py --live URL     # 显式指向已运行实例(会写其真实数据, 自行确认)
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)

REAL_DATA = os.path.join(HERE, "data")
REAL_WATCH = ["queue.json", "settings.json", "playlists.json",
              os.path.join("downloads", "index.json"),
              os.path.join("downloads", "tasks.json")]


def _digest(path):
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            return (hashlib.sha256(f.read()).hexdigest(), st.st_mtime_ns)
    except OSError:
        return None


def _snapshot():
    return {rel: _digest(os.path.join(REAL_DATA, rel)) for rel in REAL_WATCH}


def start_isolated():
    """启动使用临时数据目录的隔离服务, 返回 (httpd, tmp_dir, base_url)."""
    import server
    tmp = tempfile.mkdtemp(prefix="musichub-smoke-")
    data = os.path.join(tmp, "data")
    dl = os.path.join(data, "downloads")
    server.DATA_DIR = data
    server.QUEUE_FILE = os.path.join(data, "queue.json")
    server.SETTINGS_FILE = os.path.join(data, "settings.json")
    server.PLAYLISTS_FILE = os.path.join(data, "playlists.json")
    server.DL_DIR = dl
    server.DL_FILE = os.path.join(dl, "index.json")
    server.DL_TASKS_FILE = os.path.join(dl, "tasks.json")
    port = server.free_port(0)
    httpd = server.serve(port)
    return httpd, tmp, "http://127.0.0.1:%d" % port


def main():
    httpd = tmp = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--live":
        base = sys.argv[2].rstrip("/")
    else:
        before = _snapshot()
        httpd, tmp, base = start_isolated()

    global BASE
    BASE = base
    TOKEN = re.search(r'name="mh-token" content="([^"]+)"',
                      _opener.open(BASE + "/", timeout=10).read().decode("utf-8")).group(1)

    fails = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (" | " + detail if detail else ""))
        if not cond:
            fails.append(name)

    def get(path, timeout=25):
        req = urllib.request.Request(BASE + path, headers={"X-MH-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)

    def jget(path, timeout=25):
        st, body, _ = get(path, timeout)
        return st, json.loads(body.decode("utf-8"))

    # 0. 令牌鉴权: 无令牌必须 403
    try:
        req = urllib.request.Request(BASE + "/api/sources")
        urllib.request.urlopen(req, timeout=10)
        check("auth:no-token-403", False, "无令牌请求竟然成功")
    except urllib.error.HTTPError as e:
        check("auth:no-token-403", e.code == 403, "http=%s" % e.code)

    # 1. 搜索
    for src, kw in (("netease", "晴天"), ("qq", "晴天"), ("migu", "晴天"), ("bilibili", "晴天")):
        try:
            st, j = jget("/api/search?source=%s&keyword=%s&limit=5" % (src, urllib.parse.quote(kw)))
            songs = j.get("songs") or []
            err = j.get("error") or ""
            check("search:" + src, bool(songs), "%d 条 %s" % (len(songs), err))
            if songs:
                s0 = songs[0]
                print("     样例: %s - %s [%s] dur=%s cover=%s" %
                      (s0.get("artist"), s0.get("title"), s0.get("id"),
                       s0.get("duration"), "Y" if s0.get("cover") else "N"))
        except Exception as e:
            check("search:" + src, False, repr(e))

    # 2. 播放地址 + 3. 代理取流
    for src in ("netease", "qq", "migu", "bilibili"):
        try:
            st, j = jget("/api/search?source=%s&keyword=%s&limit=3" % (src, urllib.parse.quote("晴天")))
            songs = j.get("songs") or []
            if not songs:
                check("stream:" + src, False, "无搜索结果")
                continue
            s = songs[0]
            st2, j2 = jget("/api/stream?source=%s&id=%s&extra=%s" %
                           (src, urllib.parse.quote(str(s["id"])),
                            urllib.parse.quote(json.dumps(s.get("extra") or {}))))
            ok = bool(j2.get("url"))
            check("stream:" + src, ok, (j2.get("url") or j2.get("error") or "")[:90])
            if ok:
                st3, body3, hdr3 = get("/proxy?source=%s&id=%s&extra=%s" %
                                       (src, urllib.parse.quote(str(s["id"])),
                                        urllib.parse.quote(json.dumps(s.get("extra") or {}))))
                check("proxy:" + src, st3 == 200 and len(body3) > 1000,
                      "http=%s bytes=%d type=%s" % (st3, len(body3), hdr3.get("Content-Type")))
        except Exception as e:
            check("stream:" + src, False, repr(e))

    # 4. Range 请求(进度条拖动依赖)
    try:
        st, j = jget("/api/search?source=netease&keyword=%s&limit=1" % urllib.parse.quote("晴天"))
        songs = j.get("songs") or []
        if songs:
            s = songs[0]
            url = (BASE + "/proxy?source=netease&id=%s&extra=%s" %
                   (urllib.parse.quote(str(s["id"])),
                    urllib.parse.quote(json.dumps(s.get("extra") or {}))))
            req = urllib.request.Request(url, headers={"Range": "bytes=1000-2000", "X-MH-Token": TOKEN})
            with urllib.request.urlopen(req, timeout=25) as r:
                check("proxy:range", r.status == 206, "http=%s len=%d range=%s" %
                      (r.status, len(r.read()), r.headers.get("Content-Range")))
    except Exception as e:
        check("proxy:range", False, repr(e))

    # 5. 队列读写(隔离实例内往返)
    try:
        payload = json.dumps({"songs": [{"source": "netease", "id": "1", "title": "t"}],
                              "index": 0}).encode()
        req = urllib.request.Request(BASE + "/api/queue", data=payload,
                                     headers={"Content-Type": "application/json", "X-MH-Token": TOKEN})
        urllib.request.urlopen(req, timeout=10).read()
        st, j = jget("/api/queue")
        check("queue:roundtrip", len(j.get("songs") or []) == 1, json.dumps(j, ensure_ascii=False)[:80])
    except Exception as e:
        check("queue:roundtrip", False, repr(e))

    # 6. 网易云登录状态(只读)
    try:
        st, j = jget("/api/netease/status")
        check("netease:status", "logged_in" in j, json.dumps(j))
    except Exception as e:
        check("netease:status", False, repr(e))

    # 7. 真实数据未被改写(仅隔离模式校验)
    if httpd is not None:
        after = _snapshot()
        changed = [rel for rel in before if before[rel] != after[rel]]
        check("isolation:real-data-untouched", not changed,
              "变化: %s" % changed if changed else "queue/settings/playlists/downloads 哈希与 mtime 均不变")

    if httpd is not None:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== %d failed ===" % len(fails))
    if fails:
        print("FAILED: " + ", ".join(fails))
    sys.exit(1 if fails else 0)


BASE = None
TOKEN = None

if __name__ == "__main__":
    main()
