"""每轨效果链：照 FL 的 mixer channel 效果槽，一条轨可以按顺序挂一串效果。

存在 `track["fxChain"]` 里：

    [{"type": "highpass", "freq": 90},
     {"type": "eq", "freq": 3200, "gainDb": 2.5, "q": 1.0},
     {"type": "comp", "thresholdDb": -20, "ratio": 3, "attackMs": 8, "releaseMs": 140},
     {"type": "saturate", "driveDb": 4, "mix": 0.4},
     {"type": "chorus", "rateHz": 0.4, "depth": 0.2, "mix": 0.25},
     {"type": "delay", "timeMs": 340, "feedback": 0.25, "mix": 0.15},
     {"type": "reverb", "roomSize": 0.5, "mix": 0.12},
     {"type": "limiter", "thresholdDb": -1.0}]

位置在混音链里是「预设音色处理之后、推子与声像之前」——和 DAW 的插入槽一致。
所有 DSP 都走 pedalboard（没装就跳过该级，并在报告里写清楚），只保留少数自研的。
"""

import math

import numpy as np

from . import dsp

TYPES = ("gain", "highpass", "lowpass", "eq", "comp", "saturate", "chorus",
         "delay", "reverb", "limiter", "widen")

# 每一级给个默认值，用户只写 type 也能用
DEFAULTS = {
    "gain": {"db": 0.0},
    "highpass": {"freq": 80.0},
    "lowpass": {"freq": 14000.0},
    "eq": {"freq": 1000.0, "gainDb": 0.0, "q": 1.0},
    "comp": {"thresholdDb": -18.0, "ratio": 3.0, "attackMs": 8.0, "releaseMs": 140.0},
    "saturate": {"driveDb": 5.0, "mix": 0.5},
    "chorus": {"rateHz": 0.5, "depth": 0.2, "mix": 0.25},
    "delay": {"timeMs": 330.0, "feedback": 0.25, "mix": 0.15},
    "reverb": {"roomSize": 0.5, "damping": 0.4, "mix": 0.15},
    "limiter": {"thresholdDb": -1.0, "releaseMs": 100.0},
    "widen": {"amount": 0.25},
}


def _to_pb(data):
    a = np.asarray(data, dtype="float32")
    if a.ndim == 1:
        a = np.stack([a, a], axis=1)
    return np.ascontiguousarray(a.T)          # (channels, samples)


def _from_pb(arr, ndim):
    a = np.ascontiguousarray(arr.T).astype("float64")
    return a


def _board(stages):
    from pedalboard import (Chorus, Compressor, Gain, HighpassFilter, HighShelfFilter,
                            Limiter, LowpassFilter, LowShelfFilter, Pedalboard,
                            PeakFilter, Reverb)
    mapping = {
        "gain": lambda p: Gain(gain_db=float(p.get("db", 0.0))),
        "highpass": lambda p: HighpassFilter(cutoff_frequency_hz=float(p.get("freq", 80.0))),
        "lowpass": lambda p: LowpassFilter(cutoff_frequency_hz=float(p.get("freq", 14000.0))),
        "eq": lambda p: PeakFilter(cutoff_frequency_hz=float(p.get("freq", 1000.0)),
                                   gain_db=float(p.get("gainDb", 0.0)),
                                   q=float(p.get("q", 1.0))),
        "comp": lambda p: Compressor(threshold_db=float(p.get("thresholdDb", -18.0)),
                                     ratio=float(p.get("ratio", 3.0)),
                                     attack_ms=float(p.get("attackMs", 8.0)),
                                     release_ms=float(p.get("releaseMs", 140.0))),
        "chorus": lambda p: Chorus(rate_hz=float(p.get("rateHz", 0.5)),
                                   depth=float(p.get("depth", 0.2)),
                                   mix=float(p.get("mix", 0.25))),
        "reverb": lambda p: Reverb(room_size=float(p.get("roomSize", 0.5)),
                                   damping=float(p.get("damping", 0.4)),
                                   wet_level=float(p.get("mix", 0.15)),
                                   dry_level=1.0 - float(p.get("mix", 0.15))),
        "limiter": lambda p: Limiter(threshold_db=float(p.get("thresholdDb", -1.0)),
                                     release_ms=float(p.get("releaseMs", 100.0))),
    }
    plugs = []
    for st in stages:
        t = str(st.get("type"))
        if t in mapping:
            plugs.append(mapping[t](st))
    return Pedalboard(plugs) if plugs else None


