# -*- coding: utf-8 -*-
"""MusicHub COM 激活器: 承接 Windows 媒体卡片点击的 AUMID 激活.

注册为 AppUserModelId\\{AUMID}\\CustomActivator 指向的 LocalServer32 COM 类。
当系统通过 IApplicationActivationManager::ActivateApplication 激活 MusicHub 时,
本进程被 COM 拉起 -> 立即唤醒 MusicHub 主窗口 -> 注册类工厂应付激活握手 -> 自行退出。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
import uuid

ole32 = ctypes.windll.ole32
user32 = ctypes.windll.user32

S_OK = 0
E_NOINTERFACE = 0x80004002
E_POINTER = 0x80004003
CLASS_E_NOAGGREGATION = 0x80040110
CLSCTX_LOCAL_SERVER = 4
REGCLS_MULTIPLEUSE = 1
WM_SHOW_APP = 0x8002

IID_IUNKNOWN = "00000000-0000-0000-C000-000000000046"
IID_ICLASSFACTORY = "00000001-0000-0000-C000-000000000046"


def _guid_bytes(s):
    return uuid.UUID(s.strip("{}")).bytes_le


def _guid_ptr(s):
    return ctypes.cast(ctypes.create_string_buffer(_guid_bytes(s), 16),
                       ctypes.c_void_p)


def wake_musichub():
    """唤醒 MusicHub: 托盘窗口消息 -> 主窗口直接恢复 -> 快捷方式兜底."""
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowW.restype = wt.HWND
    tray = user32.FindWindowW("MusicHubTrayWindow", None)
    if tray:
        user32.PostMessageW(tray, WM_SHOW_APP, 0, 0)
        return "tray"
    h = user32.FindWindowW(None, "MusicHub")
    if h:
        user32.ShowWindow(h, 9)  # SW_RESTORE
        user32.ShowWindow(h, 5)  # SW_SHOW
        user32.SetForegroundWindow(h)
        return "window"
    lnk = os.path.join(os.environ.get("APPDATA", ""),
                       r"Microsoft\Windows\Start Menu\Programs\MusicHub.lnk")
    if os.path.exists(lnk):
        os.startfile(lnk)
        return "shortcut"
    return "none"


# ---- COM 对象(裸 vtable): 任何 QI 都返回 IUnknown, CreateInstance 直接唤醒 ----
_wake_done = [False]


def _wake_once():
    if not _wake_done[0]:
        _wake_done[0] = True
        wake_musichub()


_cb_ref = [1]


def _cb_qi(this, riid, ppv):
    if not ppv:
        return E_POINTER
    ppv[0] = this
    _cb_ref[0] += 1
    _wake_once()
    return S_OK


def _cb_addref(this):
    _cb_ref[0] += 1
    return _cb_ref[0]


def _cb_release(this):
    _cb_ref[0] -= 1
    return _cb_ref[0]


_CB_QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
_CB_AR = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


class _CbVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _CB_QI), ("AddRef", _CB_AR),
                ("Release", _CB_AR)]


class _CbObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_CbVtbl))]


_cb_vtbl = _CbVtbl(QueryInterface=_CB_QI(_cb_qi),
                    AddRef=_CB_AR(_cb_addref),
                    Release=_CB_AR(_cb_release))
_cb_obj = _CbObj(lpVtbl=ctypes.pointer(_cb_vtbl))

_cf_ref = [1]


def _cf_qi(this, riid, ppv):
    if not ppv:
        return E_POINTER
    b = ctypes.string_at(riid, 16)
    if b in (_guid_bytes(IID_IUNKNOWN), _guid_bytes(IID_ICLASSFACTORY)):
        ppv[0] = this
        _cf_ref[0] += 1
        return S_OK
    ppv[0] = None
    return E_NOINTERFACE


def _cf_addref(this):
    _cf_ref[0] += 1
    return _cf_ref[0]


def _cf_release(this):
    _cf_ref[0] -= 1
    return _cf_ref[0]


def _cf_create(this, pUnkOuter, riid, ppv):
    if pUnkOuter:
        return CLASS_E_NOAGGREGATION
    if not ppv:
        return E_POINTER
    ppv[0] = ctypes.cast(ctypes.pointer(_cb_obj), ctypes.c_void_p)
    _cb_addref(None)
    _wake_once()
    return S_OK


def _cf_lock(this, fLock):
    return S_OK


_CF_QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
_CF_AR = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_CF_CI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_CF_LK = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_bool)


class _CfVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _CF_QI), ("AddRef", _CF_AR),
                ("Release", _CF_AR), ("CreateInstance", _CF_CI),
                ("LockServer", _CF_LK)]


class _CfObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_CfVtbl))]


_cf_vtbl = _CfVtbl(QueryInterface=_CF_QI(_cf_qi),
                    AddRef=_CF_AR(_cf_addref),
                    Release=_CF_AR(_cf_release),
                    CreateInstance=_CF_CI(_cf_create),
                    LockServer=_CF_LK(_cf_lock))
_cf_obj = _CfObj(lpVtbl=ctypes.pointer(_cf_vtbl))


def _auto_exit():
    """注册完成后等待 COM 握手, 超时自行退出, 不留孤儿进程."""
    time.sleep(20)
    user32.PostQuitMessage(0)


def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    clsid = sys.argv[1]
    # 激活器被拉起的第一职责就是唤醒主窗口, 立即执行
    _wake_once()
    ole32.CoInitializeEx(None, 2)  # COINIT_MULTITHREADED
    ole32.CoRegisterClassObject.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD,
        ctypes.POINTER(wt.DWORD)]
    ole32.CoRegisterClassObject.restype = ctypes.c_long
    reg = wt.DWORD()
    hr = ole32.CoRegisterClassObject(
        _guid_ptr(clsid),
        ctypes.cast(ctypes.pointer(_cf_obj), ctypes.c_void_p),
        CLSCTX_LOCAL_SERVER, REGCLS_MULTIPLEUSE, ctypes.byref(reg))
    if hr < 0:
        sys.exit(1)
    threading.Thread(target=_auto_exit, daemon=True).start()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
