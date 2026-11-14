"""和声：段落级和弦进行、声部连接、power chord。

设计要点（借鉴 AI_MIDI 的乐理库）：

* 进行优先用曲风包里的 ``progressions``（七和弦/九和弦/副属都在包里写好了），
  包缺失时才退回内置表，避免「曲风配置写了一套、编曲用另一套」。
* 和声节奏由 **BPM** 决定：慢歌两小节一换，中速一小节一换，快的副歌才切半小节。
  一拍一个和弦只会让 4/4 变成「和弦节拍器」。
* 段落收尾按角色选终止式：正格 / 变格 / 阻碍 / 半终止。桥段停在属和弦上，
  把张力交给副歌；不是每段都 V–I 关门。
* 偶尔插一个副属（V7/ii、V7/vi）或调式互换（大调借 iv、bVI），
  和声才有「这一下没想到」的记忆点。
* 键盘/弦乐用 **声部连接**：后一个和弦取离前一个最近的转位，并惩罚平行五/八度。
"""

import itertools
import re

from .util import MAJOR, MINOR, parse_chord, scale_pcs

# 和弦进行（罗马数字，b 前缀表示降级；大小调都能用）。
# 曲风包（engine/assets/genres/*.json）里写了更具体的进行，编曲时优先用包里的；
# 这张表是缺包/自定义曲风时的兜底。
PROGRESSIONS = {
    "vocalrock": [
        ["i", "bVI", "bIII", "bVII"],
        ["i", "bVII", "bVI", "V"],
        ["i", "iv", "bVII", "bIII"],
        ["bVI", "bVII", "i", "i"],
    ],
    "rock": [
        ["I", "bVII", "bVI", "bVII"],
        ["I", "IV", "bVII", "I"],
        ["i", "bVI", "bVII", "i"],
        ["i", "iv", "bVII", "i"],
    ],
    "pop": [
        ["I", "V", "vi", "IV"],
        ["vi", "IV", "I", "V"],
        ["I", "IV", "vi", "V"],
        ["Imaj7", "vi7", "ii7", "V7"],
    ],
    "acg": [
        ["IVmaj7", "V7", "iii7", "vi7"],
        ["vi", "IV", "I", "V"],
        ["I", "iii", "IV", "V"],
    ],
    "citypop": [
        ["IVmaj7", "V7", "iii7", "vi7"],
        ["ii7", "V7", "Imaj7", "vi7"],
        ["Imaj7", "vi7", "ii7", "V7"],
    ],
    "rnb": [
        ["ii7", "V7", "Imaj7", "vi7"],
        ["Imaj7", "iii7", "vi7", "ii7"],
        ["iv7", "bVII7", "Imaj7", "V7"],
        ["iii7", "vi7", "ii7", "V7"],
    ],
    "folk": [["I", "IV", "V", "I"], ["I", "V", "vi", "IV"]],
    "guofeng": [["I", "vi", "IV", "V"], ["vi", "IV", "V", "I"]],
    "hiphop": [["i7", "bVIImaj7", "bVImaj7", "bVII7"], ["i", "iv", "i", "bVII"]],
    "trap": [["i", "bVI", "bVII", "i"], ["i", "bII", "i", "bVII"]],
    "house": [["i", "bVII", "bVI", "bVII"], ["vi", "IV", "I", "V"]],
    "synthwave": [["i", "bVI", "bIII", "bVII"], ["i", "bVII", "bVI", "V"]],
    "lofi": [["Imaj7", "vi7", "ii7", "V7"], ["IVmaj7", "iii7", "vi7", "ii7"]],
    "cinematic": [["i", "bVI", "bIII", "bVII"], ["i", "iv", "bVI", "V"]],
    "kids": [["I", "IV", "V", "I"], ["I", "V", "I", "IV"]],
}

