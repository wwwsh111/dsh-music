"""人声和声编排（harmonize）。

教程里「分和声 / 剪和声」这两步，在插件里原来是写死的：副歌第二句起，
音阶下移两级（低三度），够不着就丢。结果是同一句里和声忽有忽无、
音程永远只有一个，混起来一片糊。

这里把它变成可编排的：

* **分段**：哪几段要跟和声（默认副歌、桥段、最后一遍副歌）；
* **音程**：低三度 / 高三度 / 低六度 / 高五度 / 低八度 / 同度叠录；
* **剪**：按音长、音域、与主唱音程冲突、与和弦冲突逐音判断，剪掉的音
  记下原因，报告里能看见「为什么这条和声没跟」；
* **和弦优先**：工程里有 chordPlan 时，把候选音挪到最近的调内和弦音，
  和声落在和弦上才不糊。

产物和 compose 一致：midi/vocal-harmony.mid + analysis/vocal-harmony-align.json，
之后 `music_vocals { part: "harmony" }` 直接唱。
"""

from pathlib import Path

import numpy as np

from . import tune
from .config import SAMPLE_RATE
from .project import load_project, save_project
from .util import jdump, jload, midi_to_note, parse_chord, note_to_midi, scale_pcs

# 音程策略：名字 -> (音级步数, 说明)。步数在调内音阶上数，负数向下。
STRATEGIES = {
    "third_below": (-2, "低三度（最稳的伴唱，贴着主唱走）"),
    "third_above": (2, "高三度（亮，适合女声/和声高八度的上声部）"),
    "sixth_below": (-5, "低六度（更开，常用于副歌第二遍）"),
    "fifth_above": (4, "高五度（空灵，注意别盖主唱）"),
    "octave_below": (-7, "低八度（厚，男声垫底）"),
    "double": (0, "同度叠录（只加厚，不改音高）"),
}

ROLE_DEFAULT = {
    "chorus": "third_below",
    "prechorus": "third_below",
    "bridge": "sixth_below",
    "outro": "third_below",
}

DEFAULTS = {
    "minDur": 0.18,
    "lo": 52,           # E3，再低就糊
    "hi": 81,           # A5，再高就尖
    "maxInterval": 9,   # 超过 9 个半音（大六度）不跟，容易散
    "chordSnap": True,
}


def _base(project_path):
    base = Path(project_path)
    return base.parent if base.is_file() else base


def _sections_wanted(project, sections=None):
    if sections:
        want = [str(s).strip() for s in sections if str(s).strip()]
        return [s for s in project.get("sections", []) if s.get("id") in want]
    picks = []
    for sec in project.get("sections", []):
        sid = str(sec.get("id") or "")
        role = str(sec.get("role") or "")
        if role in ROLE_DEFAULT or any(k in sid for k in ("chorus", "bridge", "outro")):
            picks.append(sec)
    return picks or [s for s in project.get("sections", []) if "chorus" in str(s.get("id"))]


def _section_start_beat(project):
    starts, beat = {}, 0.0
    for sec in project.get("sections", []):
        starts[sec.get("id")] = beat
        beat += float(sec.get("bars") or 0) * 4.0
    return starts


