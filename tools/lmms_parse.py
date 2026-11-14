"""解析 LMMS 工程（.mmp / .mmpz），把 42 个自带 demo 变成可统计的「制作惯例」语料。

为什么要读别人的工程：
    我们自己定的默认值（几条轨、每轨几个效果、鼓点落在哪、力度多宽）是凭感觉写的。
    真实工程的分布可以直接量：多少轨、几成轨挂了效果、底鼓落在 16 分网格哪一格、
    力度分布多宽。量完再决定插件默认值该往哪边调。

格式说明：
    .mmpz 是 Qt 的 qCompress 输出 = 4 字节大端长度 + zlib 数据，所以解压要 raw[4:]。
"""
from __future__ import annotations

import json
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET


def load_xml(path):
    raw = Path(path).read_bytes()
    if str(path).lower().endswith(".mmpz"):
        raw = zlib.decompress(raw[4:])
    return ET.fromstring(raw.decode("utf-8", "replace"))


def parse_mmp(path):
    root = load_xml(path)
    head = root.find("head")
    prof = {
        "format": "lmms",
        "file": str(path),
        "creatorVersion": root.get("creatorversion"),
        "bpm": float(head.get("bpm")) if head is not None else None,
        "timeSig": (f"{head.get('timesig_numerator')}/{head.get('timesig_denominator')}"
                    if head is not None else None),
        "masterVol": head.get("mastervol") if head is not None else None,
        "tracks": [],
        "notes": [],
        "samples": 0,
        "patterns": 0,
    }
    for tr in root.iter("track"):
        it = tr.find("instrumenttrack")
        instr = it.find("instrument") if it is not None else None
        fx = [e.get("name") or "" for e in tr.iter("effect")]
        prof["tracks"].append({
            "name": tr.get("name") or "",
            "type": tr.get("type"),
            "vol": float(it.get("vol")) if it is not None and it.get("vol") else None,
            "pan": float(it.get("pan")) if it is not None and it.get("pan") else 0.0,
            "instrument": (instr.get("name") if instr is not None else None),
            "fx": fx,
        })
    # 音符是 <note>，挂在 track 下（不是 pattern 的子元素）；pos 单位是 tick，
    # 4/4 下 192 tick = 1 小节、48 tick = 1 拍（pattern 的 len="192" 就是一小节）。
    parent = {c: p for p in root.iter() for c in p}
    for n in root.iter("note"):
        # 找最近的 track 祖先，用来把音符归到乐器/轨道
        anc, owner = parent.get(n), None
        depth = 0
        while anc is not None and depth < 12:
            if anc.tag == "track":
                owner = anc
                break
            anc = parent.get(anc)
            depth += 1
        try:
            prof["notes"].append({
                "pos": float(n.get("pos") or 0),
                "len": float(n.get("len") or 0),
                "key": int(float(n.get("key") or 0)),
                "vol": float(n.get("vol") or 100),
                "track": (owner.get("name") or "") if owner is not None else "",
            })
        except ValueError:
            pass
    for pat in root.iter("pattern"):
        prof["patterns"] += 1
    prof["samples"] = len(list(root.iter("sampleclip")))
    return prof


DRUM_KEYS = {36: "kick", 38: "snare", 42: "hat", 39: "clap", 46: "openhat", 49: "crash"}


def summarize(prof):
    lines = [f"{Path(prof['file']).name}  {prof['bpm']} BPM {prof['timeSig']}  "
             f"轨 {len(prof['tracks'])}  音符 {len(prof['notes'])}  采样片段 {prof['samples']}"]
    for t in prof["tracks"]:
        bits = [t["name"] or "(未命名)", f"type {t['type']}"]
        if t["instrument"]:
            bits.append(t["instrument"])
        if t["vol"] is not None:
            bits.append(f"vol {t['vol']:.0f}")
        if t["fx"]:
            bits.append("FX " + ",".join(x for x in t["fx"] if x)[:40])
        lines.append("  · " + " | ".join(bits))
    return "\n".join(lines)


def corpus_report(paths):
    """跨工程统计：这些数字才是「DAW 惯例」，用来校准插件默认值。"""
    track_counts, fx_per_track, fx_names, instruments = [], [], Counter(), Counter()
    vols, pans, nnotes, bpms = [], [], [], []
    grid = defaultdict(Counter)      # drum lane -> 16 分网格位置
    vel = []
    for p in paths:
        try:
            prof = parse_mmp(p)
        except Exception:
            continue
        if prof["bpm"]:
            bpms.append(prof["bpm"])
        track_counts.append(len(prof["tracks"]))
        nnotes.append(len(prof["notes"]))
        for t in prof["tracks"]:
            fx_per_track.append(len(t["fx"]))
            for f in t["fx"]:
                if f:
                    fx_names[f] += 1
            if t["instrument"]:
                instruments[t["instrument"]] += 1
            if t["vol"] is not None:
                vols.append(t["vol"])
            if t["pan"]:
                pans.append(t["pan"])
        for n in prof["notes"]:
            vel.append(n["vol"])
            lane = DRUM_KEYS.get(n["key"])
            if lane:
                # 48 tick = 1 拍 → 12 tick = 1 个 16 分格；取小节内的格号
                step = int(round(n["pos"] / 12.0)) % 16
                grid[lane][step] += 1

    def stats(xs, fmt="{:.1f}"):
        if not xs:
            return "—"
        xs = sorted(xs)
        med = xs[len(xs) // 2]
        return (fmt.format(med) + f"（{fmt.format(xs[0])}–{fmt.format(xs[-1])}）")

    out = {
        "projects": len(track_counts),
        "bpmMedian": stats(bpms),
        "tracksPerProject": stats(track_counts, "{:.0f}"),
        "notesPerProject": stats(nnotes, "{:.0f}"),
        "fxPerTrack": stats(fx_per_track, "{:.0f}"),
        "trackVolMedian": stats(vols, "{:.0f}"),
        "panMedian": stats(pans, "{:.0f}"),
        "noteVolMedian": stats(vel, "{:.0f}"),
        "topInstruments": instruments.most_common(10),
        "topEffects": fx_names.most_common(12),
        "drumGrid16": {lane: [c[k] for k in range(16)] for lane, c in grid.items()},
    }
    return out


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else r"D:\DAWs\LMMS\data\projects\demos"
    p = Path(target)
    if p.is_file():
        print(summarize(parse_mmp(p)))
    else:
        files = sorted(list(p.glob("*.mmp")) + list(p.glob("*.mmpz")))
        rep = corpus_report(files)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
