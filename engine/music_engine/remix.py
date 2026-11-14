"""自动 Remix：分离 → 变速/变调 → 新伴奏 → 混音 → 母带。

三种模式：
- remix     ：新伴奏（目标曲风）+ 原曲人声（对齐到新速度/调性）
- cover     ：新伴奏 + 肥鱼 RVC 换声后的人声（需要声音服务）
- rearrange ：保留原曲伴奏 + 叠加目标曲风的鼓/贝斯，再整体混音

没有 Demucs 时用中心声道近似分离，报告里会明确标注质量限制。
"""
import math
import shutil
import subprocess
import time
from pathlib import Path

from . import align as align_mod
from . import compose as compose_mod
from . import master as master_mod
from . import mix as mix_mod
from . import preview as preview_mod
from . import render as render_mod
from . import separate as separate_mod
from . import vocals as vocals_mod
from .analyze import KRUMHANSL_MAJOR, KRUMHANSL_MINOR, NOTE
from .config import ROOT
from .genres import get_genre, list_genres
from .project import create_project, load_project, save_project
from .util import ensure_dir, jdump, key_name, parse_key


def _ffmpeg(*args, timeout=900):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 失败：" + (r.stderr or "")[-400:])
    return r


def _normalize(src, dst, sr=48000):
    _ffmpeg("-i", src, "-ar", sr, "-ac", "2", "-c:a", "pcm_s24le", dst)
    return dst


def _quick_analyze(wav, seconds=150):
    """比 analyze 轻：只取 BPM / 调性 / 时长，避免长曲分析太慢。"""
    import numpy as np
    import librosa

    y, sr = librosa.load(str(wav), sr=22050, mono=True, duration=seconds)
    if not len(y):
        raise ValueError("音频是空的")
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0]) or 120.0
    while bpm < 60:
        bpm *= 2
    while bpm > 180:
        bpm /= 2

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    mean = chroma.mean(axis=1)
    best = None
    for i in range(12):
        for mode, profile in (("major", KRUMHANSL_MAJOR), ("minor", KRUMHANSL_MINOR)):
            score = float(np.corrcoef(mean, np.roll(profile, i))[0, 1])
            if best is None or score > best[0]:
                best = (score, NOTE[i], mode, i)
    return {
        "bpm": round(bpm, 2), "duration": round(float(len(y)) / sr, 2),
        "key": {"tonic": best[1], "pc": best[3], "mode": best[2],
                "confidence": round(max(0.0, best[0]), 3)},
    }


def _pitch_class(name):
    from .util import PITCH, SHARP
    if name in SHARP:
        return SHARP.index(name)
    return PITCH[name[0].upper()]


def _guess_genre(text):
    if not text:
        return None
    low = str(text).lower()
    for g in list_genres():
        pack = get_genre(g["id"])
        words = [pack.get("id", ""), pack.get("name", "")] + list(pack.get("aliases") or [])
        for w in words:
            if w and str(w).lower() in low:
                return g["id"]
    return None


def _nearest_semitones(src_pc, dst_pc):
    d = (dst_pc - src_pc) % 12
    return d - 12 if d > 6 else d


def _project_meta(src):
    """如果音频在某个工程目录里，直接用 project.json 的 BPM/调性（比检测准）。"""
    import json
    p = Path(src).resolve()
    for parent in [p.parent] + list(p.parents):
        f = parent / "project.json"
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            key = d.get("key") or {}
            if d.get("tempo") and key.get("tonic"):
                return {"bpm": float(d["tempo"]), "key": key, "title": d.get("title"),
                        "project": str(parent)}
    return None


def _match_loudness(path, target=-18.0):
    """把人声/伴奏轨拉到统一的响度基准，避免分离出来的干声太小。"""
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf

    data, sr = sf.read(str(path), always_2d=True, dtype="float32")
    try:
        loud = float(pyln.Meter(sr).integrated_loudness(data))
    except Exception:
        loud = float("nan")
    if not np.isfinite(loud):
        return 0.0
    gain = max(-15.0, min(15.0, float(target) - loud))
    sf.write(str(path), (data * (10 ** (gain / 20.0))).astype("float32"), sr, subtype="FLOAT")
    return round(gain, 2)


