"""时间对齐：像 DAW 里对齐人声那样，量出并修正轨道与网格之间的整体偏移。

为什么需要这一层：采样/合成器渲染出来的伴奏是逐样本对齐到拍的，但 SVS 唱出来的
人声有自己的起音习惯（辅音先出、连音拖尾），实测中位偏离 16 分网格 20 ms 以上。
DAW 的做法是把人声当成一个 clip，量出整体偏移后 nudge 过去；这里做同一件事，
自动完成并把数字记进工程报告。
"""
from __future__ import annotations

import numpy as np

HOP = 128  # 2.67 ms @48k


def _midi_env(onsets, n_samples, sr, sigma_ms=18.0):
    """把 MIDI 起音变成一条高斯脉冲串，当作「期望的起音曲线」。"""
    n = int(np.ceil(n_samples / HOP)) + 4
    env = np.zeros(n, dtype=np.float32)
    sig = sigma_ms / 1000.0 * sr / HOP
    span = max(1, int(4 * sig))
    for t, w in onsets:
        i = t * sr / HOP
        lo, hi = int(max(0, i - span)), int(min(n, i + span + 1))
        if hi <= lo:
            continue
        xs = np.arange(lo, hi) - i
        env[lo:hi] += w * np.exp(-0.5 * (xs / sig) ** 2)
    return env


def _audio_env(y, sr):
    import librosa
    if y.ndim > 1:
        y = y.mean(axis=1)
    return librosa.onset.onset_strength(y=np.asarray(y, dtype=np.float32), sr=sr, hop_length=HOP)


def measure_offset(y, sr, onsets, max_ms=250.0):
    """返回 (offset_ms, confidence)。

    offset > 0 表示音频比 MIDI 晚（要把音频往前挪）。
    confidence = 最佳位移相对整体均值偏离几个标准差，用来判断这次测量靠不靠得住。
    """
    env_a = _audio_env(y, sr)
    env_m = _midi_env(onsets, len(y), sr)
    n = min(len(env_a), len(env_m))
    if n < 64:
        return 0.0, 0.0
    a = env_a[:n] - env_a[:n].mean()
    m = env_m[:n] - env_m[:n].mean()
    na, nm = np.linalg.norm(a), np.linalg.norm(m)
    if na < 1e-6 or nm < 1e-6:
        return 0.0, 0.0
    a, m = a / na, m / nm
    max_lag = int(max_ms / 1000.0 * sr / HOP)
    lags = np.arange(-max_lag, max_lag + 1)
    scores = np.empty(len(lags), dtype=np.float32)
    for k, lag in enumerate(lags):
        if lag >= 0:
            scores[k] = float(np.dot(a[lag:], m[: n - lag])) if n - lag > 8 else -1.0
        else:
            scores[k] = float(np.dot(a[: n + lag], m[-lag:])) if n + lag > 8 else -1.0
    best = int(np.argmax(scores))
    peak = float(scores[best])
    conf = (peak - float(np.mean(scores))) / (float(np.std(scores)) or 1e-9)
    offset_ms = float(lags[best]) * HOP / sr * 1000.0
    if 0 < best < len(scores) - 1:  # 抛物线插值到亚帧
        y0, y1, y2 = scores[best - 1], scores[best], scores[best + 1]
        denom = (y0 - 2 * y1 + y2)
        if abs(denom) > 1e-9:
            offset_ms += float((y0 - y2) / (2 * denom)) * HOP / sr * 1000.0
    return round(offset_ms, 1), round(conf, 2)


def shift(y, sr, offset_ms):
    """把音频整体前移/后移 offset_ms（DAW 里的 nudge），空出来的地方补静音。"""
    if abs(offset_ms) < 0.5:
        return y
    k = int(round(offset_ms / 1000.0 * sr))
    if k == 0:
        return y
    if k > 0:
        pad = np.zeros((k,) + y.shape[1:], dtype=y.dtype)
        return np.concatenate([pad, y[:-k]], axis=0)
    k = -k
    pad = np.zeros((k,) + y.shape[1:], dtype=y.dtype)
    return np.concatenate([y[k:], pad], axis=0)


def score_onsets(align_notes):
    """vocal-align.json 的音符 → (秒, 权重)。权重用时长：长音更能代表乐句落点。"""
    out = []
    for n in align_notes:
        t = float(n.get("start") or 0.0)
        d = float(n.get("duration") or 0.0)
        out.append((t, float(np.clip(0.35 + d * 1.6, 0.35, 1.6))))
    out.sort(key=lambda x: x[0])
    return out


def fit_vocal(y, sr, align_notes, report=None, min_conf=1.5, min_ms=4.0):
    """量一次偏移并纠正。置信度不够就不动音频，只把数字写进报告。"""
    onsets = score_onsets(align_notes)
    if not onsets or len(y) == 0:
        return y, {"offsetMs": 0.0, "appliedMs": 0.0, "confidence": 0.0}
    offset, conf = measure_offset(y, sr, onsets)
    applied = offset if (conf >= min_conf and abs(offset) >= min_ms) else 0.0
    if applied:
        y = shift(y, sr, applied)
    diag = {"offsetMs": offset, "appliedMs": applied, "confidence": conf, "notes": len(onsets)}
    if report is not None:
        report.update(diag)
    return y, diag


def grid_distance(y, sr, bpm, div=4, report=None):
    """起音离最近网格线的中位距离（毫秒）+ 落在 ±30 ms 内的比例。

    这个数是「卡不卡得上点」的客观指标，改完对齐后拿它验。
    """
    import librosa
    if y.ndim > 1:
        y = y.mean(axis=1)
    grid = 60.0 / float(bpm) / div
    on = librosa.onset.onset_detect(y=np.asarray(y, dtype=np.float32), sr=sr,
                                    hop_length=HOP, units="time", backtrack=True)
    if len(on) == 0:
        return {"medianMs": None, "within30ms": None, "onsets": 0}
    ph = np.mod(on, grid)
    ph = np.minimum(ph, grid - ph)
    out = {"medianMs": round(float(np.median(ph)) * 1000.0, 1),
           "within30ms": round(float((ph <= 0.03).mean()), 2),
           "onsets": int(len(on))}
    if report is not None:
        report.update(out)
    return out
