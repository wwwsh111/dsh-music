"""鼓组节奏库。

设计要点（为什么不是「每小节印同一段」）：

* 一套鼓的律动感来自 **重音、留白和过门**，不是来自「每拍都有音」。
  这里每个 pattern 用 16 字一行（16 分音符网格）的可读谱写成，
  大写 X = 重音，小写 x = 正常，g = ghost（很轻），o = 开着（open hat），
  . = 空拍；这样重音位置、反拍、半拍 shuffle 都能直接读出来。
* 段落角色（intro/verse/prechorus/chorus/bridge/solo/outro）决定 loop 和力度，
  4/8 小节乐句的最后一小节自动加过门，段落第一小节自动加 crash。
* 底鼓的 16 分位置会被贝斯和 sidechain 复用，鼓和低音才能真正「咬住」。

事件格式：(beat_in_section, lane, velocity)，beat 以四分音符为单位。
"""

GM = {
    "kick": 36, "snare": 38, "rim": 37, "clap": 39,
    "hat": 42, "open_hat": 46, "pedal_hat": 44,
    "crash": 49, "ride": 51, "ride_bell": 53,
    "tom_hi": 50, "tom_mid": 47, "tom_lo": 45,
    "shaker": 70,
}

# ---------------------------------------------------------------- 谱面

ROCK = {
    "verse": {
        "kick": "x.......x..x....",
        "snare": "....x.......x...",
        "hat": "x.x.x.x.x.x.x.x.",
    },
    "prechorus": {
        "kick": "x...x...x...x...",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
    },
    "chorus": {
        "kick": "x..x....x.x.....",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
    },
    "solo": {
        "kick": "x.......x.x.....",
        "snare": "....x.......x...",
        "hat": "x.xxx.x.x.xxx.x.",
    },
    "bridge": {
        "kick": "x.......x.......",
        "snare": "........x.......",
        "open_hat": "....o.......o...",
    },
    "intro": {
        "kick": "x.......x.......",
        "snare": "........x.......",
        "hat": "x...x...x...x...",
    },
    "outro": {
        "kick": "x.......x.......",
        "snare": "....x.......x...",
        "ride": "x.x.x.x.x.x.x.x.",
    },
}

# 更「冲」的一档：朋克/EMO 的 8 分底鼓 + 16 分踩镲
ROCK_DRIVE = {
    "verse": {
        "kick": "x..x..x.x..x..x.",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
    },
    "chorus": {
        "kick": "x..x..x.x..x..x.",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
        "crash": "x...............",
    },
}

# 同一曲风的「另一套鼓」：底鼓错位、踩镲切分——不同歌用不同套，
# 同一首歌的乐句之间再换一次，听起来才不是把 4 小节复制 N 遍。
ROCK_ALT = {
    "verse": {
        "kick": "x..x....x..x....",
        "snare": "....x.......x..g",
        "hat": "x.x.x.x.x.x.xx.x",
    },
    "prechorus": {
        "kick": "x...x..x..x..x..",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
    },
    "chorus": {
        "kick": "x..x....x..x..x.",
        "snare": "....x.......x...",
        "hat": "x.xxx.xxx.xxx.xx",
    },
    "solo": {
        "kick": "x.......x.....x.",
        "snare": "....x.......x...",
        "hat": "x.x.x.x.x.x.x.x.",
    },
    "bridge": {
        "kick": "x.......x.......",
        "snare": "........x.......",
        "open_hat": "....o.......o...",
    },
    "intro": {
        "kick": "x.......x.......",
        "snare": "........x.......",
        "ride": "x...x...x...x...",
    },
    "outro": {
        "kick": "x.......x.......",
        "snare": "....x.......x...",
        "ride": "x.x.x.x.x.x.x.x.",
    },
}

# 半拍（half-time）一档：更重、更空，用于桥段或慢速主歌
ROCK_HALF = {
    "verse": {
        "kick": "x.......x.......",
        "snare": "........x.......",
        "hat": "x...x...x...x...",
    },
    "chorus": {
        "kick": "x.......x...x...",
        "snare": "........x.......",
        "hat": "x...x...x...x.x.",
        "crash": "x...............",
    },
    "bridge": {
        "kick": "x...............",
        "snare": "........x.......",
        "open_hat": "....o.......o...",
    },
}