# 小调专用的兜底进行。曲风包大多按大调写（Imaj7/ii7/V7），
# 一旦用户指定小调，直接套用会把 Imaj7 弹成大三和弦，调性就散了；
# 所以小调优先用包里的「小调进行」，没有就用这张表。
MINOR_PROGRESSIONS = {
    "vocalrock": [["i", "bVI", "bIII", "bVII"], ["i", "bVII", "bVI", "V"],
                  ["i", "iv", "bVII", "bIII"], ["bVI", "bVII", "i", "i"]],
    "rock": [["i", "bVII", "bVI", "V"], ["i", "iv", "bVII", "i"],
             ["i", "bVI", "bIII", "bVII"]],
    "pop": [["i", "bVI", "bIII", "bVII"], ["i", "iv", "bVI", "V"],
            ["i", "bVII", "bVI", "bVII"]],
    "acg": [["i", "bVI", "bIII", "bVII"], ["i", "iv", "bVI", "V"],
            ["i", "bIII", "bVII", "iv"]],
    "citypop": [["i7", "bIIImaj7", "bVImaj7", "bVII7"],
                ["i7", "iv7", "bVII7", "bIIImaj7"],
                ["i7", "bVImaj7", "iv7", "bVII7"]],
    "rnb": [["i7", "iv7", "bVIImaj7", "bIII"],
            ["i7", "bIII", "bVImaj7", "bVII7"],
            ["i7", "bVImaj7", "iv7", "V7"]],
    "folk": [["i", "bVII", "bVI", "bVII"], ["i", "iv", "bVII", "i"]],
    "guofeng": [["i", "bVI", "bIII", "bVII"], ["i", "iv", "bVII", "V"]],
    "hiphop": [["i7", "iv7", "bVII", "bIII"], ["i", "bVI", "bIII", "bVII"]],
    "trap": [["i", "bVI", "bIII", "bVII"], ["i", "bII", "i", "bVII"]],
    "house": [["i7", "bVI", "bIII", "bVII"], ["i7", "iv7", "bVII", "bIII"]],
    "synthwave": [["i", "bVI", "bIII", "bVII"], ["i", "bVII", "bVI", "V"]],
    "lofi": [["i7", "iv7", "bVII", "bIII"], ["i7", "bIII", "bVI", "bVII"]],
    "cinematic": [["i", "bVI", "bIII", "bVII"], ["i", "iv", "bVI", "V"]],
    "kids": [["i", "bVI", "bIII", "bVII"], ["i", "iv", "V", "i"]],
}

# 大调兜底（曲风包里全是大调以外的进行时用）
MAJOR_FALLBACK = [["I", "V", "vi", "IV"], ["I", "IV", "V", "I"],
                  ["vi", "IV", "I", "V"]]

# 段落专用：主歌收着、副歌打开、桥段悬着
ROLE_OVERRIDE = {
    "prechorus": [["iv", "V", "bVI", "bVII"], ["i", "iv", "V", "V"]],
    "bridge": [["iv", "bVII", "bVI", "V"], ["bVI", "bVII", "i", "V"]],
    "solo": [["i", "bVII", "bVI", "bVII"], ["i", "iv", "bVII", "i"]],
    "outro": [["i", "bVII", "bVI", "i"], ["I", "IV", "I", "I"]],
    "intro": [["I", "bVII", "bVI", "bVII"], ["i", "bVI", "bVII", "i"]],
}

# 副歌第二句常改成半小节一个和弦（更密、更推）
HALF_BAR = {
    "chorus": ["i", "bVII", "bVI", "bVII", "i", "bVII", "bVI", "V"],
}

# 终止式：倒数第二小节的落点 + 最后一小节的落点。
# 名称沿用乐理书：authentic=正格 V–I，plagal=变格 IV–I，
# deceptive=阻碍 V–vi，half=半终止停在 V。
_CADENCE_APPROACH = {
    "authentic": {"major": "V7", "minor": "V7"},
    "plagal": {"major": "IV", "minor": "iv"},
    "deceptive": {"major": "V7", "minor": "V7"},
    "half": {"major": "IV", "minor": "iv"},
}
_CADENCE_FINAL = {
    "authentic": {"major": "Imaj7", "minor": "i"},
    "plagal": {"major": "I", "minor": "i"},
    "deceptive": {"major": "vi", "minor": "bVI"},
    "half": {"major": "V7", "minor": "V7"},
}

