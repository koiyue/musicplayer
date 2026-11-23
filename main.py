# -*- coding: utf-8 -*-
"""MusicHub 入口: 起本地服务 + pywebview(Edge WebView2) 窗口.

窗口约定: 无边框(frameless) + 前端自绘米哈游风格标题栏; 铺满工作区, 不可调整大小.
WebView2 原生继承 Edge 的 Windows 触控支持(惯性滚动、Pointer Events).
"""
import base64
import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

# pywebview 的 WebView2 后端依赖 pythonnet/.NET: 默认服务器 GC 会按 CPU 数量
# 预留大块私有内存(实测进程私有占用可达 800MB+)。切换为工作站 GC 并启用
# 并发回收, 显著降低常驻占用, 必须在导入 webview/clr 之前设置。
for _gc_env in ("COMPlus_gcServer", "DOTNET_gcServer"):
    os.environ.setdefault(_gc_env, "0")
for _gc_env in ("COMPlus_GCConcurrent", "DOTNET_GCConcurrent"):
    os.environ.setdefault(_gc_env, "1")


def _set_dpi_aware():
    """进程 DPI 感知必须在导入 webview 之前抢先设置为 Per-Monitor V2.

    pywebview 内部只调用 SetProcessDPIAware()(系统级感知), 而 WebView2
    Windowed hosting 在高 DPI 缩放(本机 200%)下会导致触控/指针坐标错位,
    表现为桌面程序内触控完全失效. 进程感知模式只能设置一次, 先设 PMv2 后
    pywebview 的 SetProcessDPIAware 调用会静默失败, 不再干扰.
    """
    import ctypes

    try:  # Win10 1703+
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:  # Win8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


_set_dpi_aware()

import ctypes
import ctypes.wintypes as wt

import webview

from sources import server, smtc

try:
    # 系统媒体卡片显示 MusicHub 而非"未知应用"; 必须在窗口创建前设置。
    smtc.register_app_identity()
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(smtc.AUMID)
except Exception:
    pass

try:
    from sources.desktop_lyrics import DesktopLyrics
except Exception as _desktop_lyrics_import_error:
    # The browser fallback and headless API mode must remain usable even when
    # the optional native desktop-lyrics window cannot be imported.
    DesktopLyrics = None

PORT = server.free_port(8787)
URL = "http://127.0.0.1:%d/" % PORT
TITLE = "MusicHub"

_user32 = ctypes.windll.user32
_hwnd_cache = {}
_main_window = {}
_desktop_lyrics = None

# 显式 64 位原型: HWND/HICON 走 ctypes 默认 c_int 会被截断
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wt.BOOL
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.PostMessageW.restype = wt.BOOL
_user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.GetCursorPos.restype = wt.BOOL
_user32.CreatePopupMenu.restype = wt.HMENU
_user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
_user32.AppendMenuW.restype = wt.BOOL
_user32.TrackPopupMenu.argtypes = [wt.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint, wt.HWND, ctypes.c_void_p]
_user32.TrackPopupMenu.restype = ctypes.c_uint
_user32.DestroyMenu.argtypes = [wt.HMENU]
_user32.DestroyMenu.restype = wt.BOOL


def _shutdown_desktop_lyrics():
    """Stop the native lyrics thread without changing its saved preference."""
    global _desktop_lyrics
    obj = _desktop_lyrics
    _desktop_lyrics = None
    if obj is not None:
        try:
            obj.close()
        except Exception as e:
            _log("桌面歌词关闭失败: %s" % e)


def _find_hwnd():
    """按标题查找本进程的窗口句柄(缓存).

    必须校验窗口所属 PID: 否则资源管理器打开 MusicHub 文件夹时,
    其窗口标题也是 "MusicHub", 会被误匹配并被 SetWindowPos 最大化.
    """
    if _hwnd_cache.get("h"):
        h = _hwnd_cache["h"]
        ok = bool(_user32.IsWindow(h))
        if ok:
            n = _user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(h, buf, n + 1)
            ok = buf.value == TITLE
        if ok:
            return h
        _hwnd_cache.clear()

    found = []
    exact = []
    mypid = os.getpid()
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, lp):
        if _user32.IsWindow(hwnd):
            # 托盘辅助窗口标题是 "MusicHubTray", 同样以 TITLE 开头且属于本进程,
            # 若不排除会被当成主窗口缓存, 导致铺满/最小化/关闭全部作用在隐藏窗口上.
            if hwnd == _tray.get("hwnd"):
                return True
            pid = wt.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != mypid:
                return True
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == TITLE:
                    exact.append(hwnd)
                elif buf.value.startswith(TITLE):
                    found.append(hwnd)
        return True

    _user32.EnumWindows(EnumProc(cb), 0)
    if exact:
        _hwnd_cache["h"] = exact[0]
    elif found:
        _hwnd_cache["h"] = found[0]
    return _hwnd_cache.get("h")


def _work_area():
    rect = wt.RECT()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


# ---------------- 任务托盘 ----------------
WM_TRAY = 0x8001
WM_SHOW_APP = 0x8002  # 二次启动(点媒体卡片)唤醒主窗口
WM_DESTROY = 0x0002
SW_HIDE, SW_SHOW, SW_MINIMIZE, SW_RESTORE = 0, 5, 6, 9
IDI_APPLICATION = 32512
LR_LOADFROMFILE = 0x0010
IMAGE_ICON = 1

_shell32 = ctypes.windll.shell32


def _icon_path():
    base = os.path.dirname(os.path.abspath(__file__))
    dirs = [base]
    if getattr(sys, "frozen", False):
        dirs += [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", base)]
    for d in dirs:
        p = os.path.join(d, "assets", "app.ico")
        if os.path.exists(p):
            return p
    return None


def _load_app_icon():
    """从 assets/app.ico 加载 HICON, 失败返回 None."""
    p = _icon_path()
    if not p:
        return None
    _user32.LoadImageW.restype = wt.HICON
    _user32.LoadImageW.argtypes = [wt.HINSTANCE, ctypes.c_wchar_p, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    return _user32.LoadImageW(0, p, IMAGE_ICON, 0, 0, LR_LOADFROMFILE) or None


class _NID(ctypes.Structure):
    """NOTIFYICONDATAW(完整布局, 与 shellapi.h 一致: szTip 为 128 WCHAR)."""

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_char * 16),
        ("hBalloonIcon", wt.HICON),
    ]


_tray = {"nid": None, "hwnd": None, "ready": False, "thread_id": 0, "menu_ts": 0.0}
_TRAY_CLASS = "MusicHubTrayWindow"
_TRAY_PROC_REF = None
_TRAY_CLASS_READY = False


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                                                   ctypes.c_size_t, ctypes.c_ssize_t)),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


# 托盘专用 user32 实例. ctypes 的 argtypes 挂在 DLL 对象上, 与 desktop_lyrics
# 共用 ctypes.windll.user32 会让双方的 RegisterClassExW 原型互相覆盖, 传入
# byref(另一个同名结构体)就会报 "expected LP__WNDCLASSEXW ..."; 单独加载一份
# 互不干扰(同一进程, 句柄通用).
_tray_u32 = ctypes.WinDLL("user32", use_last_error=True)
_tray_u32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
_tray_u32.RegisterClassExW.restype = wt.ATOM
_tray_u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                      wt.INT, wt.INT, wt.INT, wt.INT, wt.HWND, wt.HMENU,
                                      wt.HINSTANCE, wt.LPVOID]
_tray_u32.CreateWindowExW.restype = wt.HWND
_tray_u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
_tray_u32.DefWindowProcW.restype = ctypes.c_ssize_t
_tray_u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_tray_u32.GetMessageW.restype = wt.BOOL
_tray_u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
_tray_u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_tray_u32.DispatchMessageW.restype = ctypes.c_ssize_t
_tray_u32.PostQuitMessage.argtypes = [ctypes.c_int]
_tray_u32.PostQuitMessage.restype = None

ctypes.windll.kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
ctypes.windll.kernel32.GetModuleHandleW.restype = wt.HINSTANCE
ctypes.windll.kernel32.GetCurrentThreadId.restype = wt.DWORD

_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(_NID)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL


def _tray_remove():
    if _tray["nid"] is not None:
        _shell32.Shell_NotifyIconW(2, ctypes.byref(_tray["nid"]))  # NIM_DELETE
        _tray["nid"] = None


# ---------------- 任务栏按钮控制(ITaskbarList) ----------------
# 关闭窗口时要"缩到托盘"但仍保持窗口存在(最小化而非隐藏): 隐藏窗口无法被
# Windows 媒体卡片激活唤醒, 最小化可以。用 ITaskbarList 去掉任务栏按钮,
# 视觉上等价于收进托盘。
_CLSID_TASKBAR = "56FDF344-FD6D-11d0-958A-006097C9A090"
_IID_ITASKBAR = "56FDF342-FD6D-11d0-958A-006097C9A090"
_taskbar = {"obj": None, "tried": False}
_ole32 = ctypes.windll.ole32


