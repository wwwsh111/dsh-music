"""演奏真实感：力度层级、乐句拱形、时间感。

MIDI 听起来「像电脑」通常不是音色问题，而是三件事：

1. **力度太窄**。真人一句里的强弱差能有 20–30，程序化常只有 ±4。
   这里按「角色层级」定中心（主旋律高于伴奏、贝斯稳、pad 垫底），
   再叠乐句拱形（句首起、句中推、句尾收）和重音。
2. **时间太齐**。每个音都精确落在 16 分网格上，像鼓机。这里给每轨
   不同的「抢/拖」偏移（吉他稍抢、人声稍拖、pad 更松），再叠高斯抖动。
3. **句尾不会呼吸**。长音后半稍弱、句尾音收一点，重复音逐次加力，
   这些细节比随机 ±5 更像人。

人声/主音这类单音轨仍然会在最后削掉重叠，避免 SVS 拒绝整首。
"""

import bisect
import math

# tid -> (力度中心, 乐句拱形幅度, 时间抖动 ms, 抢/拖偏移 ms, 力度下限, 上限)
PROFILE = {
    "vocal": (97, 5.5, 7, 4, 80, 112),
    "vocal-harmony": (78, 4.0, 9, 5, 58, 96),
    "bass": (95, 3.0, 5, -2, 82, 118),
    "gtr_rhythm_l": (96, 4.0, 6, -4, 74, 124),
    "gtr_rhythm_r": (96, 4.0, 6, 3, 74, 124),
    "gtr_lead": (93, 6.0, 8, 4, 72, 118),
    "keys": (74, 4.5, 10, 3, 50, 104),
    "pad": (54, 2.5, 14, 6, 34, 86),
    "lead": (93, 5.0, 7, 3, 72, 118),
}

MONO = {"vocal", "vocal-harmony", "gtr_lead", "bass", "lead"}
LEGATO = {"gtr_lead", "lead"}

# 重音：整拍强、反拍弱、16 分弱位更弱
_ACCENT = {0.0: 4.0, 0.5: 1.5, 1.0: 2.0, 1.5: 0.0, 2.0: 3.0, 2.5: 1.0,
           3.0: 2.0, 3.5: 0.0}


def _section_spans(arr):
    spans = []
    for sec in arr.values():
        start = (int(sec["startBar"]) - 1) * 4.0
        end = start + int(sec["bars"]) * 4.0
        spans.append((start, end, sec))
    spans.sort(key=lambda s: s[0])
    return spans


def _find_section(spans, starts, beat):
    i = bisect.bisect_right(starts, beat) - 1
    if i < 0:
        i = 0
    if i >= len(spans):
        i = len(spans) - 1
    return spans[i][2] if spans else {"energy": 0.6, "role": "verse", "startBar": 1,
                                     "bars": 4}


def _accent(beat_in_bar):
    for pos, val in _ACCENT.items():
        if abs(beat_in_bar - pos) < 0.06:
            return val
    return -1.0


def apply(tracks, arr, project, spb, rng, swing=0.0):
    """就地生成新版 note 列表（不修改传入对象）。

    tracks: {tid: [(midi, start_sec, dur_sec, vel)]}
    arr:    harmony/kit 的段落编排
    """
    spans = _section_spans(arr)
    starts = [s[0] for s in spans]
    out = {}
    for tid, notes in tracks.items():
        if not notes or tid == "drums":
            out[tid] = list(notes or [])
            continue
        conf = PROFILE.get(tid)
        if not conf:
            out[tid] = list(notes)
            continue
        base, arch_amp, jit_ms, off_ms, lo, hi = conf
        notes = sorted(notes, key=lambda n: n[1])
        center = sum(n[3] for n in notes) / len(notes)
        shaped = []
        prev_pitch, repeat = None, 0
        for midi, start, dur, vel in notes:
            beat = start / spb if spb else 0.0
            sec = _find_section(spans, starts, beat)
            energy = float(sec.get("energy") or 0.6)
            sec_start = (int(sec.get("startBar") or 1) - 1) * 4.0
            sec_len = max(4.0, int(sec.get("bars") or 4) * 4.0)
        # 乐句拱形：一段四小节最多一个拱，短段落按段落长度算
            phrase = min(16.0, sec_len)
            pos = ((beat - sec_start) % phrase) / phrase
            arch = math.sin(math.pi * pos) ** 0.8
            in_bar = beat % 4.0
            accent = _accent(in_bar)
            # 长音、重复音、伴奏高音的微调
            dur_beats = dur / spb if spb else 0.5
            long_pen = -2.0 if dur_beats >= 1.5 else 0.0
            if midi == prev_pitch:
                repeat = min(3, repeat + 1)
            else:
                repeat = 0
            rep_bonus = min(3.0, repeat * 1.1)
            high_pen = 0.0
            if tid in ("keys", "pad") and midi > 72:
                high_pen = -1.5
            target = base + (energy - 0.6) * 12.0
            v = (target + (vel - center) * 0.28 + arch * arch_amp + accent
                 + long_pen + rep_bonus + high_pen + rng.gauss(0, 2.2))
            v = int(max(lo, min(hi, round(v))))

            # 时间：轨道偏移 + 高斯抖动；8 分反拍按 swing 拖后
            if swing and abs(beat * 2 % 2.0 - 1.0) < 1e-6:
                start += swing * spb
            start += (off_ms + rng.gauss(0, jit_ms)) / 1000.0
            start = max(0.0, start)
            dur2 = max(0.03, dur * rng.uniform(0.96, 1.05))
            shaped.append([midi, start, dur2, v])
            prev_pitch = midi

        # 连奏：主音吉他/合成器主音把相邻音之间的缝填到 5 ms
        if tid in LEGATO:
            shaped.sort(key=lambda n: n[1])
            for i in range(len(shaped) - 1):
                gap = shaped[i + 1][1] - (shaped[i][1] + shaped[i][2])
                if 0.0 <= gap < 0.04:
                    shaped[i][2] = shaped[i + 1][1] - shaped[i][1] - 0.005

        # 单音轨不能重叠（SVS 会拒绝），留 12 ms 呼吸
        if tid in MONO:
            shaped.sort(key=lambda n: n[1])
            fixed = []
            for i, (midi, start, dur2, v) in enumerate(shaped):
                if i + 1 < len(shaped):
                    gap = shaped[i + 1][1] - start
                    if gap <= 0.02:
                        continue
                    dur2 = min(dur2, gap - 0.012)
                fixed.append((midi, start, max(0.03, dur2), v))
            shaped = fixed
        out[tid] = [tuple(n) for n in shaped]
    return out
