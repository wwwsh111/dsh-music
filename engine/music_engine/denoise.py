"""降噪：照 Audacity 的 Noise Reduction 思路——先取一段噪声，再按频谱门限压掉。

不用 demucs 那套重型模型，纯 STFT 谱减：
  1. 噪声画像：取用户指定的噪声样本（或自动取最安静的一批帧）的每频点幅度中位数；
  2. 逐帧算保留系数 mask = sqrt(1 - (over*noise/mag)^2)，下限是 10^(-reduction/20)；
  3. mask 在时间/频率上各平滑一次，避免"水声"（musical noise）；
  4. 逆 STFT 交叠相加，按窗平方和归一，长度与原文件一致。

是真实 DSP，可离线验证：白噪 + 正弦，降噪后静音段的噪声 RMS 应明显下降，
而正弦段的电平基本不动。
"""

from pathlib import Path

import numpy as np

from . import dsp
from .util import ensure_dir

N_FFT = 2048
HOP = 512


def _window(n=N_FFT):
    return np.hanning(n + 1)[:-1].astype("float32")


def _stft(x, n_fft=N_FFT, hop=HOP):
    win = _window(n_fft)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    frames = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(frames)[:, None]
    return np.fft.rfft(x[idx] * win, axis=1), frames


def _istft(spec, n_fft=N_FFT, hop=HOP):
    win = _window(n_fft)
    frames = np.fft.irfft(spec, n=n_fft, axis=1)
    out = np.zeros(hop * (len(frames) - 1) + n_fft, dtype="float64")
    wsum = np.zeros_like(out)
    w2 = win.astype("float64") ** 2
    for i in range(len(frames)):
        s = i * hop
        out[s:s + n_fft] += frames[i].astype("float64") * win
        wsum[s:s + n_fft] += w2
    return (out / np.maximum(wsum, 1e-9)).astype("float32")


def noise_profile(path, sr=48000, seconds=None):
    """把一段纯噪声变成逐频点中位数幅度。seconds=None 时整段。"""
    data, _ = dsp.load(str(path), sr)
    data = dsp.ensure_stereo(data)
    mono = data.mean(axis=1)
    if seconds:
        mono = mono[: int(float(seconds) * sr)]
    spec, _ = _stft(mono)
    return np.median(np.abs(spec), axis=0).astype("float32")


def _auto_profile(spec, quiet_ratio=0.12):
    """没有噪声样本时：按帧能量取最安静的 12% 当噪声。"""
    mag = np.abs(spec)
    energy = mag.sum(axis=1)
    k = max(1, int(len(energy) * quiet_ratio))
    idx = np.argsort(energy)[:k]
    return np.median(mag[idx], axis=0).astype("float32")


def _smooth(mask):
    try:
        from scipy.ndimage import median_filter
    except Exception:
        return mask
    m = median_filter(mask, size=(3, 3), mode="nearest")
    return np.clip(m, 0.0, 1.0)


def denoise(path, output=None, noise=None, sr=48000, reduction_db=12.0,
            sensitivity=6.0, noise_seconds=None):
    """对整段音频降噪。noise 可以是 wav 路径，也可以是逐频点数组。"""
    data, _ = dsp.load(str(path), sr)
    data = dsp.ensure_stereo(data)

    if isinstance(noise, np.ndarray):
        prof = np.asarray(noise, dtype="float32")
    elif noise:
        prof = noise_profile(noise, sr, noise_seconds)
    else:
        prof = None

    floor = float(10.0 ** (-abs(float(reduction_db)) / 20.0))
    over = 1.0 + float(sensitivity) / 12.0

    out = np.zeros_like(data)
    pad = N_FFT
    for ch in range(data.shape[1]):
        src = np.pad(data[:, ch].astype("float32"), (pad, pad))
        spec, _ = _stft(src)
        mag = np.abs(spec)
        p = prof if prof is not None else _auto_profile(spec)
        if len(p) != mag.shape[1]:
            p = np.interp(np.linspace(0, 1, mag.shape[1]),
                          np.linspace(0, 1, len(p)), p)
        ratio = (over * p[None, :]) / np.maximum(mag, 1e-9)
        mask = np.sqrt(np.clip(1.0 - ratio ** 2, floor ** 2, 1.0))
        mask = _smooth(mask)
        out[:, ch] = _istft(spec * mask)[pad:pad + data.shape[0]]

    n = data.shape[0]
    out = out[:n]

    if output is None:
        base = Path(path)
        d = ensure_dir(base.parent / "audio" / "clean")
        output = d / f"{base.stem}-clean.wav"
    dsp.save(Path(output), out, sr, subtype="PCM_24")

    return {
        "ok": True, "input": str(path), "output": str(output),
        "noiseProfile": "file" if noise else ("array" if isinstance(noise, np.ndarray) else "auto"),
        "reductionDb": abs(float(reduction_db)), "sensitivity": float(sensitivity),
        "metrics": {
            "peakIn": _peak(data), "peakOut": _peak(out),
            "rmsIn": _rms(data), "rmsOut": _rms(out),
            "lufsIn": dsp.lufs(data, sr), "lufsOut": dsp.lufs(out, sr),
        },
    }


def _peak(x):
    return round(float(np.max(np.abs(x))), 4) if x.size else 0.0


def _rms(x):
    return round(float(np.sqrt(np.mean(np.square(x)))), 4) if x.size else 0.0