def _taskbar_obj():
    """懒加载 ITaskbarList 实例, 失败返回 None."""
    if _taskbar["tried"]:
        return _taskbar["obj"]
    _taskbar["tried"] = True
    try:
        import uuid

        def gbuf(s):
            return ctypes.cast(
                ctypes.create_string_buffer(uuid.UUID(s).bytes_le, 16),
                ctypes.c_void_p)

        def vfn(ptr, i, restype, *argtypes):
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
            fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[i]
            return ctypes.cast(fn, ctypes.WINFUNCTYPE(restype, ctypes.c_void_p,
                                                      *argtypes))

        _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
        _ole32.CoInitializeEx.restype = ctypes.c_long
        _ole32.CoInitializeEx(None, 2)
        _ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            wt.DWORD, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
        _ole32.CoCreateInstance.restype = ctypes.c_long
        obj = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(gbuf(_CLSID_TASKBAR), None, 1,
                                     gbuf(_IID_ITASKBAR), ctypes.byref(obj))
        if hr < 0 or not obj:
            return None
        init = vfn(obj, 3, ctypes.c_long)
        if init(obj) < 0:
            return None
        _taskbar["obj"] = obj
        return obj
    except Exception as e:
        _log("ITaskbarList 初始化失败: %s" % e)
        return None


def _taskbar_tab(hwnd, show):
    """显示/隐藏任务栏按钮 (show=True 为添加)."""
    obj = _taskbar_obj()
    if not obj:
        return
    try:
        def vfn(i, restype, *argtypes):
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
            fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[i]
            return ctypes.cast(fn, ctypes.WINFUNCTYPE(restype, ctypes.c_void_p,
                                                      *argtypes))

        # vtable: HrInit=3, AddTab=4, DeleteTab=5
        (vfn(4, ctypes.c_long, wt.HWND) if show else vfn(5, ctypes.c_long, wt.HWND))(obj, hwnd)
    except Exception as e:
        _log("任务栏按钮切换失败: %s" % e)


def _tray_hide_window(hwnd):
    """关闭到托盘: 去掉任务栏按钮并最小化(保持窗口存在, 可被卡片激活唤醒)."""
    _taskbar_tab(hwnd, False)
    _user32.ShowWindow(hwnd, SW_MINIMIZE)
    # 卡片激活恢复窗口时不经过 _tray_show, 用轻量轮询补回任务栏按钮
    if not _tray.get("restore_watch"):
        _tray["restore_watch"] = True
        threading.Thread(target=_tray_restore_watch, daemon=True).start()


def _tray_restore_watch():
    """等待窗口被外部(媒体卡片/任务栏)恢复后, 补回任务栏按钮并结束监听."""
    for _ in range(240):  # 最多监听 2 分钟
        time.sleep(0.5)
        h = _find_hwnd()
        if not h:
            continue
        if not _user32.IsIconic(h) and _user32.IsWindowVisible(h):
            _taskbar_tab(h, True)
            break
    _tray["restore_watch"] = False


def _tray_show():
    # 优先用 pywebview 自己的 restore/show, 避免 frameless 窗口在 Win32 下恢复失败
    w = _main_window.get("win")
    if w is not None:
        try:
            w.restore()
            w.show()
        except Exception as e:
            _log("托盘恢复(pywebview)失败: %s" % e)
    h = _find_hwnd()
    if not h:
        _log("托盘恢复失败: 未找到主窗口")
        return
    _log("托盘恢复: hwnd=%s iconic=%s visible=%s" % (
        h, bool(_user32.IsIconic(h)), bool(_user32.IsWindowVisible(h))))
    _user32.ShowWindow(h, SW_RESTORE)
    _user32.ShowWindow(h, SW_SHOW)
    _taskbar_tab(h, True)  # 恢复任务栏按钮
    _user32.SetForegroundWindow(h)
    _user32.BringWindowToTop(h)


def _tray_menu():
    """在托盘窗口上弹出菜单. 托盘窗口仅作 owner, 不影响主窗口激活."""
    owner = _tray["hwnd"]
    menu = _user32.CreatePopupMenu()
    _user32.AppendMenuW(menu, 0, 1001, "显示 MusicHub")
    _user32.AppendMenuW(menu, 0x400, 0, None)  # MF_SEPARATOR
    _user32.AppendMenuW(menu, 0, 1002, "退出")
    pt = wt.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    # TrackPopupMenu 要求 owner 为前台窗口并先发 WM_NULL, 否则点菜单外部不收起
    _user32.SetForegroundWindow(owner)
    cmd = _user32.TrackPopupMenu(menu, 0x0000 | 0x0020 | 0x0100, pt.x, pt.y, 0, owner,
                                 None)  # LEFTALIGN|TOPALIGN|RETURNCMD
    _user32.PostMessageW(owner, 0x0000, 0, 0)  # WM_NULL: 让菜单立即收起
    _user32.DestroyMenu(menu)
    if cmd == 1001:
        _tray_show()
    elif cmd == 1002:
        h = _find_hwnd()
        if h:
            _user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE: 走主窗口关闭流程


def _tray_wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_SHOW_APP:
        # 二次启动(媒体卡片/开始菜单)请求: 唤醒主窗口
        _log("收到二次启动唤醒消息")
        _tray_show()
        return 0
    if msg == WM_TRAY:
        ev = lparam & 0xFFFF
        # 兼容传统 shell(WM_LBUTTONUP/DBLCLK) 与 Win11 24H2+ 新版通知区域
        # (左键=NIN_SELECT 0x0400, 右键=WM_CONTEXTMENU 0x007B)
        if ev in (0x0202, 0x0203, 0x0400):  # 左键抬起/双击/选择 -> 恢复窗口
            _log("托盘左键 0x%04X" % ev)
            _tray_show()
        elif ev in (0x0205, 0x007B):  # 右键抬起/上下文菜单 -> 弹菜单
            # 只认抬起: 若在按下(0x0204)时弹菜单, 松开后的 0x0205 会再弹一次
            now = time.monotonic()
            if now - _tray["menu_ts"] > 0.35:  # 两种事件码同时到达时去重
                _tray["menu_ts"] = now
                _log("托盘右键 0x%04X" % ev)
                _tray_menu()
        return 0
    if msg == WM_DESTROY:
        _tray_remove()
        _tray_u32.PostQuitMessage(0)
        return 0
    return _tray_u32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _tray_thread_main():
    """托盘专属线程: 创建隐藏窗口 + 消息循环, 与 pywebview 的 WinForms 窗口完全解耦.

    旧实现子类化主窗口过程接收回调, 但 WinForms 会重置 wndproc, 导致托盘消息丢失。
    窗口类只注册一次, 重复启用时复用已有类。
    """
    global _TRAY_PROC_REF, _TRAY_CLASS_READY
    hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
    _tray["thread_id"] = ctypes.windll.kernel32.GetCurrentThreadId()
    if not _TRAY_CLASS_READY:
        try:
            _TRAY_PROC_REF = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t)(
                _tray_wnd_proc)
            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.style = 0
            wc.lpfnWndProc = _TRAY_PROC_REF
            wc.cbClsExtra = wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.lpszClassName = _TRAY_CLASS
            atom = _tray_u32.RegisterClassExW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                _log("托盘窗口类注册失败(0x%X)" % ctypes.get_last_error())
                return
            _TRAY_CLASS_READY = True
        except Exception as e:
            _log("托盘窗口类注册异常: %s" % e)
            return
    hwnd = _tray_u32.CreateWindowExW(0x00000080, _TRAY_CLASS, "MusicHubTray", 0x80000000,
                                     0, 0, 0, 0, None, None, hinst, None)  # WS_EX_TOOLWINDOW|WS_POPUP
    if not hwnd:
        _log("托盘窗口创建失败(0x%X)" % ctypes.get_last_error())
        return
    _tray["hwnd"] = hwnd

    hicon = _load_app_icon()
    if not hicon:
        _user32.LoadIconW.restype = wt.HICON
        _user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
        hicon = _user32.LoadIconW(0, IDI_APPLICATION)
    nid = _NID()
    nid.cbSize = ctypes.sizeof(_NID)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = 0x1 | 0x2 | 0x4  # NIF_MESSAGE|NIF_ICON|NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = hicon
    nid.szTip = "MusicHub"
    if not _shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):  # NIM_ADD
        _log("托盘图标注册失败(0x%X)" % ctypes.get_last_error())
        return
    _tray["nid"] = nid
    _tray["ready"] = True
    _log("托盘图标已就绪")
    # 应用图标同步给主窗口(任务栏显示), 稍后主窗口才存在
    def _sync_icon():
        for _ in range(20):
            time.sleep(0.3)
            h = _find_hwnd()
            if h and hicon:
                _user32.SendMessageW(h, 0x007F, 1, hicon)  # WM_SETICON, ICON_BIG
                _user32.SendMessageW(h, 0x007F, 0, hicon)  # ICON_SMALL
                break
    threading.Thread(target=_sync_icon, daemon=True).start()

    msg = wt.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))


def _setup_tray():
    if _tray["ready"]:
        return
    threading.Thread(target=_tray_thread_main, name="MusicHubTray", daemon=True).start()


# 应用内登录配置: 官方页面跑在真实 WebView2 里, 自行完成验证码/设备校验,
# 登录态落在 WebView2 Cookie 中, 回收后交给后端保存(绕开纯接口风控)。
_QQ_KEY_KEYS = ("qqmusic_key", "qm_keyst", "music_key", "skey", "qm36")
_INAPP_LOGIN = {
    "netease": {
        "title": "网易云音乐登录",
        "url": "https://music.163.com/#/login",
        "ready": lambda f: bool(f.get("MUSIC_U")),
        "cookie": lambda f: "MUSIC_U=%s; NMTID=%s" % (f.get("MUSIC_U", ""),
                                                      f.get("NMTID", "")),
        "api": "/api/netease/manual",
    },
    "qq": {
        "title": "QQ音乐登录",
        "url": "https://y.qq.com/",
        "ready": lambda f: bool(f.get("uin")) and any(f.get(k) for k in _QQ_KEY_KEYS),
        "cookie": lambda f: "; ".join("%s=%s" % (k, v) for k, v in f.items() if v),
        "api": "/api/qq/manual",
    },
    "douyin": {
        "title": "抖音登录",
        "url": "https://www.douyin.com/",
        "ready": lambda f: bool(f.get("sessionid") or f.get("sid_tt")),
        "cookie": lambda f: "; ".join("%s=%s" % (k, v) for k, v in f.items() if v),
        "api": "/api/douyin/manual",
    },
}
_inapp_busy = {name: threading.Event() for name in _INAPP_LOGIN}
_inapp_login_seq = [0]
_inapp_login_token = {}


