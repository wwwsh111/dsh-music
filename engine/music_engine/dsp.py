"""DSP 工具箱：测量、包络、饱和、声像、总线处理。

为什么单独一个模块：渲染（render.py）和混音（mix.py）都要用同样的
测量和音色处理；放在一起才不会出现「渲染时一种 EQ、混音时另一种」。

所有函数都写成「输入 float32 (n, ch) → 输出同样 shape」，方便串联。
"""

import math
from pathlib import Path

import numpy as np

SR = 48000


# ------------------------------------------------------------------ 基础

def db_to_gain(d):
    return 10.0 ** (float(d) / 20.0)


def gain_to_db(g):
    return 20.0 * math.log10(max(float(g), 1e-9))


def load(path, sr=None):
    import soundfile as sf
    data, rate = sf.read(str(path), always_2d=True, dtype="float32")
    if sr and rate != sr:
        data = resample(data, rate, sr)
        rate = sr
    return data, rate


# ------------------------------------------------------------------ 箱体 IR
# AK-SPKRS (CC BY 4.0, adventurekid.se) 的真实 4x12 箱体脉冲响应，
# 已转成 48 kHz / 单声道 / 4096 taps。为什么必须用真 IR：
# 合成 EQ 近似永远做不出纸盆的滚降斜率和共振峰错位，听感上就是「塑料味」。
_CAB_DIR = Path(__file__).resolve().parent.parent / "assets" / "ir"
_CAB_CACHE = {}
# 每首歌按曲风/种子挑箱体，声音就不会都一样
CABS = ("cab_modern_uk.wav", "cab_vintage_us.wav", "cab_modern_us.wav")


def _cab_ir(name="cab_modern_uk.wav"):
    """读 IR（缓存）。找不到返回 None，调用方回退合成近似。"""
    if name in _CAB_CACHE:
        return _CAB_CACHE[name]
    path = _CAB_DIR / name
    ir = None
    if path.exists():
        import soundfile as sf
        try:
            data, rate = sf.read(str(path), always_2d=True, dtype="float32")
            ir = data.mean(axis=1).astype("float32")
            if rate != SR:
                ir = resample(ir, rate, SR)
            n = np.sqrt(float(np.sum(ir.astype("float64") ** 2)))
            if n > 1e-9:
                ir = (ir / n).astype("float32")
        except Exception:
            ir = None
    _CAB_CACHE[name] = ir
    return ir


def _cab_available():
    return _cab_ir() is not None


def cab_available():
    return _cab_available()


def cab_convolve(x, sr=SR, name="cab_modern_uk.wav", mix=1.0):
    """与箱体 IR 卷积（FFT 重叠相加），并做 RMS 补偿。

    oaconvolve 而不是 pedalboard.Convolution：后者只吃 1-D IR 且慢约 3 倍
    （实测 10 s 立体声 / 4096 taps：oaconvolve 46 ms，fftconvolve 81 ms）。
    """
    from scipy.signal import oaconvolve
    ir = _cab_ir(name)
    if ir is None:
        return np.asarray(x, dtype="float32")
    data = np.asarray(x, dtype="float32")
    mono = data.ndim == 1
    if mono:
        data = data[:, None]
    n = data.shape[0]
    out = np.empty_like(data)
    for ch in range(data.shape[1]):
        y = oaconvolve(data[:, ch].astype("float64"), ir.astype("float64"))[:n]
        out[:, ch] = y.astype("float32")
    # 单位能量 IR 卷出来电平会变，按 RMS 拉回原来，mix 才有意义
    ref = np.sqrt(np.mean(np.square(data.astype("float64"))) + 1e-18)
    cur = np.sqrt(np.mean(np.square(out.astype("float64"))) + 1e-18)
    if cur > 1e-12:
        out = (out * float(ref / cur)).astype("float32")
    if mix < 1.0:
        out = (data * (1.0 - mix) + out * mix).astype("float32")
    return out[:, 0] if mono else out


def save(path, data, sr=SR, subtype="PCM_24"):
    import soundfile as sf
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(data, -1.0, 1.0) if subtype == "PCM_24" else data,
             sr, subtype=subtype)


