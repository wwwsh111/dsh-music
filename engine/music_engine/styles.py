"""每首歌的「演奏风格档案」：同曲风不同歌，律动/奏法/织体都不一样。

之所以单独一层，而不是把参数摊在 compose 里：一首歌里鼓、节奏吉他、键盘、
铺底要共享同一套风格决定（比如「这歌是半拍的闷」，那鼓要半拍、吉他也要长音），
否则各轨各随机，反而不成一体。
"""
from __future__ import annotations

import random

# 节奏吉他的基本奏法（一小节内的起音位置与持续拍数）
STRUM = {
    # 闷音 / 朋克 8 分：短、干
    "chug8": [(i * 0.5, 0.26) for i in range(8)],
    # 开放 8 分：长音互相压住，形成和声墙
    "open8": [(i * 0.5, 0.72) for i in range(8)],
    # 切分：第 2 拍后半与第 4 拍前半留白，摇滚里最常见的「推」
    "sync": [(0.0, 0.7), (0.5, 0.4), (1.5, 0.4), (2.0, 0.7), (2.5, 0.4), (3.0, 0.4), (3.5, 0.4)],
    # 半拍：一小节两下长音，重、空
    "half": [(0.0, 1.85), (2.0, 1.85)],
    # 16 分墙（最后一个副歌/高潮用）
    "drive16": [(i * 0.25, 0.24) for i in range(16)],
    # 主题 riff：8 分 + 留白，像前奏的记忆点
    "riff": [(0.0, 0.5), (0.5, 0.5), (1.5, 0.5), (2.0, 0.5), (3.0, 0.5), (3.5, 0.5)],
}

# 每小节可选的「微变化」：把这一小节的谱面换掉一点，避免 8 小节一模一样
def _bar_variation(hits, rng, bar, bars, energy=0.6):
    """返回这一小节实际要弹的 (local_beat, dur_beats) 列表。

    摇滚吉他手每一遍都不会弹得一模一样：换一下闷音断点、把一个 8 分拆成两个
    16 分、句尾抢半拍。这里每小节都动一处，动的位置和方式不同，
    所以按「起音签名」看每小节都不一样，但律动仍然连得上。
    """
    out = list(hits)
    pos = bar % 4
    if pos == 3 and bars > 4 and rng.random() < 0.55:
        # 乐句末：最后两拍变 16 分推进（过门的吉他版）
        return sorted([h for h in out if h[0] < 2.0] + [(2.0 + i * 0.25, 0.22) for i in range(8)])

    kind = rng.random()
    if kind < 0.38 and len(out) > 3:
        # 把某个 8 分拆成两个 16 分（摇滚里最自然的「加速」）
        cand = [i for i, (t, _d) in enumerate(out) if abs(t * 2 % 1.0) < 1e-6]
        if cand:
            i = rng.choice(cand)
            t = out[i][0]
            del out[i]
            out += [(t, 0.2), (t + 0.25, 0.2)]
    elif kind < 0.68 and len(out) > 4:
        # 抽掉一个不落在整拍上的音，制造呼吸
        cand = [i for i, (t, _d) in enumerate(out) if abs(t % 1.0) > 1e-6]
        if cand:
            del out[rng.choice(cand)]
    elif kind < 0.88:
        # 3.75 的抢先（push），会让律动听起来「急」
        out.append((3.75, 0.2))
    else:
        # 把某一拍的后半补上一个短音（八分变附点感）
        cand = [t for t, _d in out if abs(t * 2 % 1.0) < 1e-6 and t + 0.75 < 4.0]
        if cand:
            t = rng.choice(cand)
            out.append((t + 0.75, 0.2))
    if energy < 0.5 and rng.random() < 0.25:
        out = [h for h in out if abs(h[0] % 2.0) < 1e-6]
    return sorted(out)


def strum_bar(pattern_name, rng, bar, bars, energy=0.6):
    hits = STRUM.get(pattern_name) or STRUM["chug8"]
    return _bar_variation(hits, rng, bar, bars, energy)


# --------------------------------------------------------------- 键盘织体

KEYS_STYLES = ("block", "stab", "arp", "none")


