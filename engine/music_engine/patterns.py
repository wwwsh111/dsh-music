"""Patterns / Playlist：把一小段音乐存成可复用的块，再按段落摆到时间线上。

FL 里这套东西的价值不在"重复"，而在**改一处、全曲跟着变**：
主歌那 4 小节存成一个 pattern，摆两遍就是 8 小节；等到要改鼓，
改 pattern 本身，两处一起变。

数据结构：

    project["patterns"] = {
      "verse": {"name": "verse", "bars": 4,
                "tracks": {"drums": [{pitch, bar, beat, dur, velocity}, ...], ...}}
    }
    project["playlist"] = [
      {"pattern": "verse", "startBar": 5, "repeat": 2, "tracks": ["drums", "bass"]}
    ]

pattern 里的 bar 是**相对**的（从 1 数），摆到 playist 上由 startBar 决定绝对位置。
`apply` 会把 playlist 展开写回 `midi/<track>.mid`，之后照常 render/mix。
"""

from pathlib import Path

import numpy as np

from . import midilib
from .project import load_project, save_project


def _base(project_path):
    base = Path(project_path)
    return base.parent if base.is_file() else base


def _total_bars(project):
    return sum(int(s.get("bars", 0)) for s in project.get("sections") or [])


def _track(project, tid):
    for tr in project.get("tracks") or []:
        if str(tr.get("id")) == str(tid):
            return tr
    return None


def _midi_path(base, tr):
    return base / (tr.get("midi") or f"midi/{tr['id']}.mid")


def _capture(project, base, start_bar, bars, tracks=None):
    """把 [start_bar, start_bar+bars) 的音符抠出来，bar 改成相对值。"""
    bpm = float(project.get("tempo") or 120)
    out = {}
    for tr in project.get("tracks") or []:
        tid = tr["id"]
        if tracks and tid not in tracks:
            continue
        path = _midi_path(base, tr)
        if not path.exists():
            continue
        notes, _ = midilib.read(path)
        picked = []
        for n in midilib.to_bars(notes, bpm):
            if start_bar <= n["bar"] < start_bar + bars:
                picked.append({"pitch": n["pitch"], "bar": n["bar"] - start_bar + 1,
                               "beat": n["beat"], "dur": n["dur"],
                               "velocity": n["velocity"]})
        if picked:
            out[tid] = picked
    return out


def _expand(project, patterns, playlist):
    """把 playlist 展开成 {tid: [绝对位置的音符]}。"""
    out = {}
    for entry in playlist:
        pid = entry.get("pattern")
        pat = patterns.get(pid)
        if not pat:
            continue
        start = float(entry.get("startBar") or 1)
        repeat = max(1, int(entry.get("repeat") or 1))
        only = entry.get("tracks")
        bars = float(pat.get("bars") or 4)
        for tid, notes in (pat.get("tracks") or {}).items():
            if only and tid not in only:
                continue
            for r in range(repeat):
                off = start + r * bars - 1
                for n in notes:
                    out.setdefault(tid, []).append({
                        "pitch": int(n["pitch"]),
                        "bar": float(n["bar"]) + off,
                        "beat": float(n["beat"]),
                        "dur": float(n.get("dur", 1)),
                        "velocity": int(n.get("velocity", 96)),
                    })
    return out


def _write_track(project, base, tid, notes_bars):
    tr = _track(project, tid)
    if tr is None:
        return None
    path = _midi_path(base, tr)
    bpm = float(project.get("tempo") or 120)
    meta = {}
    if path.exists():
        try:
            _, meta = midilib.read(path)
        except Exception:
            meta = {}
    total = _total_bars(project) * 4 * 60.0 / bpm
    notes = midilib.clamp_notes(midilib.from_bars(notes_bars, bpm), total)
    midilib.write(path, bpm, notes, name=tid,
                  program=0 if tid == "drums" else meta.get("program", 0),
                  is_drum=(tid == "drums" or bool(meta.get("isDrum"))))
    return len(notes)


def edit(project_path, op="list", name=None, start_bar=None, bars=None, tracks=None,
         pattern=None, repeat=1, index=None):
    project = load_project(project_path)
    base = _base(project_path)
    patterns = dict(project.get("patterns") or {})
    playlist = list(project.get("playlist") or [])

    if op == "list":
        return {"ok": True,
                "patterns": {pid: {"name": p.get("name"), "bars": p.get("bars"),
                                   "tracks": {t: len(v) for t, v in (p.get("tracks") or {}).items()}}
                             for pid, p in patterns.items()},
                "playlist": playlist,
                "totalBars": _total_bars(project)}

    if op == "capture":
        if not name:
            raise ValueError("capture 需要 name")
        start_bar = int(start_bar or 1)
        bars = int(bars or 4)
        got = _capture(project, base, start_bar, bars, tracks)
        if not got:
            raise ValueError(f"第 {start_bar}–{start_bar + bars - 1} 小节没有音符可以抓")
        pid = str(name)
        patterns[pid] = {"name": pid, "bars": bars,
                         "fromBar": start_bar, "tracks": got}
        project["patterns"] = patterns
        save_project(base, project)
        return {"ok": True, "pattern": pid, "bars": bars,
                "tracks": {t: len(v) for t, v in got.items()},
                "note": f"已从第 {start_bar} 小节抓起，共 {sum(len(v) for v in got.values())} 个音符"}

    if op == "place":
        pid = str(pattern or name or "")
        if pid not in patterns:
            raise ValueError(f"没有这个 pattern：{pid}")
        entry = {"pattern": pid, "startBar": int(start_bar or 1),
                 "repeat": max(1, int(repeat))}
        if tracks:
            entry["tracks"] = list(tracks)
        playlist.append(entry)
        playlist.sort(key=lambda e: float(e.get("startBar") or 1))
        project["playlist"] = playlist
        save_project(base, project)
        return {"ok": True, "playlist": playlist,
                "note": f"{pid} 摆到第 {entry['startBar']} 小节，重复 {entry['repeat']} 次"}

    if op == "unplace":
        if index is not None:
            if not (0 <= int(index) < len(playlist)):
                raise ValueError(f"playlist 没有第 {index} 条")
            removed = playlist.pop(int(index))
        else:
            removed = [e for e in playlist if e.get("pattern") == str(pattern or name)]
            playlist = [e for e in playlist if e.get("pattern") != str(pattern or name)]
        project["playlist"] = playlist
        save_project(base, project)
        return {"ok": True, "removed": removed, "playlist": playlist}

    if op == "delete":
        pid = str(pattern or name or "")
        patterns.pop(pid, None)
        playlist = [e for e in playlist if e.get("pattern") != pid]
        project["patterns"] = patterns
        project["playlist"] = playlist
        save_project(base, project)
        return {"ok": True, "patterns": list(patterns), "playlist": playlist}

    if op == "apply":
        if not playlist:
            raise ValueError("playlist 是空的，先用 place 摆 pattern")
        expanded = _expand(project, patterns, playlist)
        written = {}
        for tid, notes in expanded.items():
            n = _write_track(project, base, tid, notes)
            if n is not None:
                written[tid] = n
        save_project(base, project)
        return {"ok": True, "written": written, "playlist": playlist,
                "note": "已展开写回 midi/，接着 render 就能听见"}

    raise ValueError(f"不支持的 op：{op}")
