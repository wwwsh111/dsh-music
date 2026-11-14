"""自动化轨道：照 FL 的 automation clip 那套，按小节画断点，作用到某条轨或总线上。

数据存在 project["automation"]，是一个列表：

    [
      {"id": "keys_gain", "track": "keys", "param": "gain", "shape": "linear",
       "points": [{"bar": 1, "value": -6.0}, {"bar": 9, "value": 2.0}]},
      {"id": "master_out", "track": "master", "param": "gain", "shape": "linear",
       "points": [{"bar": 1, "value": 0.0}, {"bar": 17, "value": -18.0}]}
    ]

track 写 "master" 或 "*" 就是总线。param 支持：

    gain     dB，逐样本乘到轨道上
    pan      -1（左）到 1（右），等功率声像
    lowpass  Hz，一阶低通（扫滤波）
    highpass Hz，一阶高通

点是按小节（bar，1 起）定位的，先按拍速换算成样本位置再插值，
所以改 tempo 自动化跟着走，和 FL 里对 clip 拉伸的行为一致。
shape 决定断点之间怎么走：linear 直线、step 保持、smooth 平滑（S 曲线）。
"""

import math

import numpy as np

from .util import jdump, jload  # noqa: F401  (与其它模块保持一致的导入面)

PARAMS = {
    "gain": "dB",
    "pan": "-1..1",
    "lowpass": "Hz",
    "highpass": "Hz",
}

BEATS_PER_BAR = 4


def load_lanes(project):
    lanes = (project or {}).get("automation") or []
    return [ln for ln in lanes if isinstance(ln, dict) and ln.get("points")]


def _bar_seconds(project):
    bpm = float((project or {}).get("tempo") or 120.0)
    return BEATS_PER_BAR * 60.0 / max(bpm, 1.0)


