# -*- coding: utf-8 -*-
"""登录凭据加密存储.

Windows: DPAPI(CryptProtectData), 绑定当前 Windows 用户.
其他平台(Android/Linux): 应用私有目录已由系统沙箱隔离, 这里退化为带标记的
明文 JSON, 对外保持相同的 save_json / load_json 接口.

文件格式: MAGIC + 载荷(Windows 为 DPAPI 密文, 其他平台为 UTF-8 JSON).
兼容旧版明文 JSON: 读取成功后立即改写为新格式.
"""
import json
import os
import threading

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    MAGIC = b"MHDPAPI1\n"
    _ENTROPY = b"MusicHub.cookie.v1"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wt.LPCWSTR,
                                          ctypes.POINTER(_Blob), ctypes.c_void_p,
                                          ctypes.c_void_p, wt.DWORD,
                                          ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.restype = wt.BOOL
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
                                            ctypes.POINTER(_Blob), ctypes.c_void_p,
                                            ctypes.c_void_p, wt.DWORD,
                                            ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = wt.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _in_blob(data):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _take(out):
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(data):
        inp, _b1 = _in_blob(data)
        ent, _b2 = _in_blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(inp), "MusicHub",
                                         ctypes.byref(ent), None, None,
                                         _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise ctypes.WinError()
        return _take(out)

    def unprotect(data):
        inp, _b1 = _in_blob(data)
        ent, _b2 = _in_blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent),
                                           None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                                           ctypes.byref(out)):
            raise ctypes.WinError()
        return _take(out)

else:
    # 非 Windows(Android/Linux): 依赖系统应用沙箱隔离, 不做额外加密
    MAGIC = b"MHPLAIN1\n"

    def protect(data):
        return data

    def unprotect(data):
        return data

# 按目标路径串行化写入: 同一文件的并发 save_json 不会争抢同一个 .tmp
_path_locks = {}
_path_locks_guard = threading.Lock()


def _path_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _path_locks_guard:
        lk = _path_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _path_locks[key] = lk
        return lk


def save_json(path, obj):
    """加密写入(按路径加锁 + 唯一临时文件 + 同目录原子替换)."""
    blob = MAGIC + protect(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    with _path_lock(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        try:
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_json(path):
    """读取凭据; 文件不存在/无法解密返回 None. 旧版明文 JSON 读出后迁移为新格式."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        if raw.startswith(MAGIC):
            return json.loads(unprotect(raw[len(MAGIC):]).decode("utf-8"))
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    try:
        save_json(path, obj)
    except Exception:
        pass
    return obj