POP = {
    "verse": {
        "kick": "x.......x...x...",
        "snare": "....x.......x...",
        "hat": "x.x.x.x.x.x.x.x.",
    },
    "chorus": {
        "kick": "x...x...x...x.x.",
        "snare": "....x.......x...",
        "clap": "....x.......x...",
        "hat": "x.xxx.xxx.xxx.xx",
    },
    "bridge": {
        "kick": "x...............",
        "snare": "........x.......",
        "hat": "..x...x...x...x.",
    },
}

ACG = {
    "verse": {
        "kick": "x.....x.x.......",
        "snare": "....x.......x...",
        "hat": "x.xxx.xxx.xxx.xx",
    },
    "chorus": {
        "kick": "x.x...x.x.x...x.",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
        "crash": "x...............",
    },
    "bridge": {
        "kick": "x.......x.......",
        "snare": "....x.......x..g",
        "shaker": "x.x.x.x.x.x.x.x.",
    },
}

ELECTRONIC = {
    "verse": {
        "kick": "x...x...x...x...",
        "clap": "....x.......x...",
        "hat": "..x...x...x...x.",
    },
    "chorus": {
        "kick": "x...x...x...x...",
        "clap": "....x.......x...",
        "open_hat": "..o...o...o...o.",
        "hat": "x.x.x.x.x.x.x.x.",
    },
}

HIPHOP = {
    "verse": {
        "kick": "x.....x...x.....",
        "snare": "....x.......x...",
        "hat": "x.xxx.x.x.xxx.x.",
    },
    "chorus": {
        "kick": "x.....x...x..x..",
        "snare": "....x.......x...",
        "hat": "xxxxxxxxxxxxxxxx",
    },
}

LOFI = {
    "verse": {
        "kick": "x.....x...x.....",
        "snare": "....x.......x..g",
        "hat": "x.g.x.g.x.g.x.g.",
    },
    "chorus": {
        "kick": "x.....x...x.....",
        "snare": "....x.......x...",
        "hat": "x.g.x.g.x.g.x.g.",
    },
}

TRAP = {
    "verse": {
        "kick": "x.....x.x.......",
        "snare": "........x.......",
        "hat": "xxxxxxxxxxxxxxxx",
    },
    "chorus": {
        "kick": "x..x..x...x.x...",
        "snare": "........x.......",
        "hat": "xxxxxxxxxxxxxxxx",
    },
}

FOLK = {
    "verse": {
        "kick": "x.......x.......",
        "snare": "....x.......x...",
        "shaker": "x.x.x.x.x.x.x.x.",
    },
    "chorus": {
        "kick": "x...x...x...x...",
        "snare": "....x.......x...",
        "shaker": "x.xxx.x.x.xxx.x.",
    },
}

CINEMATIC = {
    "verse": {
        "kick": "x...............",
        "snare": "................",
        "shaker": "x...x...x...x...",
    },
    "chorus": {
        "kick": "x.......x.......",
        "snare": "....x.......x...",
        "ride": "x.x.x.x.x.x.x.x.",
    },
}

FAMILY = {
    "rock": ROCK, "vocalrock": ROCK, "pop": POP, "acg": ACG, "house": ELECTRONIC, "synthwave": ELECTRONIC,
    "hiphop": HIPHOP, "lofi": LOFI, "trap": TRAP, "rnb": HIPHOP, "folk": FOLK,
    "guofeng": FOLK, "cinematic": CINEMATIC, "kids": POP, "citypop": POP,
}


def family_for(genre):
    fam = FAMILY.get(genre) or ROCK
    return fam


# 每个曲风可选的「几套鼓」：不同歌抽不同套（同一首歌里由 seed 固定）
VARIANTS = {
    "rock": [ROCK, ROCK_ALT, ROCK_HALF],
    "vocalrock": [ROCK, ROCK_DRIVE, ROCK_ALT],
    "acg": [ACG, ROCK_DRIVE],
    "pop": [POP, ROCK, ROCK_HALF],
    "folk": [FOLK, ROCK_HALF],
}


