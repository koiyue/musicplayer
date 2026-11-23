# -*- coding: utf-8 -*-
"""把主仓库的 sources/ 与 web/ 同步到 Android 工程的 Python 目录.

Chaquopy 会把 app/src/main/python 下的内容打进 APK 并在运行时解压;
server.BASE_DIR 以 sources 的父目录为准, 因此 web/ 必须与 sources/ 同级.
修改桌面版代码后, 运行一次本脚本(或 build_apk.ps1)即可刷新.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DST = os.path.join(HERE, "app", "src", "main", "python")


def sync(name):
    src = os.path.join(ROOT, name)
    dst = os.path.join(DST, name)
    if not os.path.isdir(src):
        raise SystemExit("缺少目录: %s" % src)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


if __name__ == "__main__":
    os.makedirs(DST, exist_ok=True)
    sync("sources")
    sync("web")
    sync("web-android")
    print("已同步到", DST)