def keys_bar(style, notes, rng, bar, bars, energy=0.6):
    """把一串和弦音摆成一小节的键盘事件：block=块状和弦，stab=短促切分，
    arp=8 分琶音（副歌用，能少和别人撞频段）。"""
    if style == "none" or not notes:
        return []
    if style == "arp":
        seq = list(notes) + list(reversed(notes[:-1])) if len(notes) > 1 else list(notes)
        out = []
        for i in range(8):
            pitch = seq[i % len(seq)]
            out.append((pitch, i * 0.5, 0.42))
            # 每两小节向上翻一次八度，琶音才有走向
            if (bar // 2) % 2 == 1 and i in (2, 6) and pitch + 12 <= 84:
                out.append((pitch + 12, i * 0.5 + 0.25, 0.3))
        return out
    if style == "stab":
        pools = (
            [(0.0, 0.4), (1.5, 0.3), (2.5, 0.4)],
            [(0.5, 0.3), (2.0, 0.4), (3.5, 0.3)],
            [(0.0, 0.4), (0.75, 0.25), (2.5, 0.4)],
        )
        pos = pools[bar % len(pools)]
        if energy < 0.55:
            pos = pos[:2]
        return [(p, t, d) for p in notes for t, d in pos]
    # block：整小节按住；但偶数小节改成两下（长+短），句尾补一个 16 分推
    out = []
    if bar % 2 == 0:
        out += [(p, 0.0, 3.6) for p in notes]
    else:
        out += [(p, 0.0, 1.5) for p in notes]
        out += [(p, 2.5, 1.2) for p in notes]
    if energy >= 0.75 and rng.random() < 0.5:
        out += [(p, 3.5, 0.4) for p in notes]
    if bar % 4 == 3 and len(notes) > 1 and rng.random() < 0.6:
        # 句尾：最高音往上跳一小段，给下一个乐句留个引子
        top = max(notes)
        out += [(top + 3, 3.75, 0.2), (top + 7, 3.75 + 0.12, 0.16)]
    return out


def _space(rng, kind):
    """每首歌一套「房间」：混响房型/预延迟/返回量、延迟返回量、总线宽度。

    为什么不是常数：同一套编曲换一个空间，听感从「卧室贴脸」变成「大场地」，
    这是同一支乐队不同专辑之间最明显的差别之一。
    """
    if kind == "anthem":
        room = rng.uniform(0.56, 0.70); damp = rng.uniform(0.38, 0.52)
        pre = rng.uniform(14, 26); rev = rng.uniform(0.30, 0.40)
    else:
        room = rng.uniform(0.45, 0.62); damp = rng.uniform(0.45, 0.62)
        pre = rng.uniform(8, 22); rev = rng.uniform(0.22, 0.34)
    return {
        "room": round(room, 3),
        "damping": round(damp, 3),
        "predelayMs": round(pre, 1),
        "reverbReturn": round(rev, 3),
        "delayReturn": round(rng.uniform(0.16, 0.26), 3),
        "width": round(rng.uniform(0.08, 0.22), 3),
    }


def make_profile(project, pack, rng):
    """按曲风给一首歌定一套演奏风格。同一 seed 稳定。"""
    genre = project["genre"]
    brief = " ".join(project.get("modifiers") or [])
    bpm = float(project["tempo"])
    rock = genre in ("rock", "metal", "punk", "emo", "vocalrock")
    heavy = any(w in brief for w in ("hard", "punk", "emo", "重", "硬", "冲", "燃"))
    slow = bpm < 125

    # 鼓：整首一套（kit.pick_variant 的候选池在 kit.VARIANTS 里）
    drum_set = rng.choice(["base", "alt", "half"]) if rock else rng.choice(["base", "base", "alt"])

    space = _space(rng, "anthem" if rock else "pop")

    if not rock:
        return {
            "archetype": rng.choice(["pop", "acg", "ballad"]),
            "drums": drum_set,
            "space": space,
            "strum": {r: rng.choice(["open8", "sync", "chug8"]) for r in
                      ("verse", "prechorus", "chorus", "bridge", "solo", "outro")},
            "keys": {r: rng.choice(["block", "arp"]) for r in ("intro", "verse", "chorus", "outro")},
            "keysDefault": "block",
            "padOpen": rng.random() < 0.6,
            "lift": rng.choice([2, 4]),
            "swing": SWING.get(genre, 0.0),
        }

    # 摇滚：三种原型——不同的歌听感差异主要来自这里。
    # 优先级：拍速决定默认，brief 里的词可以改写（用户说要「大合唱」就别给拍半，
    # 说「emo/慢/半拍」就别给 16 分墙），这样同样的曲风也能做出不同的歌。
    wide_words = ("大合唱", "齐唱", "开阔", "史诗", "wide", "anthem", "singalong")
    half_words = ("半拍", "half", "慢", "emo", "伤感", "ballad", "抒情")
    archetypes = ["anthem", "drive", "half"]
    if slow:
        archetypes = ["half", "anthem", "anthem"]
    if any(w in brief.lower() for w in wide_words):
        archetypes = ["anthem", "anthem", "drive"]
    if heavy:
        archetypes = ["drive", "drive", "anthem"]
    if any(w in brief.lower() for w in half_words):
        archetypes = ["half", "half", "anthem"]
    arch = rng.choice(archetypes)
    if arch not in ("anthem", "drive", "half"):
        arch = "anthem"

    if arch == "drive":
        strum = {"verse": "chug8", "prechorus": "sync", "chorus": "open8",
                 "bridge": "half", "solo": "open8", "outro": "half"}
        keys = {"intro": "none", "verse": "none", "prechorus": "stab", "chorus": "arp", "outro": "none"}
        keys_default = "none"
        pad_open = False
        strum_last_chorus = "drive16"
    elif arch == "half":
        strum = {"verse": "half", "prechorus": "chug8", "chorus": "sync",
                 "bridge": "half", "solo": "open8", "outro": "half"}
        keys = {"intro": "arp", "verse": "block", "prechorus": "stab", "chorus": "block", "outro": "block"}
        keys_default = "block"
        pad_open = True
        strum_last_chorus = "open8"
    else:  # anthem
        strum = {"verse": "chug8", "prechorus": "sync", "chorus": "open8",
                 "bridge": "half", "solo": "open8", "outro": "half"}
        keys = {"intro": "arp", "verse": "stab", "prechorus": "arp", "chorus": "block", "outro": "block"}
        keys_default = "stab"
        pad_open = True
        strum_last_chorus = "open8"

    # 箱体也按原型分：英式 4x12 更紧更「中频推」，美式现代更亮、超低频更多，
    # 复古美式偏暗一点。同一套演奏换箱体，听感就已经不是同一支乐队。
    cabs = {"anthem": ("cab_modern_uk.wav", "cab_modern_us.wav"),
            "drive": ("cab_modern_us.wav", "cab_modern_uk.wav"),
            "half": ("cab_vintage_us.wav", "cab_modern_uk.wav")}
    cab = rng.choice(cabs.get(arch, ("cab_modern_uk.wav",)))
    return {
        "archetype": arch,
        "drums": drum_set,
        "cab": cab,
        "space": space,
        "strum": strum,
        "strumLastChorus": strum_last_chorus,
        "keys": keys,
        "keysDefault": keys_default,
        "padOpen": pad_open,
        "lift": rng.choice([2, 4]),
        "swing": 0.0 if rng.random() < 0.5 else rng.uniform(0.004, 0.012),
    }


# 摇摆量：8 分反拍往后挪多少拍。0.17 ≈ 三连音摇摆。
# 之前非摇滚一律写 0，lofi/hiphop 的「拖」只靠 humanize 的 ±10–30 ms jitter，
# 听感是「不齐」而不是「摇」。
SWING = {"lofi": 0.17, "hiphop": 0.13, "rnb": 0.11, "citypop": 0.07,
         "folk": 0.06, "guofeng": 0.06, "trap": 0.05}


def swing_for(genre):
    return SWING.get(genre or "", 0.0)


def drums_variant(profile, rng, genre=None):
    """把 profile 的鼓名换成该曲风实际能用的谱面。

    旧实现无条件返回 kit.ROCK/ROCK_ALT/ROCK_HALF，于是 trap/lofi/house/
    citypop 全都在打摇滚鼓（trap 的 16 分 hat 与滚奏永远出不来）。现在按
    kit.VARIANTS 的池取；池里没有这个曲风，就退回它的 family。
    """
    from . import kit as _kit
    genre = genre or profile.get("genre") or ""
    pool = _kit.VARIANTS.get(genre)
    if pool:
        idx = {"base": 0, "alt": 1, "half": 2}.get(profile.get("drums", "base"), 0)
        return pool[min(idx, len(pool) - 1)]
    return _kit.family_for(genre)
