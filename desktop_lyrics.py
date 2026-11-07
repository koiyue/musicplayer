# -*- coding: utf-8 -*-
"""MusicHub 桌面歌词窗口（Windows 原生、无外部依赖）。

该模块只负责一个小型置顶歌词窗，不依赖 pywebview。构造时只读取偏好，
真正的窗口在 set_enabled(True) 时由专属消息线程延迟创建；因此后台启动不会
抢焦点，也不会改变主窗口的生命周期。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    import numpy as _np
except Exception:
    _np = None

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    from ctypes import wintypes as wt
    # ctypes.wintypes omits HCURSOR on some Python builds.
    if not hasattr(wt, "HCURSOR"):
        wt.HCURSOR = wt.HANDLE

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32

    HWND = wt.HWND
    HDC = wt.HDC
    HINSTANCE = wt.HINSTANCE
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, HWND, wt.UINT,
                                 ctypes.c_size_t, ctypes.c_ssize_t)

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                    ("right", wt.LONG), ("bottom", wt.LONG)]

    class _PAINTSTRUCT(ctypes.Structure):
        _fields_ = [("hdc", HDC), ("fErase", wt.BOOL), ("rcPaint", _RECT),
                    ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                    ("rgbReserved", wt.BYTE * 32)]

    class _TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("hwndTrack", HWND), ("dwHoverTime", wt.DWORD)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]

    class _BLENDFUNCTION(ctypes.Structure):
        # Windows BLENDFUNCTION 恰好 4 字节:
        # BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat
        _fields_ = [("BlendOp", wt.BYTE), ("BlendFlags", wt.BYTE),
                    ("SourceConstantAlpha", wt.BYTE), ("AlphaFormat", wt.BYTE)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", wt.DWORD * 3)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", wt.DWORD)]

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HCURSOR), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
                    ("hIconSm", wt.HICON)]

    class _LOGFONTW(ctypes.Structure):
        _fields_ = [("lfHeight", wt.LONG), ("lfWidth", wt.LONG),
                    ("lfEscapement", wt.LONG), ("lfOrientation", wt.LONG),
                    ("lfWeight", wt.LONG), ("lfItalic", wt.BYTE),
                    ("lfUnderline", wt.BYTE), ("lfStrikeOut", wt.BYTE),
                    ("lfCharSet", wt.BYTE), ("lfOutPrecision", wt.BYTE),
                    ("lfClipPrecision", wt.BYTE), ("lfQuality", wt.BYTE),
                    ("lfPitchAndFamily", wt.BYTE), ("lfFaceName", wt.WCHAR * 32)]

    # Explicit 64-bit prototypes: HWND/HDC and callbacks must never pass through
    # ctypes' default c_int conversion on a 64-bit Python process.
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.GetModuleHandleW.restype = HINSTANCE
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wt.DWORD
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wt.DWORD
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wt.ATOM
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                        wt.INT, wt.INT, wt.INT, wt.INT, HWND, wt.HMENU,
                                        HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = HWND
    user32.DefWindowProcW.argtypes = [HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [HWND]
    user32.DestroyWindow.restype = wt.BOOL
    user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.SetWindowPos.argtypes = [HWND, ctypes.c_void_p, wt.INT, wt.INT, wt.INT, wt.INT, wt.UINT]
    user32.SetWindowPos.restype = wt.BOOL
    user32.SetLayeredWindowAttributes.argtypes = [HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
    user32.SetLayeredWindowAttributes.restype = wt.BOOL
    user32.SetTimer.argtypes = [HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [HWND, ctypes.c_size_t]
    user32.KillTimer.restype = wt.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = wt.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.TranslateMessage.restype = wt.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.PostThreadMessageW.restype = wt.BOOL
    user32.PostMessageW.argtypes = [HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.PostMessageW.restype = wt.BOOL
    user32.InvalidateRect.argtypes = [HWND, ctypes.POINTER(_RECT), wt.BOOL]
    user32.InvalidateRect.restype = wt.BOOL
    user32.BeginPaint.argtypes = [HWND, ctypes.POINTER(_PAINTSTRUCT)]
    user32.BeginPaint.restype = HDC
    user32.EndPaint.argtypes = [HWND, ctypes.POINTER(_PAINTSTRUCT)]
    user32.EndPaint.restype = wt.BOOL
    user32.FillRect.argtypes = [HDC, ctypes.POINTER(_RECT), wt.HBRUSH]
    user32.FillRect.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    user32.GetCursorPos.restype = wt.BOOL
    user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TRACKMOUSEEVENT)]
    user32.TrackMouseEvent.restype = wt.BOOL
    user32.SetCapture.argtypes = [HWND]
    user32.SetCapture.restype = HWND
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = wt.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDpiForWindow.argtypes = [HWND]
    user32.GetDpiForWindow.restype = wt.UINT
    user32.GetDpiForSystem.argtypes = []
    user32.GetDpiForSystem.restype = wt.UINT
    user32.MonitorFromWindow.argtypes = [HWND, wt.DWORD]
    user32.MonitorFromWindow.restype = wt.HANDLE
    user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wt.BOOL
    user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.LoadCursorW.argtypes = [HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = wt.HCURSOR
    gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
    gdi32.CreateSolidBrush.restype = wt.HBRUSH
    gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
    gdi32.CreatePen.restype = ctypes.c_void_p
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = ctypes.c_void_p
    gdi32.Arc.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.Arc.restype = wt.BOOL
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.CreateRectRgn.restype = ctypes.c_void_p
    gdi32.SelectClipRgn.argtypes = [HDC, ctypes.c_void_p]
    gdi32.SelectClipRgn.restype = ctypes.c_int
    gdi32.GetTextExtentPoint32W.argtypes = [HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(_SIZE)]
    gdi32.GetTextExtentPoint32W.restype = wt.BOOL
    gdi32.RoundRect.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int]
    gdi32.RoundRect.restype = wt.BOOL
    gdi32.Ellipse.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.Ellipse.restype = wt.BOOL
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wt.BOOL
    gdi32.CreateFontW.argtypes = [wt.INT, wt.INT, wt.INT, wt.INT, wt.INT, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
    gdi32.CreateFontW.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [HDC, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SetBkMode.argtypes = [HDC, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.SetTextColor.argtypes = [HDC, wt.COLORREF]
    gdi32.SetTextColor.restype = wt.COLORREF
    user32.DrawTextW.argtypes = [HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(_RECT), wt.UINT]
    user32.DrawTextW.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.DeleteDC.argtypes = [HDC]
    gdi32.DeleteDC.restype = wt.BOOL
    gdi32.CreateDIBSection.argtypes = [HDC, ctypes.POINTER(_BITMAPINFO), wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [HWND]
    user32.GetDC.restype = HDC
    user32.ReleaseDC.argtypes = [HWND, HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.UpdateLayeredWindow.argtypes = [HWND, HDC, ctypes.POINTER(_POINT),
                                           ctypes.POINTER(_SIZE), HDC, ctypes.POINTER(_POINT),
                                           wt.COLORREF, ctypes.POINTER(_BLENDFUNCTION), wt.DWORD]
    user32.UpdateLayeredWindow.restype = wt.BOOL

# Window/message constants. Kept here rather than importing pywin32.
_WM_APP = 0x8000
_WM_SHOW = _WM_APP + 1
_WM_HIDE = _WM_APP + 2
_WM_RELOCK = _WM_APP + 3
_WM_REPAINT = _WM_APP + 4
_WM_SHUTDOWN = _WM_APP + 5
_WM_TIMER = 0x0113   # WinUser.h 定义 WM_TIMER=0x0113 (0x0400 是 WM_USER)
_WM_PAINT = 0x000F
_WM_ERASEBKGND = 0x0014
_WM_NCHITTEST = 0x0084
_WM_MOUSEMOVE = 0x0200
_WM_MOUSELEAVE = 0x02A3
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_MOUSEACTIVATE = 0x0021
_WM_NCACTIVATE = 0x0086
_WM_ACTIVATE = 0x0006
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_WM_SETCURSOR = 0x0020
_WM_DPICHANGED = 0x02E0

_TME_LEAVE = 0x00000002
_HTTRANSPARENT = -1
_HTCAPTION = 2
_MA_NOACTIVATE = 3
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_SW_SHOW = 5

_WS_POPUP = 0x80000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST = 0x00000008
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TRANSPARENT = 0x00000020
_GWL_EXSTYLE = -20
_GWL_STYLE = -16
_LWA_COLORKEY = 0x00000001
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_SWP_HIDEWINDOW = 0x0080
_SWP_FRAMECHANGED = 0x0020
_HWND_TOPMOST = -1

_ULW_ALPHA = 0x00000002
_AC_SRC_ALPHA = 0x01
# 解锁状态下整窗铺一层 alpha=1 的隐形底: 肉眼不可见(背景完全透明), 但 alpha>0
# 保证鼠标消息能到达窗口(colorkey/零 alpha 像素会直接穿透, 正是旧版按钮点不到的根因)。
_BASE_ALPHA = 1

if IS_WINDOWS:
    _HWND_TOPMOST_PTR = ctypes.c_void_p(-1)
    # 遮罩图用通道区分元素: 蓝=主行(白), 红=次行(灰189), 绿=控件(亮灰229)。
    # 预计算 亮度->预乘BGRA 查找表, 合成循环只做查表赋值。
    _LUT_MAIN = [bytes((v, v, v, v)) for v in range(256)]
    _LUT_CTRL = [bytes((v * 229 // 255, v * 229 // 255, v * 229 // 255, v)) for v in range(256)]
    # 红色元素用 R+G 双通道做键控(绘制时 SetTextColor=0x0000FFFF)
    _LUT_RED = [bytes((0, 0, v, v)) for v in range(256)]

# This class is process-global; hwnd -> DesktopLyrics is only touched by window threads.
_INSTANCES: Dict[int, "DesktopLyrics"] = {}
_INSTANCES_LOCK = threading.RLock()
_CLASS_READY = False
_CLASS_LOCK = threading.Lock()
_WNDPROC_REF = None
_CLASS_NAME = "MusicHubDesktopLyricsWindow"


def _hwnd_int(hwnd: Any) -> int:
    try:
        return int(hwnd)
    except Exception:
        try:
            return int(ctypes.cast(hwnd, ctypes.c_void_p).value or 0)
        except Exception:
            return 0


def _global_wndproc(hwnd, msg, wparam, lparam):
    obj = None
    with _INSTANCES_LOCK:
        obj = _INSTANCES.get(_hwnd_int(hwnd))
    if obj is not None:
        try:
            return obj._wndproc(hwnd, int(msg), int(wparam), int(lparam))
        except Exception:
            # A painting callback must never tear down the WebView/application process.
            pass
    if IS_WINDOWS:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return 0


class DesktopLyrics:
    """A transparent, topmost, non-activating desktop lyrics overlay.

    ``update`` is safe to call from a webview callback or an audio event thread.
    ``on_change`` is optional and receives a shallow state dict after preference
    changes (enabled, lock, font size, position).
    """

    WIDTH = 1400
    HEIGHT = 320   # 缓冲最大高度(高 DPI 单行), 实际窗口高度按布局收缩
    MIN_FONT = 16
    MAX_FONT = 72  # 对齐网易云桌面歌词: 默认 28px, 可调 16~72

    def __init__(self, config_path: str, on_change: Optional[Callable[[dict], None]] = None):
        self.config_path = os.path.abspath(os.path.expanduser(str(config_path)))
        self.on_change = on_change if callable(on_change) else None
        self._lock = threading.RLock()
        self._pref_lock = threading.RLock()
        self._enabled = False
        self._locked = False
        self._font_size = 28
        self._font_family = "Microsoft YaHei UI"
        self._color = (255, 105, 180)  # 已唱文字颜色(RGB), 默认粉红
        self._lut_main_color = None
        self._lut_main_cache = None
        self._lut_sub_cache = None
        self._x: Optional[int] = None
        self._y: Optional[int] = None
        self._load_prefs()

        self._hwnd = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._hover = False
        self._dragging = False
        self._drag_moved = False
        self._ui_shown = False   # 按钮层: 点击歌词字形才唤出, 收起时整窗穿透
        self._ui_deadline = 0.0  # 按钮层自动收起的截止时刻(monotonic)
        self._drag_origin = (0, 0)
        self._window_origin = (0, 0)
        self._timer_id = 71
        self._dpi_scale = 1.0  # 窗口 DPI/96, 控制按钮与边距随分辨率缩放
        # UpdateLayeredWindow 逐像素 alpha 资源(仅窗口线程访问)
        self._final_dc = None
        self._final_bmp = None
        self._final_bits = None
        self._mask_dc = None
        self._mask_bmp = None
        self._mask_bits = None
        self._screen_dc = None
        self._last_shown = None
        self._payload: Dict[str, Any] = {
            "key": None, "title": "", "artist": "", "lines": [],
            "time": 0.0, "playing": False, "rate": 1.0, "status": "idle",
        }
        self._base_time = 0.0
        self._base_mono = time.monotonic()

    # ---------- preferences ----------
    @staticmethod
    def _clamp_font(value: Any) -> int:
        try:
            value = int(float(value))
        except Exception:
            value = 30
        return max(DesktopLyrics.MIN_FONT, min(DesktopLyrics.MAX_FONT, value))

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            v = int(value)
            return v if abs(v) < 100000 else None
        except Exception:
            return None

    @staticmethod
    def _clamp_rgb(v: Any):
        try:
            if isinstance(v, str):
                s = v.strip().lstrip("#")
                if len(s) == 6:
                    v = [int(s[i:i + 2], 16) for i in (0, 2, 4)]
            r, g, b = (int(x) for x in v)
            return tuple(max(0, min(255, x)) for x in (r, g, b))
        except Exception:
            return None

    @staticmethod
    def _safe_family(value: Any) -> str:
        s = str(value or "").strip()
        if not s or len(s) >= 32 or any(c in s for c in "\"'{};<>\\\n\r"):
            return "Microsoft YaHei UI"
        return s

    def _load_prefs(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if not isinstance(obj, dict):
                return
            self._enabled = bool(obj.get("enabled", False))
            self._locked = bool(obj.get("locked", False))
            self._font_size = self._clamp_font(obj.get("font_size", 28))
            self._font_family = self._safe_family(obj.get("font_family"))
            c = self._clamp_rgb(obj.get("color"))
            if c:
                self._color = c
            self._x = self._safe_int(obj.get("x"))
            self._y = self._safe_int(obj.get("y"))
        except Exception:
            pass

    def _prefs_obj(self) -> dict:
        return {"enabled": bool(self._enabled), "locked": bool(self._locked),
                "font_size": int(self._font_size), "font_family": self._font_family,
                "color": list(self._color), "x": self._x, "y": self._y}

    def _save_prefs(self) -> None:
        obj = self._prefs_obj()
        path = self.config_path
        parent = os.path.dirname(path) or "."
        try:
            os.makedirs(parent, exist_ok=True)
            with self._pref_lock:
                fd, tmp = tempfile.mkstemp(prefix=".desktop-lyrics-", suffix=".tmp", dir=parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
                        f.flush()
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    os.replace(tmp, path)
                finally:
                    try:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    except Exception:
                        pass
        except Exception:
            pass

    def _notify_change(self) -> None:
        cb = self.on_change
        if cb is None:
            return
        try:
            cb(self.get_state())
        except Exception:
            pass

    def get_state(self) -> dict:
        with self._lock:
            return {"enabled": bool(self._enabled), "locked": bool(self._locked),
                    "font_size": int(self._font_size), "font_family": self._font_family,
                    "color": list(self._color), "x": self._x, "y": self._y,
                    "available": bool(IS_WINDOWS)}

    def set_font(self, family: Any = None, size: Any = None) -> dict:
        """设置原生桌面歌词字体与字号(非法值沿用原值)."""
        with self._lock:
            if family is not None:
                self._font_family = self._safe_family(family)
            if size is not None:
                self._font_size = self._clamp_font(size)
        self._save_prefs()
        if IS_WINDOWS:
            self._post(_WM_REPAINT)
        self._notify_change()
        return self.get_state()

    def set_color(self, value: Any) -> dict:
        """自定义已唱文字颜色: "#rrggbb" 或 [r,g,b]; 非法值忽略."""
        c = self._clamp_rgb(value)
        if c is None:
            return self.get_state()
        with self._lock:
            self._color = c
        self._save_prefs()
        if IS_WINDOWS:
            self._post(_WM_REPAINT)
        self._notify_change()
        return self.get_state()

    # ---------- public control ----------
    def _ensure_thread(self) -> None:
        if not IS_WINDOWS:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._ready.clear()
            self._thread = threading.Thread(target=self._thread_main,
                                            name="MusicHubDesktopLyrics", daemon=True)
            self._thread.start()
        self._ready.wait(2.5)

    def set_enabled(self, enabled: bool) -> dict:
        value = bool(enabled)
        with self._lock:
            self._enabled = value
        self._save_prefs()
        if IS_WINDOWS:
            if value:
                self._ensure_thread()
                self._post(_WM_SHOW)
            else:
                self._post(_WM_HIDE)
        self._notify_change()
        return self.get_state()

    def toggle_lock(self) -> dict:
        with self._lock:
            self._locked = not self._locked
            value = self._locked
        self._save_prefs()
        if IS_WINDOWS:
            self._post(_WM_RELOCK, 1 if value else 0)
        self._notify_change()
        return self.get_state()

    def close(self) -> None:
        """Stop the native message thread. Safe to call repeatedly."""
        with self._lock:
            self._enabled = False
        # close() is application shutdown, not a user clicking the window's
        # close control. Preserve the persisted enabled preference so the next
        # MusicHub launch can recreate the window automatically.
        th = self._thread
        if IS_WINDOWS and th and th.is_alive():
            self._stop.set()
            self._post(_WM_SHUTDOWN)
            if self._thread_id:
                try:
                    user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
                except Exception:
                    pass
            if th is not threading.current_thread():
                try:
                    th.join(2.5)
                except Exception:
                    pass
        self._thread = None
        self._hwnd = None

    shutdown = close

    def update(self, payload: Optional[dict]) -> None:
        """Replace the current playback snapshot and schedule a repaint.

        Invalid input is ignored. A new song key clears stale lines first; when
        the caller supplies lines for that new key they are then installed.
        """
        if not isinstance(payload, dict):
            return
        with self._lock:
            old_key = self._payload.get("key")
            new_key = payload.get("key", old_key)
            key_changed = new_key != old_key
            if key_changed:
                self._payload["lines"] = []
            self._payload["key"] = new_key
            for k in ("title", "artist", "status"):
                if k in payload:
                    self._payload[k] = str(payload.get(k) or "")
            if "playing" in payload:
                self._payload["playing"] = bool(payload.get("playing"))
            if "rate" in payload:
                try:
                    rate = float(payload.get("rate"))
                    self._payload["rate"] = rate if math.isfinite(rate) and rate > 0 else 1.0
                except Exception:
                    self._payload["rate"] = 1.0
            if "time" in payload:
                try:
                    val = float(payload.get("time"))
                    if math.isfinite(val):
                        self._payload["time"] = max(0.0, val)
                        self._base_time = max(0.0, val)
                        self._base_mono = time.monotonic()
                except Exception:
                    pass
            if "lines" in payload and isinstance(payload.get("lines"), (list, tuple)):
                self._payload["lines"] = self._normalise_lines(payload.get("lines"))
            elif key_changed:
                self._payload["lines"] = []
        if IS_WINDOWS:
            self._post(_WM_REPAINT)

    def _normalise_lines(self, lines: Iterable[Any]) -> List[dict]:
        out: List[dict] = []
        for row in lines:
            if not isinstance(row, dict):
                continue
            try:
                t = float(row.get("t", row.get("time", 0)))
                if not math.isfinite(t):
                    continue
            except Exception:
                continue
            text = str(row.get("text", "") or "").strip()
            if text:
                out.append({"t": max(0.0, t), "text": text})
        out.sort(key=lambda z: z["t"])
        return out

    # ---------- native window ----------
    def _post(self, message: int, wparam: int = 0, lparam: int = 0) -> None:
        hwnd = self._hwnd
        if IS_WINDOWS and hwnd:
            try:
                user32.PostMessageW(hwnd, message, wparam, lparam)
            except Exception:
                pass

    def _register_class(self) -> bool:
        global _CLASS_READY, _WNDPROC_REF
        with _CLASS_LOCK:
            if _CLASS_READY:
                return True
            try:
                _WNDPROC_REF = WNDPROC(_global_wndproc)
                wc = _WNDCLASSEXW()
                wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
                wc.style = 0
                wc.lpfnWndProc = _WNDPROC_REF
                wc.cbClsExtra = wc.cbWndExtra = 0
                wc.hInstance = kernel32.GetModuleHandleW(None)
                wc.hIcon = None
                wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
                wc.hbrBackground = None
                wc.lpszMenuName = None
                wc.lpszClassName = _CLASS_NAME
                wc.hIconSm = None
                atom = user32.RegisterClassExW(ctypes.byref(wc))
                if atom == 0:
                    # ERROR_CLASS_ALREADY_EXISTS is benign (e.g. a re-enabled instance).
                    if ctypes.get_last_error() != 1410:
                        return False
                _CLASS_READY = True
                return True
            except Exception:
                return False

    def _thread_main(self) -> None:
        if not IS_WINDOWS:
            self._ready.set()
            return
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hwnd = None
        try:
            if not self._register_class():
                return
            with self._lock:
                x = self._x
                y = self._y
                locked = self._locked
            try:
                dpi = int(user32.GetDpiForSystem())
                if dpi:
                    self._dpi_scale = max(1.0, dpi / 96.0)
            except Exception:
                pass
            # 宽度始终为屏幕宽度, 高度按字号布局自适应
            scr_w = int(user32.GetSystemMetrics(0)) or self.WIDTH
            scr_h = int(user32.GetSystemMetrics(1)) or 0
            self.WIDTH = scr_w
            h0 = self._layout()[2]
            if y is None or not (0 <= y <= max(0, scr_h - h0)):
                y = max(0, scr_h - h0 - 70)
            x = 0
            # 锁定不再用 WS_EX_TRANSPARENT: 改由逐像素 alpha(锁定=除解锁按钮外全 0)
            # 实现穿透, 保证锁定时解锁按钮仍可点击。
            ex = _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST | _WS_EX_NOACTIVATE
            hwnd = user32.CreateWindowExW(ex, _CLASS_NAME, "桌面歌词", _WS_POPUP,
                                         int(x), int(y), self.WIDTH, h0,
                                         None, None, kernel32.GetModuleHandleW(None), None)
            if not hwnd:
                return
            self._hwnd = hwnd
            try:
                dpi = int(user32.GetDpiForWindow(hwnd))
                if dpi:
                    self._dpi_scale = max(1.0, dpi / 96.0)
            except Exception:
                pass
            with _INSTANCES_LOCK:
                _INSTANCES[_hwnd_int(hwnd)] = self
            # 逐像素 alpha 模式: 不使用 SetLayeredWindowAttributes(两者互斥),
            # 全部呈现经由 UpdateLayeredWindow 原子提交, 从根本上消除闪烁。
            if not self._create_gdi():
                return
            user32.SetTimer(hwnd, self._timer_id, 50, None)
            self._ready.set()
            if self._enabled:
                user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
            else:
                user32.ShowWindow(hwnd, _SW_HIDE)
            msg = wt.MSG()
            while not self._stop.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._ready.set()
        finally:
            if hwnd:
                try:
                    user32.KillTimer(hwnd, self._timer_id)
                except Exception:
                    pass
                with _INSTANCES_LOCK:
                    _INSTANCES.pop(_hwnd_int(hwnd), None)
                self._cleanup_gdi()
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            self._hwnd = None
            self._thread_id = 0
            self._ready.set()

    def _set_visible(self, yes: bool) -> None:
        if not self._hwnd:
            return
        user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE if yes else _SW_HIDE)
        if yes:
            self._ui_shown = False  # 每次显示都从"纯歌词穿透"状态开始
            self._ui_deadline = 0.0
            user32.SetWindowPos(self._hwnd, _HWND_TOPMOST_PTR, 0, 0, 0, 0,
                                 _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
            self._last_shown = None
            self._repaint()

    def _set_ui(self, shown: bool) -> None:
        """显隐按钮层; 唤出后 5 秒无交互自动收起(仅窗口线程调用).

        用截止时间戳 + 50ms 心跳检查实现, 不用 WM_TIMER: WM_TIMER 只在队列空闲
        时投递, 会被歌词心跳/前端推送饿死数秒, 导致自动隐藏明显迟到。"""
        if self._ui_shown != shown:
            self._ui_shown = shown
            self._repaint()
        self._ui_deadline = time.monotonic() + 5.0 if shown else 0.0

    def _ui_touch(self) -> None:
        """按钮层保持显示并重置 5 秒倒计时."""
        if self._ui_shown:
            self._ui_deadline = time.monotonic() + 5.0

    def _apply_lock(self, locked: bool) -> None:
        # 锁定的穿透由 alpha 合成实现, 这里只需重绘
        self._repaint()

    def _effective_time(self) -> float:
        with self._lock:
            t = float(self._base_time)
            playing = bool(self._payload.get("playing"))
            rate = float(self._payload.get("rate") or 1.0)
            base = self._base_mono
        if playing:
            t += max(0.0, time.monotonic() - base) * rate
        return max(0.0, t)

    def _current_next(self) -> tuple[str, str]:
        with self._lock:
            p = dict(self._payload)
            lines = list(p.get("lines") or [])
        title = p.get("title") or ""
        artist = p.get("artist") or ""
        status = str(p.get("status") or "")
        if not lines:
            if status in ("loading", "fetching"):
                return (title or "歌词加载中…", artist or "")
            if title:
                return (title, artist or "暂无歌词")
            return ("未在播放" if status in ("idle", "") else status, artist)
        t = self._effective_time()
        idx = -1
        for i, line in enumerate(lines):
            if float(line.get("t", 0)) <= t + 0.12:
                idx = i
            else:
                break
        if idx < 0:
            return (title or "♪", str(lines[0].get("text") or ""))
        cur = str(lines[idx].get("text") or "")
        nxt = str(lines[idx + 1].get("text") or "") if idx + 1 < len(lines) else ""
        return (cur or title or "♪", nxt or (artist if idx == len(lines) - 1 else ""))

    @staticmethod
    def _xy(lparam: int) -> tuple[int, int]:
        # GET_X_LPARAM/GET_Y_LPARAM with signed 16-bit conversion.
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        return int(x), int(y)

    def _control_at(self, x: int, y: int) -> Optional[str]:
        if y < 4 or y > self._strip() + 4:
            return None
        for x0, x1, label, key, fs in self._controls():
            if x0 <= x < x1:
                return key
        return None

    # ---------- 逐像素 alpha 绘制 ----------

    def _make_dib(self):
        """创建 32bpp 自顶向下 DIB section, 返回 (bitmap, dc, 像素缓冲指针)."""
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.WIDTH
        bmi.bmiHeader.biHeight = -self.HEIGHT
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(self._screen_dc, ctypes.byref(bmi), 0,
                                     ctypes.byref(bits), None, 0)
        if not bmp or not bits:
            return None, None, None
        dc = gdi32.CreateCompatibleDC(self._screen_dc)
        gdi32.SelectObject(dc, bmp)
        return bmp, dc, bits

    def _create_gdi(self) -> bool:
        try:
            self._screen_dc = user32.GetDC(None)
            self._final_bmp, self._final_dc, self._final_bits = self._make_dib()
            self._mask_bmp, self._mask_dc, self._mask_bits = self._make_dib()
            return bool(self._final_dc and self._mask_dc)
        except Exception:
            self._cleanup_gdi()
            return False

    def _cleanup_gdi(self) -> None:
        for attr in ("_final_bmp", "_mask_bmp"):
            h = getattr(self, attr, None)
            if h:
                try:
                    gdi32.DeleteObject(h)
                except Exception:
                    pass
            setattr(self, attr, None)
        for attr in ("_final_dc", "_mask_dc"):
            h = getattr(self, attr, None)
            if h:
                try:
                    gdi32.DeleteDC(h)
                except Exception:
                    pass
            setattr(self, attr, None)
        self._final_bits = None
        self._mask_bits = None
        if self._screen_dc:
            try:
                user32.ReleaseDC(None, self._screen_dc)
            except Exception:
                pass
            self._screen_dc = None

    def _draw(self, hdc, text: str, rect: _RECT, font_size: int, color: int,
              bold: bool = False, center: bool = False) -> None:
        font = gdi32.CreateFontW(-int(font_size), 0, 0, 0, 700 if bold else 400,
                                 0, 0, 0, 134, 0, 0, 4, 0, self._font_family)
        old = gdi32.SelectObject(hdc, font)
        gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
        gdi32.SetTextColor(hdc, color)
        flags = 0x0001 | 0x0020 | (0x0002 if center else 0x0004)  # SINGLELINE|VCENTER|CENTER/LEFT
        user32.DrawTextW(hdc, str(text), -1, ctypes.byref(rect), flags)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(font)

    def _strip(self) -> int:
        """控制按钮高度(随 DPI 缩放)."""
        return max(36, int(36 * self._dpi_scale))

    def _monitor_width(self, hwnd) -> int:
        """窗口所在显示器(就近原则)的物理宽度, 失败返回 0."""
        try:
            mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                return int(mi.rcMonitor.right - mi.rcMonitor.left)
        except Exception:
            pass
        return 0

    def _controls(self):
        """按钮组水平居中. 未锁定: − + 锁 ×; 锁定: 仅解锁(红色锁字).
        返回 [(x0, x1, 标签, 键, 字号)]."""
        s = self._dpi_scale
        bw = max(38, int(38 * s))
        f = max(18, int(22 * s))
        if self._locked:
            items = (("锁", "lock"),)
        else:
            items = (("−", "minus"), ("+", "plus"), ("锁", "lock"), ("×", "close"))
        x0 = (self.WIDTH - bw * len(items)) // 2
        out = []
        for label, key in items:
            out.append((x0, x0 + bw, label, key, f))
            x0 += bw
        return out

    def _layout(self):
        """固定边距布局: 顶部控制条随 DPI 缩放, 歌词槽按字号排布, 底部 8px.

        行槽高 = 1.3*字号+12: 雅黑行框约 1.2 倍字号, 需为 y/g 等下伸部留足空间。
        返回 (内容起始 y, 行距, 窗口实际高度); 无歌词行时状态区占两行(标题+歌手)。"""
        pitch = int(self._font_size * 1.3) + 12
        y0 = self._strip() + 8
        rows = 1 if self._scroll_state() else 2
        return y0, pitch, min(self.HEIGHT, y0 + rows * pitch + 8)

    def _main_lut(self):
        """按当前自定义颜色生成/缓存 蓝通道亮度->预乘BGRA 查找表(无 numpy 兜底路径用)."""
        if self._lut_main_color != self._color:
            cR, cG, cB = self._color
            self._lut_main_cache = [bytes((cB * v // 255, cG * v // 255, cR * v // 255, v))
                                    for v in range(256)]
            # 未唱 = 同色淡化 45%; 红通道(无 numpy 路径里也承载次行亮度)
            self._lut_sub_cache = [
                bytes((cB * v * 45 // (255 * 100), cG * v * 45 // (255 * 100),
                       cR * v * 45 // (255 * 100), v)) for v in range(256)]
            self._lut_main_color = self._color
        return self._lut_main_cache

    def _sub_lut(self):
        return self._lut_sub_cache

    def _text_w(self, hdc, text: str, font_size: int, bold: bool) -> int:
        font = gdi32.CreateFontW(-int(font_size), 0, 0, 0, 700 if bold else 400,
                                 0, 0, 0, 134, 0, 0, 4, 0, self._font_family)
        old = gdi32.SelectObject(hdc, font)
        sz = _SIZE(0, 0)
        s = str(text)
        gdi32.GetTextExtentPoint32W(hdc, s, len(s), ctypes.byref(sz))
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(font)
        return int(sz.cx or 0)

    def _scroll_state(self):
        """(歌词行, 当前行下标, 行内进度[0,1], 换行滑动[0,1]); 无歌词行返回 None."""
        with self._lock:
            lines = list(self._payload.get("lines") or [])
        if not lines:
            return None
        t = self._effective_time()
        idx = -1
        for i, ln in enumerate(lines):
            if float(ln.get("t", 0)) <= t + 0.12:
                idx = i
            else:
                break
        frac = 0.0
        shift = 0.0
        if idx >= 0:
            tc = float(lines[idx]["t"])
            if idx + 1 < len(lines):
                tn = float(lines[idx + 1]["t"])
                d = tn - tc
                if d > 0.35:
                    frac = min(1.0, max(0.0, (t - tc) / d))
                    # 换行滑动固定 ~0.45s(网易云式快速切换), 不随句长拉长
                    slide = min(0.45, d * 0.4)
                    s = (t - (tn - slide)) / slide
                    shift = min(1.0, max(0.0, s))
            else:
                frac = 1.0  # 末句: 全部视为已唱
        return lines, idx, frac, shift

    def _paint_sig(self):
        """重绘签名: 换行位移按 2px、卡拉OK 进度按 ~1.5% 步进量化, 控制重绘频率."""
        st = self._scroll_state()
        if st:
            lines, idx, frac, shift = st
            off = int(round(shift * (int(self._font_size * 1.3) + 12) / 2.0)) * 2
            core = ("kar", idx, off, int(frac * 66), len(lines))
        else:
            core = ("status",) + self._current_next()
        return core + (self._font_size, self._font_family, self._locked, self.WIDTH,
                       self._ui_shown, self._color)

    def _repaint(self) -> None:
        """在遮罩 DIB 上画文字, 按通道合成预乘 ARGB, 一次性原子提交.

        网易云风格单行卡拉OK: 当前句居中加粗, 已唱部分白色、未唱部分灰色,
        边界随进度移动; 句尾 30% 时间整块平滑上移滑出/滑入下一句,
        歌词裁剪在内容区(不会压到按钮条)。"""
        hwnd = self._hwnd
        if not hwnd or not self._final_dc or not self._mask_dc:
            return
        try:
            sig = self._paint_sig()
            W, H = self.WIDTH, self.HEIGHT
            y0, pitch, h = self._layout()
            rect = _RECT(0, 0, W, H)
            black = gdi32.CreateSolidBrush(0x00000000)
            user32.FillRect(self._mask_dc, ctypes.byref(rect), black)
            gdi32.DeleteObject(black)
            fs = self._font_size
            other = max(self.MIN_FONT, fs - 8)
            center = y0 + pitch // 2
            st = self._scroll_state()
            # 通道即元素身份: 纯蓝=已唱(白), 纯红=未唱/相邻行(灰), 纯绿=控件
            if st:
                lines, idx, frac, shift = st
                shift = shift * shift * (3 - 2 * shift)  # smoothstep
                clip = gdi32.CreateRectRgn(0, y0, W, y0 + pitch)
                for j in (idx - 1, idx, idx + 1):
                    if not (0 <= j < len(lines)):
                        continue
                    txt = str(lines[j].get("text") or "")
                    if not txt:
                        continue
                    yc = int(center + (j - idx - shift) * pitch)
                    r = _RECT(14, yc - pitch // 2, W - 14, yc + pitch // 2)
                    gdi32.SelectClipRgn(self._mask_dc, clip)
                    if j == idx:
                        tw = self._text_w(self._mask_dc, txt, fs, True)
                        x_start = max(14, (W - tw) // 2)
                        bx = x_start + int(tw * min(1.0, max(0.0, frac)))
                        rgn = gdi32.CreateRectRgn(x_start, y0, bx, y0 + pitch)
                        gdi32.SelectClipRgn(self._mask_dc, rgn)
                        self._draw(self._mask_dc, txt, r, fs, 0x00FF0000, True, center=True)
                        gdi32.DeleteObject(rgn)
                        rgn = gdi32.CreateRectRgn(bx, y0, x_start + tw + 8, y0 + pitch)
                        gdi32.SelectClipRgn(self._mask_dc, rgn)
                        self._draw(self._mask_dc, txt, r, fs, 0x000000FF, True, center=True)
                        gdi32.DeleteObject(rgn)
                    else:
                        self._draw(self._mask_dc, txt, r, fs, 0x000000FF, False, center=True)
                gdi32.DeleteObject(clip)
                gdi32.SelectClipRgn(self._mask_dc, None)
            else:
                current, nxt = self._current_next()
                self._draw(self._mask_dc, current,
                           _RECT(14, center - pitch // 2, W - 14, center + pitch // 2),
                           fs, 0x00FF0000, True, center=True)
                if nxt:
                    self._draw(self._mask_dc, nxt,
                               _RECT(14, center + pitch // 2, W - 14, center + pitch // 2 + pitch),
                               other, 0x000000FF, False, center=True)
            # 按钮层默认隐藏(整窗穿透), 点击歌词字形唤出后才绘制; 锁定态 锁 字为红色
            bh = self._strip()
            if self._ui_shown:
                for x0, x1, label, key, fsize in self._controls():
                    if key == "lock":
                        # 实体底: 整个方格填充低强度灰(绿通道 70), alpha>0 保证
                        # 锁定态(无隐形底)也能在方格内任意位置命中上锁/解锁
                        pen = gdi32.CreatePen(0, 1, 0x00004600)
                        brush = gdi32.CreateSolidBrush(0x00004600)
                        oldp = gdi32.SelectObject(self._mask_dc, pen)
                        oldbr = gdi32.SelectObject(self._mask_dc, brush)
                        rad = max(4, int(bh * 0.30))
                        gdi32.RoundRect(self._mask_dc, x0 + 2, 4, x1 - 2, 4 + bh - 2, rad, rad)
                        gdi32.SelectObject(self._mask_dc, oldp)
                        gdi32.SelectObject(self._mask_dc, oldbr)
                        gdi32.DeleteObject(pen)
                        gdi32.DeleteObject(brush)
                    color = 0x0000FFFF if (key == "lock" and self._locked) else 0x0000FF00
                    self._draw(self._mask_dc, label, _RECT(x0, 4, x1, 4 + bh),
                               fsize, color, False, center=True)
            veil_on = self._ui_shown and not self._locked
            mask = ctypes.string_at(self._mask_bits, h * W * 4)
            buf = None
            if _np is not None:
                try:
                    # 通道键控: 蓝=已唱(自定义颜色) 红=未唱(灰189) 绿=按钮(灰229) 红+绿=红色元素
                    m = _np.frombuffer(mask, dtype=_np.uint8).reshape(h, W, 4)
                    bch = m[..., 0].astype(_np.int32)
                    gch = m[..., 1].astype(_np.int32)
                    rch = m[..., 2].astype(_np.int32)
                    cR, cG, cB = self._color
                    # 未唱 = 当前颜色稍变灰(淡化 45%), 按钮仍为固定亮灰
                    gray_r = rch * cR * 45 // (255 * 100) + gch * 229 // 255
                    gray_g = rch * cG * 45 // (255 * 100) + gch * 229 // 255
                    gray_b = rch * cB * 45 // (255 * 100) + gch * 229 // 255
                    pr = _np.clip(bch * cR // 255 + gray_r, 0, 255)
                    pg = _np.clip(bch * cG // 255 + gray_g, 0, 255)
                    pb = _np.clip(bch * cB // 255 + gray_b, 0, 255)
                    red = (bch == 0) & (gch > 0) & (rch > 0)
                    v = _np.maximum.reduce([bch, gch, rch])
                    aa = _np.where(red, v, _np.clip(bch + rch + gch, 0, 255))
                    if veil_on:
                        aa = _np.where(aa > 0, aa, _BASE_ALPHA)
                    out = _np.empty((h, W, 4), dtype=_np.uint8)
                    out[..., 0] = _np.where(red, 0, pb)
                    out[..., 1] = _np.where(red, 0, pg)
                    out[..., 2] = _np.where(red, v, pr)
                    out[..., 3] = aa
                    buf = out.tobytes()
                except Exception:
                    buf = None
            if buf is None:
                base = bytes((0, 0, 0, _BASE_ALPHA if veil_on else 0))
                ba = bytearray(base * (h * W))
                lut_main = self._main_lut()
                lut_sub, lut_ctrl, lut_red = self._sub_lut(), _LUT_CTRL, _LUT_RED
                end = h * W * 4
                for off in range(0, end, 4):
                    b = mask[off]
                    g = mask[off + 1]
                    r = mask[off + 2]
                    if b:
                        ba[off:off + 4] = lut_main[b]
                    elif g and r:
                        ba[off:off + 4] = lut_red[g if g > r else r]
                    elif r:
                        ba[off:off + 4] = lut_sub[r]
                    elif g:
                        ba[off:off + 4] = lut_ctrl[g]
                buf = bytes(ba)
            ctypes.memmove(self._final_bits, buf, h * W * 4)
            rr = _RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rr))
            if (rr.bottom - rr.top) != h or (rr.right - rr.left) != W:
                # 字号/屏幕变化时按左上角锚点调整窗口尺寸, 边距保持固定
                user32.SetWindowPos(hwnd, _HWND_TOPMOST_PTR, 0, 0, W, h,
                                    _SWP_NOMOVE | _SWP_NOACTIVATE)
            size = _SIZE(W, h)
            src = _POINT(0, 0)
            blend = _BLENDFUNCTION(0, 0, 255, _AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(hwnd, None, None, ctypes.byref(size),
                                       self._final_dc, ctypes.byref(src), 0,
                                       ctypes.byref(blend), _ULW_ALPHA)
            self._last_shown = sig
        except Exception:
            pass

    def _tick(self) -> None:
        """50ms 心跳: 先处理按钮层超时隐藏, 再按变化检测决定是否重绘。"""
        try:
            if self._ui_shown and time.monotonic() >= self._ui_deadline:
                self._set_ui(False)  # 5 秒无交互, 自动收起
                return
            if self._paint_sig() != self._last_shown:
                self._repaint()
        except Exception:
            pass

    def _wndproc(self, hwnd, msg: int, wparam: int, lparam: int) -> int:
        if msg == _WM_SHOW:
            self._set_visible(True)
            return 0
        if msg == _WM_HIDE:
            self._set_visible(False)
            return 0
        if msg == _WM_RELOCK:
            self._apply_lock(bool(wparam))
            return 0
        if msg == _WM_REPAINT:
            # 前端心跳也走变化检测: 内容未变则完全不重绘
            self._tick()
            return 0
        if msg == _WM_TIMER:
            self._tick()
            return 0
        if msg == _WM_SHUTDOWN:
            user32.DestroyWindow(hwnd)
            user32.PostQuitMessage(0)
            return 0
        if msg == _WM_ERASEBKGND:
            return 1
        if msg in (_WM_ACTIVATE, _WM_NCACTIVATE):
            return 0
        if msg == _WM_MOUSEACTIVATE:
            return _MA_NOACTIVATE
        if msg == _WM_NCHITTEST:
            # 穿透完全由逐像素 alpha 决定(锁定=除文字/解锁按钮外 alpha 0), 这里恒为客户区
            return 1  # HTCLIENT
        if msg == _WM_DPICHANGED:
            # 拖到高 DPI 屏(或反之): 按钮/边距按新 DPI 重排; 宽度跟随所在屏幕,
            # 尺寸变化时重建 DIB 缓冲, _repaint 内会同步窗口尺寸
            dpi = wparam & 0xFFFF
            if dpi:
                self._dpi_scale = max(1.0, dpi / 96.0)
            w2 = self._monitor_width(hwnd)
            if w2 and w2 != self.WIDTH:
                self.WIDTH = w2
                self._cleanup_gdi()
                self._create_gdi()
            self._repaint()
            return 0
        if msg == _WM_MOUSEMOVE:
            if self._dragging:
                pt = _POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                if abs(pt.x - self._drag_origin[0]) + abs(pt.y - self._drag_origin[1]) > 4:
                    self._drag_moved = True
                nx = self._window_origin[0] + int(pt.x - self._drag_origin[0])
                ny = self._window_origin[1] + int(pt.y - self._drag_origin[1])
                user32.SetWindowPos(hwnd, _HWND_TOPMOST_PTR, nx, ny, 0, 0,
                                    _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
            return 0
        if msg == _WM_LBUTTONDOWN:
            with self._lock:
                locked = self._locked
            x, y = self._xy(lparam)
            if not self._ui_shown:
                # 按钮层隐藏时只有歌词字形像素可命中: 点击即唤出按钮层(5 秒后自动收起)
                self._set_ui(True)
                return 0
            hit = self._control_at(x, y)
            if hit == "close":
                self._set_ui(False)
                self.set_enabled(False)
                return 0
            if hit == "lock":
                self.toggle_lock()
                self._ui_touch()
                self._repaint()
                return 0
            if hit in ("plus", "minus"):
                with self._lock:
                    self._font_size = self._clamp_font(self._font_size + (6 if hit == "plus" else -6))
                self._save_prefs()
                self._notify_change()
                self._ui_touch()
                self._repaint()
                return 0
            if locked:
                self._set_ui(False)  # 锁定态点空白: 收起按钮层
                return 0
            self._dragging = True
            self._drag_moved = False
            pt = _POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            rr = _RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rr))
            self._drag_origin = (int(pt.x), int(pt.y))
            self._window_origin = (int(rr.left), int(rr.top))
            user32.SetCapture(hwnd)
            return 0
        if msg == _WM_LBUTTONUP:
            if self._dragging:
                self._dragging = False
                user32.ReleaseCapture()
                if not self._drag_moved:
                    self._set_ui(False)  # 未拖动的轻点: 收起按钮层
                    return 0
                rr = _RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rr))
                with self._lock:
                    self._x, self._y = int(rr.left), int(rr.top)
                self._save_prefs()
                self._notify_change()
                self._ui_touch()
            return 0
        if msg == _WM_CLOSE:
            self.set_enabled(False)
            return 0
        if msg == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


__all__ = ["DesktopLyrics"]