# 每段用哪种终止：权重。桥段/预副歌停属和弦，把张力交给下一段。
_CADENCE_WEIGHTS = {
    "intro": [("half", 4), ("plagal", 3), ("authentic", 2)],
    "verse": [("authentic", 5), ("half", 2), ("deceptive", 2), ("plagal", 1)],
    "prechorus": [("half", 6), ("authentic", 3), ("plagal", 1)],
    "chorus": [("authentic", 8), ("deceptive", 1), ("plagal", 1)],
    "solo": [("authentic", 5), ("half", 3), ("plagal", 1)],
    "bridge": [("half", 6), ("deceptive", 2), ("plagal", 1)],
    "outro": [("plagal", 4), ("half", 3), ("authentic", 2)],
    "drop": [("authentic", 6), ("half", 2)],
    "hook": [("authentic", 5), ("deceptive", 2)],
    "build": [("half", 6), ("authentic", 2)],
    "break": [("half", 4), ("plagal", 2)],
}

_QUALITY_RE = re.compile(r"(7|9|6|sus\d?|add\d?|dim\d?|maj7|m7)", re.IGNORECASE)


def _family(genre):
    if genre in PROGRESSIONS:
        return genre
    return "pop"


def _starts_tonic(prog):
    return _roman_core(prog[0]) == "I" and not str(prog[0]).startswith(("b", "#"))


def _minorish(prog):
    """判断一条进行是不是小调写法（i/iv/v 小写，或带 bIII/bVI/bVII）。"""
    first = str(prog[0])
    m = re.match(r"^([b#]?)(VII|VI|V|IV|III|II|I)", first, re.IGNORECASE)
    if not m:
        return False
    acc, roman = m.group(1), m.group(2)
    if roman[0].islower() and roman.upper() in ("I", "IV", "V"):
        return True
    return acc == "b" and roman.upper() in ("III", "VI", "VII")


def _minor_compatible(prog):
    """小调里不能出现大三的 I / IV：Imaj7 会把调性直接拽到大调。"""
    for sym in prog:
        s = str(sym)
        core = _roman_core(s)
        if core in ("I", "IV") and s[:1].isupper():
            return False
    return True


def _roman_core(symbol):
    m = re.match(r"^[b#]?(VII|VI|V|IV|III|II|I)", str(symbol).strip(), re.IGNORECASE)
    return m.group(1).upper() if m else str(symbol)


# --------------------------------------------------------------- 和声节奏

def chord_beats(bpm, role, bars, energy=0.6, half_bar=False):
    """一个和弦持续几拍。BPM 越高，换得越慢；慢歌反而给两小节。

    规则来自配和弦指南：<60 每 2–4 小节、60–100 每 1–2 小节、
    100–140 每 1 或半小节；>140 的情歌/摇滚主歌回到 2 小节，
    避免 155 BPM 下每拍一个和弦把歌切成碎块。
    """
    bpm = float(bpm or 100)
    if bars <= 4:
        return 4.0
    if half_bar and energy >= 0.82:
        return 2.0
    if bpm < 62:
        return 8.0
    if bpm < 90:
        if role in ("verse", "intro", "bridge") and energy < 0.62:
            return 8.0
        return 4.0
    if bpm <= 132:
        if role == "chorus" and energy >= 0.86:
            return 2.0
        return 4.0
    # 155 BPM 的朋克/摇滚：主歌、前奏给两小节，副歌仍一小节
    if role in ("verse", "intro", "bridge") and energy < 0.7:
        return 8.0
    return 4.0


# --------------------------------------------------------------- 色彩 / 借用

def _degree_of(ch, key):
    return (ch["root_pc"] - key["pc"]) % 12


