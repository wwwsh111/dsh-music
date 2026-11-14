"""MIDI 读写的小底座：钢琴卷帘和 Pattern 都在这上面改音符。

pretty_midi 的时间是秒，人想的是「第几小节第几拍」——所以统一在这里换算：
写着 `bar: 5, beat: 2.5` 的音符，落到文件里是准确的秒。
"""

from pathlib import Path

import numpy as np


def _pm():
    import pretty_midi
    return pretty_midi


def read(path):
    """读一个 MIDI 文件：返回 (notes, meta)。

    notes 里每条是 {pitch, start, end, velocity}，start/end 是秒。
    """
    path = Path(path)
    pm = _pm()
    m = pm.PrettyMIDI(str(path))
    notes, meta = [], {"tempo": m.get_tempo_changes()[1][0] if len(m.get_tempo_changes()[1]) else 120.0,
                       "tracks": []}
    for inst in m.instruments:
        meta["tracks"].append({"name": inst.name, "program": inst.program,
                               "isDrum": bool(inst.is_drum), "notes": len(inst.notes)})
        for n in inst.notes:
            notes.append({"pitch": int(n.pitch), "start": float(n.start),
                          "end": float(n.end), "velocity": int(n.velocity)})
    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    if len(m.instruments) == 1:
        meta["program"] = m.instruments[0].program
        meta["isDrum"] = bool(m.instruments[0].is_drum)
        meta["name"] = m.instruments[0].name
    return notes, meta


def write(path, bpm, notes, name="track", program=0, is_drum=False):
    pm = _pm()
    m = pm.PrettyMIDI(initial_tempo=float(bpm))
    inst = pm.Instrument(program=int(program), is_drum=bool(is_drum), name=str(name))
    for n in notes:
        start = max(0.0, float(n["start"]))
        end = max(start + 0.02, float(n["end"]))
        inst.notes.append(pm.Note(velocity=int(np.clip(n.get("velocity", 96), 1, 127)),
                                  pitch=int(np.clip(n["pitch"], 0, 127)),
                                  start=start, end=end))
    m.instruments.append(inst)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    m.write(str(path))
    return len(notes)


# ---- 小节/拍 ↔ 秒 -------------------------------------------------------

def bar_seconds(bpm, meter=4.0):
    return meter * 60.0 / float(bpm or 120.0)


def to_bars(notes, bpm, meter=4.0):
    """秒 → {bar(1起), beat(1起), dur_beats}，给人和工具看。"""
    spb = 60.0 / float(bpm or 120.0)
    out = []
    for n in notes:
        pos = n["start"] / spb                    # 拍（0 起）
        dur = (n["end"] - n["start"]) / spb
        out.append({
            "pitch": int(n["pitch"]),
            "bar": int(pos // meter) + 1,
            "beat": round(pos % meter + 1.0, 4),
            "dur": round(dur, 4),
            "velocity": int(n.get("velocity", 96)),
        })
    return out


def from_bars(notes, bpm, meter=4.0):
    """{bar,beat,dur} → 秒。beat 可以带小数，bar 从 1 数。"""
    spb = 60.0 / float(bpm or 120.0)
    out = []
    for n in notes:
        pos = ((float(n.get("bar", 1)) - 1) * meter + (float(n.get("beat", 1)) - 1)) * spb
        dur = float(n.get("dur", 1.0)) * spb
        out.append({"pitch": int(n["pitch"]), "start": pos, "end": pos + max(dur, 0.02),
                    "velocity": int(n.get("velocity", 96))})
    return out


def shift(notes, beats, bpm):
    d = float(beats) * 60.0 / float(bpm or 120.0)
    return [{**n, "start": n["start"] + d, "end": n["end"] + d} for n in notes
            if n["start"] + d >= 0]


def clamp_notes(notes, total_seconds):
    out = []
    for n in notes:
        if n["start"] >= total_seconds:
            continue
        e = min(n["end"], total_seconds)
        if e - n["start"] < 0.02:
            continue
        out.append({**n, "end": e})
    return out