def pick_variant(genre, rng):
    """给一首歌选一套鼓（同 seed 稳定）。"""
    pool = VARIANTS.get(genre)
    if pool:
        return rng.choice(pool)
    return None


def _mutate(pattern, rng, level=1):
    """小节的微变化：加 ghost 军鼓、挪底鼓、抽掉踩镲、收尾开镲。

    让 4 小节乐句里的第 2、第 4 小节不一样——比换整套 pattern 有效：
    律动还认得出来，但不会「每小节一模一样」。
    返回 (新谱面, 额外的单发事件[(beat, lane, vel)])。
    """
    out = {k: list(v) for k, v in (pattern or {}).items()}
    extra = []
    if level >= 1:
        if rng.random() < 0.45:
            extra.append((rng.choice([0.75, 1.75, 2.75]), "snare", 38))
        if "hat" in out and rng.random() < 0.45:
            h = list(out["hat"])
            cands = [i for i, ch in enumerate(h) if ch in "xX" and i % 2 == 1]
            if cands:
                h[rng.choice(cands)] = "."
                out["hat"] = "".join(h)
    if level >= 2:
        if "kick" in out and rng.random() < 0.5:
            k = list(out["kick"])
            pos = [i for i, ch in enumerate(k) if ch in "xX"]
            if pos:
                i = rng.choice(pos)
                k[i] = "."
                k[(i + 2) % 16] = "x"
                out["kick"] = "".join(k)
        if rng.random() < 0.4:
            extra.append((3.5, "open_hat", 92))
    return out, extra


def genre_variant(genre, energy, brief=""):
    """同一曲风按能量/描述换一档律动（pop rock ↔ 朋克推进）。"""
    text = (brief or "").lower()
    drive_words = ("punk", "emo", "drive", "fast", "硬", "冲", "燃", "朋克", "疾走")
    if genre in ("rock", "acg", "vocalrock") and (energy >= 0.82 or any(w in text for w in drive_words)):
        return ROCK_DRIVE
    return None


def _steps(score):
    out = []
    for i, ch in enumerate(score[:16]):
        if ch in "Xxgo":
            vel = {"X": 118, "x": 96, "g": 44, "o": 100}[ch]
            out.append((i * 0.25, vel))
    return out


# GM 的踩镲/镲片样本本身偏轻，按轨道补一点力度，鼓组频谱才不会「只有底鼓」
LANE_GAIN = {"hat": 1.14, "open_hat": 1.10, "ride": 1.12, "crash": 1.12,
             "shaker": 0.75, "kick": 1.0, "snare": 1.0}


def _lane_hits(pattern, bar_offset_beats=0.0):
    hits = []
    for lane, score in pattern.items():
        g = LANE_GAIN.get(lane, 1.0)
        for beat, vel in _steps(score):
            hits.append((bar_offset_beats + beat, lane, int(min(127, round(vel * g)))))
    return hits


def _humanize(hits, rng, energy):
    out = []
    jitter = 0.006 + 0.010 * (1.0 - min(1.0, energy))
    for beat, lane, vel in hits:
        if lane in ("crash", "open_hat"):
            jitter_l = 0.0
        else:
            jitter_l = jitter
        t = beat + rng.uniform(-jitter_l, jitter_l)
        v = vel + rng.randint(-4, 4)
        out.append((max(0.0, t), lane, int(max(24, min(127, v)))))
    return out