def colorize(symbol, key, rng, flavors):
    """给三和弦加七/九/挂留色彩；已经有色彩符号就不动。

    七和弦不是到处乱加：I/IV 走 maj7，V 走属七，ii/iii/vi 走 m7，
    bVII 在 Mixolydian 里走属七。摇滚曲风包没写 maj7 的，就保持三和弦的
    干脆——加错色彩比不加更难听。
    """
    if "/" in symbol or _QUALITY_RE.search(symbol):
        return symbol
    ch = parse_chord(symbol, key)
    tri = [i % 12 for i in ch["intervals"][:3]]
    if tri not in ([0, 4, 7], [0, 3, 7]):
        return symbol
    major_triad = tri == [0, 4, 7]
    deg = _degree_of(ch, key)
    fam = set(flavors or [])

    if major_triad:
        if deg == 7:                       # V
            return symbol + "7"
        if deg == 10 and ("dominant" in fam or "extended" in fam):
            return symbol + "7"            # bVII7
        if deg in (0, 5) and ("maj7" in fam or "extended" in fam):
            return symbol + "maj7"
        if deg == 0 and "extended" in fam and rng.random() < 0.45:
            return symbol + "add9"
    else:
        if "minor7" in fam or "extended" in fam:
            if rng.random() < 0.7:
                return symbol + "7"
        if "maj7" in fam and rng.random() < 0.35:
            return symbol + "7"
    return symbol


def _add_secondary(events, key, rng, bars, energy):
    """把一个目标和弦前面的和弦换成它的副属（V7/ii、V7/vi…）。"""
    if bars < 8 or energy < 0.55 or rng.random() > 0.38:
        return events
    cands = []
    for i in range(1, len(events) - 2):
        bar, beat, sym = events[i]
        if bar >= bars - 2 or "/" in sym:
            continue
        core = _roman_core(sym).upper()
        if core in ("II", "III", "IV", "VI"):
            # 小调里 V7/iv 等于把主和弦升成属七（I7），听起来像突然转调，跳过
            if key["mode"] == "minor" and core == "IV":
                continue
            cands.append(i)
    if not cands:
        return events
    i = rng.choice(cands)
    target = events[i][2]
    # 目标和弦只看级数，去掉色彩后缀（vi7 -> vi）
    m = re.match(r"^([b#]?)(VII|VI|V|IV|III|II|I)", target, re.IGNORECASE)
    if not m:
        return events
    qual = "V7/" + m.group(2).lower()
    events[i - 1][2] = qual
    return events


def _borrow(events, key, rng, bars):
    """调式互换：大调里借 iv / bVI / bII，制造一下「不属于这里」的颜色。"""
    if key["mode"] != "major" or rng.random() > 0.22:
        return events
    swaps = {"IV": "iv", "vi": "bVI", "ii": "bII"}
    cands = []
    for i, (bar, beat, sym) in enumerate(events):
        if bar >= bars - 2 or "/" in sym or _QUALITY_RE.search(sym):
            continue
        if sym in swaps:
            cands.append(i)
    if not cands:
        return events
    i = rng.choice(cands)
    events[i][2] = swaps[events[i][2]]
    return events


# --------------------------------------------------------------- 进行

def _choose_cadence(role, rng, final_section=False):
    if final_section and role not in ("intro", "prechorus"):
        return "authentic" if rng.random() < 0.7 else "plagal"
    pool = _CADENCE_WEIGHTS.get(role) or [("authentic", 4), ("half", 1)]
    total = sum(w for _t, w in pool)
    r = rng.uniform(0, total)
    for name, w in pool:
        r -= w
        if r <= 0:
            return name
    return pool[0][0]


