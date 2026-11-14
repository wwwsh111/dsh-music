"""变速/变调对齐（rubberband CLI）。"""
import shutil
import subprocess
from pathlib import Path


def _have_rubberband():
    return shutil.which("rubberband") is not None


def stretch(src, dst, tempo_ratio=1.0, semitones=0.0, fine=True, formant=True):
    """tempo_ratio = 输出时长 / 原时长（<1 变快）。semitones 为移调半音数。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ratio = float(tempo_ratio or 1.0)
    semis = float(semitones or 0.0)
    if abs(ratio - 1.0) < 0.001 and abs(semis) < 0.01:
        shutil.copy2(src, dst)
        return {"applied": False, "tempoRatio": 1.0, "semitones": 0.0}
    if not _have_rubberband():
        # 没装 rubberband 时退回 ffmpeg atempo（只支持变速）
        if abs(semis) >= 0.01:
            raise RuntimeError("需要 rubberband 才能变调；先 apt-get install rubberband-cli")
        r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                            "-filter:a", f"atempo={1.0 / ratio:.6f}", str(dst)],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg 变速失败：" + (r.stderr or "")[-300:])
        return {"applied": True, "engine": "ffmpeg-atempo", "tempoRatio": ratio, "semitones": semis}

    cmd = ["rubberband"]
    cmd.append("-3" if fine else "-2")
    if formant and abs(semis) >= 0.01:
        cmd.append("-F")
    cmd += ["-q", "-t", f"{ratio:.6f}"]
    if abs(semis) >= 0.01:
        cmd += ["-p", f"{semis:.4f}"]
    cmd += [str(src), str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("rubberband 处理失败：" + ((r.stderr or "") + (r.stdout or ""))[-300:])
    return {"applied": True, "engine": "rubberband-r3" if fine else "rubberband-r2",
            "tempoRatio": ratio, "semitones": semis}
