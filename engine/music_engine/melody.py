"""人声旋律：主题 + 发展。

为什么重写（0.3 → 0.5）：

* 0.3 里每一句都是「按段落密度挑一个节奏 cell + 在强拍吸附和弦音」，
  句子之间只有 cell 编号不同，音高之间没有任何关系——听感就是同一条句子
  换个节奏再念一遍。这是「编曲旋律太过重复」的直接原因。
* 这里换成作曲上的两步：先造一个 **主题**（motif，全曲的记忆点），
  再对它做 **发展**：模进（sequence）、倒影（invert）、逆行（retrograde）、
  装饰（ornament）、片段（fragment）、扩展（stretch，长音化）。
  重复出现的段落（verse2 / chorus2 / 最后一遍副歌）用不同的发展手法，
  而不是原样重印。
* 每首歌的节奏词库、主题轮廓、发展顺序都由 `rng`（种子）派生，
  不同标题的工程写出来是两首不同旋律，而不是同一个模板换词。

约束：永远「一个字一个音」（SVS 按音符顺序对齐歌词）；严格单音（`_mono` 兜底）。
"""

import math
import re

from .util import clamp, midi_to_note, note_to_midi, parse_chord, scale_pcs

CJK = re.compile(r"[\u4e00-\u9fff]")


def syllables(line):
    return CJK.findall(line or "")


def _scale_pitches(key, lo, hi):
    pcs = scale_pcs(key)
    return [p for p in range(lo, hi + 1) if p % 12 in pcs]


# ---------------------------------------------------------------- 轮廓

CONTOURS = {
    "arch": lambda t: math.sin(math.pi * t) * 1.0,
    "rise": lambda t: 0.15 + 0.85 * t,
    "fall": lambda t: 1.0 - 0.85 * t,
    "wave": lambda t: 0.5 + 0.45 * math.sin(2 * math.pi * t - 0.5),
    "climb": lambda t: 0.25 * t + 0.75 * (t ** 1.8),
    "settle": lambda t: 0.9 - 0.55 * (t ** 0.6),
    "plateau": lambda t: 1.0 if 0.25 < t < 0.75 else 0.45,
}

CONTOUR_SETS = {
    "verse": ["settle", "wave", "fall"],
    "prechorus": ["climb", "rise", "wave"],
    "chorus": ["arch", "plateau", "rise"],
    "bridge": ["wave", "fall", "settle"],
    "outro": ["fall", "settle"],
}


# ---------------------------------------------------------------- 节奏词库（每首歌一套）

def _gen_cell(rng, density):
    """生成一个 4/4 小节的起音位置（拍），密度越高越容易出现 16 分与切分。"""
    if density >= 0.7:
        pool, min_gap, mu = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5], 0.25, 5.2
    elif density >= 0.45:
        pool, min_gap, mu = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5], 0.5, 4.0
    else:
        pool, min_gap, mu = [0.5, 1.0, 1.5, 2.0, 3.0], 0.5, 2.8
    want = int(clamp(round(rng.gauss(mu, 0.9)), 2, 8))
    onsets = [0.0]
    for _ in range(60):
        if len(onsets) >= want:
            break
        cand = rng.choice(pool)
        if cand in onsets:
            continue
        if min(abs(cand - o) for o in onsets) < min_gap - 1e-9:
            continue
        onsets.append(cand)
    onsets.sort()
    # 偶尔切分：把第 2 拍往后挪半拍（中文摇滚人声常见的「抢」）
    if len(onsets) > 2 and rng.random() < 0.35 and 2.0 in onsets:
        onsets.remove(2.0)
        onsets = sorted(set(onsets + [rng.choice([1.5, 2.5])]))
    return [round(x, 3) for x in onsets]


