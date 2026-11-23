# -*- coding: utf-8 -*-
"""Android 启动入口: 在应用进程内启动 MusicHub 本地服务, 返回监听端口.

必须先从 Kotlin 传入可写数据目录, 再 import sources.server——server 模块在
导入时就会根据 MUSICHUB_DATA_DIR 固定 DATA_DIR.
"""
import os
import threading

_lock = threading.Lock()
_port = 0
_httpd = None


def start(data_dir):
    """启动本地 HTTP 服务(幂等), 返回端口号."""
    global _port, _httpd
    with _lock:
        if _port:
            return _port
        data_dir = os.path.abspath(data_dir)
        os.makedirs(data_dir, exist_ok=True)
        os.environ["MUSICHUB_DATA_DIR"] = data_dir
        # 根路径返回 web-android/ 独立界面
        os.environ["MUSICHUB_CLIENT"] = "android"

        from sources import server  # noqa: E402  (必须在设置环境变量之后导入)

        # 端口被占用会自动顺延, 拿到实际端口再交给 WebView
        _httpd, port = server.serve_auto(8787)
        _port = port
        return port