_DOUYIN_LOGIN_SCRIPT = r"""
(function () {
  var nodes = document.querySelectorAll("p, span, div, li");
  for (var i = 0; i < nodes.length; i++) {
    if (((nodes[i].textContent) || "").trim() === "登录" && nodes[i].children.length === 0) {
      nodes[i].click();
      return "clicked";
    }
  }
  return "no_button";
})()
"""
_DOUYIN_QR_TAB_SCRIPT = r"""
(function () {
  var nodes = document.querySelectorAll("p, span, div, li");
  for (var i = 0; i < nodes.length; i++) {
    var t = ((nodes[i].textContent) || "").trim();
    if ((t === "扫码登录" || t === "二维码登录") && nodes[i].children.length === 0) {
      nodes[i].click();
      return "clicked";
    }
  }
  return "no_qr_tab";
})()
"""
_inapp_windows = {}       # source -> (Window, queue.Queue)
_inapp_win_lock = threading.Lock()
_douyin_qr_lock = threading.Lock()
_douyin_qr = {"png": b"", "busy": False, "error": "", "ts": 0.0, "cancel": False}
_douyin_search_lock = threading.Lock()
_douyin_favorites_lock = threading.Lock()

# 搜索接口需要页面 JS 生成 a_bogus/msToken 签名；纯 HTTP 会返回 verify_check。
# 这里在真实页面内挂 XHR/fetch 钩子，由抖音自己的脚本签名，Python 只回收响应 JSON。
_DOUYIN_SEARCH_HOOK = r"""
(function () {
  if (window.__MH_SEARCH_HOOK__) return;
  window.__MH_SEARCH_HOOK__ = true;
  window.__MH_SEARCH__ = [];
  function keep(u, t) {
    try {
      if (/general\/search|search\/single|search\/item/.test(u || "")) {
        window.__MH_SEARCH__.push({ u: u, b: String(t).slice(0, 800000) });
      }
    } catch (e) {}
  }
  try {
    var of = window.fetch;
    window.fetch = function (input, init) {
      return of.apply(this, arguments).then(function (res) {
        try {
          res.clone().text().then(function (t) {
            keep(typeof input === "string" ? input : (input && input.url) || "", t);
          });
        } catch (e) {}
        return res;
      });
    };
  } catch (e) {}
  try {
    var oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.__mh_url = u;
      return oo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      var self = this;
      this.addEventListener("load", function () {
        keep(self.__mh_url, self.responseText);
      });
      return os.apply(this, arguments);
    };
  } catch (e) {}
})()
"""
_DOUYIN_SEARCH_READ = r"""
(function () {
  var arr = window.__MH_SEARCH__ || [];
  return JSON.stringify(arr.slice(-8));
})()
"""

_DOUYIN_FAVORITES_HOOK = r"""
(function () {
  if (window.__MH_FAV_HOOK__) return;
  window.__MH_FAV_HOOK__ = true;
  window.__MH_FAV__ = [];
  function keep(u, t) {
    try {
      u = String(u || ""); t = String(t || "");
      if (!/collect|favorite|aweme\/post|user\/self/.test(u)) return;
      if (!t || t.charAt(0) !== "{") return;
      window.__MH_FAV__.push({ u: u, b: t.slice(0, 1200000) });
      if (window.__MH_FAV__.length > 40) window.__MH_FAV__.shift();
    } catch (e) {}
  }
  try {
    var of = window.fetch;
    window.fetch = function (input, init) {
      return of.apply(this, arguments).then(function (res) {
        try {
          res.clone().text().then(function (t) {
            keep(typeof input === "string" ? input : (input && input.url) || "", t);
          });
        } catch (e) {}
        return res;
      });
    };
  } catch (e) {}
  try {
    var oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.__mh_url = u;
      return oo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      var self = this;
      this.addEventListener("load", function () {
        keep(self.__mh_url, self.responseText);
      });
      return os.apply(this, arguments);
    };
  } catch (e) {}
  function scan() {
    try {
      var nodes = document.querySelectorAll('a[href*="/video/"]');
      var out = [];
      for (var i = 0; i < nodes.length; i++) {
        var m = (nodes[i].getAttribute("href") || "").match(/\/video\/(\d{6,24})/);
        if (m && out.indexOf(m[1]) === -1) out.push(m[1]);
      }
      if (out.length) window.__MH_FAV_IDS__ = out;
    } catch (e) {}
  }
  document.addEventListener("DOMContentLoaded", scan);
  [800, 2000, 4000, 7000].forEach(function (ms) { setTimeout(scan, ms); });
})()
"""
_DOUYIN_FAVORITES_READ = r"""
(function () {
  return JSON.stringify({ responses: window.__MH_FAV__ || [], ids: window.__MH_FAV_IDS__ || [] });
})()
"""

_DOUYIN_FAVORITES_SCROLL = r"""
(function () {
  var sels = [".scroll-y", "[class*=scroll]", "main", "body"];
  for (var i = 0; i < sels.length; i++) {
    var el = document.querySelector(sels[i]);
    if (el && (el.scrollHeight > el.clientHeight + 50 || i === sels.length - 1)) {
      el.scrollTop = el.scrollHeight;
      window.scrollTo(0, document.body.scrollHeight);
      break;
    }
  }
  try {
    var tabs = document.querySelectorAll("div, span, li, a");
    for (var j = 0; j < tabs.length; j++) {
      var t = (tabs[j].textContent || "").trim();
      if ((t === "收藏" || t === "我的收藏") && tabs[j].children.length === 0) {
        tabs[j].click();
        return "tab";
      }
    }
  } catch (e) {}
  return "scroll";
})()
"""


_DOUYIN_SEARCH_TAB_SCRIPT = r"""
(function () {
  var nodes = document.querySelectorAll("div, span, li, a");
  for (var i = 0; i < nodes.length; i++) {
    var t = ((nodes[i].textContent) || "").trim();
    if (t === "视频" && nodes[i].children.length === 0) {
      nodes[i].click();
      return "clicked";
    }
  }
  return "no_tab";
})()
"""

# 登录成功后页面顶栏的"登录"按钮会消失; 用它校验真实登录, 避免把匿名 session 当成登录成功
_DOUYIN_LOGIN_CHECK_JS = r"""
(function () {
  if (document.readyState !== 'complete') { window.__MH_LOGIN_CHECK__ = ''; return 1; }
  var btns = document.querySelectorAll('button');
  var hasLogin = false;
  for (var i = 0; i < btns.length; i++) {
    var t = (btns[i].textContent || '').trim();
    if (t === '登录' || t === '登录/注册') {
      var r = btns[i].getBoundingClientRect();
      if (r.width > 0 && r.height > 0) { hasLogin = true; break; }
    }
  }
  window.__MH_LOGIN_CHECK__ = JSON.stringify({
    logged: !hasLogin, href: location.href
  });
  return 1;
})()
"""


def _douyin_collected_awemes(j, out, seen):
    """递归收集收藏接口响应里的 aweme 对象。"""
    if isinstance(j, dict):
        aid = j.get("aweme_id") or j.get("item_id") or j.get("id")
        music = j.get("music")
        title = j.get("desc") or (music.get("title") if isinstance(music, dict) else "")
        if str(aid or "").isdigit() and (title or j.get("video") or music):
            k = str(aid)
            if k not in seen:
                seen.add(k)
                out.append(j)
        for v in j.values():
            if isinstance(v, (dict, list)):
                _douyin_collected_awemes(v, out, seen)
    elif isinstance(j, list):
        for v in j:
            _douyin_collected_awemes(v, out, seen)


def _douyin_favorites_from_payload(payloads, limit):
    awemes, seen = [], set()
    for p in payloads or []:
        try:
            j = json.loads((p or {}).get("b") or "{}")
        except Exception:
            continue
        _douyin_collected_awemes(j, awemes, seen)
    return server.douyin.songs_from_awemes(awemes, limit)


def _douyin_search_submit_script(keyword):
    """页面内提交搜索词，触发带签名的搜索请求。"""
    return r"""
(function () {
  var input = document.querySelector('input[data-e2e="searchbar-input"]');
  if (!input) return "no_input";
  var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  setter.call(input, %s);
  input.dispatchEvent(new Event("input", {bubbles: true}));
  input.dispatchEvent(new Event("change", {bubbles: true}));
  ["keydown", "keypress", "keyup"].forEach(function (t) {
    input.dispatchEvent(new KeyboardEvent(t, {key: "Enter", code: "Enter",
      keyCode: 13, which: 13, bubbles: true}));
  });
  return "submitted";
})()
""" % json.dumps(keyword)


