import subprocess
from pathlib import Path

from .project import load_project
from .util import ensure_dir


def preview(project_path=None, audio=None):
    base = None
    if project_path:
        base = Path(project_path)
        if base.is_file():
            base = base.parent
        project = load_project(base)
        for cand in ("audio/master/master-v1.wav", "audio/mix/mix-v1.wav"):
            p = base / cand
            if p.exists():
                audio = str(p)
                break
        if not audio:
            raise ValueError("工程还没有可预览的音频；先 render/mix/master")
    if not audio or not Path(audio).exists():
        raise ValueError("找不到要预览的音频")
    src = Path(audio)
    out_dir = ensure_dir((base / "preview") if base else src.parent)
    png = out_dir / "waveform.png"
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(src), "-filter_complex",
                        "showwavespic=s=1600x400:colors=#7cc4ff",
                        "-frames:v", "1", str(png)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not png.exists():
        raise RuntimeError("波形生成失败：" + (r.stderr or "")[-300:])
    sections = []
    if base is not None:
        data = load_project(base)
        bpm = data["tempo"]
        for s in data["sections"]:
            sections.append({"id": s["id"], "startBar": s["startBar"], "bars": s["bars"],
                             "startSec": round((s["startBar"] - 1) * 4 * 60.0 / bpm, 2)})
    return {"ok": True, "waveform": str(png), "audio": str(src), "sections": sections}
