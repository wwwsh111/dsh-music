#!/usr/bin/env python3
"""把 dsh-music 工程的母带做成 1080p MP4：渐变底 + 波形 + 标题/BPM/调性/LUFS。

先渲染一张 cover.png（渐变 + 波形 + 文字），再用它和音频合成视频，
比逐帧 blend 稳，也快很多。
"""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg 失败:\n" + "\n".join(cmd) + "\n" + (r.stderr or "")[-1200:])
    return r


def probe(path, entries):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries,
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return r.stdout.strip()


def write_text(tmp, name, text):
    """drawtext 读 textfile 时遇到多字节字符会少画最后一个字形，结尾补空格兜底。"""
    p = Path(tmp) / name
    p.write_text(str(text) + "   ", encoding="utf-8")
    return str(p)


def build(project, out, title=None, subtitle=None, footer="DSH Music  |  auto remix"):
    base = Path(project)
    data = json.loads((base / "project.json").read_text(encoding="utf-8"))
    master = base / "audio" / "master" / "master-v1.wav"
    if not master.exists():
        raise SystemExit(f"没有母带: {master}")
    wave = base / "preview" / "waveform.png"
    duration = float(probe(master, "format=duration"))

    key = data.get("key", {})
    keyname = key.get("tonic", "?") + (" minor" if key.get("mode") == "minor" else "")
    remix = data.get("remix") or {}
    title = title or data.get("title") or base.name
    mode = (remix.get("mode") or "").lower()
    label = {"remix": "Remix", "cover": "Cover", "rearrange": "Rearranged"}.get(mode, "")
    if remix and label and label.lower() not in title.lower():
        title = f"{title} - {label}"
    if len(title) > 26:
        title = title[:26]
    sub = subtitle or f"{str(data.get('genre', '')).upper()}  |  {data.get('tempo')} BPM  |  {keyname}"
    try:
        info = json.loads((base / "report" / "master-v1.json").read_text())
        tp = info.get("truePeakOut", info["truePeak"])
        meta = f"{duration:.1f}s  |  {round(float(info['lufsOut']), 1)} LUFS  |  true peak {tp} dBTP"
    except Exception:
        meta = f"{duration:.1f}s"

    title_size = 88 if len(title) <= 20 else (76 if len(title) <= 24 else 64)
    with tempfile.TemporaryDirectory() as tmp:
        t1 = write_text(tmp, "t1.txt", title)
        t2 = write_text(tmp, "t2.txt", sub)
        t3 = write_text(tmp, "t3.txt", meta)
        t4 = write_text(tmp, "t4.txt", footer)
        cover = Path(tmp) / "cover.png"
        dt = (
            f"drawtext=fontfile={FONT_B}:textfile={t1}:expansion=none:fontcolor=0xf8fafc:"
            f"fontsize={title_size}:x=(w-text_w)/2:y=190,"
            f"drawtext=fontfile={FONT_R}:textfile={t2}:expansion=none:fontcolor=0x93c5fd:"
            f"fontsize=44:x=(w-text_w)/2:y=320,"
            f"drawtext=fontfile={FONT_R}:textfile={t3}:expansion=none:fontcolor=0xcbd5e1:"
            f"fontsize=34:x=(w-text_w)/2:y=398,"
            f"drawtext=fontfile={FONT_R}:textfile={t4}:expansion=none:fontcolor=0x64748b:"
            f"fontsize=28:x=(w-text_w)/2:y=1004,"
            f"drawbox=x=160:y=452:w=1600:h=3:color=0x38bdf8@0.85:t=fill"
        )
        if wave.exists():
            fc = (f"[0:v][1:v]overlay=210:600:format=auto,noise=alls=5:allf=t+u,{dt}[v]")
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "gradients=s=1920x1080:c0=0x16213e:c1=0x070b14:n=2:d=1",
                 "-i", str(wave), "-filter_complex", fc, "-map", "[v]",
                 "-frames:v", "1", str(cover)])
        else:
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "gradients=s=1920x1080:c0=0x16213e:c1=0x070b14:n=2:d=1",
                 "-vf", "noise=alls=5:allf=t+u," + dt, "-frames:v", "1", str(cover)])

        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-loop", "1", "-framerate", "10", "-i", str(cover),
             "-i", str(master), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-r", "10", "-c:a", "aac", "-b:a", "192k",
             "-shortest", "-movflags", "+faststart", str(out)])
    return {"ok": True, "video": str(out), "duration": round(duration, 2),
            "sizeMB": round(Path(out).stat().st_size / 1e6, 2)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("out")
    ap.add_argument("--title")
    ap.add_argument("--subtitle")
    ap.add_argument("--footer", default="DSH Music  |  auto remix")
    a = ap.parse_args()
    print(json.dumps(build(a.project, a.out, a.title, a.subtitle, a.footer),
                     ensure_ascii=False, indent=1))