def make_cells(rng, count=6):
    """一首歌的节奏词库：sparse / mid / dense 各 count 个，外加收句用的 tail。"""
    out = {}
    for tier, dens in (("sparse", 0.3), ("mid", 0.55), ("dense", 0.8)):
        cells, guard = [], 0
        while len(cells) < count and guard < count * 12:
            guard += 1
            c = _gen_cell(rng, dens)
            if c not in cells:
                cells.append(c)
        out[tier] = cells
    tails = []
    for _ in range(3):
        t = [0.0, round(rng.choice([1.0, 1.5, 2.0]), 3)]
        if rng.random() < 0.4:
            t.append(round(rng.choice([2.5, 3.0]), 3))
        if t not in tails:
            tails.append(sorted(set(t)))
    out["tail"] = tails or [[0.0, 2.0]]
    return out


def _tier(density):
    return "dense" if density >= 0.7 else ("sparse" if density <= 0.42 else "mid")


def _rhythm(n, beats, rng, cells, density=0.55, final=False, stretch=1.0):
    """把 n 个字铺进 beats 拍：每小节从词库里抽一个**没用过**的 cell。

    stretch > 1 时把起音减到更少、留更长的音（扩展手法，用在最后一遍副歌）。
    """
    bars = max(1, int(math.ceil(beats / 4.0)))
    tier = _tier(density)
    lib = cells[tier]
    onsets = []
    used = []
    for b in range(bars):
        if b == bars - 1 and final and rng.random() < 0.75:
            cell = rng.choice(cells["tail"])
        else:
            pool = [c for c in lib if c not in used] or lib
            cell = rng.choice(pool)
            used.append(cell)
        for x in cell:
            v = b * 4.0 + x
            if v < beats - 1e-6:
                onsets.append(round(v, 3))
    onsets = sorted(set(onsets))

    if stretch > 1.0:
        # 扩展：按比例抽稀起音（保留整拍与句首），让音符变长
        keep = [o for o in onsets if abs(o % 1.0) < 1e-6]
        if len(keep) >= 2:
            onsets = keep

    guard = 0
    while len(onsets) > n and guard < 64:
        guard += 1
        fine = [o for o in onsets if abs(o % 0.5) > 1e-6]
        if fine:
            onsets.remove(rng.choice(fine))
        elif len(onsets) > 1:
            onsets.pop(-2)
        else:
            break
    while len(onsets) < n and guard < 192:
        guard += 1
        gaps = [(onsets[i + 1] - onsets[i], i) for i in range(len(onsets) - 1)]
        gaps = [g for g in gaps if g[0] > 0.55]
        if not gaps:
            doubled = sorted({round(o * 2, 3) for o in onsets})
            doubled = [o for o in doubled if o < beats]
            if len(doubled) >= n:
                onsets = doubled
                break
            if doubled == onsets:
                break
            onsets = doubled
            continue
        _, i = max(gaps)
        onsets.insert(i + 1, round((onsets[i] + onsets[i + 1]) / 2.0, 3))
    onsets = onsets[:n]
    while len(onsets) < n:
        onsets.append(round(onsets[-1] + 0.25, 3))

    out = []
    for k, onset in enumerate(onsets):
        nxt = onsets[k + 1] if k + 1 < len(onsets) else beats
        gap = max(0.125, nxt - onset)
        dur = gap * (0.94 if stretch <= 1.0 else 0.98)
        if k == len(onsets) - 1:
            dur = max(dur, min(2.5 * stretch, beats - onset))
        out.append((round(onset, 3), round(max(0.24, min(dur, beats - onset)), 3)))
    return out


# ---------------------------------------------------------------- 主题

def make_theme(rng, n, contour=None, span=6, leap=0.16):
    """主题：n 个音级偏移（相对起点），按轮廓走，级进为主偶尔跳进。"""
    name = contour or rng.choice(list(CONTOURS))
    f = CONTOURS[name]
    steps = [0]
    for i in range(1, n):
        target = f(i / max(1, n - 1)) * span
        d = int(round(target - steps[-1]))
        if d == 0:
            d = rng.choice([-1, 1])
        d = int(clamp(d, -3, 3))
        if rng.random() < leap:
            d = int(math.copysign(rng.choice([3, 4]), d if d else 1))
        steps.append(steps[-1] + d)
    return {"steps": steps, "contour": name}


