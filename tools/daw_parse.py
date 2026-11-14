"""DAW 工程文件解析器：把 .rpp / .mmp / .ardour / .dawproject 读成统一的「制作特征表」。

为什么要有这一层：
    插件现在的编曲/混音参数（段落长度、发送量、人声处理链、总线结构）都是拍脑袋
    定的。真正的参考是专业工程怎么摆——轨道怎么分组、发送推多少、自动化画在哪、
    人声 clip 相对网格偏了多少毫秒。这个模块把这些事实从工程文件里挖出来。

解析原则：
    * 只读文件，不调用 DAW，不依赖它们安装（格式都是文本/XML/zip）。
    * 统一模型：tracks / items / sends / automation / tempo / markers / loudness 目标。
    * 拿不准的字段宁可不填，也不要猜——报告里区分「解析到」和「没有」。
"""
from __future__ import annotations

import gzip
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


# ------------------------------------------------------------------ 通用模型

def empty_profile():
    return {
        "format": None,
        "file": None,
        "tempo": None,            # {"bpm": float, "timeSig": "4/4"}
        "tempoMap": [],           # [{"beat": float, "bpm": float}]
        "markers": [],            # [{"pos": float, "name": str}]
        "tracks": [],             # 见下方 track 结构
        "buses": [],              # 总线/分组名字
        "master": {},             # 主推子/总线效果
        "notes": [],              # 解析过程中的告警
    }


def new_track(name="", kind="audio"):
    return {
        "name": name,
        "kind": kind,
        "volumeDb": None,
        "pan": None,
        "mute": None,
        "solo": None,
        "sends": [],              # [{"to": str, "db": float, "preFader": bool}]
        "items": [],              # [{"pos": float, "length": float, "offset": float, "file": str}]
        "fx": [],                 # [{"name": str, "params": {}}]
        "automation": [],         # [{"param": str, "points": [[time, value], ...]}]
        "children": [],           # 分组子轨名字
        "rawFields": {},
    }


# ------------------------------------------------------------------ REAPER .rpp

_RPP_TOKEN = re.compile(r'"[^"]*"|\S+')


def _rpp_split(line):
    out = []
    for tok in _RPP_TOKEN.findall(line):
        if tok.startswith('"') and tok.endswith('"'):
            out.append(tok[1:-1])
        else:
            out.append(tok)
    return out


def _rpp_value(node, key, default=None):
    v = node.get("fields", {}).get(key.split()[0])
    return v if v is not None else default