def _douyin_search_tracks(payloads):
    """从捕获的搜索响应中提取音乐卡片里的曲目(带 mp3 play_url)。"""
    out = []
    for p in payloads or []:
        body = (p or {}).get("b") or ""
        if not body:
            continue
        for line in body.split("\n"):
            s = line.strip()
            if not s.startswith("{"):
                continue
            try:
                j = json.loads(s)
            except Exception:
                continue
            for row in j.get("data") or []:
                if not isinstance(row, dict):
                    continue
                groups = (row.get("music") or {}).get("music_list") or []
                for grp in groups:
                    for track in (grp or {}).get("music") or []:
                        if isinstance(track, dict) and track.get("title"):
                            out.append(track)
    return out


def _douyin_search_items(payloads):
    """从捕获的搜索响应 JSON 中提取 aweme 列表。"""
    out = []
    for p in payloads or []:
        try:
            j = json.loads((p or {}).get("b") or "{}")
        except Exception:
            continue
        rows = j.get("data") or j.get("aweme_list") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            aw = row.get("aweme_info") or row.get("aweme_detail")
            if not isinstance(aw, dict) and row.get("aweme_id"):
                aw = row
            if isinstance(aw, dict) and aw.get("aweme_id"):
                out.append(aw)
    return out


class _InappLoginApi:
    """登录窗口里的最小桥: 页面状态变化推给 Python, 不暴露主窗口功能。"""

    def __init__(self, q):
        self._q = q

    def report_login(self, qr, logged_in):
        try:
            self._q.put({"qr": qr or "", "logged_in": bool(logged_in)})
        except Exception:
            pass


def _inapp_js(source):
    return r"""
(function () {
  if (window.__MH_LOGIN_READY__) return;
  window.__MH_LOGIN_READY__ = true;
  var source = %s;
  var qr = "", logged = false, timer = null;
  var snapshot = function () {
    var out = {qr: "", logged_in: false};
    try {
      var ck = document.cookie || "";
      out.logged_in = ck.indexOf("sessionid=") !== -1 || ck.indexOf("sid_tt=") !== -1;
    } catch (e) {}
    try {
      if (!out.logged_in && window.localStorage && window.localStorage.getItem("HasUserLogin") === "1") {
        out.logged_in = true;
      }
    } catch (e) {}
    var imgs = document.querySelectorAll("#animate_qrcode_container img, img[src^='data:image']");
    for (var i = 0; i < imgs.length; i++) {
      var src = imgs[i].getAttribute("src") || "";
      if (src.indexOf("data:image") === 0 && src.indexOf("base64,") !== -1 && src.length > 100) {
        out.qr = src;
        break;
      }
    }
    return out;
  };
  var send = function (force) {
    var now = snapshot();
    var changed = force || now.qr !== qr || now.logged_in !== logged;
    if (!changed) return;
    qr = now.qr; logged = now.logged_in;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.report_login) {
      try { window.pywebview.api.report_login(qr, logged); } catch (e) {}
    }
  };
  var tick = function () {
    if (source === "douyin" && !qr) {
      try {
        var nodes = document.querySelectorAll("p, span, div, li");
        for (var i = 0; i < nodes.length; i++) {
          var t = ((nodes[i].textContent) || "").trim();
          if (t === "登录" && nodes[i].children.length === 0) { nodes[i].click(); break; }
        }
      } catch (e) {}
    }
    if (source === "douyin" && !qr) {
      try {
        var tabs = document.querySelectorAll("p, span, div, li");
        for (var j = 0; j < tabs.length; j++) {
          var tab = ((tabs[j].textContent) || "").trim();
          if ((tab === "扫码登录" || tab === "二维码登录") && tabs[j].children.length === 0) {
            tabs[j].click(); break;
          }
        }
      } catch (e) {}
    }
    send(false);
  };
  send(true);
  timer = setInterval(tick, 1000);
  document.addEventListener("click", function () { setTimeout(function () { send(false); }, 300); }, true);
})()
""" % json.dumps(source)


