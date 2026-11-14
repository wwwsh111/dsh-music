"""系统音频律动 —— 照 dsh-bgm 的路子，直接听默认输出设备的回环。

从 dsh-bgm 借的四件事：
1. 不挑播放器：采的是「系统正在输出的声音」，QQ 音乐 / 网易云 / 浏览器 / 游戏
   都是同一条链路，不用逐个适配；
2. 只出标量：PCM 只在本机内存里过一遍，对外只给响度、三个频段、起音、BPM
   和拍内相位，不落盘也不上传；
3. 轻量：一帧一次 FFT，不建 DOM、不逐字镜像；
4. 安静就安静：没声音时 playing=false，前端不假装有节奏。

服务对象不同：dsh-bgm 把节奏喂给会话界面（波面 / 判定 / 结算），
dsh-music 把节奏喂给制作 —— 面板跟着正在放的歌呼吸，
music_listen 把「我刚才听到的那首」抓下来当参考曲或采样。

关于单位：频段一律用「功率」而不是幅度。宽带信号（踩镲、镲片、人声齿音）
的能量摊在几百个 bin 上，按幅度求和会被严重低估，按功率求和才和响度对得上。
"""
from __future__ import annotations

import math
import platform
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

WINDOW = 2048          # 43 ms @ 48k：够分出低频，也够跟拍
CLIP_DB = 60.0         # 动态范围：-60 dB → 0.0，0 dB → 1.0
BANDS = 12             # 面板上那 12 根柱子
BEAT_MIN = 0.28        # 拍间隔下限 ≈ 214 BPM
BEAT_MAX = 1.20        # 上限 ≈ 50 BPM
IDLE_SECONDS = 90.0    # 没人读就走人，别一直占着声卡


def _adb(x):
    """幅度 → dB。"""
    return 20.0 * math.log10(max(float(x), 1e-9))


def _pdb(x):
    """功率 → dB。"""
    return 10.0 * math.log10(max(float(x), 1e-12))


def _norm_db(db, lo=-CLIP_DB, hi=0.0):
    return float(min(1.0, max(0.0, (db - lo) / (hi - lo))))


def _pick_backend():
    """Windows 优先 WASAPI 回环；别的平台试 sounddevice 的回环/监视设备。"""
    if platform.system() == "Windows":
        try:
            import pyaudiowpatch  # noqa: F401
            return "wasapi"
        except Exception:
            pass
    try:
        import sounddevice  # noqa: F401
        return "sounddevice"
    except Exception:
        pass
    return ""


def _hint(backend=""):
    system = platform.system()
    if backend == "wasapi" or system == "Windows":
        return "确认默认输出设备正在出声；没装采集库时先 pip install PyAudioWPatch。"
    if system == "Darwin":
        return "macOS 没有内置回环：先装 BlackHole 并把系统输出送到它，sounddevice 才听得到。"
    if system == "Linux":
        return "Linux 需要 PulseAudio/PipeWire 的 monitor 设备；没有回环设备时只能听麦克风。"
    return "当前平台没有可用的系统回环采集。"


def _loopback_device_sd(sd):
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for d in devices:
        if not d.get("max_input_channels"):
            continue
        name = str(d.get("name") or "").lower()
        if any(k in name for k in ("blackhole", "monitor", "loopback", "virtual",
                                   "vb-audio", "cable")):
            return d
    return None