def _limiter(x, sr, threshold_db=-1.0, release_ms=80.0):
    """砖墙限幅：pedalboard 的 Limiter 不保证落在阈值上（它把峰推到 0 dBFS），
    所以这里自己做——5 ms 前瞻取包络、瞬时压、一阶释放，最后再削一次顶。"""
    from scipy.ndimage import maximum_filter1d
    from scipy.signal import lfilter

    thr = dsp.db_to_gain(threshold_db)
    a = np.asarray(x, dtype="float64")
    peak = np.abs(a).max(axis=1) if a.ndim == 2 else np.abs(a)
    win = max(3, int(0.005 * sr))
    env = maximum_filter1d(peak, size=win, mode="nearest")
    gain = np.minimum(1.0, thr / np.maximum(env, 1e-9))
    coef = math.exp(-1.0 / max(1.0, sr * release_ms / 1000.0))
    gain = lfilter([1 - coef], [1, -coef], gain, zi=[coef * float(gain[0])])[0]
    out = a * (gain[:, None] if a.ndim == 2 else gain)
    return dsp.soft_clip(out, ceiling_db=threshold_db, knee_db=2.0)


def apply_chain(data, sr, chain):
    """依次挂链；返回 (音频, 报告)。单级失败只记下来，不影响其它级。"""
    report = []
    if not chain:
        return data, report
    a = np.asarray(data, dtype="float64")
    ndim = a.ndim
    stages = [dict(DEFAULTS.get(str(s.get("type")), {}), **s) for s in chain
              if str(s.get("type")) in TYPES]
    # pedalboard 那部分是连续的一段，一次调用把整条插件链跑完
    pb_stages = [s for s in stages if s["type"] in
                 ("gain", "highpass", "lowpass", "eq", "comp", "chorus", "reverb")]
    native = [s for s in stages if s["type"] in ("saturate", "delay", "widen", "limiter")]

    cur = a
    if pb_stages:
        try:
            board = _board(pb_stages)
            if board is not None:
                cur = _from_pb(board(_to_pb(cur), float(sr)), ndim)
                report.extend({"type": s["type"], "engine": "pedalboard"} for s in pb_stages)
        except Exception as exc:
            # 整条链跑不了就逐级重试：坏一级不连累其它级
            report.append({"type": "pedalboard", "stages": [s["type"] for s in pb_stages],
                           "error": str(exc), "fallback": True})
            for st in pb_stages:
                try:
                    one = _board([st])
                    if one is None:
                        continue
                    cur = _from_pb(one(_to_pb(cur), float(sr)), ndim)
                    report.append({"type": st["type"], "engine": "pedalboard", "retry": True})
                except Exception as exc2:
                    report.append({"type": st["type"], "error": str(exc2)})

    for st in native:
        t = st["type"]
        try:
            if t == "saturate":
                cur = dsp.saturate(cur, drive_db=float(st.get("driveDb", 5.0)),
                                   mix=float(st.get("mix", 0.5)))
            elif t == "delay":
                cur = dsp.delay(cur, sr, time_s=float(st.get("timeMs", 330.0)) / 1000.0,
                                feedback=float(st.get("feedback", 0.25)),
                                wet=float(st.get("mix", 0.15)))
            elif t == "widen":
                cur = dsp.widen(cur, amount=float(st.get("amount", 0.25)), sr=sr)
            elif t == "limiter":
                cur = _limiter(cur, sr, threshold_db=float(st.get("thresholdDb", -1.0)),
                               release_ms=float(st.get("releaseMs", 80.0)))
            report.append({"type": t})
        except Exception as exc:
            report.append({"type": t, "error": str(exc)})

    return cur.astype("float32"), report


