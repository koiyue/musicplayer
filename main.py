# -*- coding: utf-8 -*-
"""MusicHub 入口: 起本地服务 + pywebview(Edge WebView2) 窗口.

窗口约定: 无边框(frameless) + 前端自绘米哈游风格标题栏; 铺满工作区, 不可调整大小.
WebView2 原生继承 Edge 的 Windows 触控支持(惯性滚动、Pointer Events).
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser


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

import server

try:
    from desktop_lyrics import DesktopLyrics
except Exception as _desktop_lyrics_import_error:
    # The browser fallback and headless API mode must remain usable even when
    # the optional native desktop-lyrics window cannot be imported.
    DesktopLyrics = None

PORT = server.free_port(8787)
URL = "http://127.0.0.1:%d/" % PORT
TITLE = "MusicHub"

_user32 = ctypes.windll.user32
_hwnd_cache = {}
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
        return _hwnd_cache["h"]

    found = []
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
                if buf.value.startswith(TITLE):
                    found.append(hwnd)
        return True

    _user32.EnumWindows(EnumProc(cb), 0)
    if found:
        _hwnd_cache["h"] = found[0]
    return _hwnd_cache.get("h")


def _work_area():
    rect = wt.RECT()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


# ---------------- 任务托盘 ----------------
WM_TRAY = 0x8001
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


def _tray_show():
    h = _find_hwnd()
    if not h:
        return
    _user32.ShowWindow(h, SW_RESTORE)
    _user32.ShowWindow(h, SW_SHOW)
    _user32.SetForegroundWindow(h)


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
}
_inapp_busy = {name: threading.Event() for name in _INAPP_LOGIN}


class Api:
    """暴露给前端标题栏按钮/拖拽的原生接口."""

    def minimize(self):
        """正常最小化到任务栏."""
        h = _find_hwnd()
        if h:
            _user32.ShowWindow(h, SW_MINIMIZE)

    def close(self):
        """点 X: 缩到任务托盘后台运行, 彻底退出走托盘菜单."""
        h = _find_hwnd()
        if not h:
            return
        if _tray["ready"]:
            _user32.ShowWindow(h, SW_HIDE)
        else:
            _user32.ShowWindow(h, SW_MINIMIZE)

    def start_drag(self):
        """触发 Windows 原生标题栏拖拽循环(零延迟跟手), 由系统接管直到松开."""
        h = _find_hwnd()
        if not h:
            return
        _user32.ReleaseCapture()
        _user32.SendMessageW(h, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION

    def open_downloads(self):
        """在资源管理器中打开下载文件夹."""
        import server
        d = server.DL_DIR
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

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

    def _start_inapp_login(self, source):
        if _inapp_busy[source].is_set():
            return {"started": False, "message": "登录窗口已经打开了"}
        _inapp_busy[source].set()
        threading.Thread(target=self._inapp_login_flow, args=(source,), daemon=True).start()
        return {"started": True}

    def _inapp_login_flow(self, source):
        try:
            self._inapp_login_flow_inner(source)
        finally:
            _inapp_busy[source].clear()

    def _inapp_login_flow_inner(self, source):
        cfg = _INAPP_LOGIN[source]
        try:
            # 先开空白页: 便于在加载登录页之前清空历史 Cookie
            win = webview.create_window(
                cfg["title"],
                "about:blank",
                width=520,
                height=760,
                resizable=True,
                on_top=True,
            )
        except Exception as e:
            _log("%s窗口创建失败: %s" % (cfg["title"], e))
            return
        if win is None:
            _log("%s窗口创建失败: create_window 返回 None" % cfg["title"])
            return

        # WebView2 用户数据目录是持久的, 上次登录的 Cookie 还在的话,
        # 登录页打开即已登录, 回收循环会立刻判定成功并关窗, 导致无法重新登录/换号。
        # 因此先清空历史 Cookie(仅影响内嵌浏览器会话, 不影响已保存到文件的登录态),
        # 再加载官方登录页, 保证每次都是以未登录状态进入。
        try:
            win.clear_cookies()
        except Exception as e:
            _log("%s: 清空历史 Cookie 失败: %s" % (cfg["title"], e))
        try:
            win.load_url(cfg["url"])
        except Exception as e:
            _log("%s: 打开登录页失败: %s" % (cfg["title"], e))

        deadline = time.time() + 300  # 最多等 5 分钟
        closed = threading.Event()

        def _on_closed():
            closed.set()

        win.events.closed += _on_closed
        try:
            while time.time() < deadline and not closed.is_set():
                time.sleep(1.0)
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
                    self._store_inapp_cookie(source, found)
                    break
            else:
                _log("%s窗口已关闭或超时，未取到登录 Cookie" % cfg["title"])
        finally:
            try:
                win.destroy()
            except Exception:
                pass

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


def main():
    if sys.stdout is None or sys.stderr is None:
        sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = sink
        if sys.stderr is None:
            sys.stderr = sink

    headless = "--headless" in sys.argv or "--no-window" in sys.argv
    server.serve(PORT)
    _log("MusicHub server on " + URL)

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