class Api:
    """暴露给前端标题栏按钮/拖拽的原生接口."""

    def minimize(self):
        """正常最小化到任务栏."""
        h = _find_hwnd()
        if h:
            _user32.ShowWindow(h, SW_MINIMIZE)

    def close(self):
        """点 X: 缩到任务托盘后台运行, 彻底退出走托盘菜单.

        这里用"最小化 + 移除任务栏按钮"而不是隐藏: 隐藏窗口无法被 Windows
        媒体卡片点击激活(系统只能切前台/还原窗口, 唤不出已隐藏的窗口)。
        """
        h = _find_hwnd()
        if not h:
            return
        if _tray["ready"]:
            _tray_hide_window(h)
        else:
            _user32.ShowWindow(h, SW_MINIMIZE)

    def start_drag(self):
        """触发 Windows 原生标题栏拖拽循环(零延迟跟手), 由系统接管直到松开."""
        h = _find_hwnd()
        if not h:
            return
        _user32.ReleaseCapture()
        _user32.SendMessageW(h, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION

    def reclaim_memory(self, force=False):
        """前端主动请求回收; force=True 时执行与 60 秒定时完全相同的完整清理."""
        if not force:
            return _reclaim_memory(False)
        # WebView2 释放要等 3 秒, 放后台线程, 避免阻塞前端调用
        threading.Thread(target=_reclaim_full, kwargs={"force": True},
                         daemon=True).start()
        return {"ok": True, "async": True}

    def memory_stats(self):
        """返回内存占用: 主进程 + 整棵进程树(含 WebView2 子进程)合计."""
        private_mb, ws_mb = _process_memory_mb()
        tree_private, tree_ws, tree_private_ws, count = _process_tree_memory_mb()
        return {"private_mb": round(private_mb, 1),
                "working_set_mb": round(ws_mb, 1),
                "tree_private_mb": round(tree_private, 1),
                "tree_working_set_mb": round(tree_ws, 1),
                "tree_private_ws_mb": round(tree_private_ws, 1),
                "process_count": count,
                "reclaims": _MEM["count"]}

    def open_downloads(self):
        """在资源管理器中打开下载文件夹."""
        from sources import server
        d = server.DL_DIR
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def update_media(self, info=None, state="none", position=None):
        """前端推送当前播放内容到 Windows 原生系统媒体控件."""
        return smtc.update_media(info, state, position)

    def clear_media(self):
        """清空 Windows 原生系统媒体控件."""
        return smtc.clear_media()

    def media_status(self):
        return smtc.status()

    def open_external(self, url):
        """用系统默认浏览器打开链接(分享用)."""
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "无效链接"}
        webbrowser.open(url)
        return {"ok": True}

    def open_douyin_browser(self, url):
        """用应用内浏览器打开抖音链接, 并注入已保存的 Cookie, 便于验证登录态."""
        url = str(url or "").strip()
        if not url.startswith(("https://www.douyin.com/", "https://www.iesdouyin.com/")):
            return {"ok": False, "error": "仅支持抖音链接"}
        win = webview.create_window(
            "抖音 - MusicHub",
            html=("<html><head><meta charset='utf-8'></head><body style='background:#111;"
                  "color:#bbb;font-family:sans-serif;padding:24px'>正在加载抖音…</body></html>"),
            width=1100,
            height=860,
        )
        if win is None:
            return {"ok": False, "error": "无法创建窗口"}
        if not win.events.shown.wait(15) or not win.events.loaded.wait(15):
            try:
                win.destroy()
            except Exception:
                pass
            return {"ok": False, "error": "窗口启动失败"}
        self._prep_douyin_window(win, "(function(){})()", "抖音浏览器")
        win.load_url(url)
        return {"ok": True}

    def copy_text(self, text):
        """写入系统剪贴板. WebView2 内 navigator.clipboard 在部分环境不可用, 改走 Win32 API."""
        text = str(text or "")
        if not text:
            return {"ok": False, "error": "内容为空"}
        try:
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            kernel32.GlobalAlloc.restype = wt.HGLOBAL
            kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
            kernel32.GlobalLock.restype = wt.LPVOID
            kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
            kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
            kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
            user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
            user32.SetClipboardData.restype = wt.HANDLE
            if not user32.OpenClipboard(None):
                return {"ok": False, "error": "无法打开剪贴板"}
            try:
                user32.EmptyClipboard()
                size = (len(text) + 1) * 2
                h = kernel32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
                if not h:
                    return {"ok": False, "error": "内存分配失败"}
                ptr = kernel32.GlobalLock(h)
                if not ptr:
                    kernel32.GlobalFree(h)
                    return {"ok": False, "error": "锁定内存失败"}
                ctypes.memmove(ptr, text.encode("utf-16-le") + b"\x00\x00", size)
                kernel32.GlobalUnlock(h)
                if not user32.SetClipboardData(13, h):  # CF_UNICODETEXT
                    kernel32.GlobalFree(h)
                    return {"ok": False, "error": "写入剪贴板失败"}
            finally:
                user32.CloseClipboard()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def drag(self, dx, dy):
        """触控拖拽回退: dx/dy 为 CSS 像素增量, 换算物理像素后平移窗口."""
        h = _find_hwnd()
        if not h:
            return
        dpi = _user32.GetDpiForWindow(h) or 96
        scale = dpi / 96.0
        r = wt.RECT()
        _user32.GetWindowRect(h, ctypes.byref(r))
        nx = int(r.left + dx * scale)
        ny = int(r.top + dy * scale)
        _user32.SetWindowPos(h, 0, nx, ny, 0, 0, 0x0001 | 0x0004)  # NOSIZE|NOZORDER

    # ---------- 桌面歌词(可选的原生置顶窗口) ----------

    @staticmethod
    def _desktop():
        return _desktop_lyrics

    def desktop_lyrics_state(self):
        """返回桌面歌词窗口状态；浏览器模式下返回 unavailable。"""
        obj = self._desktop()
        if obj is None:
            return {"available": False, "enabled": False, "locked": False}
        try:
            state = obj.get_state()
            state["available"] = True
            return state
        except Exception as e:
            _log("读取桌面歌词状态失败: %s" % e)
            return {"available": False, "enabled": False, "locked": False}

    def desktop_lyrics_enable(self, enabled):
        """显示/隐藏桌面歌词窗口并返回最新状态。"""
        obj = self._desktop()
        if obj is None:
            return {"available": False, "enabled": False, "locked": False}
        try:
            state = obj.set_enabled(bool(enabled))
            state["available"] = True
            return state
        except Exception as e:
            _log("切换桌面歌词失败: %s" % e)
            return {"available": False, "enabled": False, "locked": False,
                    "error": str(e)}

    def desktop_lyrics_lock(self):
        """切换桌面歌词窗口的鼠标穿透锁定状态。"""
        obj = self._desktop()
        if obj is None:
            return {"available": False, "enabled": False, "locked": False}
        try:
            state = obj.toggle_lock()
            state["available"] = True
            return state
        except Exception as e:
            _log("切换桌面歌词锁定失败: %s" % e)
            return {"available": False, "enabled": False, "locked": False,
                    "error": str(e)}

    def desktop_lyrics_update(self, payload):
        """把当前歌曲、歌词和播放进度交给桌面歌词窗口。"""
        obj = self._desktop()
        if obj is None:
            return {"available": False}
        try:
            obj.update(payload)
            return {"available": True}
        except Exception as e:
            _log("更新桌面歌词失败: %s" % e)
            return {"available": False, "error": str(e)}

    def desktop_lyrics_color(self, value):
        """自定义桌面歌词已唱文字颜色: "#rrggbb"。"""
        obj = self._desktop()
        if obj is None:
            return {"available": False}
        try:
            st = obj.set_color(value)
            st["available"] = True
            return st
        except Exception as e:
            _log("设置桌面歌词颜色失败: %s" % e)
            return {"available": False, "error": str(e)}

    def desktop_lyrics_font(self, family, size):
        """设置桌面歌词字体与字号, 返回最新状态."""
        obj = self._desktop()
        if obj is None:
            return {"available": False}
        try:
            st = obj.set_font(family, size)
            st["available"] = True
            return st
        except Exception as e:
            _log("设置桌面歌词字体失败: %s" % e)
            return {"available": False, "error": str(e)}

    # ---------- 应用内登录(网易云/QQ, 绕开接口风控) ----------

    def netease_login(self):
        """在应用内打开网易云官方登录页，登录完成后自动回收 MUSIC_U。"""
        return self._start_inapp_login("netease")

    def qq_login(self):
        """在应用内打开 QQ 音乐官方页面，登录完成后自动回收 uin+取流密钥。"""
        return self._start_inapp_login("qq")

    def douyin_login(self):
        """在应用内打开抖音官方页面，登录完成后自动回收 sessionid 等 Cookie。"""
        self.douyin_qr_cancel()
        deadline = time.time() + 5
        while _inapp_busy["douyin"].is_set() and time.time() < deadline:
            time.sleep(0.05)
        if _inapp_busy["douyin"].is_set():
            return {"started": False, "message": "登录窗口正在关闭，请重试"}
        return self._start_inapp_login("douyin", True)

    def douyin_qr_login(self):
        """后台打开抖音官方登录页，提取页面二维码并回收扫码登录态。"""
        self.douyin_qr_cancel()
        deadline = time.time() + 5
        while _inapp_busy["douyin"].is_set() and time.time() < deadline:
            time.sleep(0.05)
        if _inapp_busy["douyin"].is_set():
            return {"started": False, "message": "登录窗口正在关闭，请重试"}
        with _douyin_qr_lock:
            _douyin_qr.update({"png": b"", "busy": True, "error": "", "ts": time.time(),
                               "cancel": False})
        result = self._start_inapp_login("douyin", False)
        with _douyin_qr_lock:
            if not result.get("started"):
                _douyin_qr["busy"] = False
                _douyin_qr["error"] = result.get("message") or "无法开始扫码登录"
        return result

    def douyin_qr_status(self):
        try:
            logged_in = bool(server.douyin.status().get("logged_in"))
        except Exception:
            logged_in = False
        with _douyin_qr_lock:
            png = _douyin_qr["png"]
            busy = _douyin_qr["busy"] and not logged_in
            error = "" if logged_in else _douyin_qr["error"]
            ts = _douyin_qr["ts"]
        return {"png": base64.b64encode(png).decode("ascii") if png else "",
                "busy": busy, "error": error, "ts": ts, "logged_in": logged_in}

    def douyin_qr_cancel(self):
        with _douyin_qr_lock:
            _douyin_qr.update({"png": b"", "busy": False, "error": "", "ts": time.time(),
                               "cancel": True})
        with _inapp_win_lock:
            item = _inapp_windows.get("douyin")
        if item:
            self._cancel_inapp_login("douyin")
        return {"ok": True}

    # ---------- 抖音搜索(在真实页面内由官方脚本签名, 绕开 verify_check) ----------

    def douyin_search(self, keyword, limit=30, page=1):
        keyword = (keyword or "").strip()
        limit = max(1, min(int(limit or 30), 50))
        page = max(1, int(page or 1))
        if not keyword:
            return {"songs": [], "page": page, "total": 0, "page_size": limit, "has_more": False}
        if not _douyin_search_lock.acquire(blocking=False):
            return {"songs": [], "page": page, "total": 0, "page_size": limit,
                    "has_more": False, "error": "抖音搜索仍在进行，请稍候"}
        try:
            return self._douyin_search_inner(keyword, limit, page)
        finally:
            _douyin_search_lock.release()

    def _douyin_search_inner(self, keyword, limit, page):
        empty = {"songs": [], "page": page, "total": 0, "page_size": limit, "has_more": False}
        url = "https://www.douyin.com/search/" + urllib.parse.quote(keyword) + "?type=general"
        # 搜索必须由抖音页面脚本签名(a_bogus/msToken); 直接接口会返回 verify_check。
        # 这里后台隐藏打开官方页面并注入已保存 Cookie, 由页面自己完成签名请求。
        win = webview.create_window(
            "抖音搜索",
            html="<html><head><meta charset='utf-8'></head><body></body></html>",
            width=1040,
            height=860,
            hidden=True,
            focus=False,
        )
        if win is None:
            return dict(empty, error="无法创建搜索窗口")
        try:
            if not win.events.shown.wait(15) or not win.events.loaded.wait(15):
                return dict(empty, error="搜索窗口启动失败")
            self._prep_douyin_window(win, _DOUYIN_SEARCH_HOOK, "抖音搜索")
            win.load_url(url)
            deadline = time.time() + 24
            payloads = []
            fired = set()
            while time.time() < deadline:
                time.sleep(1.0)
                try:
                    raw = win.evaluate_js(_DOUYIN_SEARCH_READ)
                    if isinstance(raw, str) and raw.strip():
                        payloads = json.loads(raw)
                except Exception:
                    payloads = []
                if _douyin_search_items(payloads) or _douyin_search_tracks(payloads):
                    break
                elapsed = 24 - int(deadline - time.time())
                if elapsed >= 5 and "tab" not in fired:
                    fired.add("tab")
                    try:
                        win.evaluate_js(_DOUYIN_SEARCH_TAB_SCRIPT)
                    except Exception:
                        pass
                if elapsed >= 9 and "submit" not in fired:
                    fired.add("submit")
                    try:
                        win.evaluate_js(_douyin_search_submit_script(keyword))
                    except Exception:
                        pass
            songs = server.douyin.songs_from_awemes(_douyin_search_items(payloads), limit)
            if len(songs) < limit:
                songs += server.douyin.songs_from_music_tracks(
                    _douyin_search_tracks(payloads), limit - len(songs))
            if not songs:
                return dict(empty, error="抖音搜索被风控拦截，请稍后重试或粘贴分享链接")
            return {"songs": songs, "page": page, "total": len(songs),
                    "page_size": limit, "has_more": len(songs) >= limit, "via": "webview"}
        finally:
            try:
                win.destroy()
            except Exception:
                pass

    # ---------- 抖音账号收藏(官方个人页由页面脚本签名) ----------

    def douyin_favorites(self, limit=60):
        try:
            if not server.douyin.status().get("logged_in"):
                return {"songs": [], "error": "请先在应用内登录抖音"}
        except Exception:
            return {"songs": [], "error": "无法读取抖音登录态"}
        limit = max(1, min(int(limit or 60), 100))
        if not _douyin_favorites_lock.acquire(blocking=False):
            return {"songs": [], "error": "抖音收藏仍加载中，请稍候"}
        try:
            return self._douyin_favorites_inner(limit)
        finally:
            _douyin_favorites_lock.release()

    def _douyin_favorites_inner(self, limit):
        win = webview.create_window(
            "抖音收藏",
            html="<html><head><meta charset='utf-8'></head><body></body></html>",
            width=1100,
            height=900,
            hidden=True,
            focus=False,
        )
        if win is None:
            return {"songs": [], "error": "无法创建抖音收藏窗口"}
        try:
            if not win.events.shown.wait(15) or not win.events.loaded.wait(15):
                return {"songs": [], "error": "抖音收藏窗口启动失败"}
            if not self._prep_douyin_window(win, _DOUYIN_FAVORITES_HOOK, "抖音收藏"):
                return {"songs": [], "error": "抖音收藏页初始化失败"}
            win.load_url("https://www.douyin.com/user/self?showTab=collect")
            payloads, ids = [], []
            deadline = time.time() + 24
            scrolled = False
            while time.time() < deadline:
                time.sleep(1.0)
                try:
                    raw = win.evaluate_js(_DOUYIN_FAVORITES_READ)
                    j = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
                    payloads = j.get("responses") or []
                    ids = j.get("ids") or []
                except Exception:
                    payloads, ids = [], []
                songs = _douyin_favorites_from_payload(payloads, limit)
                if len(songs) >= limit:
                    return {"songs": songs[:limit], "via": "webview"}
                if not scrolled and time.time() > deadline - 16:
                    scrolled = True
                    try:
                        win.evaluate_js(_DOUYIN_FAVORITES_SCROLL)
                    except Exception:
                        pass
            songs = _douyin_favorites_from_payload(payloads, limit)
            if len(songs) < limit:
                for aid in ids:
                    if len(songs) >= limit:
                        break
                    k = str(aid or "")
                    if not k.isdigit() or any(str(x.get("id")) == k for x in songs):
                        continue
                    detail = server.douyin._fetch_detail(k)
                    s = server.douyin._aweme_to_song(detail)
                    if s:
                        songs.append(s)
            if songs:
                return {"songs": songs[:limit], "via": "webview"}
            return {"songs": [], "error": "未读取到抖音账号收藏；请确认已登录且收藏列表可见"}
        finally:
            try:
                win.destroy()
            except Exception:
                pass

    @staticmethod
    def _prep_douyin_window(win, hook_script, tag):
        """给隐藏窗口注入已保存 Cookie, 并在文档创建时安装响应钩子。"""
        try:
            import clr  # noqa: F401
            from System import Func, Type
            form = win.native
            cookies = server.douyin.login_cookies()

            def _prep():
                cm = form.webview.CoreWebView2.CookieManager
                for name, value in cookies.items():
                    try:
                        c = cm.CreateCookie(str(name), str(value), ".douyin.com", "/")
                        c.IsSecure = True
                        c.IsHttpOnly = name in ("sessionid", "sessionid_ss", "sid_tt",
                                                "uid_tt_ss", "passport_csrf_token")
                        cm.AddOrUpdateCookie(c)
                    except Exception:
                        pass
                try:
                    form.webview.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(hook_script)
                except Exception as e:
                    _log("%s: 安装页面钩子失败: %s" % (tag, e))

            form.Invoke(Func[Type](_prep))
            time.sleep(0.3)
            return True
        except Exception as e:
            _log("%s: 初始化页面钩子/Cookie 失败: %s" % (tag, e))
            return False

    @staticmethod
    def _cancel_inapp_login(source):
        with _inapp_win_lock:
            item = _inapp_windows.get(source)
        if not item:
            return False
        win, _ = item
        try:
            win.destroy()
        except Exception as e:
            _log("%s: 关闭登录窗口失败: %s" % (_INAPP_LOGIN[source]["title"], e))
        return True

    @staticmethod
    def _update_douyin_qr(data):
        if not data or not isinstance(data, dict):
            return
        src = data.get("qr") or ""
        png = b""
        if src and "base64," in src:
            try:
                png = base64.b64decode(src.split("base64,", 1)[1], validate=True)
                if not png.startswith(b"\x89PNG"):
                    png = b""
            except Exception:
                png = b""
        with _douyin_qr_lock:
            if png:
                _douyin_qr["png"] = png
                _douyin_qr["error"] = ""
            if data.get("logged_in"):
                _douyin_qr["busy"] = False
                _douyin_qr["error"] = ""
            elif not png and not _douyin_qr["png"] and not _douyin_qr["error"]:
                _douyin_qr["error"] = "正在拉取抖音官方登录二维码…"
            _douyin_qr["ts"] = time.time()

    def _start_inapp_login(self, source, visible=True):
        with _inapp_win_lock:
            if _inapp_busy[source].is_set():
                return {"started": False, "message": "登录窗口已经打开了"}
            _inapp_busy[source].set()
            token = _inapp_login_seq[0] + 1
            _inapp_login_seq[0] = token
            _inapp_login_token[source] = token
        threading.Thread(target=self._inapp_login_flow,
                         args=(source, bool(visible), token), daemon=True).start()
        return {"started": True, "visible": bool(visible)}

    def _inapp_login_flow(self, source, visible, token):
        try:
            self._inapp_login_flow_inner(source, visible, token)
        finally:
            with _inapp_win_lock:
                if _inapp_login_token.get(source) == token:
                    _inapp_busy[source].clear()
            if source == "douyin" and not visible:
                with _douyin_qr_lock:
                    _douyin_qr["busy"] = False

    @staticmethod
    def _create_inapp_window(source, visible):
        cfg = _INAPP_LOGIN[source]
        q = queue.Queue()
        win = webview.create_window(
            cfg["title"],
            html="<html><head><meta charset='utf-8'></head><body>登录窗口准备中…</body></html>",
            width=520,
            height=760,
            resizable=True,
            hidden=not visible,
            on_top=True,
            focus=visible,
            js_api=_InappLoginApi(q),
        )
        if win is None:
            raise RuntimeError("create_window 返回 None")
        with _inapp_win_lock:
            _inapp_windows[source] = (win, q)
        return win, q

    @staticmethod
    def _on_inapp_loaded(source, win, visible):
        def _fire():
            try:
                win.evaluate_js(_inapp_js(source))
                if source == "douyin":
                    win.evaluate_js(_DOUYIN_LOGIN_SCRIPT)
                    if not visible:
                        win.evaluate_js(_DOUYIN_QR_TAB_SCRIPT)
            except Exception as e:
                _log("%s: 注入登录页监控失败: %s" % (_INAPP_LOGIN[source]["title"], e))
        threading.Thread(target=_fire, daemon=True).start()

    def _inapp_login_flow_inner(self, source, visible, token):
        cfg = _INAPP_LOGIN[source]
        try:
            win, q = self._create_inapp_window(source, visible)
        except Exception as e:
            _log("%s窗口创建失败: %s" % (cfg["title"], e))
            return
        win.events.loaded += lambda: self._on_inapp_loaded(source, win, visible)
        if not win.events.shown.wait(20) or not win.events.loaded.wait(20):
            _log("%s: 登录窗口启动失败" % cfg["title"])
            return

        # 所有 WebView2 窗口共享用户数据目录，clear_cookies() 会连带清掉
        # 其它音源和已写入的抖音登录态；这里不再全局清空。
        try:
            win.load_url(cfg["url"])
        except Exception as e:
            _log("%s: 打开登录页失败: %s" % (cfg["title"], e))
            return

        deadline = time.time() + 300  # 最多等 5 分钟
        started = time.time()
        closed = threading.Event()
        cancelled = threading.Event()

        def _on_closed():
            closed.set()
            cancelled.set()

        win.events.closed += _on_closed
        seen_qr = False
        try:
            while time.time() < deadline and not closed.is_set():
                with _inapp_win_lock:
                    active = _inapp_login_token.get(source) == token
                if not active:
                    cancelled.set()
                    break
                if source == "douyin" and not visible:
                    with _douyin_qr_lock:
                        if _douyin_qr.get("cancel"):
                            cancelled.set()
                            break
                try:
                    data = q.get(timeout=1.0)
                except queue.Empty:
                    data = None
                    elapsed = time.time() - started
                    if source == "douyin" and elapsed > 25:
                        with _douyin_qr_lock:
                            if not _douyin_qr["png"]:
                                _douyin_qr["error"] = "未取到官方二维码，请改用应用内登录或粘贴 Cookie"
                if source == "douyin":
                    self._update_douyin_qr(data)
                    if data and data.get("qr"):
                        seen_qr = True
                try:
                    cookies = win.get_cookies() or []
                except Exception as e:
                    _log("%s: 读取 Cookie 失败(重试中): %s" % (cfg["title"], e))
                    continue  # 窗口还没就绪或已被关闭
                found = {}
                for c in cookies:
                    # pywebview 返回 list[SimpleCookie]: 每项是 {cookie名: Morsel} 的 dict 子类,
                    # 不是 {"name":..,"value":..} 结构, 必须按 items() 遍历取 Morsel.value
                    if hasattr(c, "items"):
                        for name, morsel in c.items():
                            value = getattr(morsel, "value", morsel)
                            if name and value:
                                found[name] = str(value)
                    else:
                        name = getattr(c, "name", None)
                        value = getattr(c, "value", None)
                        if name and value:
                            found[name] = value
                if cfg["ready"](found):
                    # WebView2 里可能残留旧的 sessionid: 必须用页面请求验证真的登录成功
                    if source == "douyin" and not self._douyin_login_verified(win):
                        continue
                    if source == "douyin":
                        try:
                            ms = win.evaluate_js(
                                "(localStorage.getItem('xmst') || localStorage.getItem('msToken') || '')")
                            if isinstance(ms, str) and ms.strip():
                                found["msToken"] = ms.strip().strip('"')
                        except Exception:
                            pass
                        _log("抖音登录 Cookie 名称: %s" % ",".join(sorted(found.keys())))
                    self._store_inapp_cookie(source, found)
                    _log("%s登录成功，登录态已写入" % cfg["title"])
                    break
            else:
                _log("%s窗口已关闭或超时，未取到登录 Cookie" % cfg["title"])
        finally:
            with _inapp_win_lock:
                item = _inapp_windows.get(source)
                if item and item[0] is win:
                    _inapp_windows.pop(source, None)
            if cancelled.is_set() or not visible or source != "douyin" or seen_qr:
                try:
                    win.destroy()
                except Exception:
                    pass
            elif not closed.is_set():
                _log("%s: 未取到官方二维码，保留隐藏窗口以便刷新重试" % cfg["title"])

    @staticmethod
    def _douyin_login_verified(win):
        """页面顶栏不再有可见的"登录"按钮时, 才认为真的登录成功."""
        try:
            win.evaluate_js(_DOUYIN_LOGIN_CHECK_JS)
        except Exception:
            return False
        for _ in range(8):
            time.sleep(0.6)
            try:
                raw = win.evaluate_js("window.__MH_LOGIN_CHECK__ || ''")
            except Exception:
                return False
            if not raw:
                continue
            try:
                j = json.loads(raw)
            except Exception:
                return False
            return bool(j.get("logged"))
        return False

    @staticmethod
    def _store_inapp_cookie(source, found):
        """把 WebView2 里的登录态交给后端，复用已有的校验与缓存重置逻辑。"""
        cfg = _INAPP_LOGIN[source]
        cookie = cfg["cookie"](found)
        body = json.dumps({"cookie": cookie}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (PORT, cfg["api"]),
            data=body,
            headers={"Content-Type": "application/json", "X-MH-Token": server.TOKEN},
        )
        try:
            with _loopback_opener().open(req, timeout=10) as resp:
                _log("%s登录态写入: HTTP %s %s" % (
                    cfg["title"], resp.status, resp.read().decode("utf-8", "replace")[:200]))
        except Exception as e:
            _log("%s登录态写入失败: %s" % (cfg["title"], e))