def _apply_cadence(events, key, rng, role, bars, final_section, energy):
    """把最后两小节改成选定的终止式；最后一小节只留一个和弦让它响完。"""
    if bars < 2:
        return events
    kind = _choose_cadence(role, rng, final_section)
    mode = key["mode"]
    approach = _CADENCE_APPROACH[kind][mode]
    final = _CADENCE_FINAL[kind][mode]
    # 低能量结尾（outro/break）用大三和弦更「收」，不挂七度
    if role == "outro" and kind in ("plagal", "authentic") and energy < 0.5:
        final = "I" if mode == "major" else "i"

    last = bars - 1
    events = [e for e in events if e[0] < last]
    # 倒数第二小节：只留第一拍，其余让位给终止式
    events = [e for e in events if not (e[0] == last - 1 and e[1] > 1e-6)]
    if not any(e[0] == last - 1 for e in events):
        events.append([last - 1, 0.0, approach])
    else:
        for e in events:
            if e[0] == last - 1:
                e[2] = approach
    events.append([last, 0.0, final])
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def plan_progression(genre, key, role, bars, rng, half_bar=False, pack=None,
                     bpm=None, energy=0.6, final_section=False):
    """返回 [(bar, beat, symbol)]。bar 从 0 开始，beat 0–4。"""
    fam = _family(genre)
    pool = []
    flavors = []
    if pack:
        for prog in pack.get("progressions") or []:
            if isinstance(prog, list) and prog:
                try:
                    for sym in prog:
                        parse_chord(sym, key)
                    pool.append(list(prog))
                except Exception:
                    continue
        flavors = list(pack.get("keyFlavors") or [])
    if not pool:
        pool = [list(p) for p in PROGRESSIONS.get(fam, PROGRESSIONS["pop"])]

    # 调式过滤：小调不要拿大调进行硬套（Imaj7 会变成大三和弦），
    # 大调也不要拿 i–bVI 的进行；包里有对应调式的就用包的。
    if key["mode"] == "minor":
        minor_pool = [p for p in pool if _minorish(p) and _minor_compatible(p)]
        pool = minor_pool or [list(p) for p in
                              MINOR_PROGRESSIONS.get(fam, MINOR_PROGRESSIONS["pop"])]
    else:
        major_pool = [p for p in pool if not _minorish(p)]
        pool = major_pool or [list(p) for p in MAJOR_FALLBACK]

    opts = [p for p in pool if _starts_tonic(p)]
    chosen = list(rng.choice(opts or pool))

    # 色彩：先决定整段循环的颜色，再铺到网格上，保证同一和弦每次出现一致
    chosen = [colorize(sym, key, rng, flavors) for sym in chosen]

    # 4 小节一段的短段落：别再每段都「主–下属–属–主」地收一次终止，
    # 那样十段听下来全是同一个句式；把循环转个位，让段落从 bVI / iv 起，
    # 只保留能落回主/属的转位，段与段之间才有推进感。
    short_section = bars <= 4 and not final_section
    # 只有主歌/独奏段旋转循环：副歌要从主和弦砸下来，预副歌要停在属和弦上，
    # 转位会把这两个功能位弄丢。
    if short_section and role in ("verse", "solo") and len(chosen) > 2:
        options = []
        for k in (1, 2, 3):
            last = _roman_core(chosen[(k - 1) % len(chosen)])
            if last in ("I", "V"):        # 只留能落回主/属的转位
                options.append(k)
        if options and rng.random() < 0.65:
            k = rng.choice(options)
            chosen = chosen[k:] + chosen[:k]

    bpb = chord_beats(bpm, role, bars, energy, half_bar)
    events = []
    total = bars * 4.0
    pos = 0.0
    i = 0
    while pos < total - 1e-6:
        bar = int(pos // 4)
        beat = pos - bar * 4
        events.append([bar, beat, chosen[i % len(chosen)]])
        pos += bpb
        i += 1

    # 保证最后一小节有落点（慢歌两小节一换时可能错过）
    if not any(e[0] == bars - 1 for e in events):
        events.append([bars - 1, 0.0, chosen[len(events) % len(chosen)]])

    # 副歌的 push / 替代：只在半小节进行时做，避免把正常一小节一和弦打乱
    if half_bar and bars >= 8 and energy >= 0.82:
        for e in events:
            if e[0] % 4 == 2 and e[1] == 0.0 and rng.random() < 0.4:
                e[1] = 0.5

    events = _add_secondary(events, key, rng, bars, energy)
    events = _borrow(events, key, rng, bars)
    if (not short_section) or role == "intro" or final_section:
        events = _apply_cadence(events, key, rng, role, bars, final_section, energy)
    return [(int(b), float(bt), sym) for b, bt, sym in events]


# --------------------------------------------------------------- 声部连接

def _pcs(symbol, key):
    ch = parse_chord(symbol, key)
    return [(ch["root_pc"] + i) % 12 for i in ch["intervals"]]


def _voicings(symbol, key, lo=52, hi=79, max_notes=4):
    """列出所有转位候选（原始 + 依次把最低音抬高八度）。"""
    ch = parse_chord(symbol, key)
    pcs = _pcs(symbol, key)[:max_notes]
    out = []
    seen = set()
    for octv in range(2, 6):
        root = 12 * (octv + 1) + ch["root_pc"]
        base = [root + i for i in ch["intervals"][:max_notes]]
        for inv in range(len(base)):
            notes = sorted([n + 12 * (1 if i < inv else 0) for i, n in enumerate(base)])
            if notes[0] < lo or notes[-1] > hi:
                continue
            k = tuple(notes)
            if k in seen:
                continue
            seen.add(k)
            out.append(notes)
    if not out:
        mid = 12 * (4 + 1) + key["pc"]
        out = [[mid + i for i in pcs]]
    return out


def _nearest_move(prev, notes):
    """每个新音到旧声部最近音的距离和；比逐位 zip 更接近真实声部连接。"""
    return sum(min(abs(n - p) for p in prev) for n in notes) / max(1, len(notes))


def _parallel_penalty(prev, notes):
    """平行五/八度：两个声部同向移动且间隔是纯五/八度时扣分。"""
    if not prev or len(prev) < 2 or len(notes) < 2:
        return 0.0
    pen = 0.0
    pairs = min(len(prev), len(notes))
    for i in range(pairs):
        for j in range(i + 1, pairs):
            a, b = prev[i], prev[j]
            c, d = notes[i], notes[j]
            if (b - a) in (7, -7, 12, -12) and c - a == d - b and c != a:
                pen += 2.0
    return pen


def _cost(prev, notes, center=64):
    spread = notes[-1] - notes[0]
    off = abs(sum(notes) / len(notes) - center)
    if not prev:
        return spread * 0.35 + off * 0.25
    move = _nearest_move(prev, notes)
    top_move = abs(notes[-1] - prev[-1])
    common = len({n % 12 for n in prev} & {n % 12 for n in notes})
    return (move * 1.0 + top_move * 0.35 + spread * 0.30 + off * 0.20
            - common * 1.2 + _parallel_penalty(prev, notes))


def voice_chords(symbols, key, lo=52, hi=79, center=64):
    """给一串和弦做声部连接，返回 [[midi,...], ...]。"""
    out = []
    prev = None
    for sym in symbols:
        cands = _voicings(sym, key, lo, hi)
        best = min(cands, key=lambda n: _cost(prev, n, center))
        out.append(best)
        prev = best
    return out


def power_chord(symbol, key, root_octave=2):
    """根音 + 五度 + 八度（再补一个高八度根，失真下更厚）。"""
    ch = parse_chord(symbol, key)
    root = 12 * (root_octave + 1) + ch["root_pc"]
    return [root, root + 7, root + 12, root + 24]


def bass_pitch(symbol, key, octave=2):
    return 12 * (octave + 1) + parse_chord(symbol, key)["root_pc"]


def chord_scale_tones(symbol, key):
    """和弦音按调内音阶排序，供旋律选音时判断「强拍落和弦音」。"""
    pcs = set(_pcs(symbol, key))
    sc = scale_pcs(key)
    return [pc for pc in sc if pc in pcs]


def nearest_chord_tone(pitch, symbol, key, span=6):
    pcs = set(_pcs(symbol, key))
    best = pitch
    bestd = 99
    for p in range(pitch - span, pitch + span + 1):
        if p % 12 in pcs:
            d = abs(p - pitch)
            if d < bestd:
                best, bestd = p, d
    return best


def cadence_note(symbol, key, octave=4):
    """段落收尾音：主和弦取根音，属和弦取三音（留张力）。"""
    ch = parse_chord(symbol, key)
    root = 12 * (octave + 1) + ch["root_pc"]
    if str(symbol).upper().startswith("V"):
        return root + ch["intervals"][1]
    return root
