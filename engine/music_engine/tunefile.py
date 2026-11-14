"""与「洛天依」的工程文件互通：Synthesizer V 的 .svp、VOCALOID 4 的 .vsqx。

洛天依不是开源声库，这个插件不能替她发声。能做的是把两头接上：

- 导出：把工程里的主旋律与歌词写成 .svp / .vsqx，在 SynthV / VOCALOID 里
  选洛天依开唱；
- 导入：把 .svp / .vsqx 里的音符与歌词读回工程，之后 tune（调教）、
  yutiao（语调教）、import_vocal（导入干声）、mix / master / design 都能接着用。

文件格式本身是公开事实，这里的读写是按格式写的独立实现；没有搬用
VvTalk（CC BY-NC-SA 4.0）的代码或词表，只在文档里注明思路来源。

单位：
- .svp 的时间叫 blick，1 拍 = 705600000；
- .vsqx 用 480 dots/拍，速度记在 <tempo> 里（120 BPM 写作 12000）。
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .project import load_project
from .util import ensure_dir, jdump

BLICK_PER_BEAT = 705600000
VSQX_DOTS_PER_BEAT = 480


# ---------------------------------------------------------------- 谱面来源

def _notes_from_project(base):
    """取主旋律与歌词：优先 analysis/vocal-align.json，其次 midi/vocal.mid。"""
    from .tune import load_notes

    notes, src = load_notes(base)
    out = []
    for n in notes:
        out.append({
            "start": round(float(n["start"]), 4),
            "dur": round(float(n["dur"]), 4),
            "midi": int(round(float(n["midi"]))),
            "char": (n.get("char") or "啦").strip() or "啦",
            "line": n.get("line"),
        })
    return out, src


def _bpm_of(base):
    project = load_project(base)
    return float(project.get("tempo") or project.get("bpm") or 120.0)


# ---------------------------------------------------------------- 导出

def _pitch_delta_points(base, bpm, origin_sec):
    """把 tune 报告里的音高曲线换成 (秒, 音分) 关键点；没有报告就返回空。"""
    report = base / "report" / "tune-lead.json"
    if not report.exists():
        report = base / "report" / "tune.json"
    if not report.exists():
        return []
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    curve = (data.get("pitch") or {}).get("target") or data.get("targetCents")
    if not curve:
        return []
    pts = []
    step = 1.0 / 50.0 if bpm >= 90 else 1.0 / 40.0
    t0 = float(data.get("start") or origin_sec)
    for i, cents in enumerate(curve):
        pts.append((t0 + i * step, float(cents)))
    return pts


def export_svp(project_path, out=None):
    """写 Synthesizer V 工程（.svp）。音高用真实音高，不搬别人的常量基频写法。"""
    base = Path(project_path)
    notes, src = _notes_from_project(base)
    bpm = _bpm_of(base)
    total_sec = max((n["start"] + n["dur"] for n in notes), default=1.0)

    blick_per_sec = BLICK_PER_BEAT * bpm / 60.0
    def blick(sec):
        return int(round(sec * blick_per_sec))

    svp_notes = []
    for n in notes:
        onset = blick(n["start"])
        dur = max(blick(n["dur"]), 10)
        svp_notes.append({
            "onset": onset,
            "duration": dur,
            "lyrics": n["char"],
            "phonemes": "",
            "pitch": n["midi"],
            "detune": 0,
            "attributes": {},
        })

    pts = _pitch_delta_points(base, bpm, notes[0]["start"]) if notes else []
    pitch_points = [[blick(t), round(c, 1)] for t, c in pts if t <= total_sec + 1.0]

    track_uuid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    doc = {
        "library": [],
        "renderConfig": {
            "destination": "./",
            "filename": base.name,
            "numChannels": 1,
            "aspirationFormat": "noAspiration",
            "bitDepth": 16,
            "sampleRate": 44100,
            "exportMixDown": True,
        },
        "time": {
            "meter": [{"index": 0, "numerator": 4, "denominator": 4}],
            "tempo": [{"position": 0, "bpm": bpm}],
        },
        "version": 120,
        "tracks": [{
            "name": "主唱",
            "groups": [],
            "dispColor": "ff7db235",
            "dispOrder": 0,
            "renderEnabled": False,
            "mixer": {"gainDecibel": 0.0, "pan": 0.0, "mute": False,
                      "solo": False, "display": True},
            "mainGroup": {
                "name": "main",
                "uuid": track_uuid,
                "parameters": {
                    "pitchDelta": {"mode": "linear", "points": pitch_points},
                    "vibratoEnv": {"mode": "cubic", "points": []},
                    "loudness": {"mode": "cubic", "points": []},
                    "tension": {"mode": "cubic", "points": []},
                    "breathiness": {"mode": "cubic", "points": []},
                    "voicing": {"mode": "cubic", "points": []},
                    "gender": {"mode": "cubic", "points": []},
                    "toneShift": {"mode": "cubic", "points": []},
                },
                "notes": svp_notes,
            },
            "mainRef": {
                "groupID": track_uuid,
                "blickOffset": 0,
                "pitchOffset": 0,
                "isInstrumental": False,
                "database": {
                    "name": "",
                    "language": "mandarin",
                    "phoneset": "xsampa",
                    "languageOverride": "",
                    "phonesetOverride": "",
                    "backendType": "SVR2Standard",
                },
                "dictionary": "",
                "voice": {},
            },
        }],
    }

    if out is None:
        ensure_dir(base / "export")
        out = base / "export" / f"{base.name}.svp"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "ok": True,
        "format": "svp",
        "file": str(out),
        "notes": len(svp_notes),
        "bpm": bpm,
        "pitchPoints": len(pitch_points),
        "notesFrom": src,
        "next": "在 Synthesizer V 里打开，选洛天依 AI 声库；唱完导出干声，"
                "再用 music_vocals mode=import 导回工程做语调教/调教/混音。",
    }


def _xml_text(value):
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_vsqx(project_path, out=None, singer="Luotianyi_CHN_Meng"):
    """写 VOCALOID 4 工程（.vsqx）。发音符号留空，VOCALOID 会按歌词自动转换。"""
    base = Path(project_path)
    notes, src = _notes_from_project(base)
    bpm = _bpm_of(base)
    dots_per_sec = VSQX_DOTS_PER_BEAT * bpm / 60.0

    lines = []
    for n in notes:
        t = int(round(n["start"] * dots_per_sec))
        dur = max(int(round(n["dur"] * dots_per_sec)), 10)
        lines.append(
            "\t\t\t<note>\n"
            f"\t\t\t\t<t>{t}</t>\n"
            f"\t\t\t\t<dur>{dur}</dur>\n"
            f"\t\t\t\t<n>{n['midi']}</n>\n"
            "\t\t\t\t<v>64</v>\n"
            f"\t\t\t\t<y><![CDATA[{_xml_text(n['char'])}]]></y>\n"
            "\t\t\t\t<p><![CDATA[]]></p>\n"
            "\t\t\t\t<nStyle><v id=\"accent\">50</v></nStyle>\n"
            "\t\t\t</note>\n")

    head = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<vsq4 xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/"\n'
        '      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "\t<vender><![CDATA[Yamaha corporation]]></vender>\n"
        "\t<version><![CDATA[4.0.0.3]]></version>\n"
        "\t<vVoiceTable>\n\t\t<vVoice>\n\t\t\t<bs>4</bs>\n\t\t\t<pc>0</pc>\n"
        f"\t\t\t<name><![CDATA[{singer}]]></name>\n"
        "\t\t\t<vPrm><bre>0</bre><bri>0</bri><cle>0</cle><gen>0</gen><ope>0</ope></vPrm>\n"
        "\t\t</vVoice>\n\t</vVoiceTable>\n"
        "\t<mixer><masterUnit><oDev>0</oDev><rLvl>0</rLvl><vol>0</vol></masterUnit>"
        "<vsUnit><tNo>0</tNo><iGin>0</iGin><sLvl>-898</sLvl><sEnable>0</sEnable>"
        "<m>0</m><s>0</s></vsUnit></mixer>\n"
        "\t<vsTrack>\n\t\t<tNo>0</tNo>\n\t\t<name><![CDATA[主唱]]></name>\n"
        "\t\t<comment><![CDATA[]]></comment>\n\t\t<vsPart>\n"
        "\t\t\t<t>0</t>\n"
        f"\t\t\t<playTime>{int(round(max((n['start'] + n['dur'] for n in notes), default=2.0) * dots_per_sec))}</playTime>\n"
        "\t\t\t<name><![CDATA[主唱]]></name>\n"
        "\t\t\t<comment><![CDATA[]]></comment>\n"
        "\t\t\t<singer><t>0</t><bs>4</bs><pc>0</pc></singer>\n"
        "\t\t\t<cc><t>0</t><v id=\"S\">17</v></cc>\n")
    tail = (
        "\t\t</vsPart>\n\t</vsTrack>\n"
        "\t<monoTrack>\n\t</monoTrack>\n"
        "\t<stTrack>\n\t</stTrack>\n"
        "\t<aux><id><![CDATA[AUX_VST_HOST_CHUNK_INFO]]></id>"
        "<content><![CDATA[VlNDSwAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=]]></content></aux>\n"
        "</vsq4>\n")

    if out is None:
        ensure_dir(base / "export")
        out = base / "export" / f"{base.name}.vsqx"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(head + "".join(lines) + tail, encoding="utf-8")
    return {
        "ok": True,
        "format": "vsqx",
        "file": str(out),
        "notes": len(notes),
        "bpm": bpm,
        "notesFrom": src,
        "next": "在 VOCALOID 4 里打开，选洛天依，全选后按一次「歌词/发音 转换」；"
                "唱完导出 wav，再用 music_vocals mode=import 导回。",
    }


def export_tune(project_path, fmt="svp", out=None):
    fmt = (fmt or "svp").lower().lstrip(".")
    if fmt == "svp":
        return export_svp(project_path, out)
    if fmt == "vsqx":
        return export_vsqx(project_path, out)
    raise ValueError("只支持 svp / vsqx")


# ---------------------------------------------------------------- 导入

def _strip_ns(tag):
    return tag.split("}")[-1]


def _sec_from_dots(dots, bpm):
    return float(dots) * 60.0 / (bpm * VSQX_DOTS_PER_BEAT)


def parse_vsqx(path):
    """读 vsqx：返回 (notes, meta)。音高 n、时长 dur、歌词 y，全部换成秒。"""
    tree = ET.parse(str(path))
    root = tree.getroot()
    bpm = 120.0
    for el in root.iter():
        if _strip_ns(el.tag) == "tempo":
            for v in el:
                if _strip_ns(v.tag) == "v" and v.text:
                    try:
                        bpm = float(v.text) / 100.0
                    except ValueError:
                        pass
    notes = []
    for el in root.iter():
        if _strip_ns(el.tag) != "note":
            continue
        item = {}
        for child in el:
            tag = _strip_ns(child.tag)
            text = (child.text or "").strip()
            if tag in ("t", "dur", "n", "y"):
                item[tag] = text
        if not all(k in item for k in ("t", "dur", "n")):
            continue
        lyric = item.get("y") or "啦"
        notes.append({
            "start": round(_sec_from_dots(item["t"], bpm), 4),
            "dur": round(max(_sec_from_dots(item["dur"], bpm), 0.05), 4),
            "midi": int(float(item["n"])),
            "char": lyric[:1],
        })
    notes.sort(key=lambda n: n["start"])
    return notes, {"format": "vsqx", "tempo": bpm}


def parse_svp(path):
    """读 svp：blicks 换秒，取音符最多的那条轨。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tempo = 120.0
    try:
        tempo = float(data["time"]["tempo"][0]["bpm"])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    per_sec = BLICK_PER_BEAT * tempo / 60.0

    best = []
    for track in (data.get("tracks") or []):
        group = (track.get("mainGroup") or {})
        items = group.get("notes") or []
        if len(items) > len(best):
            best = items
    notes = []
    for it in best:
        onset = it.get("onset")
        dur = it.get("duration")
        pitch = it.get("pitch")
        if onset is None or dur is None or pitch is None:
            continue
        lyric = (it.get("lyrics") or "啦").strip() or "啦"
        notes.append({
            "start": round(float(onset) / per_sec, 4),
            "dur": round(max(float(dur) / per_sec, 0.05), 4),
            "midi": int(round(float(pitch))),
            "char": lyric[:1],
        })
    notes.sort(key=lambda n: n["start"])
    return notes, {"format": "svp", "tempo": tempo}