def _log(message):
    try:
        print(message, flush=True)
    except Exception:
        pass
    try:  # pythonw 无控制台, 同步写一份到日志文件便于排查
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), message)
        with open(os.path.join(server.DATA_DIR, "musichub.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------- 主动内存回收 ----------------
_MEM = {"private_mb": 0.0, "last_reclaim": 0.0, "count": 0}


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _process_memory_mb():
    """返回当前进程私有提交内存(MB)与工作集(MB)."""
    try:
        psapi = ctypes.windll.psapi
        kernel32 = ctypes.windll.kernel32
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        c = _PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                          ctypes.byref(c), c.cb):
            return 0.0, 0.0
        return c.PrivateUsage / 1048576.0, c.WorkingSetSize / 1048576.0
    except Exception:
        return 0.0, 0.0


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),  # ULONG_PTR
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def _process_tree_pids(root_pid=None):
    """枚举当前进程及其所有子孙进程 PID(WebView2 的 browser/GPU/渲染进程都是子进程)."""
    root_pid = root_pid or os.getpid()
    try:
        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002
        kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wt.BOOL
        kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wt.BOOL
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wt.HANDLE(-1).value:
            return [root_pid]
        children = {}
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        try:
            ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                children.setdefault(int(e.th32ParentProcessID), []).append(
                    int(e.th32ProcessID))
                ok = kernel32.Process32NextW(snap, ctypes.byref(e))
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return [root_pid]
    out, stack = [], [root_pid]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


