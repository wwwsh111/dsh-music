import csv
import shutil
import uuid
from pathlib import Path

from .project import load_project
from .util import ensure_dir, jdump

BEATS_PER_BAR = 4


def _sec(project):
    bpm = float(project["tempo"])
    total = sum(int(s["bars"]) for s in project["sections"])
    return total * BEATS_PER_BAR * 60.0 / bpm


def _audacity_labels(project, path):
    bpm = float(project["tempo"])
    rows = []
    for s in project["sections"]:
        start = (int(s["startBar"]) - 1) * BEATS_PER_BAR * 60.0 / bpm
        end = start + int(s["bars"]) * BEATS_PER_BAR * 60.0 / bpm
        rows.append(f"{start:.6f}\t{end:.6f}\t{s['id']}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _guid(*parts):
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "dsh-music:" + ":".join(map(str, parts)))).upper() + "}"


def _q(text):
    return '"' + str(text).replace('"', "'") + '"'


def _reaper_rpp(project, out, seconds):
    bpm = float(project["tempo"])
    lines = [
        '<REAPER_PROJECT 0.1 "7.0/linux" 1700000000',
        "  RIPPLE 0",
        "  GROUPOVERRIDE 0 0 0",
        "  AUTOXFADE 1",
        "  ENVATTACH 1",
        "  POOLEDENVATTACH 0",
        "  MIXERUIFLAGS 11 48",
        f"  TEMPO {bpm:g} 4 4",
        "  PLAYRATE 1 1 0 -1 0 0.0025",
        "  <MASTERFADER",
        "    VOLPAN 1 0 1 -1",
        "  >",
    ]
    used = 0
    for i, t in enumerate(project.get("tracks") or []):
        tid = t.get("id")
        wav = out / "stems" / f"{tid}.wav"
        if not wav.exists():
            continue
        used += 1
        gain = float(t.get("gainDb", -6))
        lin = 10 ** (gain / 20.0)
        pan = float(t.get("pan") or 0)
        lines += [
            f"  <TRACK {_guid(project['id'], tid, 'track')}",
            f"    NAME {_q(tid)}",
            "    PEAKCOL 16576",
            "    BEAT -1",
            "    VOLPAN %.4f %.3f -1 -1 1" % (lin, pan),
            "    MUTESOLO 0 0 0",
            "    IPHASE 0",
            "    PLAYOFFS 0 1",
            "    ISBUS 0 0",
            "    BUSCOMP 0 0 0 0 0",
            "    SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0",
            "    FREEMODE 0",
            "    SEL 0",
            "    REC 0 0 1 0 0 0 0 0",
            "    VU 2",
            "    TRACKHEIGHT 0 0 0 0 0 0",
            "    INQ 0 0 0 0.5 100 0 0 100",
            "    NCHAN 2",
            "    FX 1",
            f"    TRACKID {_guid(project['id'], tid, 'trackid')}",
            "    PERF 0",
            "    MIDIOUT -1",
            "    MAINSEND 1 0",
            "    <ITEM",
            "      POSITION 0",
            "      SNAPOFFS 0",
            f"      LENGTH {seconds:.6f}",
            "      LOOP 1",
            "      ALLTAKES 0",
            "      FADEIN 1 0 0 1 0 0 0",
            "      FADEOUT 1 0 0 1 0 0 0",
            "      MUTE 0 0",
            "      SEL 0",
            f"      IGUID {_guid(project['id'], tid, 'item')}",
            "      IID 1",
            f"      NAME {_q(tid)}",
            "      VOLPAN 1 0 1 -1",
            "      SOFFS 0",
            "      PLAYRATE 1 1 0 -1 0 0.0025",
            "      CHANMODE 0",
            f"      GUID {_guid(project['id'], tid, 'itemguid')}",
            "      <SOURCE WAVE",
            f"        FILE {_q('stems/' + tid + '.wav')}",
            "      >",
            "    >",
            "  >",
        ]
    lines.append(">")
    (out / "session" / "session.rpp").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return used


