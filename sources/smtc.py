# -*- coding: utf-8 -*-
"""MusicHub 原生系统媒体控件(SMTC)桥接.

WebView2/Chromium 会为网页音频自行注册一条泛化的系统媒体会话, 显示成
"未知应用 / 一个网站正在播放媒体"; 网页 Media Session 元数据不会覆盖它。
这里直接为主窗口注册 Win32 桌面程序可用的 Windows SMTC 会话, 由前端推送
真实歌曲信息, 并通过 WebView2 浏览器参数关闭 Chromium 的媒体会话服务,
避免两个会话互相抢占。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import uuid

try:
    import clr
    import System
    from System.Runtime.InteropServices import Marshal
    clr.AddReference("System.Runtime.WindowsRuntime")
except Exception:
    clr = None
    System = None
    Marshal = None


_HRESULT = ctypes.c_long
_HSTRING = ctypes.c_void_p
_IID_IACTIVATION_FACTORY = "00000035-0000-0000-c000-000000000046"
_IID_INTEROP = "ddb0472d-c911-4a1f-86d9-dc3d71a95f5a"
_IID_SMTC = "99fa3ff4-1742-42a6-902e-087d41f965ec"
_SMTC_CLASS = "Windows.Media.SystemMediaTransportControls"
AUMID = "MusicHub.Desktop.Media"
# CustomActivator COM 类: 媒体卡片点击 AUMID 激活时由 COM 拉起并唤醒主窗口
ACTIVATOR_CLSID = "{A5B27E1D-4F8C-4A6B-9D2E-7C3F1B5A8E64}"
_MEDIA_TYPE = "Windows.Media.MediaPlaybackType, Windows.Media, ContentType=WindowsRuntime"
_MEDIA_STATUS = "Windows.Media.MediaPlaybackStatus, Windows.Media, ContentType=WindowsRuntime"

_smtc = {"controls": None, "display": None, "lock": threading.RLock(),
         "hwnd": None, "last": {}, "started": False, "error": ""}
_combase = None
_user32 = None


def _make_guid(s):
    return ctypes.create_string_buffer(uuid.UUID(s).bytes_le, 16)


def _com_ptr(ptr):
    return ctypes.cast(ptr, ctypes.c_void_p)


def _vtable_fn(ptr, index, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.cast(fn, ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes))


def _setup_winrt():
    """初始化 RoGetActivationFactory / HSTRING 的 ctypes 原型."""
    global _combase, _user32
    if _combase is not None:
        return _combase
    cb = ctypes.windll.combase
    cb.RoInitialize.argtypes = [ctypes.c_int]
    cb.RoInitialize.restype = _HRESULT
    cb.WindowsCreateString.argtypes = [wt.LPCWSTR, wt.UINT, ctypes.POINTER(_HSTRING)]
    cb.WindowsCreateString.restype = _HRESULT
    cb.WindowsDeleteString.argtypes = [_HSTRING]
    cb.WindowsDeleteString.restype = _HRESULT
    cb.RoGetActivationFactory.argtypes = [_HSTRING, ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_void_p)]
    cb.RoGetActivationFactory.restype = _HRESULT
    hr = cb.RoInitialize(1)  # RO_INIT_MULTITHREADED
    if hr < 0 and (hr & 0xffffffff) not in (0x80010106, 0x00000001):
        raise OSError("RoInitialize 失败: 0x%08X" % (hr & 0xffffffff))
    _combase = cb
    _user32 = ctypes.windll.user32
    return cb


def _get_controls(hwnd):
    """通过 ISystemMediaTransportControlsInterop::GetForWindow 取主窗口 SMTC."""
    if clr is None or System is None or Marshal is None:
        raise RuntimeError("pythonnet/WinRT 不可用")
    cb = _setup_winrt()
    hs = _HSTRING()
    hr = cb.WindowsCreateString(_SMTC_CLASS, len(_SMTC_CLASS), ctypes.byref(hs))
    if hr < 0:
        raise OSError("WindowsCreateString 失败: 0x%08X" % (hr & 0xffffffff))
    try:
        factory = ctypes.c_void_p()
        hr = cb.RoGetActivationFactory(
            hs, _com_ptr(_make_guid(_IID_IACTIVATION_FACTORY)), ctypes.byref(factory))
        if hr < 0 or not factory:
            raise OSError("RoGetActivationFactory 失败: 0x%08X" % (hr & 0xffffffff))
        qi = _vtable_fn(factory, 0, _HRESULT, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p))
        interop = ctypes.c_void_p()
        hr = qi(factory, _com_ptr(_make_guid(_IID_INTEROP)), ctypes.byref(interop))
        if hr < 0 or not interop:
            raise OSError("ISystemMediaTransportControlsInterop 不可用: 0x%08X" % (hr & 0xffffffff))
        get_for_window = _vtable_fn(
            interop, 6, _HRESULT, wt.HWND, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p))
        raw = ctypes.c_void_p()
        hr = get_for_window(interop, hwnd, _com_ptr(_make_guid(_IID_SMTC)),
                            ctypes.byref(raw))
        if hr < 0 or not raw:
            raise OSError("GetForWindow 失败: 0x%08X" % (hr & 0xffffffff))
        return Marshal.GetObjectForIUnknown(System.IntPtr(raw.value))
    finally:
        cb.WindowsDeleteString(hs)


def _enum(name, value):
    t = System.Type.GetType(name)
    return System.Enum.Parse(t, value)


def _set_thumbnail(display, artwork):
    """把封面 URL 转成 WinRT RandomAccessStreamReference."""
    if not artwork or System is None:
        return
    try:
        from System import Uri
        ref_type = System.Type.GetType(
            "Windows.Storage.Streams.RandomAccessStreamReference, "
            "Windows.Storage.Streams, ContentType=WindowsRuntime")
        method = ref_type.GetMethod("CreateFromUri")
        display.Thumbnail = method.Invoke(
            None, System.Array[System.Object]([Uri(str(artwork))]))
    except Exception:
        pass


def _set_app_icon(display):
    """把应用图标写入 SMTC 显示属性, 避免 Windows 显示空应用图标."""
    png = _app_icon_png()
    if not os.path.exists(png) or System is None:
        return
    try:
        from System import Uri
        ref_type = System.Type.GetType(
            "Windows.Storage.Streams.RandomAccessStreamReference, "
            "Windows.Storage.Streams, ContentType=WindowsRuntime")
        method = ref_type.GetMethod("CreateFromUri")
        display.AppMediaId = AUMID
        display.Thumbnail = method.Invoke(
            None, System.Array[System.Object]([Uri(png)]))
    except Exception:
        pass


def _app_icon_png():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "MusicHub", "app.png")


def _apply_controls(controls, display, info, state, position):
    """把前端传来的歌曲/播放状态写入 Windows 媒体控件."""
    display.Type = _enum(_MEDIA_TYPE, "Music")
    try:
        display.AppMediaId = AUMID
    except Exception:
        pass
    _set_app_icon(display)
    props = display.MusicProperties
    props.Title = str(info.get("title") or "未在播放")[:128]
    props.Artist = str(info.get("artist") or "—")[:128]
    props.AlbumTitle = str(info.get("album") or "MusicHub")[:128]
    try:
        props.AlbumArtist = "MusicHub"
    except Exception:
        pass
    _set_thumbnail(display, info.get("artwork"))
    display.Update()
    controls.IsEnabled = True
    controls.IsPlayEnabled = True
    controls.IsPauseEnabled = True
    controls.IsStopEnabled = True
    controls.IsNextEnabled = True
    controls.IsPreviousEnabled = True
    status = "Playing" if state == "playing" else "Paused" if state == "paused" else "Stopped"
    controls.PlaybackStatus = _enum(_MEDIA_STATUS, status)
    try:
        from System import TimeSpan
        d = max(0.0, float(position.get("duration") or 0))
        t = min(max(0.0, float(position.get("position") or 0)), d or float("inf"))
        timeline = System.Activator.CreateInstance(System.Type.GetType(
            "Windows.Media.SystemMediaTransportControlsTimelineProperties, "
            "Windows.Media, ContentType=WindowsRuntime"))
        timeline.StartTime = TimeSpan(0)
        timeline.EndTime = TimeSpan(int(d * 10000000))
        timeline.Position = TimeSpan(int(t * 10000000))
        timeline.MinSeekTime = TimeSpan(0)
        timeline.MaxSeekTime = TimeSpan(int(d * 10000000))
        controls.UpdateTimelineProperties(timeline)
    except Exception:
        pass


def set_window_icon(hwnd, hicon):
    """给主窗口补应用图标, 避免系统媒体卡片拿不到应用图标."""
    if not hwnd or not hicon:
        return False
    try:
        u32 = ctypes.windll.user32
        u32.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        u32.SendMessageW.restype = ctypes.c_ssize_t
        u32.SendMessageW(hwnd, 0x007F, 1, hicon)  # WM_SETICON, ICON_BIG
        u32.SendMessageW(hwnd, 0x007F, 0, hicon)  # ICON_SMALL
        return True
    except Exception:
        return False


def _set_window_app_id(hwnd):
    """在主窗口属性存储中写入显式 AUMID、显示名和图标资源."""
    import uuid as _uuid

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wt.DWORD)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wt.WORD), ("wReserved1", wt.WORD),
                    ("wReserved2", wt.WORD), ("wReserved3", wt.WORD),
                    ("value", ctypes.c_void_p)]

    class _Vtbl(ctypes.Structure):
        _fields_ = [(n, ctypes.c_void_p) for n in
                    ("QueryInterface", "AddRef", "Release", "GetCount", "GetAt",
                     "GetValue", "SetValue", "Commit")]

    class _Ptr(ctypes.Structure):
        _fields_ = [("v", ctypes.POINTER(_Vtbl))]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx(None, 2)
    shell32.SHGetPropertyStoreForWindow.argtypes = [
        wt.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    store = ctypes.c_void_p()
    iid = _uuid.UUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99").bytes_le
    hr = shell32.SHGetPropertyStoreForWindow(
        hwnd, _com_ptr(ctypes.create_string_buffer(iid, 16)), ctypes.byref(store))
    if hr < 0 or not store:
        return False
    fmt = _uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le
    ptr = ctypes.cast(store, ctypes.POINTER(_Ptr))
    set_value = ctypes.cast(
        ptr.contents.v.contents.SetValue,
        ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                           _PROPERTYKEY, _PROPVARIANT))
    commit = ctypes.cast(ptr.contents.v.contents.Commit,
                         ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p))
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(base, "main.py")
    ico = os.path.join(base, "assets", "app.ico")
    vals = {
        2: '"%s" "%s"' % (sys.executable.replace("python.exe", "pythonw.exe"), main_py),
        3: (ico + ",0") if os.path.exists(ico) else "",
        4: "MusicHub",
        5: AUMID,
    }
    keep = []
    for pid, text in vals.items():
        if not text:
            continue
        k = _PROPERTYKEY()
        k.fmtid = (ctypes.c_ubyte * 16)(*fmt)
        k.pid = pid
        buf = ctypes.create_unicode_buffer(text)
        keep.append(buf)
        v = _PROPVARIANT()
        v.vt = 31  # VT_LPWSTR
        v.value = ctypes.cast(buf, ctypes.c_void_p).value
        set_value(store, k, v)
    return commit(store) >= 0


def init_window(hwnd, window=None):
    """窗口 HWND 可用后绑定原生 SMTC, 并把系统按钮事件转发给前端."""
    with _smtc["lock"]:
        if _smtc["started"]:
            return True
        if not hwnd:
            return False
        try:
            _set_window_app_id(hwnd)
            controls = _get_controls(hwnd)
            display = controls.DisplayUpdater
            _smtc["controls"] = controls
            _smtc["display"] = display
            _smtc["hwnd"] = hwnd
            _smtc["started"] = True
            _smtc["error"] = ""
            _apply_controls(controls, display, _smtc["last"].get("info", {}),
                            _smtc["last"].get("state", "none"),
                            _smtc["last"].get("position", {}))
            _bind_buttons(controls, window)
            return True
        except Exception as e:
            _smtc["error"] = str(e)
            return False


def _bind_buttons(controls, window):
    """系统媒体按钮 -> 前端 window.MusicHubMedia.action(action)."""
    if window is None:
        return
    try:
        button_map = {
            "Play": "play", "Pause": "pause", "Stop": "stop",
            "Next": "next", "Previous": "prev",
            "FastForward": "seekforward", "Rewind": "seekbackward",
        }

        def on_button(sender, args):
            action = button_map.get(str(args.Button))
            if not action:
                return
            try:
                window.evaluate_js(
                    "window.MusicHubMedia && window.MusicHubMedia.action(%s)" %
                    json.dumps(action))
            except Exception:
                pass

        controls.ButtonPressed += on_button
        _smtc["handler"] = on_button
    except Exception as e:
        _smtc["error"] = "媒体按钮绑定失败: %s" % e


def update_media(info=None, state="none", position=None):
    """前端调用: 更新系统媒体卡片."""
    info = info or {}
    position = position or {}
    with _smtc["lock"]:
        _smtc["last"] = {"info": info, "state": state, "position": position}
        if not _smtc["started"]:
            return {"ok": False, "pending": True, "error": _smtc["error"]}
        try:
            _apply_controls(_smtc["controls"], _smtc["display"], info, state, position)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def clear_media():
    """前端调用: 停止播放时清空系统媒体卡片."""
    return update_media({}, "stopped", {"duration": 0, "position": 0})


def status():
    with _smtc["lock"]:
        return {"started": _smtc["started"], "hwnd": _smtc["hwnd"],
                "error": _smtc["error"]}


def _shortcut_path():
    return os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\MusicHub.lnk")


def _read_shortcut_aumid(lnk):
    """读取快捷方式的 System.AppUserModel.ID(pid 5), 失败返回空串."""
    import uuid as _uuid

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wt.DWORD)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wt.WORD), ("r1", wt.WORD), ("r2", wt.WORD),
                    ("r3", wt.WORD), ("value", ctypes.c_void_p)]

    def _guid(s):
        return _com_ptr(ctypes.create_string_buffer(_uuid.UUID(s).bytes_le, 16))

    def _vfn(ptr, i, restype, *argtypes):
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[i]
        return ctypes.cast(fn, ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes))

    if not os.path.exists(lnk):
        return ""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)
    link = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        _guid("00021401-0000-0000-C000-000000000046"), None, 1,
        _guid("000214F9-0000-0000-C000-000000000046"), ctypes.byref(link))
    if hr < 0 or not link:
        return ""
    qi = _vfn(link, 0, _HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    pf = ctypes.c_void_p()
    qi(link, _guid("0000010b-0000-0000-C000-000000000046"), ctypes.byref(pf))
    load = _vfn(pf, 5, _HRESULT, wt.LPCWSTR, wt.DWORD)
    if load(pf, lnk, 0) < 0:
        return ""
    store = ctypes.c_void_p()
    qi(link, _guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), ctypes.byref(store))
    if not store:
        return ""
    get_count = _vfn(store, 3, _HRESULT, ctypes.POINTER(wt.DWORD))
    get_at = _vfn(store, 4, _HRESULT, wt.DWORD, ctypes.POINTER(_PROPERTYKEY))
    get_value = _vfn(store, 5, _HRESULT, _PROPERTYKEY, ctypes.POINTER(_PROPVARIANT))
    count = wt.DWORD()
    if get_count(store, ctypes.byref(count)) < 0:
        return ""
    want = _uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le
    for i in range(count.value):
        k = _PROPERTYKEY()
        if get_at(store, i, ctypes.byref(k)) < 0 or k.pid != 5:
            continue
        if bytes(k.fmtid) != want:
            continue
        v = _PROPVARIANT()
        if get_value(store, k, ctypes.byref(v)) < 0:
            continue
        if v.vt == 31 and v.value:  # VT_LPWSTR
            return ctypes.wstring_at(v.value)
    return ""


def _write_shortcut(lnk, use_load):
    """创建/更新 MusicHub 快捷方式并写入 AppUserModel.ID, 返回是否成功."""
    import uuid as _uuid

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wt.DWORD)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wt.WORD), ("r1", wt.WORD), ("r2", wt.WORD),
                    ("r3", wt.WORD), ("value", ctypes.c_void_p)]

    def _guid(s):
        return _com_ptr(ctypes.create_string_buffer(_uuid.UUID(s).bytes_le, 16))

    def _vfn(ptr, i, restype, *argtypes):
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        fn = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[i]
        return ctypes.cast(fn, ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes))

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
    ole32.CoInitializeEx.restype = _HRESULT
    ole32.CoInitializeEx(None, 2)
    ole32.CoCreateInstance.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = _HRESULT
    link = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        _guid("00021401-0000-0000-C000-000000000046"), None, 1,
        _guid("000214F9-0000-0000-C000-000000000046"), ctypes.byref(link))
    if hr < 0 or not link:
        return False
    qi = _vfn(link, 0, _HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    pf = ctypes.c_void_p()
    qi(link, _guid("0000010b-0000-0000-C000-000000000046"), ctypes.byref(pf))
    if use_load and os.path.exists(lnk):
        load = _vfn(pf, 5, _HRESULT, wt.LPCWSTR, wt.DWORD)
        load(pf, lnk, 0)
    set_path = _vfn(link, 20, _HRESULT, wt.LPCWSTR)
    set_args = _vfn(link, 11, _HRESULT, wt.LPCWSTR)
    set_workdir = _vfn(link, 9, _HRESULT, wt.LPCWSTR)
    set_desc = _vfn(link, 7, _HRESULT, wt.LPCWSTR)
    set_icon = _vfn(link, 17, _HRESULT, wt.LPCWSTR, ctypes.c_int)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(base, "main.py")
    ico = os.path.join(base, "assets", "app.ico")
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    set_path(link, pythonw)
    set_args(link, '"%s"' % main_py)
    set_workdir(link, base)
    set_desc(link, "MusicHub")
    if os.path.exists(ico):
        set_icon(link, ico, 0)
    store = ctypes.c_void_p()
    qi(link, _guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), ctypes.byref(store))
    if not store:
        return False
    set_value = _vfn(store, 6, _HRESULT, _PROPERTYKEY, _PROPVARIANT)
    commit = _vfn(store, 7, _HRESULT)
    fmt = _uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le
    vals = {
        2: '"%s" "%s"' % (pythonw, main_py),
        3: (ico + ",0") if os.path.exists(ico) else "",
        4: "MusicHub",
        5: AUMID,
    }
    keep = []
    for pid, text in vals.items():
        if not text:
            continue
        k = _PROPERTYKEY()
        k.fmtid = (ctypes.c_ubyte * 16)(*fmt)
        k.pid = pid
        buf = ctypes.create_unicode_buffer(text)
        keep.append(buf)
        v = _PROPVARIANT()
        v.vt = 31  # VT_LPWSTR
        v.value = ctypes.cast(buf, ctypes.c_void_p).value
        if set_value(store, k, v) < 0:
            return False
    if commit(store) < 0:
        return False
    save = _vfn(pf, 6, _HRESULT, wt.LPCWSTR, wt.BOOL)
    return save(pf, lnk, True) >= 0


def _register_shortcut_aumid():
    """让开始菜单快捷方式显式携带 AUMID, 否则系统媒体卡片无法解析应用名/图标/启动命令."""
    if not sys.platform.startswith("win"):
        return False
    lnk = _shortcut_path()
    try:
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        # 先更新现有快捷方式; 失败则删除后重建; 每次写完都回读校验,
        # 避免 Save 静默失败或被 Shell 改写后 AUMID 失配导致卡片点击无效。
        for use_load, remove_first in ((True, False), (False, True), (False, True)):
            try:
                if remove_first and os.path.exists(lnk):
                    os.remove(lnk)
                if _write_shortcut(lnk, use_load) and _read_shortcut_aumid(lnk) == AUMID:
                    shell32 = ctypes.windll.shell32
                    shell32.SHChangeNotify.argtypes = [wt.LONG, wt.UINT,
                                                        ctypes.c_void_p, ctypes.c_void_p]
                    shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
                    return True
            except Exception as e:
                _log_smtc("写快捷方式失败(%s): %s" % (lnk, e))
        _smtc["error"] = "快捷方式 AUMID 校验失败: %s" % _read_shortcut_aumid(lnk)
        return False
    except Exception as e:
        _smtc["error"] = "注册快捷方式 AUMID 失败: %s" % e
        return False


def _log_smtc(msg):
    try:
        from sources import server
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
        with open(os.path.join(server.DATA_DIR, "musichub.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass



def _register_activator(winreg):
    """注册 CustomActivator COM 类(LocalServer32), 供 AUMID 激活唤起 MusicHub.

    媒体卡片点击时, Shell 读取 AppUserModelId\\{AUMID}\\CustomActivator 得到 CLSID,
    再按 CLSID\\InprocServer32/LocalServer32 拉起 COM 服务器完成激活。
    激活器进程(sources/com_activator.py)被拉起后唤醒主窗口并自行退出。
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    activator = os.path.join(base, "sources", "com_activator.py")
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    local_server = '"%s" "%s" %s' % (pythonw, activator, ACTIVATOR_CLSID)
    clsid = ACTIVATOR_CLSID.strip("{}")

    def setv(path, name, value, typ=winreg.REG_SZ):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, typ, value)

    # 1) AUMID -> CLSID 映射
    setv(r"Software\Classes\AppUserModelId\%s" % AUMID,
         "CustomActivator", ACTIVATOR_CLSID)
    # 2) CLSID 描述 + LocalServer32 (COM 本地服务器)
    setv(r"Software\Classes\CLSID\%s" % clsid,
         "", "MusicHub Media Activator")
    setv(r"Software\Classes\CLSID\%s\LocalServer32" % clsid,
         "", local_server)
    # 3) AppID DllSurrogate 为空(允许非 DLL 本地服务器) — 对 LocalServer32 非必需, 但兼容激活查询
    setv(r"Software\Classes\AppID\%s" % clsid, "DllSurrogate", "")

    # 4) 同时按 exe 绝对路径注册 AUMID 入口(IAAM 对自定义字符串不识别,
    #    只认 AppsFolder 里以可执行文件路径为 AppID 的桌面应用)
    if os.path.exists(activator):
        setv(r"Software\Classes\AppUserModelId\%s" % activator,
             "DisplayName", "MusicHub", winreg.REG_EXPAND_SZ)
        setv(r"Software\Classes\AppUserModelId\%s" % activator,
             "IconUri", os.path.join(base, "assets", "app.ico") + ",0",
             winreg.REG_EXPAND_SZ)
        setv(r"Software\Classes\AppUserModelId\%s" % activator,
             "CustomActivator", ACTIVATOR_CLSID)


