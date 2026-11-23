# -*- coding: utf-8 -*-
"""重启 MusicHub 音乐播放器: 精确杀掉运行本项目 main.py 的旧进程, 再启动新实例.

进程匹配只认"解释器为 python/pythonw 且命令行包含本项目 main.py 的完整路径",
不再用 CommandLine 包含 'musicplayer' 这类宽泛条件, 避免误杀无关进程.
"""
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.normcase(os.path.normpath(os.path.join(BASE_DIR, "main.py")))


def _ps_query():
    # PowerShell 单引号字符串内 ' 需写成 ''; 路径按字面正则转义后做不区分大小写包含匹配
    esc = re.escape(MAIN).replace("'", "''")
    return (
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
        "$_.CommandLine -imatch \"%s\" } | "
        "Select-Object -ExpandProperty ProcessId" % esc
    )


def find_pids():
    """返回正在运行的目标 main.py 进程 PID 列表."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _ps_query()],
        capture_output=True, text=True, creationflags=0x08000000,
    ).stdout
    pids = []
    me = os.getpid()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit() and int(line) != me:
            pids.append(int(line))
    return pids


def kill_pids(pids):
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)


def _start_via_wmi(pythonw):
    """用 WMI 创建进程: 不受当前终端/作业对象生命周期影响, 关掉终端也不会带走应用。

    返回 (return_value, pid); 失败时 pid 为 None。
    """
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine='\"%s\" \"%s\"'; CurrentDirectory='%s'}; "
        "Write-Output (\"{0} {1}\" -f $r.ReturnValue, $r.ProcessId)"
        % (pythonw, MAIN, BASE_DIR)
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, creationflags=0x08000000,
        )
    except Exception as e:
        return -1, None, str(e)
    text = (out.stdout or "").strip() or (out.stderr or "").strip()
    parts = text.split()
    if len(parts) >= 2 and parts[0].isdigit():
        rv = int(parts[0])
        pid = int(parts[1]) if parts[1].isdigit() else None
        return rv, pid, text
    return -1, None, text


def start(pythonw):
    rv, pid, detail = _start_via_wmi(pythonw)
    if rv == 0 and pid:
        print("started via WMI, pid:", pid)
        return pid
    print("WMI 启动失败(ReturnValue=%s): %s" % (rv, detail))
    proc = subprocess.Popen(
        [pythonw, MAIN], cwd=BASE_DIR,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    print("started via Popen, pid:", proc.pid)
    return proc.pid


def main():
    pids = find_pids()
    if pids:
        kill_pids(pids)
        print("killed:", pids)
        # 等待进程退出、端口释放
        for _ in range(20):
            if not find_pids():
                break
            time.sleep(0.3)

    # 优先使用安装了 pywebview 的 conda 环境
    candidates = [
        r"C:\Users\z1762\miniconda3\envs\gops-rocm\pythonw.exe",
        r"C:\Users\z1762\miniconda3\envs\gops\pythonw.exe",
    ]
    pythonw = next((c for c in candidates if os.path.exists(c)), None)
    if pythonw is None:
        py = sys.executable
        pythonw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = py
    pid = start(pythonw)
    # 等待进程真正起来, 避免"启动了但立刻退出"被误报成功
    for _ in range(20):
        time.sleep(0.3)
        if find_pids():
            print("running pids:", find_pids())
            return
    print("警告: 进程未存活, pid:", pid)


if __name__ == "__main__":
    main()
