"""采样器 / 通道机架：把自己导入的音频当乐器弹。

FL 的 Channel rack 核心就两件事：**一个采样 + 一张键盘**。这里做同一件事：

* `mode: "pitch"` —— 按音高变调（采样当乐器，升八度=时长减半），默认；
* `mode: "oneshot"` —— 不管音高，按下就完整播一遍（鼓、音效、人声切片）。

采样只读一次、按 sr 缓存；每个音符按 `2**((pitch-root)/12)` 重采样后叠加到轨道上，
再用 attack/release 包络把首尾磨一下，避免点按声。
"""

import math
from pathlib import Path

import numpy as np

from . import dsp
from .project import load_project, save_project

CACHE = {}


def _key(path, sr):
    st = Path(path).stat()
    return (str(path), st.st_mtime_ns, sr)


def load_sample(path, sr):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"采样文件不存在：{path}")
    k = _key(path, sr)
    if k in CACHE:
        return CACHE[k]
    data, sr0 = dsp.load(path, sr)
    mono = np.asarray(data, dtype="float64")
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    peak = float(np.max(np.abs(mono)) or 1.0)
    if peak > 0:
        mono = mono / peak
    CACHE[k] = mono
    return mono


def _resampled(sample, ratio):
    """ratio > 1 音更高、更短。"""
    n_out = max(2, int(round(len(sample) / ratio)))
    idx = np.arange(n_out) * ratio
    idx = idx[idx < len(sample) - 1]
    if len(idx) < 2:
        idx = np.array([0.0, max(0.0, len(sample) - 2.0)])
    return np.interp(idx, np.arange(len(sample)), sample)


def _env(n, sr, attack_ms, release_ms):
    a = max(1, int(sr * attack_ms / 1000.0))
    r = max(1, int(sr * release_ms / 1000.0))
    e = np.ones(n)
    if a < n:
        e[:a] = np.linspace(0.0, 1.0, a)
    if r < n:
        e[-r:] *= np.linspace(1.0, 0.0, r)
    return e


def _defn_file(defn):
    """采样器里的 file；如果它来自音效库，文件被挪走时按 libraryKey 重新找回来。"""
    path = Path(str(defn.get("file") or ""))
    if path.exists():
        return path
    if defn.get("libraryKey"):
        from . import library
        return library.audio_path(ident=str(defn["libraryKey"]))
    return path


def render_notes(notes, defn, sr, total_samples):
    """把一串秒级音符渲染成一条立体声轨。notes: [{pitch,start,end,velocity}]"""
    sample = load_sample(_defn_file(defn), sr)
    mode = str(defn.get("mode") or "pitch")
    root = int(defn.get("rootNote", 60))
    gain = dsp.db_to_gain(float(defn.get("gainDb") or 0.0))
    attack = float(defn.get("attackMs", 3.0))
    release = float(defn.get("releaseMs", 60.0))
    max_sec = defn.get("maxSec")
    out = np.zeros((int(total_samples), 2), dtype="float64")
    placed = 0
    for n in notes:
        start = int(round(float(n["start"]) * sr))
        if start >= total_samples or start < 0:
            continue
        if mode == "oneshot":
            seg = sample
        else:
            ratio = 2.0 ** ((int(n["pitch"]) - root) / 12.0)
            seg = _resampled(sample, ratio)
        if max_sec:
            seg = seg[:int(float(max_sec) * sr)]
        seg = seg * _env(len(seg), sr, attack, release)
        if n.get("end") and mode != "oneshot":
            want = int(round((float(n["end"]) - float(n["start"])) * sr))
            if 0 < want < len(seg):
                # 音符更短：尾巴收掉，不硬切
                fade = min(len(seg) - want, int(0.02 * sr)) if len(seg) > want else 0
                seg = seg[:want]
                if fade > 0:
                    pass
        vel = float(n.get("velocity", 96)) / 127.0
        seg = seg * (vel ** 1.4) * gain
        end = min(start + len(seg), total_samples)
        if end <= start:
            continue
        out[start:end, 0] += seg[:end - start]
        out[start:end, 1] += seg[:end - start]
        placed += 1
    return out, {"placed": placed, "mode": mode, "root": root,
                 "sample": str(defn["file"]), "seconds": round(total_samples / sr, 2)}


def _definitions(project):
    return {str(s.get("id")): s for s in (project.get("samplers") or [])}


def edit(project_path, op="list", sampler=None, sid=None):
    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    items = list(project.get("samplers") or [])

    if op == "list":
        return {"ok": True, "samplers": items,
                "tracks": [{t["id"]: t.get("sampler")} for t in project.get("tracks") or []
                           if t.get("sampler")]}

    if op == "set":
        if not sampler:
            raise ValueError("set 需要 sampler")
        data = dict(sampler)
        raw = str(data.get("file") or "")
        libid = data.pop("libraryId", None) or (raw[4:] if raw.startswith("lib:") else None)
        if libid:
            from . import library
            item = library.get(ident=str(libid), peaks=False)["item"]
            data["file"] = item["path"]
            data["libraryKey"] = item["id"]
        if not data.get("id"):
            data["id"] = Path(str(data.get("file"))).stem
        if not data.get("file"):
            raise ValueError("sampler.file 必填")
        f = Path(str(data["file"])).expanduser()
        if not f.is_absolute():
            f = base / f
        if not f.exists():
            raise ValueError(f"采样不存在：{f}")
        data["file"] = str(f)
        data.setdefault("rootNote", 60)
        data.setdefault("mode", "pitch")
        data.setdefault("gainDb", 0.0)
        data.setdefault("attackMs", 3.0)
        data.setdefault("releaseMs", 60.0)
        items = [s for s in items if str(s.get("id")) != str(data["id"])]
        items.append(data)
        project["samplers"] = items
        save_project(base, project)
        return {"ok": True, "sampler": data, "samplers": items}

    if op == "remove":
        target = str(sid or (sampler or {}).get("id") or "")
        items = [s for s in items if str(s.get("id")) != target]
        project["samplers"] = items
        save_project(base, project)
        return {"ok": True, "removed": target, "samplers": items}

    raise ValueError(f"不支持的 op：{op}")


def add_track(project_path, track_id, sampler_id, bar_start=None):
    """给工程加一条采样器轨（默认插在 tracks 末尾）。"""
    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    defs = _definitions(project)
    if str(sampler_id) not in defs:
        raise ValueError(f"没有这个采样器：{sampler_id}（先 sampler set）")
    tracks = [t for t in project.get("tracks") or [] if t["id"] != track_id]
    tracks.append({"id": track_id, "sampler": str(sampler_id), "fx": "sampler",
                   "midi": f"midi/{track_id}.mid"})
    project["tracks"] = tracks
    save_project(base, project)
    return {"ok": True, "tracks": [t["id"] for t in tracks], "sampler": str(sampler_id)}