def parse_rpp(path):
    """REAPER 工程：纯文本 INI 风格，用栈解析 <BLOCK ... > 结构。"""
    prof = empty_profile()
    prof["format"] = "reaper"
    prof["file"] = str(path)
    root = {"tag": "ROOT", "attrs": [], "fields": {}, "children": []}
    stack = [root]
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("//"):
            continue
        if line.startswith("<"):
            parts = _rpp_split(line)
            tag = parts[0][1:]
            node = {"tag": tag, "attrs": parts[1:], "fields": {}, "children": []}
            stack[-1]["children"].append(node)
            if not raw.rstrip().endswith(">") or raw.rstrip() == line.lstrip():
                stack.append(node)
            continue
        if line == ">":
            if len(stack) > 1:
                stack.pop()
            continue
        parts = _rpp_split(line)
        key = parts[0]
        node = stack[-1]
        if key in node["fields"]:
            node["fields"][key] = node["fields"][key] + " " + " ".join(parts[1:])
        else:
            node["fields"][key] = parts[1:]

    proj = root["children"][0] if root["children"] else root
    fields = proj["fields"]

    if "TEMPO" in fields:
        t = fields["TEMPO"]
        try:
            bpm = float(t[0]); ts = f"{int(float(t[1]))}/{int(float(t[2]))}"
            prof["tempo"] = {"bpm": bpm, "timeSig": ts}
        except Exception:
            prof["notes"].append("TEMPO 字段无法解析")

    # 标记：MARKER index pos "name" 1
    for i, vals in enumerate([]):
        pass
    markers = {}
    for child in proj["children"]:
        pass
    # RPP 的 MARKER 是平铺在 project 下的多行，字段字典会把同名键拼在一起
    # 这里重新扫一遍原始文本，稳妥取标记和速度点。
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r"^\s*MARKER\s+(\d+)\s+([0-9.]+)\s+\"?([^\"]*)\"?", text, re.M):
        markers[int(m.group(1))] = {"pos": float(m.group(2)), "name": m.group(3).strip()}
    prof["markers"] = [markers[k] for k in sorted(markers) if markers[k]["name"]]
    for m in re.finditer(r"^\s*TEMPO\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)", text, re.M):
        prof["tempoMap"].append({"bpm": float(m.group(1)),
                                 "timeSig": f"{int(float(m.group(2)))}/{int(float(m.group(3)))}"})

    def walk(node, depth=0):
        for ch in node["children"]:
            if ch["tag"].startswith("TRACK"):
                walk_track(ch, depth)
            else:
                walk(ch, depth)

    def walk_track(node, depth=0):
        tr = new_track()
        f = node["fields"]
        if "NAME" in f:
            tr["name"] = " ".join(f["NAME"]).strip()
        if "VOLPAN" in f:
            try:
                vol, pan = float(f["VOLPAN"][0]), float(f["VOLPAN"][1])
                tr["volumeDb"] = round(20 * __import__("math").log10(max(vol, 1e-6)), 2) if vol > 0 else None
                tr["pan"] = round(pan, 3)
            except Exception:
                pass
        if "MUTESOLO" in f:
            try:
                tr["mute"] = bool(int(f["MUTESOLO"][0]))
                tr["solo"] = bool(int(f["MUTESOLO"][1]))
            except Exception:
                pass
        for ch in node["children"]:
            tag = ch["tag"]
            if tag.startswith("ITEM"):
                it = {"pos": 0.0, "length": 0.0, "offset": 0.0, "file": None}
                for c2 in ch["children"]:
                    if c2["tag"].startswith("SOURCE"):
                        for c3 in c2["children"]:
                            if "FILE" in c3["fields"]:
                                it["file"] = " ".join(c3["fields"]["FILE"]).strip()
                for k, key in (("POSITION", "pos"), ("LENGTH", "length"),
                               ("SOFFS", "offset")):
                    if k in ch["fields"]:
                        try:
                            it[key] = float(ch["fields"][k][0])
                        except Exception:
                            pass
                tr["items"].append(it)
            elif tag.startswith("FXCHAIN"):
                for c2 in ch["children"]:
                    if c2["tag"].startswith("VST") or c2["tag"].startswith("JS"):
                        nm = " ".join(c2["attrs"][:1]) if c2["attrs"] else c2["tag"]
                        tr["fx"].append({"name": str(nm)[:120]})
            elif tag.startswith("AUXRECV"):
                tr["sends"].append({"raw": ch["fields"].get("AUXRECV", [])})
            elif tag.startswith("PARMENV"):
                env = {"param": " ".join(ch["fields"].get("PARMENV", [])), "points": []}
                tr["automation"].append(env)
            elif tag.startswith("TRACK"):
                tr["children"].append("child")
                walk_track(ch, depth + 1)
        prof["tracks"].append(tr)

    walk(proj)
    prof["notes"].append(f"解析到 {len(prof['tracks'])} 条轨道")
    return prof


def summarize(prof):
    """把 profile 压成一段人能读的中文摘要（给报告用）。"""
    lines = [f"格式：{prof['format']}    文件：{Path(prof['file']).name if prof['file'] else '?'}"]
    if prof.get("tempo"):
        lines.append(f"速度：{prof['tempo']['bpm']} BPM {prof['tempo']['timeSig']}")
    if prof.get("tempoMap") and len(prof["tempoMap"]) > 1:
        lines.append(f"速度变化点：{len(prof['tempoMap'])} 个")
    if prof.get("markers"):
        lines.append(f"标记：{len(prof['markers'])} 个 → " +
                     " / ".join(m["name"] for m in prof["markers"][:12]))
    audio = [t for t in prof["tracks"] if t["items"]]
    lines.append(f"轨道：{len(prof['tracks'])} 条（有音频片段 {len(audio)} 条）")
    for t in prof["tracks"]:
        bits = [t["name"] or "(未命名)"]
        if t["volumeDb"] is not None:
            bits.append(f"{t['volumeDb']:+.1f} dB")
        if t["pan"]:
            bits.append(f"pan {t['pan']:+.2f}")
        if t["items"]:
            bits.append(f"{len(t['items'])} clip")
        if t["fx"]:
            bits.append("FX: " + ", ".join(x["name"][:18] for x in t["fx"][:3]))
        if t["sends"]:
            bits.append(f"{len(t['sends'])} send")
        lines.append("  · " + " | ".join(bits))
    return "\n".join(lines)


# ------------------------------------------------------------------ 入口

def parse(path):
    p = Path(path)
    suf = p.suffix.lower()
    if suf == ".rpp":
        return parse_rpp(p)
    raise ValueError(f"暂不支持的工程格式: {suf}（目前支持 .rpp；.mmp/.ardour/.dawproject 在下一版）")


if __name__ == "__main__":
    import sys
    prof = parse(sys.argv[1])
    print(summarize(prof))
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
