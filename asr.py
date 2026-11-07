# -*- coding: utf-8 -*-
"""系统音频回环实时语音识别(FunASR paraformer 流式中文模型).

用 pyaudiowpatch 的 WASAPI loopback 捕获当前正在播放的声音, 重采样为 16kHz
单声道后送入 FunASR 流式模型; 模型从国内 ModelScope 自动下载, 无需 API Key。
输出定稿字幕段和正在识别的半句, 由前端映射到播放进度。
"""
import audioop
import json
import os
import threading
import time

_lock = threading.Lock()
_ctl = threading.Lock()   # 串行化 start/stop, 防并发启停竞态
_model = None            # 进程级缓存(首次加载/下载模型需要一些时间)
_thread = None
_stop = None
_state = {"running": False, "error": ""}
_seq = 0
_segments = []           # [{seq, t, text}] 环形保留最近 _MAX_KEEP 条
_partial = ""            # 当前未定稿的半句
_MAX_KEEP = 400

_TARGET_RATE = 16000
_CHUNK_SECONDS = 0.6     # paraformer streaming 标准 600ms 步长
_CHUNK_BYTES = int(_TARGET_RATE * _CHUNK_SECONDS) * 2


def _load_model():
    global _model
    if _model is not None:
        return _model
    import torch
    from funasr import AutoModel
    torch.set_num_threads(8)
    _model = AutoModel(model="paraformer-zh-streaming", device="cpu",
                       disable_update=True)
    return _model


def _pick_loopback(p):
    """优先取默认输出设备对应的回环。
    回环设备名通常带 " [Loopback]" 后缀, 直接等值比较会失配而选错无声设备."""
    want = None
    try:
        want = (p.get_default_output_device_info() or {}).get("name")
    except Exception:
        pass

    def norm(name):
        return (name or "").replace("[Loopback]", "").strip().lower()

    first = None
    for info in p.get_loopback_device_info_generator():
        if first is None:
            first = info
        if want and norm(info.get("name")) == norm(want):
            return info
    return first


def _clean(text):
    # FunASR 中文一般无空格; 去掉空白和首尾空白, 纯标点丢弃
    t = "".join((text or "").split())
    return t if any("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in t) else ""


def _run(stop_ev):
    """识别线程: 停止事件经参数传入, 不读全局 _stop, 避免新旧实例竞态覆盖."""
    global _thread, _stop, _seq, _partial
    err = ""
    utterance = ""
    silence_since = None

    def finish_sentence():
        nonlocal utterance
        global _seq, _partial
        text = _clean(utterance)
        utterance = ""
        _partial = ""
        if text:
            with _lock:
                _seq += 1
                _segments.append({"seq": _seq, "t": time.time(), "text": text})
                del _segments[:-_MAX_KEEP]

    try:
        import numpy as np
        import pyaudiowpatch as pyaudio
        model = _load_model()
        p = pyaudio.PyAudio()
        try:
            dev = _pick_loopback(p)
            if not dev:
                raise RuntimeError("未找到 WASAPI 回环设备")
            rate = int(dev.get("defaultSampleRate") or 48000)
            chunk = 2400
            # WASAPI 回环通常只允许按设备声道数(2)打开; 识别器要 16k 单声道
            channels = 2 if int(dev.get("maxInputChannels") or 2) >= 2 else 1
            stream = p.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                            input_device_index=dev["index"], frames_per_buffer=chunk)
            try:
                cache = {}
                pcm = bytearray()
                while not stop_ev.is_set():
                    data = stream.read(chunk, exception_on_overflow=False)
                    if channels == 2:
                        data = audioop.tomono(data, 2, 0.5, 0.5)
                    data, _ = audioop.ratecv(data, 2, 1, rate, _TARGET_RATE, None)
                    pcm.extend(data)
                    if len(pcm) < _CHUNK_BYTES:
                        continue
                    block = bytes(pcm[:_CHUNK_BYTES])
                    del pcm[:_CHUNK_BYTES]
                    samples = np.frombuffer(block, dtype=np.int16)
                    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
                    now = time.time()
                    if rms > 250:
                        silence_since = None
                    elif silence_since is None:
                        silence_since = now
                    wave = samples.astype(np.float32) / 32768.0
                    res = model.generate(input=wave, cache=cache, is_final=False,
                                         chunk_size=[0, 10, 5],
                                         encoder_chunk_look_back=4,
                                         decoder_chunk_look_back=1)
                    text = _clean(res[0].get("text") if res else "")
                    if text:
                        # paraformer streaming 每块返回增量文本, 拼成当前句
                        utterance += text
                        _partial = utterance
                    # 停顿收尾, 或句子过长时主动切行, 防止字幕越滚越长
                    if utterance and ((silence_since is not None and now - silence_since > 1.0)
                                      or len(utterance) > 64):
                        finish_sentence()
                        silence_since = None
            finally:
                stream.stop_stream()
                stream.close()
        finally:
            p.terminate()
    except Exception as e:
        err = str(e)
    if utterance:
        finish_sentence()
    with _lock:
        _state["running"] = False
        _state["error"] = err
    # 身份判断后再清全局引用: 若 start() 已装入新实例, 绝不覆盖新句柄
    if _stop is stop_ev:
        _stop = None
    if _thread is threading.current_thread():
        _thread = None


def start():
    global _thread, _stop, _seq, _partial
    with _ctl:
        with _lock:
            if _state["running"]:
                return {"ok": True, "running": True}
        th = _thread
        if th is not None and th.is_alive():
            # 上一次实例仍在收尾: 先置停并等待, 避免双线程同时占用回环设备
            ev = _stop
            if ev is not None:
                ev.set()
            try:
                th.join(5)
            except Exception:
                pass
        try:
            _load_model()
        except Exception as e:
            return {"error": "加载 FunASR 模型失败: %s" % e}
        with _lock:
            _segments.clear()
            _seq = 0
            _state["running"] = True
            _state["error"] = ""
        _partial = ""
        ev = threading.Event()
        _stop = ev
        _thread = threading.Thread(target=_run, args=(ev,), name="MusicHubFunASR", daemon=True)
        _thread.start()
        return {"ok": True, "running": True, "engine": "funasr"}


def stop():
    with _ctl:
        ev = _stop
        th = _thread
        if ev:
            ev.set()
        if th and th.is_alive() and th is not threading.current_thread():
            try:
                th.join(3)
            except Exception:
                pass
        with _lock:
            _state["running"] = False
        return {"ok": True, "running": False}


def poll(after):
    try:
        after = int(after)
    except Exception:
        after = 0
    with _lock:
        out = [s for s in _segments if s["seq"] > after][-100:]
        return {"running": _state["running"], "error": _state["error"],
                "segments": out, "partial": _partial, "seq": _seq,
                "engine": "funasr"}