def _sorted_points(lane):
    pts = []
    for p in lane.get("points") or []:
        try:
            pts.append((float(p.get("bar", 1.0)), float(p.get("value", 0.0))))
        except (TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    return pts


def _eval_at(pts, bars, shape):
    """bars 是逐样本的小节位置数组（1 起），返回同形状的数值数组。"""
    if not pts:
        return np.zeros_like(bars)
    xs = np.array([p[0] for p in pts], dtype="float64")
    ys = np.array([p[1] for p in pts], dtype="float64")
    if len(xs) == 1:
        return np.full_like(bars, ys[0], dtype="float64")
    if shape == "step":
        idx = np.searchsorted(xs, bars, side="right") - 1
        idx = np.clip(idx, 0, len(ys) - 1)
        return ys[idx].astype("float64")
    if shape == "smooth":
        # 只在相邻断点之间做余弦缓动，断点本身精确命中
        seg = np.zeros_like(bars, dtype="float64")
        for i in range(len(xs) - 1):
            a, b, va, vb = xs[i], xs[i + 1], ys[i], ys[i + 1]
            mask = (bars >= a) & (bars <= b)
            if not np.any(mask):
                continue
            local = (bars[mask] - a) / max(b - a, 1e-9)
            seg[mask] = va + (vb - va) * (0.5 - 0.5 * np.cos(math.pi * local))
        seg[bars < xs[0]] = ys[0]
        seg[bars > xs[-1]] = ys[-1]
        return seg
    return np.interp(bars, xs, ys).astype("float64")


def values_for(project, lane, n, sr):
    bar_s = _bar_seconds(project)
    bars = 1.0 + (np.arange(n, dtype="float64") / max(bar_s * sr, 1e-9))
    return _eval_at(_sorted_points(lane), bars, lane.get("shape") or "linear")


def _pan_gains(v):
    """等功率声像：v=-1 全左，v=+1 全右。"""
    theta = (np.clip(v, -1.0, 1.0) + 1.0) * (math.pi / 4.0)
    return np.cos(theta), np.sin(theta)


def _one_pole_lowpass(x, sr, cutoff):
    """时变一阶低通：y[n] = y[n-1] + a[n]*(x[n]-y[n-1])。

    系数逐样本变，没法用一次 lfilter 算完；按 20 ms 一块、带着 zi 递推，
    曲线本身是慢变的，块内当常数不会听出阶梯。
    """
    from scipy.signal import lfilter

    a = 1.0 - np.exp(-2.0 * math.pi * np.clip(cutoff, 20.0, sr * 0.45) / sr)
    n = x.shape[0]
    block = max(64, int(0.02 * sr))
    y = np.empty_like(x)
    zi = np.zeros(1, dtype="float64")
    for s in range(0, n, block):
        e = min(s + block, n)
        aa = float(a[s])
        if aa <= 0.0:
            y[s:e] = x[s:e]
            continue
        # y[n] = (1-aa)*y[n-1] + aa*x[n]
        out, zi = lfilter([aa], [1.0, -(1.0 - aa)], x[s:e], zi=zi)
        y[s:e] = out
    return y


def _apply_param(data, param, values, sr):
    if param == "gain":
        g = np.power(10.0, values / 20.0)
        if data.ndim == 2:
            return data * g[:, None]
        return data * g

    if param == "pan":
        left, right = _pan_gains(values)
        if data.ndim == 1:
            data = np.repeat(data[:, None], 2, axis=1)
        # 先归一化，再乘等功率系数：单声道源铺开也能保持电平
        k = math.sqrt(2.0)
        return np.stack([data[:, 0] * left * k, data[:, 1] * right * k], axis=1)

    if param in ("lowpass", "highpass"):
        if data.ndim == 2:
            cols = [_one_pole_lowpass(data[:, c], sr, values) for c in range(data.shape[1])]
            low = np.stack(cols, axis=1)
        else:
            low = _one_pole_lowpass(data, sr, values)
        return low if param == "lowpass" else data - low

    raise ValueError(f"不认识的自动化参数：{param}")


def _summary(lane, values, n, sr):
    dur = n / float(sr or 48000)
    return {
        "id": lane.get("id"),
        "param": lane.get("param"),
        "shape": lane.get("shape") or "linear",
        "from": round(float(values[0]), 2),
        "to": round(float(values[-1]), 2),
        "min": round(float(np.min(values)), 2),
        "max": round(float(np.max(values)), 2),
        "seconds": round(dur, 2),
    }


def apply_track(project, track_id, data, sr):
    """把作用在这条轨上的 lane 依次套上；返回 (新数组, 报告)。"""
    lanes = [ln for ln in load_lanes(project)
             if str(ln.get("track") or "") == str(track_id)]
    if not lanes:
        return data, None
    out, report = data, []
    for lane in lanes:
        param = str(lane.get("param") or "gain")
        if param not in PARAMS:
            continue
        try:
            values = values_for(project, lane, out.shape[0], sr)
            out = _apply_param(out, param, values, sr)
            report.append(_summary(lane, values, out.shape[0], sr))
        except Exception as exc:  # 单条 lane 出错不该把整首混音带下去
            report.append({"id": lane.get("id"), "error": str(exc)})
    return out, report


def apply_master(project, data, sr):
    """总线 lane：track 写 "master" 或 "*"。"""
    lanes = [ln for ln in load_lanes(project)
             if str(ln.get("track") or "").lower() in ("master", "*", "bus")]
    if not lanes:
        return data, None
    out, report = data, []
    for lane in lanes:
        param = str(lane.get("param") or "gain")
        if param not in PARAMS:
            continue
        try:
            values = values_for(project, lane, out.shape[0], sr)
            out = _apply_param(out, param, values, sr)
            report.append(_summary(lane, values, out.shape[0], sr))
        except Exception as exc:
            report.append({"id": lane.get("id"), "error": str(exc)})
    return out, report


def _lane_id(lane):
    if lane.get("id"):
        return str(lane["id"])
    return f"{lane.get('track') or 'master'}_{lane.get('param') or 'gain'}"


def edit(project_path, op="list", lane=None):
    """增删查自动化：op = list | set | clear。

    set 时 lane 传完整的 lane 对象；同名（id）的会被替换，不传 id 就用
    <track>_<param> 当 id，所以同一轨同一参数只有一条曲线——和 FL 里
    「一条自动化 clip 管一个参数」一致。clear 不传 id 就清掉该轨全部参数。
    """
    from .project import load_project, save_project

    project = load_project(project_path)
    lanes = list(project.get("automation") or [])

    if op == "list":
        return {"ok": True, "lanes": lanes, "count": len(lanes)}

    if op == "clear":
        if not lane:
            lanes = []
        else:
            wid = lane.get("id")
            track = lane.get("track")
            param = lane.get("param")
            lanes = [ln for ln in lanes
                     if not ((wid and _lane_id(ln) == wid)
                             or (not wid and track and str(ln.get("track")) == str(track)
                                 and (not param or ln.get("param") == param)))]
        project["automation"] = lanes
        save_project(project_path, project)
        return {"ok": True, "lanes": lanes, "count": len(lanes)}

    if op != "set":
        raise ValueError(f"不支持的 op：{op}")
    if not lane:
        raise ValueError("set 需要 lane")

    param = str(lane.get("param") or "gain")
    if param not in PARAMS:
        raise ValueError(f"param 只能是 {', '.join(PARAMS)}")
    track = str(lane.get("track") or "master")
    points = lane.get("points") or []
    if len(points) < 1:
        raise ValueError("points 至少一个断点")

    clean = {
        "id": _lane_id({**lane, "track": track, "param": param}),
        "track": track,
        "param": param,
        "shape": lane.get("shape") or "linear",
        "points": [{"bar": float(p.get("bar", 1.0)), "value": float(p.get("value", 0.0))}
                   for p in points],
    }
    clean["points"].sort(key=lambda p: p["bar"])
    lanes = [ln for ln in lanes if _lane_id(ln) != clean["id"]]
    lanes.append(clean)
    lanes.sort(key=lambda ln: (str(ln.get("track")), str(ln.get("param"))))
    project["automation"] = lanes
    save_project(project_path, project)
    return {"ok": True, "lane": clean, "lanes": lanes, "count": len(lanes)}