def export(project_path, daw="generic", sample_rate=48000, bit_depth=24):
    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    out = ensure_dir(base / "export" / "v1")
    for sub in ("stems", "midi", "session", "lyrics", "preview"):
        ensure_dir(out / sub)

    renders = base / "audio" / (project.get("renders", {}).get("current") or "render-v1")
    for wav in sorted(renders.glob("*.wav")):
        shutil.copy2(wav, out / "stems" / wav.name)
    vocal_dir = base / "audio" / "vocal"
    if vocal_dir.exists():
        vox = sorted(vocal_dir.glob("vocal-*.wav"))
        if vox:
            shutil.copy2(vox[-1], out / "stems" / "vocal.wav")
    for mid in sorted((base / "midi").glob("*.mid")):
        shutil.copy2(mid, out / "midi" / mid.name)

    bpm = project["tempo"]
    seconds = _sec(project)
    with (out / "session" / "markers.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "startBar", "bars", "startSec"])
        for s in project["sections"]:
            sec = (s["startBar"] - 1) * BEATS_PER_BAR * 60.0 / bpm
            w.writerow([s["id"], s["startBar"], s["bars"], round(sec, 3)])
    _audacity_labels(project, out / "session" / "audacity_labels.txt")

    lyrics = base / "lyrics" / "lyrics.txt"
    if lyrics.exists():
        shutil.copy2(lyrics, out / "lyrics" / "lyrics.txt")
        lines = [l.strip() for l in lyrics.read_text(encoding="utf-8").splitlines() if l.strip()]
        with (out / "lyrics" / "lyrics.lrc").open("w", encoding="utf-8") as f:
            t = 0.0
            for line in lines:
                m, s = divmod(t, 60)
                f.write(f"[{int(m):02d}:{s:05.2f}]{line}\n")
                t += 4 * 60.0 / bpm

    notes = f"""# MIX NOTES — {project.get('title')}

- Genre: {project.get('genre')}  BPM: {project.get('tempo')}  Key: {project['key']['tonic']} {project['key']['mode']}
- Target LUFS: {project.get('mix', {}).get('targets', {}).get('lufs')}  TP: -1
- Stems: 48 kHz / 24-bit，等长，峰值留 -6 dBFS

## 每轨建议
"""
    for t in project.get("tracks", []):
        notes += f"- {t['id']}: gain {t.get('gainDb')} dB, pan {t.get('pan')}\n"
    notes += "\n人声：HPF 80 Hz、压缩 3:1、3 kHz +1.5 dB、板混响 12%。\n"
    (out / "MIX_NOTES.md").write_text(notes, encoding="utf-8")

    tracks_used = 0
    if daw == "reaper":
        tracks_used = _reaper_rpp(project, out, seconds)

    importmd = """# 导入说明

## Audacity
1. 新建工程，采样率 48000 Hz。
2. 拖入 `stems/*.wav`（每轨一条）。
3. 文件 → 导入 → 标签：选择 `session/audacity_labels.txt` 生成段落标记。
4. 混完导出 48 kHz/24-bit WAV，交给 music_master。

## Reaper
1. 直接打开 `session/session.rpp`（BPM、轨道名、增益/声像已写好）。
2. 需要 MIDI 时再拖入 `midi/*.mid`。
3. 按 `MIX_NOTES.md` 加效果链。

## Ardour / LMMS / 其他 DAW
导入 `stems/`（WAV）与 `midi/`，按 `session/markers.csv` 建段落标记。
"""
    (out / "IMPORT.md").write_text(importmd, encoding="utf-8")
    jdump(out / "project.json", project)
    return {"ok": True, "export": str(out), "daw": daw,
            "reaperTracks": tracks_used, "seconds": round(seconds, 3),
            "files": [str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()]}