def _chord_at(project, section, beat_in_sec):
    plan = section.get("chordPlan") or []
    if not plan:
        ch = section.get("chords") or []
        if not ch:
            return None
        idx = min(len(ch) - 1, int(beat_in_sec // 4))
        return ch[idx]
    cur = None
    for row in plan:
        t = float(row.get("bar") or 0) * 4.0 + float(row.get("beat") or 0)
        if t <= beat_in_sec + 1e-6:
            cur = row.get("chord")
        else:
            break
    return cur or (plan[0].get("chord") if plan else None)


def _chord_pcs(symbol, key):
    """和弦音级集合（只用根音 + 音程表，够判断候选音落没落在和弦上）。"""
    try:
        ch = parse_chord(symbol, key)
    except Exception:
        return None
    if not ch:
        return None
    root = int(ch.get("root_pc") or 0)
    ivs = ch.get("intervals") or []
    if not ivs:
        return None
    return {(root + int(i)) % 12 for i in ivs}


def _scale_index(scale, p):
    """在调内音阶表里找到 p 的序号；不在表里就取最近的。"""
    if p in scale:
        return scale.index(p)
    return int(np.argmin([abs(x - p) for x in scale]))


def _harmony_pitch(p, steps, scale, key, chord_pcs=None, snap=True):
    """按调内音级步数取和声音，可选吸附到和弦音。"""
    if steps == 0:
        cand = p
    else:
        idx = _scale_index(scale, p) + steps
        if idx < 0 or idx >= len(scale):
            return None
        cand = scale[idx]
    if not snap or not chord_pcs:
        return cand
    # 在候选音附近 ±1 个调内音级里找和弦音，音程不变得更难听才换
    idx = _scale_index(scale, cand)
    options = [cand]
    for d in (-1, 1):
        j = idx + d
        if 0 <= j < len(scale):
            options.append(scale[j])
    good = [x for x in options if x % 12 in chord_pcs]
    if not good:
        return cand
    best = min(good, key=lambda x: (abs(x - cand), x))
    # 吸附只允许挪半音级：超过 1 个半音就不是「同一个音程」了，
    # 低三度会被拉到低五度，和声一下变空，宁可不对和弦也不改音程。
    if abs(best - cand) > 1:
        return cand
    return best


def plan(project_path, sections=None, strategy=None, cut="default", density="full",
         write=True, sing=None):
    """给主旋律编和声。

    cut: "off" 只按调内/音域剪（不做时值与时程冲突剪枝）；"default" 再剪短音、贴太近、音程过宽；
    "strict" 只留落在和弦音上的候选。
    density: "full" 每音都跟；"half" 只跟长音（≥0.3 s）；"phrase" 只跟每句尾音。
    """
    base = _base(project_path)
    project = load_project(base)
    notes, src = tune.load_notes(base)
    key = project.get("key") or {"tonic": "C", "pc": 0, "mode": "major"}
    spb = 60.0 / float(project.get("tempo") or 120)
    starts = _section_start_beat(project)
    picks = _sections_wanted(project, sections)
    pick_ids = {s.get("id") for s in picks}
    sec_by_id = {s.get("id"): s for s in project.get("sections", [])}
    scale = [p for p in range(36, 97) if p % 12 in scale_pcs(key)]
    limits = dict(DEFAULTS)

    harm, meta, cuts = [], [], {}
    per_section = {}
    for i, n in enumerate(notes):
        sid = n.get("section")
        if sid not in pick_ids:
            continue
        sec = sec_by_id.get(sid) or {}
        role = str(sec.get("role") or "")
        strat = strategy or ROLE_DEFAULT.get(role) or "third_below"
        if strat == "auto":
            strat = "sixth_below" if sec is picks[-1] and role == "chorus" else "third_below"
        steps = STRATEGIES.get(strat, (-2, ""))[0]

        def cut_note(reason):
            cuts[reason] = cuts.get(reason, 0) + 1

        dur = float(n["dur"])
        if cut != "off" and dur < limits["minDur"]:
            cut_note("tooShort")
            continue
        if density == "half" and dur < 0.3:
            cut_note("density")
            continue
        if density == "phrase" and i + 1 < len(notes) and \
                notes[i + 1].get("line") == n.get("line"):
            cut_note("density")
            continue

        beat_abs = float(n.get("beat") if n.get("beat") is not None
                         else n["start"] / spb)
        chord = _chord_at(project, sec, beat_abs - starts.get(sid, 0.0))
        pcs = _chord_pcs(chord, key) if chord else None
        hp = _harmony_pitch(n["midi"], steps, scale, key, pcs,
                            snap=limits["chordSnap"])
        if hp is None:
            cut_note("outOfScale")
            continue
        semis = hp - n["midi"]
        # 八度策略本来就是 12 个半音，不能拿普通音程上限去剪它
        max_iv = 12 if strat == "octave_below" else limits["maxInterval"]
        if steps != 0 and cut != "off":
            if abs(semis) < 3:
                cut_note("tooClose")
                continue
            if abs(semis) > max_iv:
                cut_note("tooWide")
                continue
        if hp < limits["lo"] or hp > limits["hi"]:
            cut_note("outOfRange")
            continue
        if hp == n["midi"] and steps != 0:
            cut_note("unison")
            continue
        if cut == "strict":
            if pcs and hp % 12 not in pcs:
                cut_note("notChordTone")
                continue
            prev = harm[-1] if harm else None
            if prev and prev[0] == hp and abs(prev[1] - n["start"]) < 0.06:
                cut_note("repeat")
                continue

        t = float(n["start"])
        harm.append((int(hp), t, dur * 0.95, 96))
        meta.append({"char": n.get("char") or "", "pitch": midi_to_note(int(hp)),
                     "start": round(t, 3), "duration": round(dur * 0.95, 3),
                     "beat": n.get("beat"), "section": sid,
                     "line": n.get("line"), "strategy": strat,
                     "interval": int(semis), "chord": chord})
        per_section[sid] = per_section.get(sid, 0) + 1

    src_name = "vocal-harmony"
    out_dir = base / "midi"
    report = {"ok": True, "score": src, "sections": sorted(pick_ids),
              "strategies": {sid: (strategy or ROLE_DEFAULT.get(
                  str((sec_by_id.get(sid) or {}).get("role") or "")) or "third_below")
                  for sid in pick_ids},
              "harmonyNotes": len(meta), "leadNotes": len(notes),
              "cut": cuts, "perSection": per_section}

    if not write:
        report["dryRun"] = True
        return report

    if not meta:
        for p in (out_dir / f"{src_name}.mid", base / "analysis" / "vocal-harmony-align.json"):
            if p.exists():
                p.unlink()
        project.setdefault("vocal", {})["harmony"] = False
        save_project(base, project)
        report["harmonyNotes"] = 0
        jdump(base / "analysis" / "harmony-plan.json", report)
        return report

    import pretty_midi
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=53, name=src_name)
    for midi, start, dur, vel in harm:
        inst.notes.append(pretty_midi.Note(velocity=int(vel), pitch=int(midi),
                                           start=float(start),
                                           end=float(start + max(0.05, dur))))
    pm.instruments.append(inst)
    out_dir.mkdir(parents=True, exist_ok=True)
    pm.write(str(out_dir / f"{src_name}.mid"))
    jdump(base / "analysis" / "vocal-harmony-align.json", {"notes": meta})
    jdump(base / "analysis" / "harmony-plan.json", report)

    project.setdefault("vocal", {})["harmony"] = True
    project["vocal"]["harmonyPlan"] = {"sections": sorted(pick_ids),
                                       "strategy": strategy or "auto",
                                       "cut": cut, "density": density}
    save_project(base, project)
    report["midi"] = str(out_dir / f"{src_name}.mid")
    report["align"] = str(base / "analysis" / "vocal-harmony-align.json")

    if sing:
        from . import vocals
        report["sing"] = vocals.sing(base, section=sing, part="harmony",
                                     wait=False)
    return report


def show(project_path):
    base = _base(project_path)
    rep = jload(base / "analysis" / "harmony-plan.json", None)
    align = jload(base / "analysis" / "vocal-harmony-align.json", None)
    return {"ok": True, "plan": rep,
            "notes": len((align or {}).get("notes") or []),
            "midiExists": (base / "midi" / "vocal-harmony.mid").exists()}


def clear(project_path):
    base = _base(project_path)
    removed = []
    for p in (base / "midi" / "vocal-harmony.mid",
              base / "analysis" / "vocal-harmony-align.json",
              base / "analysis" / "harmony-plan.json"):
        if p.exists():
            p.unlink()
            removed.append(str(p))
    try:
        project = load_project(base)
        project.setdefault("vocal", {})["harmony"] = False
        save_project(base, project)
    except Exception:
        pass
    return {"ok": True, "removed": removed}
