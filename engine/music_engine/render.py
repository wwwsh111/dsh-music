"""渲染：MIDI → 分轨音频，并按乐器挂上音色处理。

0.3.0 的问题：fluidsynth 出来什么样就什么样，GM 的「电吉他」是干净的
钢弦音色，所以整首歌听起来像钢琴小样。现在每一轨渲染完立刻进 DSP 链：

* 节奏吉他：clean DI → 前级失真 → 箱体低通（双轨各一次，pan 分左右）
* 主音吉他：更高的增益 + 延迟尾巴
* 贝斯：轻饱和 + 中低频塑形
* 鼓：瞬时整形 + 并行压缩 + 饱和 + 轻房间
* 键盘/铺底：只做高通和轻微压缩，留给混音
* 人声：不在这里渲染（走肥鱼 SVS 的真实人声），混音时再挂链
"""

import subprocess
from pathlib import Path

import numpy as np

from . import dsp
from .config import MELODIC_SOUNDFONT, SOUNDFONT
from .project import load_project, save_project
from .util import ensure_dir, jdump

# 每类分轨的目标 RMS（dBFS）：决定「谁在台前」
NORM_RMS = {
    "drums": -16.5, "drum_bus": -16.5, "bass": -19.0, "vocal": -18.0,
    "amp_rhythm": -19.5, "amp_lead": -21.0,
    "keys": -26.0, "pad": -30.0, "synth": -24.0, "generic": -22.0,
}


def _mono(data):
    return np.asarray(data, dtype="float32").mean(axis=1, keepdims=True)


def _normalize_rms(data, target_db, peak_ceiling=-1.0):
    rms = float(np.sqrt(np.mean(np.asarray(data, dtype="float32") ** 2) + 1e-12))
    if rms <= 1e-9:
        return data
    g = dsp.db_to_gain(target_db) / rms
    out = data * g
    ceil = dsp.db_to_gain(peak_ceiling)
    for _ in range(3):
        peak = float(np.max(np.abs(out))) if out.size else 0.0
        if peak <= ceil:
            break
        out = dsp.soft_clip(out, ceiling_db=peak_ceiling, knee_db=3.0)
        peak = float(np.max(np.abs(out))) if out.size else 0.0
        if peak > ceil:
            out = out * (ceil / peak)
    return out.astype("float32")