def register_app_identity():
    """为显式 AUMID 注册显示名和图标, 供 Windows 媒体卡片解析应用身份."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ico = os.path.join(base, "assets", "app.ico")
        icon_dir = os.path.join(os.environ.get("LOCALAPPDATA", base), "MusicHub")
        os.makedirs(icon_dir, exist_ok=True)
        png = os.path.join(icon_dir, "app.png")
        if os.path.exists(ico) and (not os.path.exists(png) or
                                    os.path.getmtime(ico) > os.path.getmtime(png)):
            from PIL import Image
            with Image.open(ico) as im:
                im.save(png)
        key_path = r"Software\Classes\AppUserModelId\%s" % AUMID
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_EXPAND_SZ, "MusicHub")
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_EXPAND_SZ, png)
            winreg.SetValueEx(key, "IconBackgroundColor", 0, winreg.REG_SZ, "#12121A")
        _register_activator(winreg)
        _register_shortcut_aumid()
        return True
    except Exception as e:
        _smtc["error"] = "注册应用身份失败: %s" % e
        return False


def configure_browser_flags():
    """关闭 WebView2/Chromium 自动媒体会话, 让原生 MusicHub 会话接管."""
    flags = []
    for key in ("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS_MUSICHUB"):
        v = os.environ.get(key, "")
        if v:
            flags.append(v.strip())
    flags.append("--disable-features=MediaSessionService,HardwareMediaKeyHandling,GlobalMediaControls")
    # 暴露 window.gc(), 供前端主动回收 V8 堆(长时间使用后内存回落)
    flags.append("--js-flags=--expose-gc")
    joined = " ".join(dict.fromkeys(flags))
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = joined
    return joined
