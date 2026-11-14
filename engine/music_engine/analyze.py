from pathlib import Path

NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
KRUMHANSL_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KRUMHANSL_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def analyze(audio, stems="off", target_bpm=None, target_key=None, deep=False,
            asr=None, deep_stems="off", start=None, end=None):
    import numpy as np
    import librosa
    import soundfile as sf
    import pyloudnorm as pyln

    path = Path(audio)
    if not path.exists():
        raise ValueError(f"音频不存在: {path}")
    # 注意：下面算段落时会复用 start/end 作为循环变量，先把区间参数存下来
    scope_start, scope_end = start, end
    y, sr = librosa.load(str(path), sr=22050, mono=True)
    duration = round(float(len(y) / sr), 2)
    warnings = []

    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    bpm = float(np.atleast_1d(tempo)[0])
    if bpm < 60:
        warnings.append(f"BPM {bpm:.1f} 偏低，可能被识别成半速")
    if target_bpm and abs(bpm - float(target_bpm)) > 3:
        warnings.append(f"检测 BPM {bpm:.1f} 与目标 {target_bpm} 不一致")

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    mean = chroma.mean(axis=1)
    best = None
    for i in range(12):
        for mode, profile in (("major", KRUMHANSL_MAJOR), ("minor", KRUMHANSL_MINOR)):
            score = float(np.corrcoef(mean, np.roll(profile, i))[0, 1])
            if best is None or score > best[0]:
                best = (score, NOTE[i], mode, i)
    confidence = round(max(0.0, best[0]), 3)
    if confidence < 0.5:
        warnings.append("调性置信度较低，建议人工确认")

    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    n_sections = max(2, min(8, int(round(duration / 20))))
    try:
        bounds = librosa.segment.agglomerative(mfcc, n_sections)
        times = list(librosa.frames_to_time(bounds, sr=sr)) + [duration]
    except Exception:
        times = [0.0, duration]
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    sections = []
    for i in range(len(times) - 1):
        start, end = float(times[i]), float(times[i + 1])
        a = int(start * sr / 512)
        b = max(a + 1, int(end * sr / 512))
        energy = float(np.mean(rms[a:b])) if a < len(rms) else 0.0
        sections.append({"id": f"section_{i + 1}", "start": round(start, 2),
                         "end": round(end, 2), "energy": round(min(1.0, energy * 8), 3)})

    data, rate = sf.read(str(path), always_2d=True, dtype="float64")
    lufs = round(float(pyln.Meter(rate).integrated_loudness(data)), 2)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    result = {
        "ok": True, "audio": str(path), "duration": duration,
        "bpm": round(bpm, 2), "key": {"tonic": best[1], "mode": best[2], "confidence": confidence},
        "sections": sections, "beats": [round(float(t), 2) for t in beats[:200]],
        "loudness": {"lufs": lufs, "peakDb": round(20 * np.log10(peak + 1e-9), 2)},
        "warnings": warnings,
        "targets": {"bpm": target_bpm, "key": target_key},
    }
    if stems and stems != "off":
        result["stems"] = {"available": False, "reason": "Demucs 未安装；先装上再分离"}
    if scope_start not in (None, "") or scope_end not in (None, ""):
        result["scope"] = {"start": scope_start or 0, "end": scope_end or result["duration"]}
    if deep:
        try:
            from . import soundcheck
            raw = soundcheck.analyze(str(path), start=scope_start, end=scope_end,
                                     stems=(deep_stems if deep_stems not in (None, "", "off") else None),
                                     asr=asr)
            concise = soundcheck.condense(raw, section=bool(scope_start or scope_end))
            result["deep"] = concise
            # LUFS/真峰值以它的测量为准（带削波计数；我们自己只算 sample peak）。
            # 只分析了某一段时不要覆盖整首的 loudness——两边的统计范围不同。
            dl = concise.get("loudness") or {}
            scoped = scope_start not in (None, "") or scope_end not in (None, "")
            if not scoped:
                if dl.get("lufs") is not None:
                    result["loudness"]["lufs"] = dl["lufs"]
                if dl.get("truePeakDb") is not None:
                    result["loudness"]["truePeakDb"] = dl["truePeakDb"]
            if dl.get("clipped"):
                warnings.append("检测到削波（近满刻度采样数 > 0）")
            # 人声判定只有在分轨后才有意义：没分轨时 AudioSet 在整混音上
            # 基本听不出人声（实测把有人声的混音报成「无」），不能当结论用
            if deep_stems not in (None, "", "off"):
                # 分轨后以 stems.vocals（存在性 + 占比）为准；整混音上的人声分类不可靠
                sv = (concise.get("stems") or {}).get("vocals") or {}
                if sv.get("present") is not None:
                    result["vocalVerdict"] = "有人声" if sv["present"] else "纯器乐"
                v = (concise.get("vocal") or {}).get("verdict")
                if v and "vocalVerdict" not in result:
                    result["vocalVerdict"] = v
            else:
                warnings.append("人声判定需 deepStems:4s/6s 分轨；未分轨时 deep.vocal 仅供参考")
            if concise.get("bpm", {}).get("primary") is not None:
                result["deepBpm"] = concise["bpm"]
        except Exception as e:
            warnings.append(f"深分析不可用（music_analyze 仍给出基础结果）：{e}")
    return result