def _fill(style, rng, energy, bar_beats=4.0):
    """小节末尾过门：军鼓 16 分 → 通鼓下行，能量越高越密。"""
    hits = []
    if style == "snare_run":
        base = 4.0 if energy < 0.7 else 2.0
        n = int(round((bar_beats - base) / 0.25))
        for i in range(n):
            hits.append((base + i * 0.25, "snare", 88 + (i * 3)))
    elif style == "tom_run":
        toms = ["tom_hi", "tom_hi", "tom_mid", "tom_mid", "tom_lo", "tom_lo"]
        start = 2.5 if energy < 0.75 else 2.0
        n = int(round((bar_beats - start) / 0.25))
        for i in range(n):
            hits.append((start + i * 0.25, toms[i % len(toms)], 100 - i * 2))
    elif style == "kick_run":
        # 低能量段落的出口：底鼓 8 分推 4 拍，比军鼓滚奏更「闷」更稳
        for i in range(4):
            hits.append((float(i) * 1.0, "kick", 100 + i * 2))
    elif style == "crash_hit":
        hits.append((0.0, "crash", 104))
        hits.append((2.0, "kick", 104))
    elif style == "drop_out":
        hits.append((0.0, "kick", 110))
        hits.append((2.0, "snare", 108))
    return hits


def _open_hat_lift(hits, rng):
    """段落切换前最后一个 8 分踩镲开着，进场更「抬」。"""
    hits.append((3.5, "open_hat", 96))
    return hits


def section_groove(genre, role, bars, rng, energy=0.6, brief="", phrase=4, variant=None):
    """生成一个段落的鼓事件（beat 相对段落起点）。

    variant：整首歌选定的那套鼓（pick_variant 的结果）；不传则按能量现选。
    每个乐句的后半换一次微变化，第二个乐句起再叠一层，所以 8 小节不会听起来是
    同一小节复制 8 遍。
    """
    fam = variant or genre_variant(genre, energy, brief) or family_for(genre)
    role_key = role if role in fam else ("chorus" if energy >= 0.8 else "verse")
    pattern = fam.get(role_key) or fam.get("verse")
    if pattern is None:
        pattern = ROCK["verse"]

    hits = []
    for bar in range(bars):
        bar_beats = bar * 4.0
        bar_in_phrase = bar % phrase
        level = 0
        if bar_in_phrase >= max(1, phrase // 2):
            level = 1
        if bar >= phrase:
            level = 2
        pat, extra = _mutate(pattern, rng, level) if level else (pattern, [])
        hits += _lane_hits(pat, bar_beats)
        for t, lane, v in extra:
            hits.append((bar_beats + t, lane, v))
        last_of_phrase = bar_in_phrase == phrase - 1
        # 段落第一小节给一个 crash；bridge/solo 用 ride 而不是踩镲
        if bar == 0 and role_key != "bridge":
            hits.append((bar_beats, "crash", 100 if energy > 0.7 else 88))
        if last_of_phrase and bar != bars - 1:
            if energy >= 0.75:
                hits += [(bar_beats + t, l, v) for t, l, v in _fill("tom_run", rng, energy)]
            elif energy >= 0.45:
                hits += [(bar_beats + t, l, v) for t, l, v in _fill("snare_run", rng, energy)]
            else:
                hits += [(bar_beats + t, l, v) for t, l, v in _fill("kick_run", rng, energy)]
        if bar == bars - 1 and role in ("prechorus", "build", "verse", "solo"):
            # 段落出口：开镲 + 半拍军鼓，把下一段「顶」进来
            hits = _open_hat_lift(hits, rng)
            hits += [(bar_beats + 2.75, "snare", 104)]
    # 力度渐强：按小节位置给重音
    out = []
    for t, lane, v in hits:
        bar = int(t // 4)
        pos = t - bar * 4.0
        accent = 6 if abs(pos % 1.0) < 1e-6 else 0
        if lane in ("crash", "open_hat"):
            accent = 0
        grow = 0.0
        if bars >= 8:
            grow = 6.0 * max(0.0, (t - 4.0 * bars * 0.5) / max(1.0, 4.0 * bars * 0.5))
        out.append((t, lane, int(max(24, min(127, v + accent + grow)))))
    return _humanize(out, rng, energy)


def kick_beats(hits):
    return sorted({round(t, 3) for t, lane, _v in hits if lane == "kick"})


def ghost_fill(hits, rng, density=0.25):
    """在军鼓空档里撒 ghost note，让律动「有手感」而不是机械。"""
    out = []
    for t, lane, v in hits:
        out.append((t, lane, v))
        if lane == "snare" and rng.random() < density * 0.5:
            out.append((t + 0.25, "snare", 36))
    return out