class Capture:
    """常驻采集：后台线程只做「读设备 → 塞进块队列」，分析交给消费者。"""

    BLOCK = 1024

    def __init__(self, keep=10.0):
        self.keep = float(keep)
        self.sr = 48000
        self.ch = 2
        self.device = ""
        self.backend = _pick_backend()
        self.error = ""
        self._blocks = deque(maxlen=int(self.keep * 48000 / self.BLOCK) + 8)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_use = 0.0

    @property
    def running(self):
        t = self._thread
        return bool(t and t.is_alive())

    def start(self):
        if self.running:
            return True
        if not self.backend:
            self.error = "没有可用的系统音频回环后端"
            return False
        self._stop.clear()
        self.error = ""
        self._last_use = time.time()
        with self._lock:
            self._blocks.clear()
        self._thread = threading.Thread(target=self._run, name="music-live", daemon=True)
        self._thread.start()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self.error or self._size():
                break
            time.sleep(0.02)
        if self.error:
            self._thread = None
            return False
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def _size(self):
        with self._lock:
            return len(self._blocks)

    def buffered(self):
        """缓冲里现在有多少帧（约等于「已经积累了几秒」）。"""
        with self._lock:
            return sum(int(b.size) for b in self._blocks)

    def _run(self):
        try:
            if self.backend == "wasapi":
                self._run_wasapi()
            else:
                self._run_sounddevice()
        except Exception as e:  # 设备被拔、独占模式、权限……
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._thread = None

    def _run_wasapi(self):
        import pyaudiowpatch as pa

        p = pa.PyAudio()
        try:
            try:
                info = next(p.get_loopback_device_info_generator())
            except StopIteration:
                raise RuntimeError("系统没有可用的 WASAPI 回环设备")
            self.sr = int(info["defaultSampleRate"]) or 48000
            self.ch = max(1, min(2, int(info["maxInputChannels"]) or 2))
            self.device = str(info.get("name") or "")
            stream = p.open(format=pa.paFloat32, channels=self.ch, rate=self.sr,
                            input=True, input_device_index=info["index"],
                            frames_per_buffer=self.BLOCK)
            try:
                while not self._stop.is_set():
                    if self._last_use and time.time() - self._last_use > IDLE_SECONDS:
                        break
                    raw = stream.read(self.BLOCK, exception_on_overflow=False)
                    self._push(np.frombuffer(raw, dtype=np.float32))
            finally:
                stream.stop_stream()
                stream.close()
        finally:
            p.terminate()

    def _run_sounddevice(self):
        import sounddevice as sd

        dev = _loopback_device_sd(sd)
        if dev is None:
            raise RuntimeError("没有找到回环/监视设备（macOS 请先装 BlackHole）")
        self.device = str(dev.get("name") or "")
        self.sr = int(dev.get("default_samplerate") or 48000)
        self.ch = max(1, min(2, int(dev.get("max_input_channels") or 1)))
        with sd.InputStream(device=dev["index"], channels=self.ch, samplerate=self.sr,
                            blocksize=self.BLOCK, dtype="float32") as stream:
            while not self._stop.is_set():
                if self._last_use and time.time() - self._last_use > IDLE_SECONDS:
                    break
                data, _ = stream.read(self.BLOCK)
                self._push(np.asarray(data, dtype=np.float32).reshape(-1))

    def _push(self, x):
        if x is None or x.size == 0:
            return
        if self.ch > 1 and x.size % self.ch == 0:
            x = x.reshape(-1, self.ch).mean(axis=1)
        with self._lock:
            self._blocks.append(x.astype(np.float32, copy=False))

    def read(self, seconds=None):
        with self._lock:
            blocks = list(self._blocks)
        self._last_use = time.time()
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        if seconds is None:
            return np.concatenate(blocks)
        need = max(1, int(float(seconds) * self.sr))
        out, got = [], 0
        for b in reversed(blocks):
            out.append(b)
            got += b.size
            if got >= need:
                break
        x = np.concatenate(out[::-1])
        return x[-need:]