def _render_midi(midi, wav, quiet=True, font=None):
    """渲染一个 MIDI 轨。鼓走 FluidR3（鼓组最全），旋律走 GeneralUser（吉他采样多）。"""
    cmd = ["fluidsynth", "-ni", "-g", "0.9", "-r", "48000"]
    if quiet:
        cmd += ["-o", "synth.reverb.active=0", "-o", "synth.chorus.active=0"]
    cmd += ["-F", str(wav), str(font or SOUNDFONT), str(midi)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise ValueError("找不到 fluidsynth")
    if r.returncode != 0 or not wav.exists():
        raise RuntimeError(f"fluidsynth 渲染失败（{Path(midi).name}）：{r.stderr[-400:]}")


def _apply_fx(tid, fx, data, sr, project):
    """按 fx 名挂处理链；返回 (音频, 说明)。"""
    note = fx
    if fx == "drum_bus":
        energy_avg = sum(s.get("energy", 0.6) for s in project["sections"]) / max(
            1, len(project["sections"]))
        data = dsp.drum_bus(data, sr, punch=0.35 + 0.25 * energy_avg, room=0.09)
    elif fx == "bass":
        data = dsp.bass_chain(_mono(data), sr, drive=4.0)
    elif fx == "amp_rhythm":
        # 左右两轨给不同的箱体截止，叠起来频响更宽
        left = tid.endswith("_l")
        cab = (project.get("profile") or {}).get("cab") or "cab_modern_uk.wav"
        data = dsp.guitar_amp(_mono(data), sr, drive=15.0 if left else 17.0,
                              tone=0.52 if left else 0.62,
                              presence=0.45 if left else 0.55, cab=cab)
        data = _mono(data)
    elif fx == "amp_lead":
        cab = (project.get("profile") or {}).get("cab") or "cab_modern_uk.wav"
        data = dsp.guitar_amp(_mono(data), sr, drive=24.0, tone=0.66, presence=0.7, cab=cab)
        data = _mono(data)
        data = dsp.delay(data, sr, time_s=0.36, feedback=0.22, wet=0.12, pingpong=False)
    elif fx in ("keys", "pad"):
        try:
            from pedalboard import Compressor, HighpassFilter, Pedalboard
            board = Pedalboard([HighpassFilter(cutoff_frequency_hz=110),
                                Compressor(threshold_db=-18.0, ratio=2.2,
                                           attack_ms=12.0, release_ms=180.0)])
            data = board(data.T, sr).T.astype("float32")
        except Exception:
            pass
    elif fx == "synth":
        try:
            from pedalboard import Chorus, HighpassFilter, Pedalboard
            board = Pedalboard([HighpassFilter(cutoff_frequency_hz=140),
                                Chorus(rate_hz=0.5, depth=0.15, mix=0.25)])
            data = board(data.T, sr).T.astype("float32")
        except Exception:
            pass
    return data, note


def _render_sampler(project, tid, tr, midi, sr, n_samples):
    """采样器轨：读 midi 的音符，用采样器把它们弹出来。"""
    from . import midilib
    from . import sampler as sampler_mod
    defs = {str(s.get("id")): s for s in (project.get("samplers") or [])}
    sid = str(tr.get("sampler"))
    if sid not in defs:
        raise ValueError(f"轨 {tid} 指定了采样器 {sid}，但工程里没有这个采样器定义")
    if not Path(midi).exists():
        return np.zeros((n_samples, 2), dtype="float32"), f"sampler:{sid}（没有 midi，空轨）"
    notes, _meta = midilib.read(midi)
    data, report = sampler_mod.render_notes(notes, defs[sid], sr, n_samples)
    return data, f"sampler:{sid} 音符 {report.get('placed', 0)} 个，模式 {report.get('mode')}"


def render(project_path, quality=None, only=None):
    project = load_project(project_path)
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    if not Path(MELODIC_SOUNDFONT).exists() and not Path(SOUNDFONT).exists():
        raise ValueError(
            "找不到 SoundFont（GeneralUser / FluidR3_GM 都没有）。"
            "把 GeneralUser.sf2 放到 %USERPROFILE%\\soundfonts\\，"
            "或设置环境变量 MUSIC_SOUNDFONT_MELODIC 指向 sf2 文件。"
        )
    drum_font = SOUNDFONT if Path(SOUNDFONT).exists() else MELODIC_SOUNDFONT
    sr = int(project.get("sampleRate") or 48000)
    out = ensure_dir(base / "audio" / "render-v1")
    tmp = ensure_dir(base / "audio" / "render-v1" / ".raw")
    total_bars = sum(int(s["bars"]) for s in project["sections"])
    seconds = total_bars * 4 * 60.0 / float(project["tempo"])
    n_samples = int(round(seconds * sr))

    files, notes = {}, {}
    tracks = project.get("tracks") or []
    for tr in tracks:
        tid = tr["id"]
        fx = tr.get("fx") or "generic"
        if only and tid not in (only if isinstance(only, list) else [only]):
            continue
        if fx == "vocal" or tid.startswith("vocal"):
            continue  # 人声由肥鱼 SVS 唱，MIDI 只给混音阶段定位
        midi = base / tr.get("midi", f"midi/{tid}.mid")
        if not midi.exists():
            continue
        raw = tmp / f"{tid}.wav"
        if tr.get("sampler"):
            # 通道机架：这条轨不用 SoundFont，而是把自己导入的采样按音高弹出来
            data, note = _render_sampler(project, tid, tr, midi, sr, n_samples)
        else:
            # 鼓优先用 FluidR3（鼓组更全）；没装就回退到 GeneralUser，别让渲染整个失败
            font = drum_font if tid == "drums" else MELODIC_SOUNDFONT
            _render_midi(midi, raw, quiet=(fx != "generic"), font=font)
            data, _rate = dsp.load(raw, sr)
            data = dsp.fit_length(dsp.ensure_stereo(data), n_samples)
            if tid == "drums":
                data = dsp.ensure_stereo(data)
            data, note = _apply_fx(tid, fx, data, sr, project)
        data = _normalize_rms(dsp.fit_length(data, n_samples),
                              NORM_RMS.get(fx, -22.0))
        wav = out / f"{tid}.wav"
        dsp.save(wav, data, sr, subtype="FLOAT")
        files[tid] = str(wav)
        notes[tid] = {"fx": note, "rmsTarget": NORM_RMS.get(fx, -22.0),
                      "rms": round(20 * np.log10(float(np.sqrt(np.mean(data ** 2) + 1e-12))), 2),
                      "peak": dsp.true_peak(data, sr)}

    project.setdefault("renders", {})["current"] = "render-v1"
    save_project(base, project)
    jdump(base / "report" / "render-v1.json",
          {"files": files, "notes": notes, "soundfont": str(drum_font),
           "melodicSoundfont": str(MELODIC_SOUNDFONT),
           "drumSoundfont": "FluidR3_GM" if drum_font == SOUNDFONT else "GeneralUser(回退，缺 FluidR3_GM)",
           "seconds": round(seconds, 3), "sampleRate": sr})
    return {"ok": True, "project": str(base), "render": "render-v1",
            "stems": files, "seconds": round(seconds, 3), "processing": notes,
            "drumSoundfont": "FluidR3_GM" if drum_font == SOUNDFONT else "GeneralUser(回退)"}
