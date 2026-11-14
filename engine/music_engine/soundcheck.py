"""把 dsh-soundcheck（本地音频分析插件）接到 dsh-music。

为什么单独接一个引擎：music 自己的分析只有 BPM/调性/粗段落，
但混音验收真正要看的是 LUFS、真峰值、结构变化点、人声在不在、
分轨能量占比、钩子时间。这些 dsh-soundcheck 已经算得很细，而且
全程在本机跑、不上传音频。

调用方式：找到它的 analyze.py，用 `--json` 起一个子进程，读回结构化结果。
找不到引擎不报错——深分析是可选能力，调用方自己决定降级。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import CONFIG

# dsh-soundcheck 的 JSON 结构会随版本变，这里只取稳定字段；
# 缺字段时返回 None，不猜。
_ENGINE_NAME = "dsh-soundcheck"


def _plugin_dirs():
    """候选引擎目录：环境变量 > 配置 > 与 dsh-music 同级的 node_modules > 各 profile。"""
    out = []
    for env in (os.environ.get("MUSIC_SOUNDCHECK_ENGINE"),
                CONFIG.get("soundcheckEngine")):
        if env:
            p = Path(env).expanduser()
            out.append(p if p.name != "analyze.py" else p.parent)
    # 插件自己所在位置：<node_modules>/dsh-music/engine/music_engine/soundcheck.py
    here = Path(__file__).absolute()
    for anchor in (here, here.resolve()):
        try:
            out.append(anchor.parents[3] / _ENGINE_NAME / "engine")
        except IndexError:
            pass
    profiles = Path.home() / ".dsh" / "profiles"
    if profiles.is_dir():
        for prof in profiles.iterdir():
            cand = prof / "node_modules" / _ENGINE_NAME / "engine"
            if cand.is_dir():
                out.append(cand)
    # 去重、保序
    seen, uniq = set(), []
    for p in out:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def find_engine():
    """返回 {dir, script, python} 或 None。不跑 analyze.py，避免每个请求都等十几秒。"""
    for d in _plugin_dirs():
        script = d / "analyze.py"
        if not script.exists():
            continue
        py = None
        for cand in (d / "venv" / "Scripts" / "python.exe",
                     d / "venv" / "bin" / "python",
                     d / ".venv" / "Scripts" / "python.exe",
                     d / ".venv" / "bin" / "python"):
            if cand.exists():
                py = cand
                break
        if py is None:
            py = shutil.which("python") or sys.executable
        if py:
            return {"dir": str(d), "script": str(script), "python": str(py)}
    return None


def _has_model(d, name):
    """模型可能在 engine 根目录，也可能在 models/stems/… 的 HuggingFace 缓存里。"""
    if (d / name).exists():
        return True
    return bool(list(d.glob("**/" + name)))


def status():
    found = find_engine()
    if not found:
        return {"available": False, "plugin": _ENGINE_NAME,
                "hint": "装 dsh-soundcheck 插件（或设置 MUSIC_SOUNDCHECK_ENGINE 指向它的 engine 目录）"}
    d = Path(found["dir"])
    models = {n: _has_model(d, n) for n in
              ("yamnet.onnx", "musicnn.onnx", "frontend.json",
               "beat-this-final0.onnx", "htdemucs_fp16weights.onnx",
               "htdemucs_6s_fp16weights.onnx")}
    return {"available": True, "plugin": _ENGINE_NAME, "engine": str(d),
            "python": found["python"], "models": models,
            "stems": models.get("htdemucs_fp16weights.onnx", False),
            "stems6": models.get("htdemucs_6s_fp16weights.onnx", False),
            "asr": (d / "venv").exists() and any((d / "venv").rglob("whisper*"))}


def analyze(audio, start=None, end=None, stems=None, asr=None, timeout=1800):
    """跑一次 dsh-soundcheck 的 JSON 分析。

    start/end 是秒（也接受 "1:30" 这种字符串，交给它自己解析）；
    stems 用 "4s"/"6s"；asr 用 "tiny"/"base"/"small"。
    """
    found = find_engine()
    if not found:
        raise RuntimeError("未找到 dsh-soundcheck 引擎；先装插件或设置 MUSIC_SOUNDCHECK_ENGINE")
    path = Path(audio)
    if not path.exists():
        raise ValueError(f"音频不存在: {path}")
    cmd = [found["python"], found["script"], str(path), "--json"]
    if start not in (None, ""):
        cmd += ["--start", str(start)]
    if end not in (None, ""):
        cmd += ["--end", str(end)]
    if stems and stems != "off":
        cmd += ["--stems", str(stems)]
    if asr and asr != "off":
        cmd += ["--asr", str(asr)]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"音频分析超时（>{timeout}s）：{path.name}")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
        raise RuntimeError(f"dsh-soundcheck 分析失败（exit {proc.returncode}）：{tail}")
    # stdout 可能混着日志，截取最外层的 JSON 对象
    i, j = out.find("{"), out.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("dsh-soundcheck 没有返回 JSON：" + out[:400])
    try:
        return json.loads(out[i:j + 1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"dsh-soundcheck 返回的 JSON 解析失败: {e}")


def condense(raw, section=False):
    """把 soundcheck 的大 JSON 压成混音验收关心的那几项。

    只搬运已有字段，不做推断；拿不到的就不写（沉默 = 测不到）。
    """
    s = (raw or {}).get("summary") or {}
    loud = s.get("loudness") or {}
    key = s.get("key") or {}
    tl = (raw or {}).get("timeline") or {}
    src = (raw or {}).get("sources") or {}
    act = (raw or {}).get("activity") or {}
    out = {
        "engine": _ENGINE_NAME,
        "duration": s.get("duration_sec"),
        "bpm": {"primary": s.get("bpm"), "secondary": (s.get("bpm_layers") or {}).get("secondary"),
                "maybeDouble": bool(s.get("bpm_maybe_double")),
                "stable": s.get("tempo_stable")},
        "key": {"best": key.get("best"), "confidence": key.get("confidence"),
                "candidates": [c.get("name") for c in (key.get("candidates") or [])[:3]]},
        "loudness": {"lufs": loud.get("lufs"), "truePeakDb": loud.get("peak_dbfs"),
                     "rmsDb": loud.get("rms_dbfs"), "clipped": loud.get("clipped"),
                     "overScaleSamples": loud.get("over_scale_samples")},
        "energy": s.get("energy_range_dbfs"),
        "structureBoundaries": [{"sec": b.get("time_sec"), "jumpDb": b.get("jump_db")}
                                for b in (tl.get("section_boundaries") or [])],
        "sections": [{"start": x.get("start_sec"), "end": x.get("end_sec")}
                     for x in ((tl.get("structure") or {}).get("sections") or [])][:12],
        "chords": {"roots": [v.get("root") for v in ((raw or {}).get("chords") or {}).get("vocabulary", [])][:8],
                   "unresolvedRatio": ((raw or {}).get("chords") or {}).get("unresolved_ratio")},
        "instruments": [(i.get("name"), i.get("seconds")) for i in (src.get("instruments") or [])][:6],
        "vocal": src.get("vocal"),
        "activity": act.get("summary"),
        "section": bool(section),
    }
    # 分轨结果在 raw["stems"]（不是 sources）；比例是相对整段混音的平均能量
    st = (raw or {}).get("stems") or {}
    if st.get("stems"):
        out["stems"] = {
            "model": st.get("model"),
            "mixRmsDb": st.get("mix_rms_db"),
            "ratios": {k: v.get("ratio") for k, v in st["stems"].items()},
            "vocals": st.get("vocals"),
        }
    if out["vocal"]:
        v = out["vocal"]
        out["vocal"] = {"verdict": v.get("verdict"), "peak": v.get("peak"),
                        "segments": v.get("segments") or []}
    # 分轨过就以分轨为准：整混音上的人声分类会把有人声的混音报成「无」
    sv = (out.get("stems") or {}).get("vocals") or {}
    if sv.get("present") is not None:
        out["vocal"] = {"verdict": sv.get("label"), "present": sv["present"],
                        "ratio": sv.get("ratio"), "source": "stems"}
    # 去掉空值，别让工具输出充满 null
    return {k: v for k, v in out.items() if v not in (None, {}, [], "")}
