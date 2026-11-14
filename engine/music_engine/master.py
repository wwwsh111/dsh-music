"""母带：把混音推到目标响度，并守住真峰值。

0.3.0 用 ffmpeg 的 alimiter（只是采样峰值限制，不是真峰值限制），
靠「留 0.8 dB 余量」碰运气。0.4.0 试过 pedalboard 的 Limiter，但实测它
不管 threshold_db 给多少，输出峰值都钉在 0 dBFS（等于硬削波），
真峰值守不住。最终方案：自己做「软限幅 + 迭代增益」——

    增益 → soft_clip（tanh 折进上限）→ 量真峰值 → 超标就整体退一点
    → 量 LUFS → 差多少补多少，最多 8 轮

软限幅只在接近上限的那部分做非线性，主体不被压，所以响度能一级一级推上去；
每一步都复测 4 倍过采样的真峰值，宁少 0.3 LU 也不超 -1 dBTP。
"""

import subprocess
from pathlib import Path

import numpy as np

from . import dsp
from .project import load_project, save_project
from .util import ensure_dir, jdump


def _fade(data, sr, fade_in=0.05, fade_out=0.0):
    out = data.copy()
    n_in = max(1, int(sr * fade_in))
    if n_in < out.shape[0]:
        out[:n_in] *= np.linspace(0.0, 1.0, n_in)[:, None]
    n_out = max(0, int(sr * fade_out))
    if n_out and n_out < out.shape[0]:
        out[-n_out:] *= np.linspace(1.0, 0.0, n_out)[:, None]
    return out


def _limit(data, sr, ceiling_db=-1.0, release_ms=90.0):
    """软限幅替代限制器：只折掉接近上限的峰，主体电平不动。

    release_ms 保留在签名里（旧调用方式不用改），软限幅不需要释放时间。
    """
    return dsp.soft_clip(np.asarray(data, dtype="float32"),
                         ceiling_db=float(ceiling_db), knee_db=5.0)


def master(project_path=None, mix_path=None, target_lufs=None, true_peak=-1.0,
           preset=None, fade_out=None):
    base = None
    project = None
    if project_path:
        project = load_project(project_path)
        base = Path(project_path)
        if base.is_file():
            base = base.parent
        mix_path = mix_path or str(base / "audio" / "mix" / "mix-v1.wav")
    if not mix_path:
        raise ValueError("需要 mix 路径或 project")
    mix_path = str(mix_path)

    sr = int((project or {}).get("sampleRate") or 48000)
    target = float(target_lufs or (project or {}).get("mix", {}).get("targets", {}).get("lufs", -9))
    tp = float(true_peak)

    data, _ = dsp.load(mix_path, sr)
    data = dsp.ensure_stereo(data)
    duration = round(data.shape[0] / sr, 2)

    fade = fade_out if fade_out is not None else (project or {}).get("mix", {}).get("fadeOut")
    fade = max(0.0, min(float(fade or 0.0), duration / 2.0 if duration else 0.0))
    if fade > 0:
        data = _fade(data, sr, 0.05, fade)

    lufs_in = dsp.lufs(data, sr)
    tp_in = dsp.true_peak(data, sr)
    if lufs_in is None:
        raise RuntimeError("读不到输入响度（缺 pyloudnorm/soundfile）")

    gain = target - lufs_in
    out = data
    hist = []
    for i in range(8):
        cand = data * dsp.db_to_gain(gain)
        # 折峰上限留 0.6 dB 给过采样冒头；超了就整体退，不靠运气
        cand = _limit(cand, sr, ceiling_db=tp - 0.6)
        cand_tp = dsp.true_peak(cand, sr)
        if cand_tp is not None and cand_tp > tp:
            cand = cand * dsp.db_to_gain(tp - cand_tp - 0.05)
            cand_tp = dsp.true_peak(cand, sr)
        cand_lufs = dsp.lufs(cand, sr)
        hist.append({"iter": i + 1, "gainDb": round(gain, 2),
                     "lufs": cand_lufs, "truePeak": cand_tp})
        out = cand
        if cand_lufs is None:
            break
        delta = target - cand_lufs
        if abs(delta) <= 0.2 and (cand_tp if cand_tp is not None else -99) <= tp:
            break
        gain += delta * 0.85

    # 最后一次安全衰减：宁可少 0.3 LU，也不要 dBTP 超标
    tp_out = dsp.true_peak(out, sr)
    if tp_out is not None and tp_out > tp:
        out = out * dsp.db_to_gain(tp - tp_out - 0.05)

    out_dir = ensure_dir((base / "audio" / "master") if base else Path("master"))
    wav = out_dir / "master-v1.wav"
    mp3 = out_dir / "master-v1.mp3"
    dsp.save(wav, out, sr, subtype="PCM_24")
    r = subprocess.run(["ffmpeg", "-y", "-i", str(wav), "-b:a", "320k",
                        "-c:a", "libmp3lame", str(mp3)], capture_output=True, text=True,
                       timeout=300)

    lufs_out = dsp.lufs(out, sr)
    crest_out = dsp.crest_factor(out)
    report = {
        "input": mix_path, "master": str(wav), "mp3": str(mp3),
        "targetLufs": target, "truePeakCeiling": tp,
        "lufsIn": lufs_in, "truePeakIn": tp_in,
        "lufsOut": lufs_out, "truePeakOut": dsp.true_peak(out, sr),
        "crestOut": crest_out,
        "lufsTargetMissed": round(abs(target - (lufs_out or target)), 2),
        "overCompressed": bool(crest_out is not None and crest_out < 7.0),
        "bandsOut": dsp.band_energies(out, sr),
        "duration": duration, "fadeOut": round(fade, 2),
        "iterations": hist, "mp3Ok": r.returncode == 0,
    }
    if base is not None:
        project.setdefault("renders", {})["master"] = "master-v1"
        save_project(base, project)
        jdump(base / "report" / "master-v1.json", report)
    return {"ok": True, "master": str(wav), "mp3": str(mp3),
            "metrics": {k: report[k] for k in ("lufsOut", "truePeakOut", "crestOut",
                                               "lufsIn", "truePeakIn", "duration")},
            "next": ["music_preview", "music_export"]}