class Analyzer:
    """一帧样本 → 一组标量。窗口 2048，帧率由调用方决定（面板用 30 Hz）。"""

    def __init__(self, sr=48000, fps=30.0):
        self.sr = int(sr or 48000)
        self._step = 1.0 / max(1.0, float(fps or 30.0))
        self._win = np.hanning(WINDOW).astype(np.float32)
        freqs = np.fft.rfftfreq(WINDOW, 1.0 / self.sr)
        self._freqs = freqs
        top = min(self.sr / 2.0 - 200.0, 14000.0)
        edges = np.geomspace(40.0, max(top, 2000.0), BANDS + 1)
        self._bands = [(freqs >= edges[i]) & (freqs < edges[i + 1]) for i in range(BANDS)]
        self._low = (freqs >= 30) & (freqs < 200)
        self._mid = (freqs >= 200) & (freqs < 2000)
        self._high = freqs >= 2000
        self._prev_mag = None
        self._flux = deque(maxlen=48)       # 短窗，用来定阈值
        self._env = deque(maxlen=200)       # 6 秒，用来估周期
        self._onsets = deque(maxlen=64)
        self._cand = deque(maxlen=9)        # 最近几次 BPM 候选，取中位数
        self._agree = 0                     # 偏离当前速度的次数
        self.bpm = 0.0
        self._last_beat = 0.0
        self._prev_t = 0.0
        self._silent = 0.0
        self._playing = False
        self.env = {"rms": 0.0, "bass": 0.0, "mid": 0.0, "treble": 0.0, "onset": 0.0}
        self.env_bands = [0.0] * BANDS

    def _update_bpm(self):
        """用起音包络的自相关找主周期。

        直方图法在 16 分踩镲上会锁定到半拍（电音尤其常见），
        自相关在整拍处有更大的峰，再对半速/倍速做一次归一到 70–190。
        """
        rho = np.asarray(self._env, dtype=np.float64)
        if rho.size < int(2.2 / self._step):
            return
        rho = rho - rho.mean()
        if not np.any(rho):
            return
        ac = np.correlate(rho, rho, "full")[rho.size - 1:]
        if ac[0] <= 0:
            return
        ac = ac / ac[0]
        lo = max(1, int(round(BEAT_MIN / self._step)))
        hi = min(len(ac) - 2, int(round(BEAT_MAX / self._step)))
        if hi <= lo:
            return
        lag = lo + int(np.argmax(ac[lo:hi + 1]))
        y0, y1, y2 = ac[lag - 1], ac[lag], ac[lag + 1]
        den = y0 - 2.0 * y1 + y2
        if abs(den) > 1e-9:                 # 抛物线插值，抹掉整帧台阶
            lag = lag + 0.5 * (y0 - y2) / den
        cand = 60.0 / (max(lag, 1e-6) * self._step)
        while cand > 190.0:
            cand /= 2.0
        while cand < 70.0:
            cand *= 2.0
        self._cand.append(cand)
        if not self.bpm:
            self.bpm = round(float(np.median(self._cand)), 1)
            return
        # 系统回环里随时会混进别的声音（通知音、会议、另一首歌），
        # 单帧候选会突然跳到别的速度上。用中位数稳住，偏差过大要连续 8 次才肯换速度。
        ratio = cand / self.bpm if self.bpm else 1.0
        if 0.82 <= ratio <= 1.22:
            self._agree = 0
            self.bpm = round(float(np.median(list(self._cand)[-5:])), 1)
        else:
            self._agree += 1
            if self._agree >= 8:
                self._agree = 0
                self._cand.clear()
                self._cand.append(cand)
                self.bpm = round(cand, 1)

    def push(self, x, now=None):
        now = time.time() if now is None else float(now)
        seg = np.zeros(WINDOW, dtype=np.float32)
        n = min(int(x.size) if x is not None else 0, WINDOW)
        if n > 0:
            seg[-n:] = np.asarray(x, dtype=np.float32)[-n:]
        spec = np.fft.rfft(seg * self._win)
        mag = np.abs(spec)
        power = (mag ** 2) / float(WINDOW)   # 各 bin 功率之和 ≈ 帧功率
        rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))

        def energy(mask):
            return float(power[mask].sum()) if mask.any() else 0.0

        bass = _norm_db(_pdb(energy(self._low)))
        mid = _norm_db(_pdb(energy(self._mid)))
        treble = _norm_db(_pdb(energy(self._high)))

        raw = [_pdb(energy(m)) for m in self._bands]
        med = float(np.median(raw))
        loud = rms > 0.0035
        # 静音时所有段都等于中位数，归一化会给出一个假的 0.6 —— 静音帧按 0 算。
        # 归一化分两截：形状看「相对中位数」，音量看响度包络。
        # 只按绝对 dB 映射，安静的歌会整排趴地；只按中位数，一排柱子又永远一样高。
        gain = 0.45 + 0.55 * self.env["rms"]
        targets_band = ([min(1.0, max(0.0, (0.6 + (v - med) / 40.0) * gain)) for v in raw]
                        if loud else [0.0] * BANDS)
        # 不能逐帧硬跳零：系统回环里鼓点之间本来就有几十毫秒真空，
        # 硬门控会让柱子以几 Hz 抽搐。和响度用同一套攻/放，柱子才是「呼吸」。
        bands = []
        for i, v in enumerate(targets_band):
            p = self.env_bands[i]
            self.env_bands[i] = p + (v - p) * (0.55 if v > p else 0.10)
            bands.append(self.env_bands[i])

        if self._prev_mag is None:
            flux = 0.0
        else:
            flux = float(np.maximum(0.0, mag - self._prev_mag).sum()
                         / (float(mag.sum()) + 1e-9))
        self._prev_mag = mag
        self._flux.append(flux)
        self._env.append(flux)
        thr = (float(np.median(self._flux)) if self._flux else 0.0) * 1.6 + 0.015
        beat = False
        if flux > thr and now - self._last_beat > 0.16:
            beat = True
            self._last_beat = now
            self._onsets.append(now)
            self._update_bpm()

        top = max(0.02, max(self._flux) if self._flux else 0.02)
        onset = min(1.0, flux / top) if loud else 0.0

        dt = max(0.0, now - self._prev_t) if self._prev_t else self._step
        self._prev_t = now
        if rms > 0.0035:
            self._silent = 0.0
            self._playing = True
        else:
            self._silent += dt
            if self._silent > 0.8:
                self._playing = False

        targets = {"rms": _norm_db(_adb(rms)), "bass": bass, "mid": mid,
                   "treble": treble, "onset": onset}
        for k, v in targets.items():
            p = self.env[k]
            self.env[k] = p + (v - p) * (0.65 if v > p else 0.18)

        phase = 0.0
        if self.bpm > 40 and self._last_beat:
            beat_len = 60.0 / self.bpm
            phase = ((now - self._last_beat) % beat_len) / beat_len

        return {
            "playing": self._playing,
            "rms": round(self.env["rms"], 3),
            "bass": round(self.env["bass"], 3),
            "mid": round(self.env["mid"], 3),
            "treble": round(self.env["treble"], 3),
            "onset": round(self.env["onset"], 3),
            "bpm": round(self.bpm, 1),
            "phase": round(phase, 3),
            "beat": bool(beat) and self._playing,
            "bands": [round(v, 3) for v in bands],
        }