def parse_tune(path):
    p = Path(path)
    if not p.exists():
        raise ValueError(f"文件不存在: {p}")
    ext = p.suffix.lower()
    if ext == ".svp":
        return parse_svp(p)
    if ext == ".vsqx":
        return parse_vsqx(p)
    raise ValueError("只认 .svp / .vsqx")


def import_tune(project_path, source, part="lead", overwrite_lyrics=True):
    """把 .svp/.vsqx 的音符与歌词写回工程，供 tune / yutiao / import_vocal 使用。"""
    from . import midilib

    base = Path(project_path)
    notes, meta = parse_tune(source)
    if not notes:
        raise ValueError("这个工程文件里没有音符")

    base_notes = [{"start": n["start"], "end": n["start"] + n["dur"],
                   "pitch": n["midi"], "velocity": 96} for n in notes]
    ensure_dir(base / "midi")
    midi_path = base / "midi" / ("vocal-harmony.mid" if part == "harmony" else "vocal.mid")
    midilib.write(midi_path, meta.get("tempo", 120.0), base_notes, name="vocal")

    ensure_dir(base / "lyrics")
    lyric_path = base / "lyrics" / ("pinyin-harmony.txt" if part == "harmony" else "lyrics.txt")
    if overwrite_lyrics:
        text = "".join(n["char"] for n in notes)
        lyric_path.write_text(text + "\n", encoding="utf-8")

    # 句号信息文件里没有，用大于一口气的间隔（0.34s）重建分行，
    # 这样 tune/yutiao 的 _phrases 还能按行分句
    line = 0
    prev_end = None
    for n in notes:
        if prev_end is not None and n["start"] - prev_end > 0.34:
            line += 1
        n["line"] = line
        prev_end = n["start"] + n["dur"]

    align = {
        "source": "tunefile",
        "format": meta.get("format"),
        "tempo": meta.get("tempo"),
        "notes": [
            {"start": n["start"], "dur": n["dur"], "midi": n["midi"],
             "char": n["char"], "line": n["line"]}
            for n in notes
        ],
    }
    ensure_dir(base / "analysis")
    jdump(base / "analysis" / ("vocal-align-harmony.json" if part == "harmony"
                               else "vocal-align.json"), align)

    return {
        "ok": True,
        "format": meta.get("format"),
        "notes": len(notes),
        "tempo": meta.get("tempo"),
        "midi": str(midi_path),
        "lyrics": str(lyric_path),
        "notesPreview": [f"{n['char']} {n['midi']} {n['start']:.2f}s" for n in notes[:8]],
        "next": "现在可以先 music_yutiao dryRun 看声调标注，或直接 music_vocals mode=sing。",
    }