def develop(theme, op, rng, k=0):
    """把主题变换出一个新句子（这是「不重复」的核心）。"""
    s = list(theme["steps"])
    if not s:
        return {"steps": [], "contour": theme.get("contour")}
    if op == "literal":
        out = s
    elif op == "transpose":
        kk = k or rng.choice([-3, -2, 2, 3])
        out = [x + kk for x in s]
    elif op == "sequence":
        # 模进：后半句整体移位，像楼梯一样推上去/落下来
        kk = k or rng.choice([-2, -1, 1, 2])
        half = max(1, len(s) // 2)
        out = [x + (kk if i >= half else 0) for i, x in enumerate(s)]
    elif op == "invert":
        out = [s[0] - (x - s[0]) for x in s]
    elif op == "retrograde":
        d = [s[i] - s[i - 1] for i in range(1, len(s))]
        out = [s[0]]
        for x in reversed(d):
            out.append(out[-1] + x)
    elif op == "ornament":
        out = []
        for i, x in enumerate(s):
            if 0 < i < len(s) - 1 and rng.random() < 0.45:
                out.append(x + rng.choice([-1, 1]))
            else:
                out.append(x)
    elif op == "fragment":
        out = s[:max(2, len(s) // 2)]
    else:
        out = s
    contour = theme.get("contour")
    if op in ("invert", "retrograde"):
        contour = {"arch": "fall", "fall": "arch", "rise": "fall",
                   "climb": "settle", "settle": "climb"}.get(contour, contour)
    return {"steps": out, "contour": contour, "op": op}


def fit(steps, n, rng):
    """把主题伸缩到 n 个字：长了截头，短了沿着它的音程继续（不是复制尾巴）。"""
    if not steps:
        return [0] * n
    if len(steps) == n:
        return list(steps)
    if len(steps) > n:
        return list(steps[:n])
    out = list(steps)
    d = [steps[i] - steps[i - 1] for i in range(1, len(steps))] or [1]
    j = 0
    while len(out) < n:
        out.append(out[-1] + d[j % len(d)] + rng.choice([0, 0, 0, 1, -1]))
        j += 1
    return out


# ---------------------------------------------------------------- 落谱

def _chord_at(chord_events, beat):
    cur = chord_events[0][2] if chord_events else "I"
    for bar, b, sym in chord_events:
        if bar * 4 + b <= beat + 1e-6:
            cur = sym
        else:
            break
    return cur


def _chord_pcs(sym, key):
    try:
        ch = parse_chord(sym, key)
    except Exception:
        return set()
    return {(int(ch["root_pc"]) + int(i)) % 12 for i in ch.get("intervals") or [0, 4, 7]}


def _snap_index(idx, chord_events, onset, key, scale, span=2):
    """强拍/长音吸附到最近的「音阶内的和弦音」。"""
    pcs = _chord_pcs(_chord_at(chord_events, onset), key)
    if not pcs:
        return idx
    best = None
    for j in range(max(0, idx - span), min(len(scale), idx + span + 1)):
        if scale[j] % 12 in pcs:
            if best is None or abs(j - idx) < abs(best - idx):
                best = j
    return idx if best is None else best


def realize(steps, scale, base_pitch, chord_events, key, rhythm, rng,
            lo, hi, snap=True, cadence_pitch=None):
    """把音级偏移落成 MIDI 音高：级进为主、强拍吸附和弦音、音域夹紧。"""
    bi = min(range(len(scale)), key=lambda i: abs(scale[i] - base_pitch))
    steps = fit(steps, len(rhythm), rng) if len(steps) != len(rhythm) else steps
    pitches, prev = [], None
    for i, (onset, dur) in enumerate(rhythm):
        idx = int(clamp(bi + steps[i], 0, len(scale) - 1))
        if prev is not None and abs(idx - prev) > 2 and rng.random() < 0.6:
            idx = prev + int(math.copysign(2, idx - prev))
        idx = int(clamp(idx, 0, len(scale) - 1))
        if snap and (abs(onset % 1.0) < 1e-6 or dur >= 0.9):
            idx = _snap_index(idx, chord_events, onset + 0.01, key, scale)
        p = scale[idx]
        if prev is not None and p == scale[prev] and dur < 0.55 and rng.random() < 0.7:
            idx = int(clamp(idx + rng.choice([-1, 1]), 0, len(scale) - 1))
            p = scale[idx]
        pitches.append(p)
        prev = idx
    if cadence_pitch is not None and pitches:
        target = min(scale, key=lambda x: abs(x - cadence_pitch))
        if abs(target - pitches[-1]) <= 7:
            pitches[-1] = target
    return pitches


def cadence_target(sym, key, lo, hi):
    """终止音：优先主和弦根音/三音，落在舒适音区。"""
    pcs = _chord_pcs(sym, key) or set()
    scale = scale_pcs(key)
    tonic = key.split("m")[0] if isinstance(key, str) else "C"
    try:
        root = note_to_midi(tonic + "4") % 12
    except Exception:
        root = 0
    for cand in (root, (root + 7) % 12, (root + 4) % 12):
        p = lo + ((cand - lo) % 12)
        if lo <= p <= hi and p % 12 in scale:
            return p
    return lo + ((root - lo) % 12)


# ---------------------------------------------------------------- 段落对齐

ALIASES = {
    "verse": ("verse", "主歌", "a"),
    "prechorus": ("prechorus", "pre", "预副歌", "导歌"),
    "chorus": ("chorus", "副歌", "b", "hook", "高潮"),
    "bridge": ("bridge", "桥", "桥段", "c"),
    "outro": ("outro", "尾", "结尾"),
    "intro": ("intro", "前奏"),
    "solo": ("solo", "间奏"),
}


def _canon_role(name):
    """把「chorus3」「副歌」「B 段」都归到同一个角色名。"""
    s = str(name or "").lower().strip()
    for canon, alist in ALIASES.items():
        if s.startswith(canon) or any(a in s for a in alist):
            return canon
    return "verse"


def _sections_for_lyrics(lyrics, project_sections):
    """把歌词段落对齐到工程段落。

    按名字优先，其次按角色；同角色的重复段落（第二个副歌、第二个主歌）
    会**复用**同一段歌词——歌里副歌唱两遍，词也就唱两遍；旋律由
    发展手法（转位/加花/扩张）做出差别，而不是留空。
    """
    lsecs = lyrics.get("sections", []) or []
    if not lsecs:
        return []
    roles = [(_canon_role(ls.get("section")), ls) for ls in lsecs]
    used = set()
    out = []
    for sec in project_sections:
        sid = str(sec["id"]).lower()
        role = _canon_role(sec.get("role") or sid)
        exact = next((ls for _c, ls in roles if str(ls.get("section", "")).lower() == sid), None)
        if exact is not None:
            out.append({"section": sec["id"], "lines": exact.get("lines", [])})
            used.add(id(exact))
            continue
        fresh = [(c, ls) for c, ls in roles if c == role and id(ls) not in used]
        if fresh:
            out.append({"section": sec["id"], "lines": fresh[0][1].get("lines", [])})
            used.add(id(fresh[0][1]))
            continue
        same = [(c, ls) for c, ls in roles if c == role]
        if same and role in ("verse", "prechorus", "chorus", "bridge"):
            out.append({"section": sec["id"], "lines": same[0][1].get("lines", [])})
    return out


# 段落 → 发展手法序列（同一段落第 2 次出现换手法，这是消除复读的关键）
ROLE_OPS = {
    "verse": [["literal", "sequence", "ornament"],
              ["invert", "sequence", "fragment"],
              ["ornament", "retrograde", "sequence"]],
    "prechorus": [["sequence", "ornament", "climb"],
                  ["transpose", "sequence", "ornament"]],
    "chorus": [["literal", "sequence", "ornament"],
               ["transpose", "ornament", "sequence"],
               ["sequence", "transpose", "ornament"],
               ["stretch", "ornament", "transpose"]],
    "bridge": [["retrograde", "fragment", "ornament"],
               ["invert", "sequence", "fragment"]],
    "outro": [["fragment", "retrograde"], ["literal", "fragment"]],
    "solo": [["literal"]],
}

REGISTER = {"chorus": 3.0, "prechorus": 1.0, "verse": -3.0,
            "bridge": -4.0, "solo": 0.0, "outro": -1.0}

DENSITY = {"chorus": 0.78, "prechorus": 0.66, "verse": 0.46,
           "bridge": 0.42, "outro": 0.34, "solo": 0.5}


def write_vocal(project, lyrics, arrangement, rng, base=None):
    """给整首歌写人声（主旋律 + 和声），返回 (lead_notes, lead_meta, harm_notes, harm_meta)。"""
    key = project["key"]
    spb = 60.0 / float(project["tempo"])
    pack_vocal = (project.get("genrePack") or {}).get("vocal") or {}
    rng_range = pack_vocal.get("range") or ["A3", "D5"]
    lo, hi = note_to_midi(rng_range[0]) + 1, note_to_midi(rng_range[1]) - 1
    scale = _scale_pitches(key, lo, hi)
    if not scale:
        scale = list(range(lo, hi + 1))

    plan = _sections_for_lyrics(lyrics, project["sections"])
    if not plan:
        return [], [], [], []

    cells = make_cells(rng, count=6)          # 每首歌自己的节奏词库
    # 主题：先用第一段有歌词的段落的第一句定调，再按段落角色派生
    first = plan[0]
    first_sec = next((s for s in project["sections"] if s["id"] == first["section"]), None)
    first_lines = [ln.get("text", "") for ln in first["lines"] if syllables(ln.get("text", ""))]
    n_theme = max([len(syllables(x)) for x in first_lines] or [4])
    theme_name = rng.choice(CONTOUR_SETS.get(str(first_sec.get("role") or "verse").lower(),
                                             CONTOUR_SETS["verse"]))
    theme_a = make_theme(rng, n_theme, contour=theme_name, span=6)
    # 副歌主题：从主歌主题派生（音区更高、跨度更大）——同一首歌的材料，不同的形态
    theme_b = make_theme(rng, n_theme, contour=rng.choice(CONTOUR_SETS["chorus"]), span=8)

    lead_notes, lead_meta = [], []
    harm_notes, harm_meta = [], []
    seen_role = {}
    melody_plan = []

    for entry in plan:
        sec = next((s for s in project["sections"] if s["id"] == entry["section"]), None)
        if sec is None:
            continue
        arr = arrangement.get(sec["id"]) or {}
        chord_events = arr.get("chords") or [(0, 0.0, "I")]
        sec_start = (sec["startBar"] - 1) * 4.0
        role = str(sec.get("role") or re.sub(r"\d+$", "", sec["id"])).lower()
        bars = int(sec["bars"])
        lines = [ln.get("text", "") for ln in entry["lines"]]
        lines = [ln for ln in lines if syllables(ln)]
        if not lines:
            continue
        idx = seen_role.get(role, 0)
        seen_role[role] = idx + 1
        ops = ROLE_OPS.get(role) or ROLE_OPS["verse"]
        op_set = ops[min(idx, len(ops) - 1)]
        theme = theme_b if role == "chorus" else theme_a
        register = REGISTER.get(role, 0.0)
        density = DENSITY.get(role, 0.55)
        per_line = max(2, int(round(bars / len(lines))))

        for li, line in enumerate(lines):
            line_bar = li * per_line
            if line_bar >= bars:
                break
            span_bars = min(per_line, bars - line_bar)
            beats = span_bars * 4.0
            op = op_set[min(li, len(op_set) - 1)]
            last_line = li == len(lines) - 1
            stretch = 1.35 if op == "stretch" else 1.0
            rhythm = _rhythm(len(syllables(line)), beats, rng, cells,
                             density=0.32 if op == "stretch" else density,
                             final=last_line, stretch=stretch)
            developed = develop(theme, op, rng) if op != "climb" else develop(theme, "sequence", rng, k=2)
            local_events = [(b - line_bar, beat, sym) for b, beat, sym in chord_events
                            if line_bar <= b < line_bar + span_bars]
            if not local_events:
                local_events = [(0, 0.0, _chord_at(chord_events, line_bar * 4.0))]

            center = (lo + hi) / 2.0 + register
            base_pitch = int(clamp(round(center), lo, hi))
            steps = fit(developed["steps"], len(rhythm), rng)
            cad = cadence_target(_chord_at(chord_events, line_bar * 4.0 + beats - 0.5), key, lo, hi) \
                if last_line else None
            pitches = realize(steps, scale, base_pitch, local_events, key, rhythm, rng,
                              lo, hi, cadence_pitch=cad)
            if op == "stretch" and len(pitches) >= 2:
                # 扩展手法：句尾再顶一下（副歌最后一句的高潮）
                top = min(hi, max(pitches) + 2)
                pitches[-1] = min(scale, key=lambda x: abs(x - top))

            base_beat = sec_start + line_bar * 4.0
            chars = syllables(line)
            for (pitch, onset, dur), ch in zip(zip(pitches, [r[0] for r in rhythm],
                                                   [r[1] for r in rhythm]), chars):
                t = (base_beat + onset) * spb
                lead_notes.append((int(pitch), t, dur * spb * 0.95, 104))
                lead_meta.append({"char": ch, "pitch": midi_to_note(int(pitch)),
                                  "start": round(t, 3), "duration": round(dur * spb * 0.95, 3),
                                  "beat": round(base_beat + onset, 3),
                                  "section": sec["id"], "op": op,
                                  "line": li, "contour": developed.get("contour")})
            melody_plan.append({"section": sec["id"], "line": li, "op": op,
                                "contour": developed.get("contour"),
                                "notes": len(rhythm)})

            # 和声不在旋律里「顺手」生成：写死低三度会跟和弦打架，也剪不掉糊成一团的音。
            # 统一由 harmonize.plan 在整首歌写完之后按段落、按和弦编排（见 compose）。

    if base is not None:
        try:
            from pathlib import Path as _P

            from .util import ensure_dir, jdump
            jdump(ensure_dir(_P(base) / "analysis") / "melody-plan.json",
                  {"cells": {k: v for k, v in cells.items()},
                   "theme": theme_a, "themeChorus": theme_b,
                   "lines": melody_plan,
                   "ops": {k: v for k, v in ROLE_OPS.items()}})
        except Exception:
            pass

    return _mono(lead_notes, lead_meta), lead_meta, _mono(harm_notes, harm_meta), harm_meta


def _mono(notes, meta):
    """SVS 要求单音旋律：把重叠/过短的音削成严格单调，否则服务直接拒绝。"""
    if not notes:
        return notes
    out = []
    rows = sorted(zip(notes, range(len(notes))), key=lambda r: (r[0][1], r[1]))
    for i, (note, idx) in enumerate(rows):
        pitch, start, dur, vel = note
        nxt = rows[i + 1][0][1] if i + 1 < len(rows) else None
        if nxt is not None:
            gap = nxt - start
            if gap <= 0.02:
                continue
            dur = min(dur, gap * 0.94)
        dur = max(dur, 0.05)
        out.append((pitch, start, dur, vel))
    if meta:
        keep = {round(n[1], 3) for n in out}
        meta = [m for m in meta if round(m.get("start", -1), 3) in keep]
    return out
