"""编曲：把段落、和声、律动、人声组装成 MIDI。

每个乐器一个函数，输入「段落编排」输出音符列表 (midi, start_sec, dur_sec, vel)。
编排（arrangement）先由 harmony 计划和弦，再由 kit 计划鼓，其余乐器跟着这两样走——
贝斯跟底鼓、吉他也跟底鼓，低音和节奏才咬得紧。
"""

import math
import random
import re
from pathlib import Path

from . import kit, melody, performance, styles
from .genres import get_genre
from .harmony import (bass_pitch, cadence_note, plan_progression, power_chord,
                      voice_chords, _pcs)
from .lyrics import load_lyrics
from .project import load_project, save_project
from .util import (clamp, ensure_dir, jdump, midi_to_note, note_to_midi,
                   parse_chord, scale_pcs)

PROGRAMS = {
    "drums": 0, "bass": 34, "gtr_rhythm_l": 27, "gtr_rhythm_r": 27,
    "gtr_lead": 27, "keys": 4, "pad": 89, "lead": 81, "vocal": 53,
}

# 每个乐器用哪种音色处理（render.py 按这个跑 DSP 链）
FX = {
    "drums": "drum_bus", "bass": "bass",
    "gtr_rhythm_l": "amp_rhythm", "gtr_rhythm_r": "amp_rhythm",
    "gtr_lead": "amp_lead", "keys": "keys", "pad": "pad", "lead": "synth",
    "vocal": "vocal", "vocal-harmony": "vocal",
}

ROLE_ORDER = ["intro", "verse", "prechorus", "chorus", "bridge", "solo", "outro"]


def _role(sec):
    return str(sec.get("role") or re.sub(r"\d+$", "", str(sec.get("id", "")))).lower()


def _is_rock(genre):
    return genre in ("rock", "metal", "punk", "emo", "vocalrock")


# --------------------------------------------------------------- 编排

def plan_arrangement(project, pack, rng, profile=None):
    genre = project["genre"]
    key = project["key"]
    brief = " ".join(project.get("modifiers") or [])
    profile = profile or styles.make_profile(project, pack, rng)
    drum_set = styles.drums_variant(profile, rng, genre=genre)
    arr = {}
    sections = project["sections"]
    for idx, sec in enumerate(sections):
        role = _role(sec)
        bars = int(sec["bars"])
        energy = float(sec.get("energy") or 0.6)
        half_bar = _is_rock(genre) and role in ("chorus", "solo", "prechorus") and bars >= 8
        chords = plan_progression(genre, key, role, bars, rng, half_bar=half_bar,
                                  pack=pack, bpm=project.get("tempo"), energy=energy,
                                  final_section=(idx == len(sections) - 1))
        chords = _vary_chords(chords, key, rng, role, bars, energy)
        drums = kit.section_groove(genre, role, bars, rng, energy, brief, phrase=4,
                                   variant=drum_set, swing=styles.swing_for(genre))
        if role == "intro" and _is_rock(genre):
            # 前奏只要镲片和最后一小节的军鼓滚奏，吉他负责主题
            drums = [(b, l, v) for b, l, v in drums
                     if l in ("crash", "ride", "hat", "open_hat")]
            drums += [(bars * 4.0 - 4.0 + i * 0.25, "snare", 70 + i * 6) for i in range(12)]
        arr[sec["id"]] = {
            "role": role, "bars": bars, "energy": energy,
            "startBar": int(sec["startBar"]),
            "chords": chords,
            "drums": drums,
            "kickBeats": kit.kick_beats(drums),
            "halfBar": half_bar,
        }
    return arr