_source = None
_source_lock = threading.Lock()


def source():
    global _source
    with _source_lock:
        if _source is None:
            _source = Capture(keep=35.0)
        return _source


def probe():
    cap = source()
    ok = cap.start()
    return {
        "ok": bool(ok),
        "backend": cap.backend or "none",
        "device": cap.device,
        "sampleRate": cap.sr,
        "channels": cap.ch,
        "error": cap.error,
        "hint": "" if ok else _hint(cap.backend),
    }


def stream(fps=30, session_seconds=None):
    """按 fps 产出特征事件；SSE 处理器一行一个 data: {...}。"""
    cap = source()
    if not cap.start():
        yield {"ok": False, "playing": False, "error": cap.error or "采集没起来",
               "hint": _hint(cap.backend)}
        return
    ana = Analyzer(cap.sr, fps=fps)
    step = 1.0 / max(1.0, min(60.0, float(fps or 30)))
    deadline = time.time() + float(session_seconds) if session_seconds else None
    last = 0.0
    while True:
        now = time.time()
        if deadline and now >= deadline:
            return
        wait = step - (now - last)
        if wait > 0:
            time.sleep(min(wait, 0.05))
            continue
        ev = ana.push(cap.read(step * 1.1), now)
        ev["t"] = round(now, 3)
        yield ev
        last = now


def _default_grab():
    from .config import ROOT
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return ROOT / "live" / f"live-{stamp}.wav"


def snapshot(seconds=8.0, grab=False, out="", analyze=True):
    """抓一段正在播放的系统声音：给分析结果，可选留成 wav。"""
    import tempfile

    import soundfile as sf

    seconds = max(1.0, min(30.0, float(seconds or 8)))
    cap = source()
    if not cap.start():
        return {"ok": False, "playing": False, "error": cap.error or "采集没起来",
                "hint": _hint(cap.backend)}
    need = int(seconds * cap.sr * 0.9)
    deadline = time.time() + seconds + 1.5
    while time.time() < deadline:
        if cap.buffered() >= need:
            break
        time.sleep(0.05)
    x = cap.read(seconds)
    if x.size == 0:
        return {"ok": False, "playing": False, "error": "没抓到数据"}
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    playing = rms > 0.0035
    tmpdir = Path(tempfile.mkdtemp(prefix="dsh-live-"))
    tmp = tmpdir / "grab.wav"
    try:
        sf.write(str(tmp), np.clip(x, -1, 1), cap.sr)
        result = {
            "ok": True,
            "playing": playing,
            "seconds": round(x.size / cap.sr, 2),
            "rms": round(rms, 4),
            "device": cap.device,
            "backend": cap.backend,
            "sampleRate": cap.sr,
        }
        if not playing:
            result["note"] = "这一段基本是静音：确认默认输出设备正在放声音。"
        if analyze and playing:
            from . import analyze as analyze_mod
            try:
                a = analyze_mod.analyze(str(tmp))
                result["analysis"] = {k: a.get(k) for k in
                                      ("bpm", "key", "loudness", "duration", "warnings")}
            except Exception as e:
                result["analysis"] = {"error": f"{type(e).__name__}: {e}"}
        if grab:
            dst = Path(out).expanduser() if out else _default_grab()
            dst.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(dst), np.clip(x, -1, 1), cap.sr)
            result["grabbed"] = str(dst)
        return result
    finally:
        import shutil as _shutil
        try:
            _shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass
