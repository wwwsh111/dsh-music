import hashlib
import json
import math
import re
import time
from pathlib import Path

PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR_DEGREES = {"I": 0, "II": 2, "III": 4, "IV": 5, "V": 7, "VI": 9, "VII": 11}
MINOR_DEGREES = {"I": 0, "II": 2, "III": 3, "IV": 5, "V": 7, "VI": 8, "VII": 10}
DEGREES = MAJOR_DEGREES
QUALITIES = {
    "": [0, 4, 7],
    "m": [0, 3, 7],
    "7": [0, 4, 7, 10],
    "maj7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10],
    "dim": [0, 3, 6],
    "dim7": [0, 3, 6, 9],
    "sus4": [0, 5, 7],
    "sus2": [0, 2, 7],
    "add9": [0, 4, 7, 14],
    "9": [0, 4, 7, 10, 14],
    "maj9": [0, 4, 7, 11, 14],
    "m9": [0, 3, 7, 10, 14],
    "6": [0, 4, 7, 9],
    "m6": [0, 3, 7, 9],
}

MAJOR = [0, 2, 4, 5, 7, 9, 11]
MINOR = [0, 2, 3, 5, 7, 8, 10]


def jload(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8-sig"))


def jdump(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(name or "").strip()).strip("-").lower()
    if not s:
        s = "song-" + time.strftime("%Y%m%d-%H%M%S")
    return s[:48]


def parse_key(text):
    text = (text or "C").strip()
    m = re.match(r"^([A-Ga-g])([#b]?)(.*)$", text)
    if not m:
        raise ValueError(f"无法识别的调性: {text}")
    letter, accidental, rest = m.group(1).upper(), m.group(2), m.group(3).lower()
    pc = PITCH[letter] + (1 if accidental == "#" else -1 if accidental == "b" else 0)
    pc %= 12
    mode = "minor" if rest.startswith("m") and not rest.startswith("maj") else "major"
    return {"tonic": SHARP[pc], "pc": pc, "mode": mode}


def key_name(key):
    return key["tonic"] + ("m" if key["mode"] == "minor" else "")


def note_to_midi(name):
    m = re.match(r"^([A-Ga-g])([#b]?)(-?\d+)$", name.strip())
    if not m:
        raise ValueError(f"无法识别的音名: {name}")
    pc = PITCH[m.group(1).upper()]
    if m.group(2) == "#":
        pc += 1
    elif m.group(2) == "b":
        pc -= 1
    return (int(m.group(3)) + 1) * 12 + pc % 12


def midi_to_note(n):
    return f"{SHARP[int(n) % 12]}{int(n) // 12 - 1}"


def _roman_pc(roman_raw, accidental, key):
    """罗马数字级数 -> 相对主音的半音偏移（支持 b/# 前缀）。"""
    roman = roman_raw.upper()
    degrees = MAJOR_DEGREES if key["mode"] == "major" else MINOR_DEGREES
    degree = degrees[roman]
    if accidental == "b":
        degree -= 1
    elif accidental == "#":
        degree += 1
    return degree % 12


def parse_chord(symbol, key):
    """解析罗马数字和弦，支持副属和弦（V7/ii、V/vi）与七/九/挂留色彩。

    副属和弦是「目标级的属和弦」：V7/ii 表示在 ii 之前插入它自己的属七，
    根音取 ii 根音上方五度。这样进行表里可以直接写 V7/ii 这种乐理符号，
    声部连接和旋律配音仍然按真实音高算。
    """
    s = symbol.strip()
    m = re.match(r"^([b#]?)(VII|VI|V|IV|III|II|I)(.*?)(?:/([b#]?)(VII|VI|V|IV|III|II|I))?$",
                 s, re.IGNORECASE)
    if not m:
        raise ValueError(f"无法识别的和弦: {symbol}")
    accidental, roman_raw, suffix = m.group(1), m.group(2), m.group(3)
    target_acc, target_roman = m.group(4), m.group(5)
    major_case = roman_raw[0].isupper()
    q = suffix.lower()

    if target_roman:
        # 副属：目标级上方纯五度作根音，按属七/大三和弦处理
        target = _roman_pc(target_roman, target_acc, key)
        root_pc = (key["pc"] + target + 7) % 12
        intervals = QUALITIES["7"] if q in ("7", "9", "13") else QUALITIES[""]
        return {"symbol": symbol, "root_pc": root_pc, "intervals": intervals,
                "secondary": True}

    # 小调里 bIII / bVI / bVII 就是自然小调的 III / VI / VII，
    # 再加一个降号会多降半音（bVI 变成 V，整段和声就飘了）。
    if key["mode"] == "minor" and accidental == "b" and roman_raw.upper() in ("III", "VI", "VII"):
        accidental = ""
    degree = _roman_pc(roman_raw, accidental, key)
    if q == "":
        intervals = QUALITIES[""] if major_case else QUALITIES["m"]
    elif q == "7" and not major_case:
        intervals = QUALITIES["m7"]
    elif q == "9" and not major_case:
        intervals = QUALITIES["m9"]
    elif q in QUALITIES:
        intervals = QUALITIES[q]
    else:
        intervals = QUALITIES[""] if major_case else QUALITIES["m"]
    root_pc = (key["pc"] + degree) % 12
    return {"symbol": symbol, "root_pc": root_pc, "intervals": intervals}

def chord_midi(symbol, key, octave=4, max_notes=4):
    ch = parse_chord(symbol, key)
    root = 12 * (octave + 1) + ch["root_pc"]
    notes = [root + i for i in ch["intervals"][:max_notes]]
    return notes


def scale_pcs(key):
    base = MINOR if key["mode"] == "minor" else MAJOR
    return [(key["pc"] + i) % 12 for i in base]


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def bars_to_seconds(bars, bpm, meter=4):
    return bars * meter * 60.0 / float(bpm)


def ensure_dir(path):
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