def resample(data, sr0, sr1):
    if int(sr0) == int(sr1):
        return data.astype("float32")
    from scipy.signal import resample_poly
    g = math.gcd(int(sr0), int(sr1))
    up, down = int(sr1 // g), int(sr0 // g)
    if data.ndim == 1:
        return resample_poly(data, up, down).astype("float32")
    return np.stack([resample_poly(data[:, c], up, down)
                     for c in range(data.shape[1])], axis=1).astype("float32")


def ensure_stereo(data):
    if data.ndim == 1:
        return np.stack([data, data], axis=1)
    if data.shape[1] == 1:
        return np.repeat(data, 2, axis=1)
    return data


def fit_length(data, n):
    if data.shape[0] < n:
        return np.pad(data, ((0, n - data.shape[0]), (0, 0)))
    return data[:n]


def pan(data, value, mono=None):
    """声像：-1 左、0 中、+1 右。

    单声道源用等功率分配（中间 -3 dB，两端 0 dB），
    立体声源用「平衡」衰减（只压对侧），这样双轨吉他不会被推成单声道。
    """
    v = max(-1.0, min(1.0, float(value or 0.0)))
    arr = np.asarray(data, dtype="float32")
    if arr.ndim == 1 or arr.shape[1] == 1:
        m = arr if arr.ndim == 1 else arr[:, 0]
        angle = (v + 1.0) * math.pi / 4.0
        gl, gr = math.cos(angle), math.sin(angle)
        return np.stack([m * gl, m * gr], axis=1).astype("float32")
    out = ensure_stereo(arr).copy()
    if v <= 0:
        out[:, 1] *= (1.0 + v)
    else:
        out[:, 0] *= (1.0 - v)
    return out.astype("float32")


# ------------------------------------------------------------------ 测量

def lufs(data, sr=SR):
    try:
        import pyloudnorm as pyln
        meter = pyln.Meter(int(sr))
        d = np.asarray(data, dtype="float64")
        if d.ndim == 1:
            d = d[:, None]
        return round(float(meter.integrated_loudness(d)), 2)
    except Exception:
        return None


def true_peak(data, sr=SR, oversample=4):
    """4 倍过采样后的采样峰值，比裸峰值更接近 dBTP。

    过采样必须逐声道做：先把左右声道 max 成一轨会得到不存在的中间值
    （旧实现就犯了这个错，2D 输入会被拆成一维再算，读数偏高）。
    """
    d = np.asarray(data, dtype="float32")
    if d.size == 0:
        return -np.inf
    if d.ndim == 1:
        d = d[:, None]
    peak = float(np.max(np.abs(d)))
    try:
        from scipy.signal import resample_poly
        up = resample_poly(d, oversample, 1, axis=0)
        peak = max(peak, float(np.max(np.abs(up))))
    except Exception:
        pass
    return round(gain_to_db(peak), 2)


BANDS = [(30, 120), (120, 300), (300, 800), (800, 2000), (2000, 5000), (5000, 12000)]


def band_energies(data, sr=SR):
    """6 个频段的 RMS（dB），用来做 reference 匹配和报告。"""
    from scipy.signal import welch
    d = np.asarray(data, dtype="float64")
    mono = d.mean(axis=1) if d.ndim == 2 else d
    if mono.size < 4096:
        return []
    f, pxx = welch(mono, fs=sr, nperseg=8192)
    out = []
    for lo, hi in BANDS:
        m = (f >= lo) & (f < hi)
        e = float(np.trapezoid(pxx[m], f[m])) if m.any() else 1e-12
        out.append(round(10 * math.log10(max(e, 1e-12)), 2))
    return out


def spectral_centroid(data, sr=SR):
    from scipy.signal import welch
    d = np.asarray(data, dtype="float64")
    mono = d.mean(axis=1) if d.ndim == 2 else d
    if mono.size < 4096:
        return None
    f, pxx = welch(mono, fs=sr, nperseg=8192)
    s = float(pxx.sum())
    return round(float((f * pxx).sum() / s), 1) if s > 0 else None


def crest_factor(data):
    d = np.asarray(data, dtype="float64")
    rms = float(np.sqrt(np.mean(d ** 2) + 1e-12))
    peak = float(np.max(np.abs(d)))
    return round(gain_to_db(peak) - gain_to_db(rms), 2)


def stereo_correlation(data):
    d = ensure_stereo(np.asarray(data, dtype="float64"))
    if d.shape[0] < 1024:
        return None
    l, r = d[:, 0], d[:, 1]
    if l.std() < 1e-9 or r.std() < 1e-9:
        return 1.0
    return round(float(np.corrcoef(l, r)[0, 1]), 3)


def silence_stats(data, sr=SR, floor_db=-60.0):
    """最长静音段（秒）与整体 RMS 曲线最低段——用来查「黑场/断音」。"""
    d = ensure_stereo(np.asarray(data, dtype="float32"))
    mono = np.abs(d).max(axis=1)
    win = max(1, int(sr * 0.05))
    n = mono.size // win
    if n == 0:
        return {"longestSilence": 0.0, "silentRatio": 0.0}
    blocks = mono[: n * win].reshape(n, win).max(axis=1)
    silent = blocks < db_to_gain(floor_db)
    longest = cur = 0
    for s in silent:
        cur = cur + 1 if s else 0
        longest = max(longest, cur)
    return {"longestSilence": round(longest * win / sr, 2),
            "silentRatio": round(float(silent.mean()), 3)}


# ------------------------------------------------------------------ 动态

def envelope(x, sr=SR, attack_ms=1.0, release_ms=40.0):
    """包络跟随（峰值保持 + 平滑）。

    用 maximum_filter 做「快起」，uniform_filter 做「慢落」，
    全向量化——逐样本 Python 循环在 3 分钟 48k 音频上会慢到不可用。
    """
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    mono = np.abs(np.asarray(x, dtype="float32"))
    if mono.ndim == 2:
        mono = mono.max(axis=1)
    fast = max(1, int(sr * attack_ms / 1000.0))
    slow = max(1, int(sr * release_ms / 1000.0))
    env = maximum_filter1d(mono, size=fast)
    env = uniform_filter1d(env, size=slow)
    return np.clip(env, 0.0, None).astype("float32")


def transient_shape(x, sr=SR, attack=0.35, sustain=-0.15):
    """瞬时整形：放大起音、收一点尾巴，鼓「打」起来而不是糊成一团。"""
    x = np.asarray(x, dtype="float32")
    env_f = envelope(x, sr, attack_ms=0.5, release_ms=8.0)
    env_s = envelope(x, sr, attack_ms=25.0, release_ms=120.0)
    diff = (env_f - env_s) / (env_s + 1e-4)
    gain = 1.0 + attack * np.clip(diff, -1.0, 3.0) + sustain * np.clip(-diff, 0.0, 1.0)
    gain = np.clip(gain, 0.2, 3.0).astype("float32")
    if x.ndim == 2:
        return (x * gain[:, None]).astype("float32")
    return (x * gain).astype("float32")


def saturate(x, drive_db=6.0, mix=1.0, bias=0.0):
    """tanh 软饱和：给总线/贝斯加谐波，听感更「实」。"""
    x = np.asarray(x, dtype="float32")
    g = db_to_gain(drive_db)
    y = np.tanh((x + bias) * g) / max(g, 1e-6)
    if mix >= 0.999:
        return y.astype("float32")
    return ((1.0 - mix) * x + mix * y).astype("float32")


def soft_clip(x, ceiling_db=-1.0, knee_db=4.0):
    """只削峰、不动主体：超过 knee 的部分按 tanh 折进去，RMS 基本不变。

    为什么要它：鼓的瞬态尖，硬缩放会把整条轨道压低 5 dB，
    混音里就变成「鼓永远推不上去」。这里让步进软限幅吸收掉那几个尖峰。
    """
    x = np.asarray(x, dtype="float32")
    ceil = db_to_gain(ceiling_db)
    knee = ceil / db_to_gain(knee_db)
    ax = np.abs(x)
    over = ax > knee
    if not np.any(over):
        return x
    y = x.copy()
    span = ceil - knee
    y[over] = np.sign(x[over]) * (knee + span * np.tanh((ax[over] - knee) / max(span, 1e-9)))
    return y.astype("float32")


def parallel_compress(x, sr=SR, thr=-26.0, ratio=6.0, wet=0.35, attack=3.0, release=90.0):
    try:
        from pedalboard import Compressor, Pedalboard
    except Exception:
        return x
    board = Pedalboard([Compressor(threshold_db=thr, ratio=ratio,
                                   attack_ms=attack, release_ms=release)])
    y = board(x.T, sr).T.astype("float32")
    return ((1.0 - wet) * x + wet * y).astype("float32")


def duck(data, env, depth_db):
    if env is None:
        return data
    depth = 1.0 - db_to_gain(-float(depth_db))
    gain = (1.0 - depth * env).astype("float32")
    return (data * gain[:, None]).astype("float32")


def kick_envelope(drums, sr=SR):
    """底鼓包络（低通 + 峰值保持 + 平滑），sidechain 用。"""
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    from scipy.signal import butter, sosfilt
    mono = np.asarray(drums, dtype="float32").mean(axis=1)
    sos = butter(4, min(120, sr * 0.4) / (sr / 2), btype="low", output="sos")
    low = sosfilt(sos, mono)
    env = np.abs(low)
    env = maximum_filter1d(env, size=max(1, int(sr * 0.008)))
    env = uniform_filter1d(env, size=max(1, int(sr * 0.09)))
    env = np.clip(env, 0.0, None)
    peak = float(env.max())
    if peak <= 0:
        return None
    return np.clip((env / peak) ** 0.6, 0.0, 1.0).astype("float32")


# ------------------------------------------------------------------ 空间

def reverb(x, sr=SR, wet=0.12, room=0.55, damping=0.5, width=0.9, predelay_ms=12):
    try:
        from pedalboard import Pedalboard, Reverb
    except Exception:
        return ensure_stereo(np.asarray(x, dtype="float32"))
    data = ensure_stereo(np.asarray(x, dtype="float32"))
    board = Pedalboard([Reverb(room_size=float(room), damping=float(damping),
                               wet_level=0.0, dry_level=1.0, width=float(width))])
    n = int(sr * predelay_ms / 1000.0)
    src = np.pad(data, ((n, 0), (0, 0)))[: data.shape[0]]
    wet_sig = board(src.T, sr).T.astype("float32")
    return ((1.0 - wet) * data + wet * wet_sig).astype("float32")


def delay(x, sr=SR, time_s=0.32, feedback=0.25, wet=0.15, pingpong=True):
    try:
        from pedalboard import Delay, Pedalboard
    except Exception:
        return x
    d = Pedalboard([Delay(delay_seconds=float(time_s), feedback=float(feedback), mix=1.0)])
    out = x.copy().astype("float32")
    src = ensure_stereo(x)
    wet_sig = d(src.T, sr).T.astype("float32")
    if not pingpong:
        wet_sig = wet_sig.mean(axis=1, keepdims=True).repeat(2, axis=1)
    return ((1.0 - wet) * out + wet * wet_sig).astype("float32")


def widen(x, amount=0.25, sr=SR):
    """把中间信息往两边推一点：摇滚吉他需要宽度。"""
    d = ensure_stereo(np.asarray(x, dtype="float32"))
    mid = (d[:, 0] + d[:, 1]) * 0.5
    side = (d[:, 0] - d[:, 1]) * (0.5 + amount)
    return np.stack([mid + side, mid - side], axis=1).astype("float32")


# ------------------------------------------------------------------ 乐器链

def guitar_amp(x, sr=SR, drive=14.0, tone=0.55, tight=True, cab=True,
               double_offset_ms=0.0, presence=0.5):
    """单声道 DI → 音箱模拟（前级失真 + 箱体低通 + 中频塑形）。

    为什么用 clean DI 再自己失真，而不是直接用 GM 的 distortion 音色：
    GM 的失真吉他样本已经「烧死」了，再叠加任何处理只会更糊；
    干净的 DI 进 tanh 前级，增益、箱体截止和临场感才可控。
    """
    try:
        from pedalboard import (Compressor, Distortion, Gain, HighpassFilter,
                                HighShelfFilter, LadderFilter, LowpassFilter,
                                Pedalboard, PeakFilter)
    except Exception:
        return x
    x = ensure_stereo(np.asarray(x, dtype="float32")) if np.asarray(x).ndim == 2 else np.asarray(x, dtype="float32")
    data = x if x.ndim == 2 else np.stack([x, x], axis=1)
    # 电子管前级：非对称饱和（多一点偶次谐波），比纯 tanh 更像「过载」而不是「失真」
    pre = _tube_pre(np.asarray(data, dtype="float32"), drive_db=drive)
    chain = []
    if tight:
        chain.append(HighpassFilter(cutoff_frequency_hz=95))
        chain.append(PeakFilter(cutoff_frequency_hz=420, gain_db=-3.5, q=0.9))
    chain.append(PeakFilter(cutoff_frequency_hz=700, gain_db=2.5 + 1.5 * presence, q=0.8))
    chain.append(Distortion(drive_db=max(4.0, drive * 0.35)))
    if cab:
        if not _cab_available():
            # 没有 IR 时的合成近似：几个共振 + 一刀低通
            chain += [
                PeakFilter(cutoff_frequency_hz=85, gain_db=3.2, q=0.8),
                PeakFilter(cutoff_frequency_hz=115, gain_db=2.2, q=1.2),
                PeakFilter(cutoff_frequency_hz=310, gain_db=-2.0, q=1.0),
                PeakFilter(cutoff_frequency_hz=2400, gain_db=2.0 + 2.0 * presence, q=1.0),
                LowpassFilter(cutoff_frequency_hz=4600 + 2400 * tone),
                HighShelfFilter(cutoff_frequency_hz=6500, gain_db=-2.2),
            ]
    chain.append(Compressor(threshold_db=-16.0, ratio=2.5, attack_ms=6.0, release_ms=90.0))
    chain.append(Gain(gain_db=-drive * 0.55))
    board = Pedalboard(chain)
    out = board(pre.T, sr).T.astype("float32")
    if cab and _cab_available():
        # 真箱体脉冲响应：4x12 的纸盆滚降、中频塌陷、麦克风位置染色都在里面
        cab_name = cab if isinstance(cab, str) else "cab_modern_uk.wav"
        out = cab_convolve(out, sr=sr, name=cab_name, mix=1.0)
        # 箱体之后补一点「话筒前移」的临场感。IR 本身是近场 4x12 的滚降，
        # 混在密集编曲里会显得闷（实测 2–5 kHz 比 300–800 Hz 低 4 dB、
        # 5–12 kHz 低 22 dB），所以在 IR 之后补一个 3 kHz 峰和 7 kHz 空气感。
        # 参考：REAPER 自带 amp_models 里 Marshall JCM800 的频谱重心 2331 Hz，
        # 我们的吉他原先只有 1.3–1.5 kHz，这就是「闷」的量化来源。
        try:
            air = Pedalboard([
                PeakFilter(cutoff_frequency_hz=3000, gain_db=1.0 + 2.0 * presence, q=0.9),
                HighShelfFilter(cutoff_frequency_hz=7000, gain_db=1.2 + 1.6 * presence),
            ])
            out = air(out.T, sr).T.astype("float32")
        except Exception:
            pass
    if double_offset_ms:
        n = int(sr * abs(double_offset_ms) / 1000.0)
        out = np.pad(out, ((n, 0), (0, 0)))[: data.shape[0]] if double_offset_ms > 0 else \
            np.pad(out, ((0, n), (0, 0)))[n:]
        out = saturate(out, drive_db=1.5, mix=0.35)
    return out


def _tube_pre(data, drive_db=12.0):
    """非对称 tanh：真空管过载的偶次谐波，比对称削波「暖」。

    先减去静态偏置产生的直流，避免整条波形被推离零点。
    """
    g = 10 ** (drive_db / 20.0)
    bias = 0.055
    x = np.asarray(data, dtype="float32") / max(1.0, np.max(np.abs(data)) + 1e-9)
    y = np.tanh((x + bias) * g) - np.tanh(bias * g)
    peak = np.max(np.abs(y)) + 1e-9
    return (y / peak).astype("float32")


def bass_chain(x, sr=SR, drive=3.0):
    try:
        from pedalboard import (Compressor, Gain, HighpassFilter, LowpassFilter,
                                LowShelfFilter, Pedalboard, PeakFilter)
    except Exception:
        return ensure_stereo(np.asarray(x, dtype="float32"))
    data = ensure_stereo(np.asarray(x, dtype="float32"))
    board = Pedalboard([
        HighpassFilter(cutoff_frequency_hz=38),
        # GM 指弹贝斯的能量堆在 120–300 Hz，缺 60–110 的厚度，靠低架补
        LowShelfFilter(cutoff_frequency_hz=110, gain_db=3.5),
        PeakFilter(cutoff_frequency_hz=70, gain_db=2.0, q=1.0),
        PeakFilter(cutoff_frequency_hz=300, gain_db=-2.5, q=1.1),
        Gain(gain_db=drive),
        Compressor(threshold_db=-20.0, ratio=4.0, attack_ms=4.0, release_ms=110.0),
        LowpassFilter(cutoff_frequency_hz=5200),
        Gain(gain_db=-drive * 0.7),
    ])
    out = board(data.T, sr).T.astype("float32")
    return saturate(out, drive_db=1.5, mix=0.4)


def drum_bus(x, sr=SR, punch=0.45, glue=True, room=0.10):
    """鼓总线：瞬时整形 → 并行压缩 → 饱和 → 轻房间。"""
    data = ensure_stereo(np.asarray(x, dtype="float32"))
    data = transient_shape(data, sr, attack=punch, sustain=-0.12)
    data = parallel_compress(data, sr, thr=-30.0, ratio=8.0, wet=0.42)
    if glue:
        try:
            from pedalboard import (Compressor, HighShelfFilter, LowShelfFilter,
                                    Limiter, Pedalboard, PeakFilter)
            board = Pedalboard([
                PeakFilter(cutoff_frequency_hz=55, gain_db=2.0, q=0.9),
                PeakFilter(cutoff_frequency_hz=320, gain_db=-2.5, q=1.0),
                PeakFilter(cutoff_frequency_hz=3600, gain_db=2.0, q=1.1),
                HighShelfFilter(cutoff_frequency_hz=6000, gain_db=4.0),
                Compressor(threshold_db=-18.0, ratio=3.0, attack_ms=18.0, release_ms=160.0),
                # 底鼓的尖峰把整体电平顶死，先压住，后面归一化才推得上去
                Limiter(threshold_db=-2.0, release_ms=60.0),
            ])
            data = board(data.T, sr).T.astype("float32")
        except Exception:
            pass
    data = saturate(data, drive_db=1.8, mix=0.35)
    if room > 0:
        data = reverb(data, sr, wet=room, room=0.35, damping=0.6, predelay_ms=8)
    return data.astype("float32")


def vocal_chain(x, sr=SR, presence=0.6, warmth=0.4, comp=3.4, deess=True):
    try:
        from pedalboard import (Compressor, Deesser, Gain, HighpassFilter,
                                HighShelfFilter, Pedalboard, PeakFilter)
    except Exception:
        from pedalboard import (Compressor, Gain, HighpassFilter, HighShelfFilter,
                                Pedalboard, PeakFilter)
        Deesser = None
    data = ensure_stereo(np.asarray(x, dtype="float32"))
    chain = [
        HighpassFilter(cutoff_frequency_hz=95),
        PeakFilter(cutoff_frequency_hz=340, gain_db=-2.5 * warmth, q=1.0),
        # 虚拟歌手声库在 2–6 kHz 偏暗（实测人声 2k–5k 比 300–800 低 10 dB），
        # 不补的话人声会闷在吉他和弦里，副歌顶不上去。
        PeakFilter(cutoff_frequency_hz=1800, gain_db=1.5 * presence, q=1.1),
        PeakFilter(cutoff_frequency_hz=3000, gain_db=4.5 * presence, q=0.8),
        HighShelfFilter(cutoff_frequency_hz=6500, gain_db=3.0 * presence),
        Compressor(threshold_db=-20.0, ratio=comp, attack_ms=6.0, release_ms=110.0),
    ]
    if deess and Deesser is not None:
        chain.insert(0, Deesser())
    board = Pedalboard(chain)
    out = board(data.T, sr).T.astype("float32")
    return saturate(out, drive_db=0.8, mix=0.25)


def master_bus(x, sr=SR, glue=0.35):
    """混音总线：极轻的胶水压缩 + 轻微倾斜 EQ，不做响度。"""
    try:
        from pedalboard import (Compressor, HighShelfFilter, LowShelfFilter,
                                Pedalboard, PeakFilter)
    except Exception:
        return ensure_stereo(x)
    data = ensure_stereo(np.asarray(x, dtype="float32"))
    chain = [
        # 摇滚混音的能量重心应该在 60–120 Hz（底鼓+贝斯）；实测 300–800 Hz
        # （吉他箱体的「盒子味」）比低频还响，所以在总线上把低频抬回来一点。
        PeakFilter(cutoff_frequency_hz=250, gain_db=-1.0, q=0.8),
        LowShelfFilter(cutoff_frequency_hz=110, gain_db=2.4),
        PeakFilter(cutoff_frequency_hz=75, gain_db=1.2, q=0.9),
        HighShelfFilter(cutoff_frequency_hz=8000, gain_db=0.8),
        Compressor(threshold_db=-12.0, ratio=1.0 + 2.0 * glue,
                   attack_ms=18.0, release_ms=180.0),
        Compressor(threshold_db=-8.0, ratio=1.15, attack_ms=40.0, release_ms=300.0),
    ]
    board = Pedalboard(chain)
    return board(data.T, sr).T.astype("float32")


# ------------------------------------------------------------------ reference

def reference_profile(path, sr=SR):
    """分析参考曲：响度、频段、crest、重心。用于 mix 的匹配。"""
    data, _ = load(path, sr)
    data = ensure_stereo(data)
    return {
        "file": str(path),
        "lufs": lufs(data, sr),
        "bands": band_energies(data, sr),
        "centroidHz": spectral_centroid(data, sr),
        "crest": crest_factor(data),
        "truePeak": true_peak(data, sr),
        "stereoCorr": stereo_correlation(data),
        "seconds": round(data.shape[0] / sr, 2),
    }


def match_reference(data, sr, profile, max_boost=6.0):
    """按参考曲的频段能量差做 6 段静态倾斜，并报告差异。

    只做「形状」匹配（相对差），不复制参考的绝对电平，
    因为绝对电平属于母带目标，不属于混音。
    """
    if not profile or not profile.get("bands"):
        return data, {}
    cur = band_energies(data, sr)
    if not cur:
        return data, {}
    import numpy as _np
    try:
        from pedalboard import (Gain, Pedalboard, PeakFilter)
    except Exception:
        return data, {}
    centers = [75, 200, 520, 1300, 3400, 8000]
    # 参考与当前各自去均值，只比「形状」
    ref = _np.array(profile["bands"], dtype="float64")
    ref = ref - ref.mean()
    ours = _np.array(cur, dtype="float64")
    ours = ours - ours.mean()
    delta = _np.clip(ref - ours, -max_boost, max_boost)
    chain = [PeakFilter(cutoff_frequency_hz=c, gain_db=float(g), q=0.8)
             for c, g in zip(centers, delta)]
    board = Pedalboard(chain)
    out = board(ensure_stereo(data).T, sr).T.astype("float32")
    return out, {"bandsOur": cur, "bandsRef": profile["bands"],
                 "appliedDb": [round(float(x), 2) for x in delta],
                 "targetLufs": profile.get("lufs")}