class _WORKING_SET_EX_INFO(ctypes.Structure):
    _fields_ = [("VirtualAddress", ctypes.c_void_p),
                ("Attributes", ctypes.c_size_t)]


def _pid_memory_mb(pid):
    """返回指定进程 (私有提交MB, 工作集MB)."""
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not h:
            return 0.0, 0.0
        try:
            c = _PROCESS_MEMORY_COUNTERS_EX()
            c.cb = ctypes.sizeof(c)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return 0.0, 0.0
            return c.PrivateUsage / 1048576.0, c.WorkingSetSize / 1048576.0
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return 0.0, 0.0


def _pid_private_ws_mb(pid):
    """返回进程私有工作集(MB): 排除与其他进程共享的 DLL/共享内存页.

    任务管理器「内存」列就是这个口径(active private working set)。直接用
    WorkingSetSize 会把各进程共享的页重复累加, 合计明显偏高。
    做法: QueryWorkingSet 取工作集页地址 -> QueryWorkingSetEx 逐页看 Shared 位。
    """
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        psapi.QueryWorkingSet.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
        psapi.QueryWorkingSet.restype = wt.BOOL
        psapi.QueryWorkingSetEx.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
        psapi.QueryWorkingSetEx.restype = wt.BOOL
        h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not h:
            return 0.0
        try:
            c = _PROCESS_MEMORY_COUNTERS_EX()
            c.cb = ctypes.sizeof(c)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return 0.0
            pages = int(c.WorkingSetSize // 4096) + 1024
            if pages <= 0 or pages > 4 * 1024 * 1024:  # 上限 16GB 工作集
                return 0.0
            size = pages * ctypes.sizeof(ctypes.c_size_t)
            buf = ctypes.create_string_buffer(size)
            if not psapi.QueryWorkingSet(h, buf, size):
                return 0.0
            addrs = (ctypes.c_size_t * pages).from_buffer(buf)
            info = (_WORKING_SET_EX_INFO * pages)()
            for i in range(pages):
                a = addrs[i]
                info[i].VirtualAddress = a & ~0xFFF if a else 0
            if not psapi.QueryWorkingSetEx(h, ctypes.byref(info),
                                           ctypes.sizeof(info)):
                return 0.0
            private_pages = 0
            for i in range(pages):
                if not info[i].VirtualAddress:
                    continue
                v = info[i].Attributes
                # bit0=Valid, bit15=Shared
                if (v & 1) and not ((v >> 15) & 1):
                    private_pages += 1
            return private_pages * 4096 / 1048576.0
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return 0.0


def _process_tree_memory_mb():
    """返回整棵进程树 (私有提交MB, 工作集MB, 私有工作集MB, 进程数)."""
    pids = _process_tree_pids()
    total_private = 0.0
    total_ws = 0.0
    total_private_ws = 0.0
    for pid in pids:
        priv, ws = _pid_memory_mb(pid)
        total_private += priv
        total_ws += ws
        total_private_ws += _pid_private_ws_mb(pid)
    return total_private, total_ws, total_private_ws, len(pids)


def _reclaim_memory(force=False):
    """主动内存回收: GC + 释放工作集.

    只回收 Python 侧(循环引用/临时大对象); WebView2 的 V8 堆由前端
    window.gc() 配合 --js-flags=--expose-gc 回收。
    force=True 时才 EmptyWorkingSet(会短暂把页面换出, 恢复时略卡),
    温和回收只做 gc, 避免频繁抖动。
    """
    import gc
    before_private, before_ws = _process_memory_mb()
    try:
        gc.collect()
    except Exception:
        pass
    if force:
        try:
            # EmptyWorkingSet 把物理页交还系统, 降低任务管理器可见占用
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            psapi.EmptyWorkingSet.argtypes = [wt.HANDLE]
            psapi.EmptyWorkingSet.restype = wt.BOOL
            psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
        except Exception:
            pass
    after_private, after_ws = _process_memory_mb()
    _MEM["private_mb"] = after_private
    _MEM["last_reclaim"] = time.time()
    _MEM["count"] += 1
    if before_ws - after_ws > 10:
        _log("内存回收: 工作集 %.0fMB -> %.0fMB, 私有 %.0fMB"
             % (before_ws, after_ws, after_private))
    return {"ok": True, "private_mb": round(after_private, 1),
            "working_set_mb": round(after_ws, 1),
            "released_mb": round(max(0.0, before_ws - after_ws), 1)}


def _reclaim_webview_memory():
    """释放 WebView2 渲染/GPU 进程内存.

    WebView2 没有公开的"清缓存"接口, 但提供两条官方通道:
      1) MemoryUsageTargetLevel=Low: 让 Chromium 切到低内存模式;
      2) CDP Memory.simulatePressureNotification / HeapProfiler.collectGarbage:
         主动触发内存压力回收与 JS 堆回收。
    3 秒后恢复 Normal, 避免长期低内存模式影响视频解码与滚动流畅度。
    """
    wins = list(getattr(webview, "windows", []) or [])
    if not wins:
        _MEM["wv2_last"] = "no-window"
        return False
    # pywebview 的实际控件保存在 winforms.BrowserView.instances[uid] (EdgeChrome)
    ctrl = None
    try:
        from webview.platforms.winforms import BrowserView
        uid = getattr(wins[0], "uid", None)
        browser = BrowserView.instances.get(uid) if uid is not None else None
        if browser is None and BrowserView.instances:
            browser = next(iter(BrowserView.instances.values()))
        ctrl = getattr(browser, "webview", None)
    except Exception as e:
        _MEM["wv2_last"] = "import-fail:%s" % e
        return False
    if ctrl is None:
        _MEM["wv2_last"] = "no-webview"
        return False

    # CoreWebView2 只能在 UI 线程访问: 所有读写都放进 Invoke 的闭包里
    def _low():
        notes = []
        try:
            core = ctrl.CoreWebView2
        except Exception as e:
            _MEM["wv2_last"] = "core-fail:%s" % e
            return
        if core is None:
            _MEM["wv2_last"] = "core-null"
            return
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Lv
            core.MemoryUsageTargetLevel = Lv.Low
            notes.append("level=low")
        except Exception as e:
            notes.append("level-fail:%s" % e)
        for method, params in (
            ("HeapProfiler.collectGarbage", "{}"),
            ("Memory.simulatePressureNotification", '{"level":"critical"}'),
        ):
            try:
                core.CallDevToolsProtocolMethodAsync(method, params)
                notes.append(method.split(".")[-1] + "=sent")
            except Exception as e:
                notes.append(method.split(".")[-1] + "-fail:%s" % e)
        _MEM["wv2_last"] = ",".join(notes)

    def _normal():
        try:
            core = ctrl.CoreWebView2
            if core is None:
                return
            from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Lv
            core.MemoryUsageTargetLevel = Lv.Normal
        except Exception:
            pass

    try:
        from System import Action
        # CoreWebView2 必须在其 UI 线程访问, 通过 WinForms 控件 Invoke 切过去
        ctrl.Invoke(Action(_low))
        time.sleep(3)
        ctrl.Invoke(Action(_normal))
        return True
    except Exception as e:
        _MEM["wv2_last"] = "invoke-fail:%s" % e
        return False


_RECLAIM_LOCK = threading.Lock()


def _reclaim_full(force=True):
    """完整清理: 主进程 gc + 释放工作集 + WebView2 子进程释放.

    切换/打开播放器触发的前端回收与 60 秒定时回收走同一套逻辑, 保证效果一致。
    非阻塞: 上一次还没结束直接跳过, 避免连续切歌时堆积。
    """
    if not _RECLAIM_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True}
    try:
        result = _reclaim_memory(bool(force))
        result["webview_ok"] = _reclaim_webview_memory()
        return result
    finally:
        _RECLAIM_LOCK.release()