def remix(input=None, mode="remix", brief=None, keep=None, output=None,
          target_genre=None, bpm=None, key=None, bars=None, seed=None,
          backend="auto"):
    mode = (mode or "remix").lower()
    if mode not in ("remix", "cover", "rearrange"):
        raise ValueError(f"未知模式 {mode}；可用 remix / cover / rearrange")
    if not input:
        raise ValueError("缺少 input（要 remix 的音频路径）")
    src = Path(input).expanduser()
    if not src.exists():
        raise ValueError(f"音频不存在: {src}")

    tmp = ensure_dir(ROOT / ("_remix-" + time.strftime("%Y%m%d-%H%M%S")))
    try:
        wav = _normalize(src, tmp / "source.wav")
        info = _quick_analyze(wav)
        meta = _project_meta(src)
        if meta:
            info["detected"] = {"bpm": info["bpm"], "key": info["key"]}
            info["bpm"] = meta["bpm"]
            info["key"] = {"tonic": meta["key"]["tonic"], "pc": _pitch_class(meta["key"]["tonic"]),
                           "mode": meta["key"]["mode"], "confidence": 1.0}
            info["source"] = "project.json"
        src_bpm, src_key, duration = info["bpm"], info["key"], info["duration"]

        genre_id = target_genre or _guess_genre(brief) or "citypop"
        pack = get_genre(genre_id)
        target_bpm = int(round(float(bpm or src_bpm)))
        if bpm and abs(target_bpm - src_bpm) < 0.5:
            target_bpm = int(round(src_bpm))
        key_text = key or key_name(src_key)
        target_key = parse_key(key_text)

        auto_bars = max(8, int(math.ceil(duration * src_bpm / 240.0)))
        auto_bars = int(math.ceil(auto_bars / 4.0) * 4)
        name = output or f"remix-{src.stem}-{genre_id}"
        res = create_project(name=name, genre=genre_id, bpm=target_bpm,
                             key=key_text, bars=int(bars or auto_bars))
        base = Path(res["project"])
        project = load_project(base)
        target_bpm = int(project["tempo"])
        target_key = project["key"]

        shutil.copy2(wav, base / "source" / f"{src.stem}.wav")
        warnings = []

        # 1) 分离
        sep = separate_mod.separate(wav, base / "source" / "separated", backend=backend)

        include_vocal = (keep in (None, "both", "vocals") if mode == "rearrange"
                         else keep != "instrumental")
        include_inst = mode == "rearrange" and keep in (None, "both", "instrumental")
        if include_vocal:
            warnings += sep["warnings"]

        # 2) 对齐参数：速度比 = 源 BPM / 目标 BPM（输出时长倍数）
        tempo_ratio = float(src_bpm) / float(target_bpm)
        semis = _nearest_semitones(src_key["pc"], target_key["pc"])
        if info.get("source") != "project.json" and float(src_key.get("confidence") or 0) < 0.5:
            warnings.append("调性识别置信度低，已默认沿用原调；要移调请显式传 key")
        if include_vocal and (abs(tempo_ratio - 1.0) > 1.6 or abs(tempo_ratio - 1.0) < 0.35):
            warnings.append(f"变速幅度较大（{src_bpm:.1f} → {target_bpm} BPM），人声可能有拖影")

        # 3) 人声（cover 先换声，再对齐）
        vocal_src = sep["vocals"]
        conversion = None
        if mode == "cover":
            try:
                converted = base / "audio" / "vocal" / "converted.wav"
                conversion = vocals_mod.convert_voice(vocal_src, converted, voice="feiyu")
                vocal_src = str(converted)
            except Exception as e:
                raise ValueError(
                    "cover 模式需要肥鱼声音服务（RVC 换声）。当前不可用：" + str(e)
                    + "。可改用 mode=remix 保留原唱，或先启动声音服务。")

        vocal_out = base / "audio" / "vocal" / "vocal-remix-v1.wav"
        align_vocal, vocal_gain = None, None
        if include_vocal:
            align_vocal = align_mod.stretch(vocal_src, vocal_out, tempo_ratio=tempo_ratio,
                                            semitones=semis, fine=True, formant=True)
            vocal_gain = _match_loudness(vocal_out, -18.0)
            if vocal_gain is not None and abs(vocal_gain) > 9:
                warnings.append(f"人声轨电平补了 {vocal_gain:+.1f} dB，可能分离质量一般")

        # 4) 生成新伴奏
        compose_mod.compose(base, seed=seed)
        render_mod.render(base)

        align_inst, inst_gain = None, None
        if include_inst:
            inst_out = base / "audio" / "render-v1" / "instrumental.wav"
            align_inst = align_mod.stretch(sep["instrumental"], inst_out, tempo_ratio=tempo_ratio,
                                           semitones=semis, fine=False, formant=False)
            inst_gain = _match_loudness(inst_out, -18.0)

        # rearrange 只叠鼓与贝斯，避免和原曲和声打架
        if mode == "rearrange":
            extras = ensure_dir(base / "audio" / "render-v1" / "_extras")
            for tid in ("keys", "lead", "pad", "guitar"):
                src_wav = base / "audio" / "render-v1" / f"{tid}.wav"
                if src_wav.exists():
                    src_wav.rename(extras / f"{tid}.wav")
                    warnings.append(f"rearrange：{tid} 只留在 _extras/ 作参考，未进混音")

        # 5) 轨道增益
        tracks = project.get("tracks") or []
        if include_vocal:
            tracks.append({"id": "vocal", "source": "remix:input", "gainDb": -3.0, "pan": 0.0})
        if include_inst:
            tracks.append({"id": "instrumental", "source": "remix:input", "gainDb": -5.0, "pan": 0.0})
        project["tracks"] = tracks
        project["modifiers"] = ["remix", mode]
        project.setdefault("mix", {})["fadeOut"] = 2.0 if mode == "rearrange" else 3.0
        project["remix"] = {
            "source": str(src), "mode": mode, "keep": keep,
            "sourceBpm": src_bpm, "targetBpm": target_bpm,
            "sourceKey": key_name(src_key), "targetKey": key_name(target_key),
            "semitones": semis, "separation": sep["backend"],
        }
        save_project(base, project)

        # 6) 混音 + 母带
        mixed = mix_mod.mix(project_path=base, preset=pack.get("mixPreset"))
        mst = master_mod.master(project_path=base, target_lufs=pack.get("lufs"))
        prev = preview_mod.preview(project_path=base)
        if mode == "cover":
            warnings.append("cover 使用 RVC 换声，音色与咬字以声音服务输出为准")

        report = {
            "source": str(src), "mode": mode, "genre": genre_id, "project": str(base),
            "analysis": info, "tempo": {"source": src_bpm, "target": target_bpm},
            "key": {"source": key_name(src_key), "target": key_name(target_key),
                    "semitones": semis, "confidence": src_key.get("confidence")},
            "separation": sep, "alignment": {"vocal": align_vocal, "instrumental": align_inst},
            "conversion": conversion, "loudnessMatch": {"vocalDb": vocal_gain, "instrumentalDb": inst_gain},
            "outputs": {"master": mst["master"], "mp3": mst["mp3"],
                        "waveform": prev.get("waveform"),
                        "vocals": sep["vocals"], "instrumental": sep["instrumental"]},
            "metrics": {"lufs": mst["metrics"]["lufsOut"], "duration": mixed["metrics"]["duration"],
                        "clipping": mixed["metrics"]["clipping"]},
            "warnings": warnings,
        }
        jdump(base / "report" / "remix.json", report)
        return {"ok": True, "remix": str(base), "mode": mode, "genre": genre_id,
                "tempo": report["tempo"], "key": report["key"],
                "separation": {k: sep[k] for k in ("backend", "presence", "warnings")},
                "outputs": report["outputs"], "metrics": report["metrics"],
                "warnings": warnings, "next": ["music_preview", "music_export"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