def _vary_chords(chords, key, rng, role, bars, energy):
    """和弦节奏的变化：换和弦的点不再永远落在小节头。

    摇滚里和弦常「提前半拍」进来（push），或者在第 3 拍补一次切分；
    重复的副歌还要有替代和弦，否则同样是 I-V-vi-IV 反复听就腻。
    """
    if not chords or bars < 4:
        return chords
    out = []
    for i, (bar, beat, sym) in enumerate(chords):
        out.append([bar, beat, sym])
    # 段落内部的「推」：每 4 小节至少有一次提前半拍
    push_bars = [b for b in range(bars) if b % 4 == 2]
    for row in out:
        if row[0] in push_bars and row[1] == 0.0 and rng.random() < 0.45:
            row[1] = 0.5
            prev_bar = row[0] - 1
            if prev_bar >= 0:
                for r2 in out:
                    if r2[0] == prev_bar and abs(r2[1]) < 1e-6:
                        # 前一小节最后一拍补一个切分重击，把提前的和弦「接住」
                        r2[1] = 0.0
                        break
    out.sort(key=lambda r: (r[0], r[1]))
    return [(int(b), float(bt), s) for b, bt, s in out]


def _sec_beats(arr_sec):
    return arr_sec["bars"] * 4.0


# --------------------------------------------------------------- 鼓

def drums_notes(arr, spb):
    out = []
    for sec in arr.values():
        base = (sec["startBar"] - 1) * 4.0
        for beat, lane, vel in sec["drums"]:
            dur = 0.5 if lane in ("crash", "open_hat") else 0.12
            out.append((kit.GM.get(lane, 38), (base + beat) * spb, dur * spb,
                        int(clamp(vel * (0.75 + 0.35 * sec["energy"]), 20, 127))))
    return out


# --------------------------------------------------------------- 贝斯

