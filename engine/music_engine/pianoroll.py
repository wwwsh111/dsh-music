"""钢琴卷帘：直接在谱面上改音符——改音高、挪位置、改时值、改力度、量化、人性化。

和 FL 的 Piano roll 一样，坐标是「小节 + 拍」，不是秒；内部再用 tempo 换算。
所有操作都写回 `midi/<track>.mid`，所以改完直接 `music_render` 就能听见。
"""

from pathlib import Path

import numpy as np

from . import midilib
from .project import load_project, save_project


def _track(project, tid):
    for tr in project.get("tracks") or []:
        if str(tr.get("id")) == str(tid):
            return tr
    raise ValueError(f"工程里没有这条轨：{tid}")


def _midi_path(base, tr):
    return Path(base) / (tr.get("midi") or f"midi/{tr['id']}.mid")


def _total_seconds(project):
    bars = sum(int(s.get("bars", 0)) for s in project.get("sections") or [])
    return bars * 4 * 60.0 / float(project.get("tempo") or 120)


def _load(project, base, tid):
    tr = _track(project, tid)
    path = _midi_path(base, tr)
    if not path.exists():
        return tr, path, [], {}
    notes, meta = midilib.read(path)
    return tr, path, notes, meta


def _select(bars_notes, sel):
    """按条件挑音符：sel 里可以给 indices / bar / toBar / pitch / minVelocity。"""
    if not sel:
        return list(range(len(bars_notes)))
    if "indices" in sel and sel["indices"] is not None:
        return [int(i) for i in sel["indices"] if 0 <= int(i) < len(bars_notes)]
    idx = []
    for i, n in enumerate(bars_notes):
        if "bar" in sel and sel["bar"] is not None and n["bar"] < float(sel["bar"]):
            continue
        if "toBar" in sel and sel["toBar"] is not None and n["bar"] > float(sel["toBar"]):
            continue
        if "pitch" in sel and sel["pitch"] is not None and int(n["pitch"]) != int(sel["pitch"]):
            continue
        if "minVelocity" in sel and n["velocity"] < int(sel["minVelocity"]):
            continue
        idx.append(i)
    return idx


def _write(project, base, tr, path, bars_notes):
    bpm = float(project.get("tempo") or 120)
    notes = midilib.from_bars(bars_notes, bpm)
    notes = midilib.clamp_notes(notes, _total_seconds(project))
    meta = {}
    if path.exists():
        try:
            _, meta = midilib.read(path)
        except Exception:
            meta = {}
    midilib.write(path, bpm, notes, name=tr["id"],
                  program=meta.get("program", 0) if not tr["id"] == "drums" else 0,
                  is_drum=(tr["id"] == "drums" or bool(meta.get("isDrum"))))
    return notes


def edit(project_path, tid, op="get", notes=None, sel=None, **kw):
    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    bpm = float(project.get("tempo") or 120)
    tr, path, raw, meta = _load(project, base, tid)
    bars_notes = midilib.to_bars(raw, bpm)
    for i, n in enumerate(bars_notes):
        n["index"] = i

    if op == "get":
        return {"ok": True, "track": tid, "tempo": bpm,
                "totalBars": sum(int(s.get("bars", 0)) for s in project.get("sections") or []),
                "notes": bars_notes, "count": len(bars_notes),
                "file": str(path), "exists": path.exists()}

    changed = 0
    if op == "add":
        add = notes or []
        if not add:
            raise ValueError("add 需要 notes")
        for n in add:
            bars_notes.append({"pitch": int(n["pitch"]), "bar": float(n.get("bar", 1)),
                               "beat": float(n.get("beat", 1)), "dur": float(n.get("dur", 1)),
                               "velocity": int(n.get("velocity", 96))})
        changed = len(add)

    elif op == "set":
        bars_notes = [{"pitch": int(n["pitch"]), "bar": float(n.get("bar", 1)),
                       "beat": float(n.get("beat", 1)), "dur": float(n.get("dur", 1)),
                       "velocity": int(n.get("velocity", 96))} for n in (notes or [])]
        changed = len(bars_notes)

    elif op == "remove":
        idx = set(_select(bars_notes, sel))
        bars_notes = [n for i, n in enumerate(bars_notes) if i not in idx]
        changed = len(idx)

    elif op == "clear":
        changed = len(bars_notes)
        bars_notes = []

    elif op in ("move", "transpose", "velocity", "quantize", "humanize", "duration"):
        idx = _select(bars_notes, sel)
        rng = np.random.RandomState(int(kw.get("seed") or 0) or None)
        for i in idx:
            n = bars_notes[i]
            if op == "move":
                n["bar"] = float(n["bar"]) + float(kw.get("barDelta", 0))
                n["beat"] = float(n["beat"]) + float(kw.get("beatDelta", 0))
                n["pitch"] = int(np.clip(int(n["pitch"]) + int(kw.get("pitchDelta", 0)), 0, 127))
                # beat 越界时把它折回相邻小节
                meter = 4.0
                while n["beat"] > meter:
                    n["beat"] -= meter
                    n["bar"] += 1
                while n["beat"] < 1.0:
                    n["beat"] += meter
                    n["bar"] -= 1
                n["bar"] = max(1.0, float(n["bar"]))
                changed += 1
            elif op == "transpose":
                n["pitch"] = int(np.clip(int(n["pitch"]) + int(kw.get("semitones", 0)), 0, 127))
                changed += 1
            elif op == "velocity":
                if "value" in kw and kw["value"] is not None:
                    n["velocity"] = int(np.clip(int(kw["value"]), 1, 127))
                else:
                    n["velocity"] = int(np.clip(round(n["velocity"] * float(kw.get("scale", 1.0))), 1, 127))
                changed += 1
            elif op == "duration":
                if "value" in kw and kw["value"] is not None:
                    n["dur"] = float(kw["value"])
                else:
                    n["dur"] = max(0.05, float(n["dur"]) * float(kw.get("scale", 1.0)))
                changed += 1
            elif op == "quantize":
                grid = float(kw.get("grid", 0.5))          # 以拍为单位，0.5 = 八分音符
                strength = float(kw.get("strength", 1.0))
                pos = (float(n["bar"]) - 1) * 4.0 + float(n["beat"]) - 1.0
                snapped = round(pos / grid) * grid
                pos = pos + (snapped - pos) * strength
                n["bar"] = int(pos // 4.0) + 1
                n["beat"] = round(pos % 4.0 + 1.0, 6)
                if kw.get("durations"):
                    n["dur"] = max(0.05, round(float(n["dur"]) / grid) * grid)
                changed += 1
            elif op == "humanize":
                jitter_s = float(kw.get("timingMs", 6.0)) / 1000.0
                bps = float(bpm) / 60.0
                n["beat"] = float(n["beat"]) + rng.normal(0, jitter_s * bps)
                vel_j = float(kw.get("velocity", 10.0))
                n["velocity"] = int(np.clip(n["velocity"] + rng.normal(0, vel_j), 1, 127))
                changed += 1

    else:
        raise ValueError(f"不支持的 op：{op}")

    notes_out = _write(project, base, tr, path, bars_notes)
    save_project(base, project)
    return {"ok": True, "track": tid, "op": op, "changed": changed,
            "count": len(notes_out), "notes": midilib.to_bars(notes_out, bpm),
            "file": str(path)}
