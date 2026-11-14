import re
from pathlib import Path

from .util import jdump, jload

PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】—…·~～\-]+")
CJK = re.compile(r"[\u4e00-\u9fff]")


def count_syllables(line):
    text = PUNCT.sub("", line)
    cjk = len(CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]+", text))
    return cjk + latin


def parse_text(text):
    sections = []
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            current = {"section": m.group(1).strip(), "lines": []}
            sections.append(current)
            continue
        if not line:
            current = None
            continue
        if current is None:
            current = {"section": f"part{len(sections) + 1}", "lines": []}
            sections.append(current)
        current["lines"].append(line)
    return sections


def validate_text(text):
    sections = parse_text(text)
    warnings = []
    out = []
    for sec in sections:
        lines = []
        for line in sec["lines"]:
            n = count_syllables(line)
            bars = max(1, int((n + 3) // 4))
            if n == 0:
                warnings.append(f"空句：{line}")
            if n > 20:
                warnings.append(f"句子过长（{n} 字）：{line[:16]}…")
            if n % 4 != 0:
                warnings.append(f"「{line[:12]}」{n} 字，不是 4 的倍数；建议拆成 {bars} 小节或增减 {4 - n % 4} 字")
            lines.append({"text": line, "syllables": n, "suggestedBars": bars})
        out.append({"section": sec["section"], "lines": lines,
                    "syllables": sum(l["syllables"] for l in lines)})
    return {"sections": out, "warnings": warnings,
            "totalSyllables": sum(s["syllables"] for s in out)}


def set_lyrics(project_path, text, section=None):
    p = Path(project_path)
    if p.is_file():
        p = p.parent
    data = validate_text(text)
    record = {"language": "zh", "text": text, "sections": data["sections"],
              "warnings": data["warnings"], "totalSyllables": data["totalSyllables"]}
    jdump(p / "lyrics" / "lyrics.json", record)
    (p / "lyrics" / "lyrics.txt").write_text(text, encoding="utf-8")
    return {"ok": True, "project": str(p), **data}


def load_lyrics(project_path):
    p = Path(project_path)
    if p.is_file():
        p = p.parent
    data = jload(p / "lyrics" / "lyrics.json", None)
    if data:
        return data
    # 兜底：用户直接丢一个带 [段名] 的 lyrics.txt 也能用
    txt = p / "lyrics" / "lyrics.txt"
    if txt.exists():
        text = txt.read_text(encoding="utf-8")
        parsed = validate_text(text)
        return {"language": "zh", "text": text, "sections": parsed["sections"],
                "warnings": parsed["warnings"],
                "totalSyllables": parsed["totalSyllables"]}
    return {"sections": []}