def bass_notes(arr, project, rng, spb):
    """贝斯：跟底鼓起音，但根音不是唯一选择。

    弱拍给五度/八度/三音，换和弦前用半音或级进接近下一个根音，
    小节尾用五度/八度推过去——低音才有「走」的线条，
    而不是每小节在根音上砸四下。属和弦后排不落根音，避免「句句回家」。
    """
    key = project["key"]
    rock = _is_rock(project["genre"])
    out = []
    for sec in arr.values():
        base = (sec["startBar"] - 1) * 4.0
        energy = sec["energy"]
        chords = sec["chords"]
        total = _sec_beats(sec)
        kicks = set(sec["kickBeats"])
        onsets = set(kicks)
        if energy >= 0.75:
            onsets |= {round(b * 0.5, 2) for b in range(int(total * 2))}
        elif energy >= 0.45:
            onsets |= {float(b) for b in range(int(total))}
        # 16 分抢拍：高能量段偶尔在拍点前 1/4 推一下
        if energy >= 0.7:
            for b in sorted(onsets):
                if abs(b % 1.0) < 1e-6 and rng.random() < 0.10:
                    onsets.add(round(b + 0.75, 2))
        events = sorted(onsets)
        for idx, beat in enumerate(events):
            if beat >= total:
                continue
            bar = int(beat // 4)
            local = beat - bar * 4
            sym = _chord_between(chords, bar, local)
            nxt = events[idx + 1] if idx + 1 < len(events) else total
            seg = max(0.25, min(nxt - beat, 2.0))
            ch = parse_chord(sym, key)
            root = bass_pitch(sym, key, octave=2)
            pitch = root
            dur = seg * 0.9
            vel = 106 if abs(local) < 1e-6 else (100 if abs(local - 2.0) < 1e-6 else 92)

            # 弱拍：五度/八度/三音，低音才有横向线条
            if abs(local % 1.0) > 1e-6 or abs(local - 2.0) < 1e-6:
                if rng.random() < 0.35:
                    pitch = root + rng.choice([7, 12])
                elif rng.random() < 0.12:
                    pitch = root + ch["intervals"][1]

            # 换和弦前的最后一拍：半音/级进接近下一个根音
            if idx + 1 < len(events) and nxt - beat <= 1.0:
                next_sym = _chord_between(chords, bar, local + 1.0)
                if next_sym != sym:
                    target = bass_pitch(next_sym, key, octave=2)
                    if rng.random() < 0.55:
                        pitch = target - 1 if rng.random() < 0.6 else target - 2
                        dur = min(dur, 0.45)
                        vel = 96

            # 小节尾半拍过门：五度或高八度，把下一小节推过去
            if abs(local - 3.5) < 1e-6 and rng.random() < (0.55 if rock else 0.25):
                pitch = root + rng.choice([7, 12])
                dur = min(dur, 0.4)
                vel = 100
            elif abs(local - 2.5) < 1e-6 and rng.random() < 0.15:
                pitch = root + 12
            # 属和弦不落在根音上，避免「每小节都回家」
            if rock and str(sym).upper().startswith("V") and local >= 2.0:
                pitch += 7
            out.append((int(pitch), (base + beat) * spb, max(0.12, dur) * spb, vel))
    return out


def _chord_between(chord_events, bar, local_beat):
    cur = chord_events[0][2] if chord_events else "I"
    for b, beat, sym in chord_events:
        if (b * 4 + beat) <= (bar * 4 + local_beat) + 1e-6:
            cur = sym
        else:
            break
    return cur


# --------------------------------------------------------------- 吉他

def guitar_rhythm_notes(arr, project, rng, spb, offset_beats=0.0, layer="l", profile=None):
    """双轨节奏吉他：同一谱面、不同起音微差和力度，叠起来才是「墙」。

    摇滚的动态差全靠奏法切换（profile 决定哪段用哪种）：
      主歌  闷音 8 分或半拍长音，短、干，给军鼓让路
      副歌  开放 8 分或切分，长音互相压住，形成持续的和声墙
      最后一遍副歌 可能升到 16 分「墙」把歌推到顶
    每小节还有微变化（乐句末 16 分推进、抽音、3.75 抢先），
    所以 8 小节不会听起来是同一小节复制 8 遍。
    """
    key = project["key"]
    rock = _is_rock(project["genre"])
    profile = profile or {}
    strum_map = profile.get("strum") or {"verse": "chug8", "chorus": "open8"}
    out = []
    chorus_seen = 0
    for sec in arr.values():
        role = sec["role"]
        if role in ("intro",) and rock:
            out += _intro_riff(sec, key, base_spb=spb, rng=rng)
            continue
        base = (sec["startBar"] - 1) * 4.0
        energy = sec["energy"]

        if role == "outro":
            # 尾奏：只在最后两小节给收尾和弦，前面对空，留混响尾巴
            bars = sec["bars"]
            for bar in range(bars):
                if bar < bars - 2:
                    continue
                sym = _chord_between(sec["chords"], bar, 0.0)
                chord = power_chord(sym, key, root_octave=2)
                last = bar == bars - 1
                dur = 8.0 if last else 4.0
                pos = bar * 4
                for pitch in chord:
                    out.append((pitch + (12 if last else 0),
                                (base + pos + offset_beats) * spb,
                                dur * spb, 112))
            continue

        if role == "chorus":
            chorus_seen += 1
            last_chorus = chorus_seen >= _chorus_total(project) and chorus_seen > 1
            pattern_name = (profile.get("strumLastChorus") or strum_map.get("chorus", "open8")) \
                if last_chorus else strum_map.get("chorus", "open8")
        else:
            pattern_name = strum_map.get(role) or strum_map.get("verse", "chug8")

        muted = pattern_name in ("chug8", "sync", "drive16") and energy < 0.8
        bars = sec["bars"]
        for bar in range(bars):
            hits = styles.strum_bar(pattern_name, rng, bar, bars, energy)
            for local, dur_beats in hits:
                pos = bar * 4 + local
                sym = _chord_between(sec["chords"], bar, local)
                chord = power_chord(sym, key, root_octave=2)
                strong = abs(local % 1.0) < 1e-6
                if muted:
                    dur = min(dur_beats * 0.55, 0.34)
                    vel = 112 if strong else 96
                else:
                    dur = max(dur_beats * 0.92, 0.3)
                    vel = 120 if strong else 100
                vel = int(clamp(vel * (0.80 + 0.26 * energy), 40, 127))
                jitter = 0.0 if layer == "l" else rng.uniform(0.004, 0.012)
                # 右轨每 4 小节换一次根音位置（低音区/高音区），双轨不糊成一坨
                oct_shift = 0 if layer == "l" else (12 if (bar // 4) % 2 else 0)
                for pitch in chord:
                    out.append((pitch + oct_shift,
                                (base + pos + offset_beats + jitter) * spb,
                                dur * spb, vel))
    return out


def _chorus_total(project):
    return max(1, sum(1 for s in project["sections"] if _role(s) == "chorus"))


def _chord_len(chord_events, bar, beat):
    """这个和弦持续多少拍（跨小节算，慢歌两小节一换才不会被截断）。"""
    cur = bar * 4.0 + beat
    nxt = 16.0
    for b, bt, _sym in chord_events:
        pos = b * 4.0 + bt
        if pos > cur + 1e-6:
            nxt = pos - cur
            break
    return max(0.5, nxt)


def _intro_riff(sec, key, base_spb, rng):
    """前奏主题：一首歌的「记忆点」往往在这里先出现，所以用 8 分 + 留白。"""
    out = []
    base = (sec["startBar"] - 1) * 4.0
    scale = _scale_pitches(key, note_to_midi("E3") - 7, note_to_midi("E5"))
    bars = sec["bars"]
    for bar in range(bars):
        sym = _chord_between(sec["chords"], bar, 0.0)
        chord = power_chord(sym, key, root_octave=2)
        motif = [(0.0, 0.5), (0.5, 0.5), (1.5, 0.5), (2.0, 0.5), (3.0, 0.5), (3.5, 0.5)]
        last_bar = bar >= bars - 2
        if last_bar:
            motif = [(i * 0.25, 0.22) for i in range(16)]
        for local, dur in motif:
            vel = 100 if abs(local % 1.0) < 1e-6 else 88
            for pitch in chord:
                out.append((pitch, (base + bar * 4 + local) * base_spb, dur * base_spb, vel))
    return out


def _scale_pitches(key, lo, hi):
    pcs = scale_pcs(key)
    return [p for p in range(lo, hi + 1) if p % 12 in pcs]


def guitar_lead_notes(arr, project, rng, spb):
    """主音吉他：solo 走音阶，副歌给长音 hook，预副歌末两小节上行过门。"""
    key = project["key"]
    out = []
    for sec in arr.values():
        role = sec["role"]
        if role == "solo":
            out += _solo_notes(sec, key, rng, spb)
        elif role == "chorus":
            out += _hook_notes(sec, key, rng, spb)
        elif role == "prechorus":
            out += _pc_lick(sec, key, rng, spb)
        elif role == "outro":
            bars = sec["bars"]
            base = (sec["startBar"] - 1) * 4.0
            sym = _chord_between(sec["chords"], bars - 1, 0.0)
            tone = _chord_tone_at(sym, key, note_to_midi("E5"))
            out.append((tone, (base + (bars - 1) * 4) * spb, 6.0 * spb, 104))
    return out


def _solo_notes(sec, key, rng, spb):
    base = (sec["startBar"] - 1) * 4.0
    scale = _scale_pitches(key, note_to_midi("A3"), note_to_midi("E6"))
    pent = [p for p in scale if (p % 12) in
            {(key["pc"] + i) % 12 for i in ([0, 3, 5, 7, 10] if key["mode"] == "minor"
                                            else [0, 2, 4, 7, 9])}]
    pool = pent if len(pent) >= 6 else scale
    bars = sec["bars"]
    idx = len(pool) // 2
    out = []
    for bar in range(bars):
        sym = _chord_between(sec["chords"], bar, 0.0)
        phase = bar % 4
        if phase in (0, 1):
            steps, direction = [0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75,
                                2.0, 2.5, 3.0, 3.5], 1
        elif phase == 2:
            steps, direction = [0.0, 2.0], 0
        else:
            steps, direction = [0.0, 0.5, 1.0, 1.5, 2.0, 2.25, 2.5, 2.75,
                                3.0, 3.25, 3.5, 3.75], -1
        for k, local in enumerate(steps):
            if direction == 0:
                idx = min(range(len(pool)), key=lambda i: abs(pool[i] - note_to_midi("E5")))
                dur = 2.0 if k == 0 else 1.2
            else:
                if k > 0:
                    idx = int(clamp(idx + direction * rng.choice([1, 1, 2, -1]),
                                    0, len(pool) - 1))
                dur = 0.5 if phase != 3 else 0.28
            pitch = pool[idx]
            if abs(local % 1.0) < 1e-6:
                midi = _chord_tone_at(sym, key, pitch)
                idx = min(range(len(pool)), key=lambda i: abs(pool[i] - midi))
                pitch = pool[idx]
            vel = int(clamp(100 + rng.randint(-6, 8), 60, 120))
            out.append((pitch, (base + bar * 4 + local) * spb, dur * spb, vel))
    return out


def _hook_notes(sec, key, rng, spb):
    """副歌上空的一条长音吉他线：两三拍一个音，落在和弦音上，和人声错开呼吸。"""
    base = (sec["startBar"] - 1) * 4.0
    out = []
    prev = note_to_midi("G4")
    for bar in range(sec["bars"]):
        for beat in (0.0, 2.0):
            sym = _chord_between(sec["chords"], bar, beat)
            target = _chord_tone_at(sym, key, prev + rng.choice([-2, 2, 3, -3, 0]))
            target = int(clamp(target, note_to_midi("F4"), note_to_midi("A5")))
            dur = 2.0 if beat == 0.0 else 1.6
            out.append((target, (base + bar * 4 + beat) * spb, dur * spb,
                        int(clamp(96 + rng.randint(-6, 6), 60, 118))))
            prev = target
        # 每两小节尾巴加一个 8 分小尾巴，避免长音太呆
        if bar % 2 == 1:
            sym = _chord_between(sec["chords"], bar, 3.5)
            out.append((_chord_tone_at(sym, key, prev + 4),
                        (base + bar * 4 + 3.5) * spb, 0.4 * spb, 92))
    return out


def _pc_lick(sec, key, rng, spb):
    """预副歌最后两小节：16 分上行推上去，交给副歌。"""
    base = (sec["startBar"] - 1) * 4.0
    scale = _scale_pitches(key, note_to_midi("B3"), note_to_midi("A5"))
    start_bar = max(0, sec["bars"] - 2)
    out = []
    idx = len(scale) // 3
    for bar in range(start_bar, sec["bars"]):
        for k in range(16):
            local = k * 0.25
            idx = int(clamp(idx + (1 if k % 2 == 0 else rng.choice([0, 1])),
                            0, len(scale) - 1))
            vel = int(clamp(78 + k * 2.4, 60, 122))
            out.append((scale[idx], (base + bar * 4 + local) * spb, 0.22 * spb, vel))
    return out


def _chord_tone_at(sym, key, pitch):
    pcs = set(_pcs(sym, key))
    best = pitch
    bestd = 99
    for p in range(pitch - 5, pitch + 6):
        if p % 12 in pcs and abs(p - pitch) < bestd:
            best, bestd = p, abs(p - pitch)
    return best


# --------------------------------------------------------------- 键盘 / 铺底

def keys_notes(arr, project, rng, spb, profile=None):
    """键盘：不再是「一小节一块和弦」——按段落换成琶音/切分/块状。

    块状和弦在小节间完全一样，是重复感的主要来源之一；琶音自带横向运动，
    切分则给节奏让位置。
    """
    key = project["key"]
    rock = _is_rock(project["genre"])
    profile = profile or {}
    kstyle = profile.get("keys") or {}
    fallback = profile.get("keysDefault") or "block"
    out = []
    for sec in arr.values():
        role = sec["role"]
        style = kstyle.get(role, fallback)
        if role == "intro" and rock and style == "none":
            continue
        if style == "none":
            continue
        base = (sec["startBar"] - 1) * 4.0
        events = sec["chords"]
        symbols = [sym for _bar, _b, sym in events]
        voiced = voice_chords(symbols, key, lo=52, hi=76, center=62)
        by_bar = {}
        for (bar, beat, sym), notes in zip(events, voiced):
            by_bar.setdefault(bar, []).append((beat, notes))
        bars = sec["bars"]
        for bar in range(bars):
            for beat, notes in by_bar.get(bar, []):
                # 半小节换和弦时后半拍也要铺；低能量主歌保持整小节一块
                if beat > 0 and sec["energy"] < 0.7:
                    continue
                limit = 4.0 - beat
                for b2, bt2, _s in events:
                    if b2 == bar and bt2 > beat + 1e-6:
                        limit = min(limit, bt2 - beat)
                if limit <= 0.05:
                    continue
                vel = int(clamp(64 + 40 * sec["energy"], 40, 112))
                for pitch, local, dur in styles.keys_bar(style, notes, rng, bar, bars,
                                                         sec["energy"]):
                    if local >= limit - 1e-6:
                        continue
                    out.append((pitch, (base + bar * 4 + beat + local) * spb,
                                min(dur, limit - local) * spb, vel))
    return out


def pad_notes(arr, project, rng, spb, profile=None):
    key = project["key"]
    profile = profile or {}
    open_voicing = bool(profile.get("padOpen"))
    out = []
    for sec in arr.values():
        if sec["energy"] < 0.5 and sec["role"] not in ("intro", "bridge"):
            continue
        base = (sec["startBar"] - 1) * 4.0
        symbols = [sym for _bar, _b, sym in sec["chords"]]
        lo, hi, center = (48, 84, 66) if open_voicing else (55, 81, 66)
        voiced = voice_chords(symbols, key, lo=lo, hi=hi, center=center)
        for (bar, beat, sym), notes in zip(sec["chords"], voiced):
            if sec["energy"] < 0.68 and beat > 0:
                continue
            dur = min(_chord_len(sec["chords"], bar, beat), 8.0)
            picks = notes[:3] if not open_voicing else notes[::2][:4]
            for pitch in picks:
                out.append((pitch, (base + bar * 4 + beat) * spb, dur * spb, 60))
            # 交替小节在中间补一次弱起，pad 才有「呼吸」而不是一条直线
            if bar % 2 == 1 and sec["energy"] >= 0.7 and beat == 0:
                for pitch in picks[:2]:
                    out.append((pitch, (base + bar * 4 + 2.0) * spb, 1.9 * spb, 48))
    return out


# --------------------------------------------------------------- 写文件

def _humanize_notes(notes, rng, spb, tid, swing=0.0, timing_ms=5.0):
    """演奏级人性化：起音早晚、力度起伏、时值松紧。

    MIDI 量化到 16 分听起来「机器」，真人录音里每个音的落点有 3–8 ms 抖动、
    力度每下一个音都不一样、按住的时间也长短不一。这里按轨给不同的幅度：
    鼓已经由 kit 做过，贝斯和吉他跟得紧一点，键盘/铺底松一点。
    """
    if not notes:
        return notes
    tight = {"bass": 0.6, "gtr_rhythm_l": 0.8, "gtr_rhythm_r": 0.8,
             "gtr_lead": 1.0, "lead": 1.0, "keys": 1.5, "pad": 1.8}.get(tid, 1.0)
    jitter = timing_ms * tight
    out = []
    for midi, start, dur, vel in notes:
        beat = start / spb if spb else 0.0
        # swing：8 分位置的后半拍稍微往后拖（三连音感）
        if swing and abs(beat * 2 % 2.0 - 1.0) < 1e-6:
            start += swing * spb
        start += rng.uniform(-jitter, jitter) / 1000.0
        dur2 = dur * rng.uniform(0.93, 1.06)
        v = int(clamp(vel + rng.randint(-4, 4), 1, 127))
        out.append((midi, max(0.0, start), max(0.03, dur2), v))
    # 单音轨（人声、主音吉他）不能让抖动把两个音叠在一起：
    # 声音服务遇到同时发声的音符会直接拒绝整首，MIDI 里耳朵也听得出打架。
    # 和弦轨（节奏吉他、keys、pad）本来就同时响，不能这么削。
    mono = {"vocal", "vocal-harmony", "gtr_lead", "bass", "lead"}
    if tid not in mono:
        return out
    out.sort(key=lambda n: n[1])
    fixed = []
    for i, (midi, start, dur, v) in enumerate(out):
        if i + 1 < len(out):
            gap = out[i + 1][1] - start
            if gap <= 0.02:
                continue
            dur = min(dur, gap - 0.012)
        fixed.append((midi, start, max(0.03, dur), v))
    return fixed


def _write_midi(path, bpm, inst_name, notes, is_drum=False, program=0):
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    inst = pretty_midi.Instrument(program=int(program), is_drum=bool(is_drum), name=inst_name)
    for midi, start, dur, vel in notes:
        inst.notes.append(pretty_midi.Note(velocity=int(clamp(vel, 1, 127)), pitch=int(midi),
                                           start=max(0.0, float(start)),
                                           end=max(0.05, float(start) + float(dur))))
    pm.instruments.append(inst)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(path))
    return len(notes)


def compose(project_path, seed=None, track=None):
    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    pack = get_genre(project["genre"])
    rng = random.Random(int(seed or project.get("seed") or 1))
    bpm = float(project["tempo"])
    spb = 60.0 / bpm
    lyrics = load_lyrics(base)
    profile = styles.make_profile(project, pack, rng)

    arr = plan_arrangement(project, pack, rng, profile=profile)
    # 把编排写回 project：mix 需要知道段落和能量，调试时也能直接看
    for sec in project["sections"]:
        a = arr.get(sec["id"]) or {}
        sec["chords"] = [sym for _bar, _b, sym in a.get("chords", [])] or sec.get("chords")
        sec["chordPlan"] = [{"bar": b, "beat": bt, "chord": sym}
                            for b, bt, sym in a.get("chords", [])]
        sec["groove"] = "kit"

    genre = project["genre"]
    tracks = {}
    tracks["drums"] = drums_notes(arr, spb)
    tracks["bass"] = bass_notes(arr, project, rng, spb)

    if _is_rock(genre):
        tracks["gtr_rhythm_l"] = guitar_rhythm_notes(arr, project, rng, spb, layer="l",
                                                    profile=profile)
        tracks["gtr_rhythm_r"] = guitar_rhythm_notes(arr, project, rng, spb,
                                                    offset_beats=0.0, layer="r",
                                                    profile=profile)
        lead = guitar_lead_notes(arr, project, rng, spb)
        if lead:
            tracks["gtr_lead"] = lead
        tracks["keys"] = keys_notes(arr, project, rng, spb, profile=profile)
        tracks["pad"] = pad_notes(arr, project, rng, spb, profile=profile)
    else:
        tracks["guitar"] = guitar_rhythm_notes(arr, project, rng, spb, layer="l",
                                               profile=profile)
        tracks["gtr_rhythm_l"] = tracks.pop("guitar")
        tracks["keys"] = keys_notes(arr, project, rng, spb, profile=profile)
        tracks["pad"] = pad_notes(arr, project, rng, spb, profile=profile)
        if any(s["energy"] >= 0.8 for s in arr.values()):
            tracks["lead"] = _synth_lead_notes(arr, project, rng, spb)

    vocal_notes, vocal_meta, harm_notes, harm_meta = melody.write_vocal(project, lyrics, arr, rng,
                                                                       base=base)
    if vocal_notes:
        tracks["vocal"] = vocal_notes

    # 演奏真实感：力度层级 + 乐句拱形 + 轨道时间偏移；鼓在 kit 里已处理
    swing = float(profile.get("swing") or 0.0)
    tracks = performance.apply(tracks, arr, project, spb, rng, swing=swing)

    # 轨道表：让 render/mix 知道每个轨怎么处理、多大电平
    project["tracks"] = _track_table(project, tracks)
    project["arrangement"] = {sid: {"role": a["role"], "bars": a["bars"],
                                    "energy": a["energy"],
                                    "chords": [list(c) for c in a["chords"]]}
                              for sid, a in arr.items()}
    project.setdefault("vocal", {}).setdefault("autoHarmony", True)
    project["profile"] = {"archetype": profile.get("archetype"),
                          "drums": profile.get("drums"),
                          "cab": profile.get("cab"),
                          "space": profile.get("space"),
                          "strum": profile.get("strum"),
                          "strumLastChorus": profile.get("strumLastChorus"),
                          "keys": profile.get("keys"),
                          "keysDefault": profile.get("keysDefault"),
                          "padOpen": profile.get("padOpen")}

    written = {}
    for tid, notes in tracks.items():
        if track and tid != track:
            continue
        if not notes:
            continue
        is_drum = tid == "drums"
        p = base / "midi" / f"{tid}.mid"
        n = _write_midi(p, bpm, tid, notes, is_drum=is_drum,
                        program=0 if is_drum else PROGRAMS.get(tid, 0))
        written[tid] = {"file": str(p), "notes": n}

    if vocal_meta:
        jdump(base / "analysis" / "vocal-align.json", {"notes": vocal_meta})
    if vocal_notes and (not track or track in ("vocal", "vocal-harmony")) \
            and project.get("vocal", {}).get("autoHarmony", True) is not False:
        # 和声统一交给 harmonize：和弦优先 + 按段落挑策略 + 剪掉会糊成一片的音。
        # 它从磁盘读工程（要 chordPlan），所以先把谱面落盘再排。
        save_project(base, project)
        try:
            from . import harmonize as harmonize_mod
            hp = harmonize_mod.plan(base, write=True)
            entry = written.setdefault("vocal-harmony", {"file": str(base / "midi" / "vocal-harmony.mid")})
            entry["notes"] = hp.get("harmonyNotes") or 0
            entry["sections"] = hp.get("sections")
            if hp.get("harmonyNotes"):
                project.setdefault("vocal", {})["harmony"] = True
                project["vocal"]["harmonyPlan"] = {"sections": hp.get("sections"),
                                                   "strategy": "auto", "cut": "default",
                                                   "density": "full"}
                harm_meta = []
            else:
                project.setdefault("vocal", {})["harmony"] = False
                jdump(base / "analysis" / "vocal-harmony-align.json", {"notes": []})
                harm_meta = []
        except Exception as exc:  # 和声排不出来不该让整首歌编不出来
            written.setdefault("vocal-harmony", {"file": str(base / "midi" / "vocal-harmony.mid")})
            written["vocal-harmony"]["harmonyError"] = str(exc)

    project.setdefault("renders", {})["current"] = "render-v1"
    save_project(base, project)
    duration = sum(int(s["bars"]) for s in project["sections"]) * 4 * spb
    return {"ok": True, "project": str(base), "written": written,
            "durationSec": round(duration, 2), "vocalNotes": len(vocal_meta),
            "harmonyNotes": (written.get("vocal-harmony") or {}).get("notes", 0),
            "style": {"archetype": profile.get("archetype"),
                      "drums": profile.get("drums"),
                      "keys": profile.get("keysDefault"),
                      "chorusStrum": profile.get("strum", {}).get("chorus")}}


def _track_table(project, tracks):
    """每轨的增益/声像/处理链；摇滚双吉他一左一右，solo 居中。"""
    rock = _is_rock(project["genre"])
    table = []
    # 电平由 render 的每轨 RMS 目标决定，这里只做艺术性微调（±3 dB 以内）
    defaults = {
        "drums": (0.5, 0.0), "bass": (0.0, 0.0),
        "gtr_rhythm_l": (-1.5, -0.55), "gtr_rhythm_r": (-1.5, 0.55),
        "gtr_lead": (-2.5, 0.0), "keys": (-3.0, -0.25),
        "pad": (-6.0, 0.0), "lead": (-3.0, 0.15), "vocal": (2.0, 0.0),
        "vocal-harmony": (-7.5, 0.0),
    }
    for tid in tracks:
        g, p = defaults.get(tid, (-8.0, 0.0))
        table.append({"id": tid, "midi": f"midi/{tid}.mid", "fx": FX.get(tid, "generic"),
                      "gainDb": g, "pan": p,
                      "send": {"reverb": 0.12 if tid in ("vocal", "pad") else 0.0,
                               "delay": 0.10 if tid in ("gtr_lead", "lead") else 0.0}})
    if "vocal" in tracks:
        table.append({"id": "vocal-harmony", "midi": "midi/vocal-harmony.mid",
                      "fx": "vocal", "gainDb": -9.5, "pan": 0.0,
                      "send": {"reverb": 0.18, "delay": 0.0}})
    return table


def _synth_lead_notes(arr, project, rng, spb):
    key = project["key"]
    out = []
    for sec in arr.values():
        if sec["energy"] < 0.8:
            continue
        base = (sec["startBar"] - 1) * 4.0
        scale = _scale_pitches(key, note_to_midi("C4"), note_to_midi("C6"))
        idx = len(scale) // 2
        for bar in range(sec["bars"]):
            for k in range(8):
                local = k * 0.5
                if rng.random() < 0.2:
                    continue
                idx = int(clamp(idx + rng.choice([-2, -1, 1, 2]), 0, len(scale) - 1))
                out.append((scale[idx], (base + bar * 4 + local) * spb, 0.4 * spb, 96))
    return out
