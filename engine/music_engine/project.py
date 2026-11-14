import math
import time
from pathlib import Path

from .config import ROOT, SAMPLE_RATE
from .genres import default_bars, get_genre
from .util import ensure_dir, jdump, jload, key_name, parse_key, slugify


def _dirs(base):
    for name in ["source", "analysis", "midi", "lyrics", "preview", "report", "logs",
                 "audio/render-v1", "audio/mix", "audio/master", "audio/vocal", "export"]:
        ensure_dir(base / name)


def project_file(path):
    p = Path(path)
    if p.is_file():
        return p
    return p / "project.json"


def load_project(path):
    p = project_file(path)
    if not p.exists():
        raise ValueError(f"工程不存在: {p}")
    return jload(p, {})


def save_project(path, project):
    p = project_file(path)
    jdump(p, project)
    return p


def create_project(name=None, genre="citypop", bpm=None, key=None, bars=None):
    pack = get_genre(genre)
    keyinfo = parse_key(key or pack.get("defaultKey") or ("Am" if "minor" in (pack.get("keyFlavors") or []) else "C"))
    bpm = int(bpm or round(sum(pack.get("bpm", [90, 120])) / 2))
    structure = list(pack.get("structure") or ["intro", "verse", "chorus", "outro"])

    base_bars = [default_bars(s) for s in structure]
    total = sum(base_bars)
    want = int(bars or total)
    if want != total:
        if want >= 4 * len(base_bars):
            factor = want / float(total)
            scaled = [max(4, int(round(b * factor / 4.0)) * 4) for b in base_bars]
            diff = want - sum(scaled)
            i = len(scaled) - 1
            while diff != 0 and i >= 0:
                step = 4 if diff > 0 else -4
                if scaled[i] + step >= 4:
                    scaled[i] += step
                    diff -= step
                i -= 1
            if diff != 0:
                scaled[-1] += diff
            base_bars = scaled
        else:
            # 小节预算连每段 4 小节都放不下：从尾部砍段落，而不是把段落压成 0
            keep = max(1, want // 4)
            structure = structure[:keep]
            base_bars = [4] * keep
            base_bars[-1] += want - 4 * keep

    progressions = pack.get("progressions") or [["I", "V", "vi", "IV"]]
    sections = []
    bar = 1
    for i, (name_, nbars) in enumerate(zip(structure, base_bars)):
        prog = progressions[i % len(progressions)]
        chords = [prog[j % len(prog)] for j in range(nbars)]
        energy = {"intro": 0.25, "verse": 0.5, "prechorus": 0.65, "chorus": 0.9,
                  "bridge": 0.55, "outro": 0.3, "drop": 1.0, "hook": 0.85,
                  "build": 0.7, "break": 0.35}.get(str(name_).lower(), 0.6)
        sections.append({
            "id": f"{name_}{i + 1}", "role": str(name_), "startBar": bar, "bars": nbars,
            "energy": energy, "chords": chords,
        })
        bar += nbars

    slug = slugify(name or genre + "-" + time.strftime("%m%d-%H%M"))
    base = ROOT / slug
    n = 2
    while base.exists():
        base = ROOT / f"{slug}-{n}"
        n += 1
    _dirs(base)

    instruments = pack.get("instruments") or {}
    # 轨道表与增益由 compose 决定（它知道段落、律动和混音平衡），
    # 这里只记录曲风声源，避免出现一套过时的电平默认值。
    tracks = [{"id": "drums", "source": "kit:" + (pack.get("drumPatterns") or ["pop_16"])[0],
               "midi": "midi/drums.mid"},
              {"id": "bass", "source": instruments.get("bass", "bass:pick"),
               "midi": "midi/bass.mid"},
              {"id": "gtr_rhythm_l", "source": instruments.get("guitar", "guitar:dist"),
               "midi": "midi/gtr_rhythm_l.mid"},
              {"id": "gtr_rhythm_r", "source": instruments.get("guitar", "guitar:dist"),
               "midi": "midi/gtr_rhythm_r.mid"},
              {"id": "keys", "source": instruments.get("keys", "keys:electric"),
               "midi": "midi/keys.mid"},
              {"id": "pad", "source": instruments.get("pad", "strings:pad"),
               "midi": "midi/pad.mid"}]
    if "lead" in instruments:
        tracks.append({"id": "lead", "source": instruments["lead"], "midi": "midi/lead.mid"})

    project = {
        "schema": 2, "id": base.name, "title": name or pack.get("name", genre),
        "genre": genre, "modifiers": [], "tempo": bpm, "key": keyinfo,
        "meter": "4/4", "sampleRate": SAMPLE_RATE,
        "sections": sections, "tracks": tracks,
        "vocal": {"enabled": True, "language": "zh", "voice": "feiyu",
                  "style": "cute", "lyrics": "lyrics/lyrics.json",
                  "melody": "midi/vocal.mid", "alignment": "analysis/vocal-align.json",
                  "renders": {}},
        "mix": {"preset": pack.get("mixPreset", genre),
                "reference": None,
                "targets": {"lufs": pack.get("lufs", -10), "truePeak": -1.0}},
        "export": {"daw": "generic", "sampleRate": SAMPLE_RATE, "bitDepth": 24},
        "renders": {"current": None, "mix": None, "master": None},
        "seed": int(time.time()) % 100000,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "genrePack": {"name": pack.get("name"), "lufs": pack.get("lufs"),
                      "vocal": pack.get("vocal")},
    }
    save_project(base, project)
    return {"ok": True, "project": str(base), "data": project}


def list_projects():
    out = []
    if ROOT.exists():
        for p in sorted(ROOT.iterdir()):
            f = p / "project.json"
            if f.exists():
                d = jload(f, {})
                out.append({"id": d.get("id", p.name), "title": d.get("title"),
                            "genre": d.get("genre"), "tempo": d.get("tempo"),
                            "path": str(p), "updatedAt": d.get("createdAt")})
    return out