def edit(project_path, op="list", track=None, chain=None, preset=None):
    """op = list | set | add | clear | preset"""
    from pathlib import Path

    from .project import load_project, save_project

    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    tracks = project.get("tracks") or []

    def get(tid):
        for tr in tracks:
            if str(tr.get("id")) == str(tid):
                return tr
        raise ValueError(f"工程里没有这条轨：{tid}")

    if op == "list":
        return {"ok": True, "chains": {t["id"]: t.get("fxChain") or [] for t in tracks},
                "types": list(TYPES), "presets": list(PRESETS)}

    if op == "clear":
        tr = get(track)
        tr["fxChain"] = []
        save_project(base, project)
        return {"ok": True, "track": tr["id"], "fxChain": []}

    if op == "preset":
        name = str(preset or "").lower()
        if name not in PRESETS:
            raise ValueError(f"没有这个预设：{name}（有 {', '.join(PRESETS)}）")
        tr = get(track)
        tr["fxChain"] = [dict(s) for s in PRESETS[name]]
        save_project(base, project)
        return {"ok": True, "track": tr["id"], "fxChain": tr["fxChain"], "preset": name}

    if op == "set":
        tr = get(track)
        if not isinstance(chain, list):
            raise ValueError("set 需要 chain 数组")
        cleaned = []
        for s in chain:
            t = str(s.get("type"))
            if t not in TYPES:
                raise ValueError(f"不认识的 type：{t}（可选 {', '.join(TYPES)}）")
            cleaned.append(dict(DEFAULTS.get(t, {}), **s))
        tr["fxChain"] = cleaned
        save_project(base, project)
        return {"ok": True, "track": tr["id"], "fxChain": cleaned}

    if op == "add":
        tr = get(track)
        t = str((chain or [{}])[0].get("type") if chain else "")
        if t not in TYPES:
            raise ValueError(f"不认识的 type：{t}")
        item = dict(DEFAULTS.get(t, {}), **(chain[0]))
        tr["fxChain"] = list(tr.get("fxChain") or []) + [item]
        save_project(base, project)
        return {"ok": True, "track": tr["id"], "fxChain": tr["fxChain"]}

    raise ValueError(f"不支持的 op：{op}")


# 几个开箱可用的链子：名字对应「这条轨现在缺什么」
PRESETS = {
    "vocal-clean": [
        {"type": "highpass", "freq": 95},
        {"type": "eq", "freq": 2800, "gainDb": 2.0, "q": 1.2},
        {"type": "comp", "thresholdDb": -22, "ratio": 2.5, "attackMs": 6, "releaseMs": 160},
        {"type": "reverb", "roomSize": 0.35, "mix": 0.10},
    ],
    "keys-air": [
        {"type": "highpass", "freq": 120},
        {"type": "eq", "freq": 6500, "gainDb": 2.5, "q": 0.9},
        {"type": "comp", "thresholdDb": -20, "ratio": 2.0, "attackMs": 15, "releaseMs": 200},
    ],
    "drum-punch": [
        {"type": "eq", "freq": 70, "gainDb": 2.5, "q": 1.0},
        {"type": "eq", "freq": 3200, "gainDb": 1.5, "q": 1.4},
        {"type": "comp", "thresholdDb": -16, "ratio": 4.0, "attackMs": 3, "releaseMs": 90},
        {"type": "saturate", "driveDb": 3.0, "mix": 0.35},
    ],
    "guitar-bite": [
        {"type": "highpass", "freq": 100},
        {"type": "eq", "freq": 1600, "gainDb": 2.0, "q": 1.0},
        {"type": "comp", "thresholdDb": -18, "ratio": 3.0, "attackMs": 5, "releaseMs": 120},
        {"type": "delay", "timeMs": 300, "feedback": 0.15, "mix": 0.08},
    ],
    "bass-tight": [
        {"type": "highpass", "freq": 32},
        {"type": "comp", "thresholdDb": -20, "ratio": 4.0, "attackMs": 10, "releaseMs": 120},
        {"type": "eq", "freq": 90, "gainDb": 1.5, "q": 0.9},
        {"type": "saturate", "driveDb": 2.0, "mix": 0.25},
    ],
}