def _start_memory_reclaimer(interval=60):
    """后台主动释放: 每 60 秒回收 Python 与 WebView2 两侧内存."""
    def loop():
        first = True
        while True:
            try:
                time.sleep(interval)
                result = _reclaim_full()
                # 首次执行后记录一次结果(成功/失败), 便于确认链路是否通
                if first:
                    first = False
                    _log("WebView2 内存释放: %s %s"
                         % (result.get("webview_ok"), _MEM.get("wv2_last", "")))
            except Exception as e:
                if first:
                    first = False
                    _log("内存回收循环异常: %r" % e)
                continue

    threading.Thread(target=loop, daemon=True).start()


def _writable_storage_path():
    """选择可写的 WebView2 用户数据目录，优先使用用户 LocalAppData。"""
    candidates = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "MusicHub", "WebView2"))
    candidates.append(os.path.join(server.DATA_DIR, "webview-profile"))

    errors = []
    for path in candidates:
        probe = None
        try:
            os.makedirs(path, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=path)
            os.close(fd)
            os.remove(probe)
            _log("WebView2 storage: " + path)
            return path
        except Exception as e:
            errors.append("%s: %s" % (path, e))
            if probe:
                try:
                    os.remove(probe)
                except OSError:
                    pass
    raise OSError("没有可写的 WebView2 用户数据目录: " + " | ".join(errors))


def _loopback_opener():
    """访问 127.0.0.1 用的 opener: 显式禁用代理。

    urllib 的默认 opener 会读取 HTTP_PROXY/HTTPS_PROXY。一旦终端里带了代理
    变量，连本机回环请求也会被转发出去而失败（表现为首页自检 502、写入登录态
    失败）。这里固定走直连。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _check_local_page():
    """在启动 WebView 前验证本进程能通过回环读取本地首页。"""
    with _loopback_opener().open(URL, timeout=3) as response:
        sample = response.read(512).decode("utf-8", "replace")
        if response.status != 200 or "<html" not in sample.lower():
            raise RuntimeError("首页探测返回异常 (HTTP %s)" % response.status)
        _log("Local page self-check: HTTP %s, %s" % (response.status, URL))


_LOAD_HINT = (
    "排查方向: (1) 进程是否被沙箱/Job Object 限制, 导致 WebView2 子进程连不上 127.0.0.1; "
    "(2) WebView2 Runtime 是否安装(系统级或随包分发); "
    "(3) 用户数据目录是否可写; "
    "(4) 用 --debug 启动看 DevTools 的加载错误码。"
)


def _acquire_single_instance():
    """单实例: 命名互斥体标记首实例, 二次启动只唤醒已有窗口后退出.

    点击 Windows 媒体卡片/开始菜单会按 RelaunchCommand 再次启动本程序;
    没有单实例保护时会开出第二个后台实例, 而隐藏在托盘的主窗口不会被唤起。
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.GetLastError.restype = wt.DWORD
    h = kernel32.CreateMutexW(None, False, "Local\\MusicHubSingleInstance")
    if h and kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(h)
        return False
    return True


def _activate_existing_instance():
    """唤醒已在托盘/最小化的主窗口, 供媒体卡片/开始菜单二次启动调用.

    二次启动是独立进程, 不能用 _find_hwnd()(它只认本进程 PID);
    跨进程找托盘辅助窗口(类名 MusicHubTrayWindow), 发 WM_SHOW_APP 让它自己恢复主窗口。
    """
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowW.restype = wt.HWND
    tray_hwnd = user32.FindWindowW(_TRAY_CLASS, None)
    if tray_hwnd:
        _user32.PostMessageW(tray_hwnd, WM_SHOW_APP, 0, 0)
        _log("唤醒首实例托盘窗口: hwnd=%s" % tray_hwnd)
        return
    # 托盘窗口未就绪(启动早期), 退而求其次按标题找主窗口(可能属其他进程)
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    h = user32.FindWindowW(None, TITLE)
    if h:
        _user32.ShowWindow(h, SW_RESTORE)
        _user32.ShowWindow(h, SW_SHOW)
        _user32.SetForegroundWindow(h)
        _log("跨进程唤醒主窗口: hwnd=%s" % h)
        return
    _log("激活失败: 未找到 MusicHub 托盘窗口或主窗口")


def main():
    if not _acquire_single_instance():
        # 二次启动(点媒体卡片/开始菜单): 唤醒已有主窗口, 不再开新实例
        _log("检测到已有 MusicHub 实例, 唤醒主窗口")
        _activate_existing_instance()
        return
    if sys.stdout is None or sys.stderr is None:
        sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = sink
        if sys.stderr is None:
            sys.stderr = sink

    headless = "--headless" in sys.argv or "--no-window" in sys.argv
    server.serve(PORT)
    _log("MusicHub server on " + URL)
    _start_memory_reclaimer()

    if headless:
        # 无界面模式: 仅起服务, 便于 API 测试
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            return

    global _desktop_lyrics
    if DesktopLyrics is not None:
        try:
            lyrics_path = os.path.join(server.DATA_DIR, "desktop_lyrics.json")
            _desktop_lyrics = DesktopLyrics(lyrics_path)
            if _desktop_lyrics.get_state().get("enabled"):
                # The constructor only reads preferences. Recreate a previously
                # enabled window once the desktop client is about to start.
                _desktop_lyrics.set_enabled(True)
        except Exception as e:
            _desktop_lyrics = None
            _log("桌面歌词初始化失败，已禁用: %s" % e)

    try:
        _check_local_page()
    except Exception as e:
        _log("本地页面自检失败(%s)，打开系统浏览器: %s" % (e, URL))
        _shutdown_desktop_lyrics()
        try:
            webbrowser.open(URL)
        except Exception as open_error:
            _log("系统浏览器打开失败: %s" % open_error)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            return

    try:
        _log("WebView2 媒体会话参数: " + smtc.configure_browser_flags())
        window = webview.create_window(
            TITLE,
            URL,
            frameless=True,
            resizable=False,
            easy_drag=False,
            hidden=False,
            background_color="#12121a",
            js_api=Api(),
        )
        _main_window["win"] = window

        page_loaded = threading.Event()

        def _on_loaded():
            page_loaded.set()
            _log("WebView 页面加载完成: " + URL)

            # frameless 下用 Win32 铺满工作区(避开任务栏), 不依赖 maximized
            def _fit():
                import time
                for _ in range(20):
                    time.sleep(0.3)
                    h = _find_hwnd()
                    if not h:
                        continue
                    x, y, w, hh = _work_area()
                    _user32.SetWindowPos(h, 0, x, y, w, hh, 0x0004)  # NOZORDER
                    if not smtc.status().get("started"):
                        ok = smtc.init_window(h, window)
                        _log("原生 SMTC 初始化: %s %s" % (ok, smtc.status()))
                    try:
                        smtc.set_window_icon(h, _load_app_icon())
                    except Exception:
                        pass
                    r = wt.RECT()
                    _user32.GetWindowRect(h, ctypes.byref(r))
                    if (r.right - r.left) >= w - 4:
                        break

            threading.Thread(target=_fit, daemon=True).start()

        window.events.loaded += _on_loaded
        _setup_tray()
        storage_path = _writable_storage_path()

        def _fallback_if_not_loaded():
            # WebView2 冷启动(首次创建用户数据目录)可能要 4-8 秒, 阈值放宽到 20 秒,
            # 避免把"启动慢"误判成"加载失败"而多弹一个浏览器窗口.
            if not page_loaded.wait(20):
                _log("WebView 页面 20 秒内未触发 loaded，改用系统浏览器: " + URL)
                _log(_LOAD_HINT)
                try:
                    webbrowser.open(URL)
                except Exception as e:
                    _log("系统浏览器打开失败: %s" % e)

        threading.Thread(target=_fallback_if_not_loaded, daemon=True).start()
        try:
            webview.start(
                gui="edgechromium",
                debug="--debug" in sys.argv,
                storage_path=storage_path,
            )
        finally:
            _shutdown_desktop_lyrics()
    except Exception as e:
        _log("桌面窗口启动失败(%s)，回退到浏览器模式" % e)
        _shutdown_desktop_lyrics()
        try:
            webbrowser.open(URL)
        except Exception as open_error:
            _log("系统浏览器打开失败: %s" % open_error)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
