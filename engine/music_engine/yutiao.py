"""语调教：让唱出来的「准音源」按汉语的声调说话，而不是按谱面唱。

思路来自 @幻夜jyw 提供的语调教技术支持，以及洛叶的公开教程
（https://luo-ling-yi.github.io/yutiao-tutorial/）里「ACE Studio 抽参法」的规则：

  第二声上扬、第四声全降、第三声先降后升、阴平高平略降、轻声短促下压；
  疑问句（吗/么/呢/嘛结尾）最后一个字尾音上扬；
  字尾收短、字头加重，句内响度抹平，避免一个字一个字地「顶」。

实现上不重做声码器：复用 tune.py 里已经跑通的 WORLD 分析/合成链
（harvest + cheaptrick + d4c + synthesize），只把目标 F0 换成声调曲线，
时长完全不动，所以可以和同一份伴奏、同一份谱面直接对上。
"""

import math
from pathlib import Path

import numpy as np

from . import dsp
from .config import SAMPLE_RATE
from .project import load_project
from .tune import (FRAME_MS, _analyze, _apply_pitch, _hz, _loudness,
                   _phrases, _pitch_stats, _vocal_file, load_notes)
from .util import ensure_dir, jdump

TONE_NAMES = {1: "阴平", 2: "阳平", 3: "上声", 4: "去声", 0: "轻声"}
QUESTION_TAILS = "吗么呢嘛"


def _pinyin_of(char):
    if not char:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
        out = lazy_pinyin(char, style=Style.TONE3, neutral_tone_with_five=True,
                          errors="ignore")
        return out[0] if out else ""
    except Exception:
        return ""


def _tone_of(char):
    """取这个字的声调号：1/2/3/4，轻声和拿不到拼音都算 0。"""
    syl = _pinyin_of(char)
    if syl and syl[-1] in "12345":
        t = int(syl[-1])
        return 0 if t == 5 else t
    return 0


def _tone_contour(tone, u):
    """声调曲线（音分，相对音符音高）。u 是字内进度 0→1。"""
    if tone == 1:                                   # 阴平：高平，尾略降
        return 85.0 - 30.0 * u
    if tone == 2:                                   # 阳平：向上
        return -55.0 + 150.0 * np.power(u, 1.15)
    if tone == 3:                                   # 上声：先降后升
        dip = -70.0 * np.exp(-((u - 0.42) / 0.26) ** 2)
        rise = 48.0 * np.clip((u - 0.62) / 0.38, 0.0, 1.0) ** 1.4
        return -50.0 + dip + rise
    if tone == 4:                                   # 去声：全降
        return 75.0 - 215.0 * np.power(u, 0.85)
    return -45.0 * u                                # 轻声：短促下压


def _design_speech(notes, t, intensity, seed, declination=32.0, question_rise=110.0):
    """谱面 + 声调 → 目标 F0 曲线 / 力度曲线 / 每个字的计划表。"""
    rng = np.random.default_rng(seed)
    n = len(t)
    target_hz = np.zeros(n, dtype=np.float64)
    note_hz = np.zeros(n, dtype=np.float64)
    gain_db = np.zeros(n, dtype=np.float64)
    phrase_of = _phrases(notes)
    plan = []

    for phrase in phrase_of:
        p_start = float(phrase[0]["start"])
        p_end = max(float(x["start"]) + float(x["dur"]) for x in phrase)
        span = max(p_end - p_start, 0.2)
        question = bool(phrase[-1].get("char") or "") and \
            (phrase[-1].get("char") or "") in QUESTION_TAILS

        for ni, note in enumerate(phrase):
            s, d = float(note["start"]), float(note["dur"])
            seg = (t >= s) & (t < s + d)
            if not np.any(seg):
                continue
            char = note.get("char") or ""
            tone = _tone_of(char)
            base = _hz(note["midi"])
            dt = t[seg] - s
            u = np.clip(dt / max(d, 1e-3), 0.0, 1.0)

            # 短音画不出调形（16 分音符只有 0.15 s），按字长衰减
            amount = float(np.clip(d / 0.30, 0.25, 1.0)) * float(intensity)
            cents = _tone_contour(tone, u) * amount

            # 陈述句整体语气：越到句尾越压一点，否则一串字听着像在念清单
            decl = -declination * np.clip((t[seg] - p_start) / span, 0.0, 1.0) * float(intensity)
            cents = cents + decl

            # 疑问句：最后一个字上扬，这是「问」和「陈述」在听感上最大的差别
            if question and ni == len(phrase) - 1 and d >= 0.18:
                cents = cents + question_rise * np.clip((u - 0.55) / 0.45, 0.0, 1.0) ** 1.3 * float(intensity)

            # 说话的基频不是直线：留一点每条字各自的抖动，去机器人味
            cents = cents + rng.normal(0.0, 3.0, size=cents.shape)

            target_hz[seg] = base * 2.0 ** (cents / 1200.0)
            note_hz[seg] = base

            # 力度：字头短暂顶一下（咬字），重拍的字再抬一点
            head = 1.1 * np.exp(-np.maximum(dt, 0.0) / 0.032)
            accent = 0.0
            beat = note.get("beat")
            if beat is not None:
                b = float(beat)
                if abs(b - round(b / 4.0) * 4.0) < 0.01:
                    accent = 1.3
                elif abs(b - round(b / 2.0) * 2.0) < 0.01:
                    accent = 0.5
            gain_db[seg] = head + accent

            plan.append({
                "index": len(plan), "char": char, "pinyin": _pinyin_of(char),
                "tone": tone, "toneName": TONE_NAMES.get(tone, "轻声"),
                "start": round(s, 3), "dur": round(d, 3),
                "note": round(float(note["midi"]), 2),
                "centsRange": [round(float(np.min(cents)), 1),
                               round(float(np.max(cents)), 1)],
                "question": bool(question and ni == len(phrase) - 1),
            })

    if np.any(gain_db):
        from scipy.ndimage import uniform_filter1d
        size = max(3, int(0.05 / (FRAME_MS / 1000.0)))
        gain_db = uniform_filter1d(gain_db, size=size, mode="nearest")
    return target_hz, note_hz, gain_db, phrase_of, plan


def _taper(y, sr, notes, min_dur=0.16, fade=0.045):
    """字尾收短：说话的音节收得比唱干净，长音拖尾是「唱」的味道。

    只动 ≥ min_dur 的字；短音本来就没气可收，再削会把字吃掉。
    """
    env = np.ones(len(y), dtype=np.float32)
    for note in notes:
        d = float(note["dur"])
        if d < min_dur:
            continue
        s = float(note["start"])
        i0 = int(max(0, round(s * sr)))
        i1 = int(min(len(y), round((s + d) * sr)))
        nf = int(min(fade * sr, max(1, (i1 - i0) * 0.35)))
        if i1 - i0 <= nf + 8:
            continue
        env[i1 - nf:i1] *= np.linspace(1.0, 0.0, nf) ** 1.2
    return y * env


def _even_loudness(y, sr, notes, power=0.5, lo=0.75, hi=1.3):
    """句内响度抹平：SVS 每个字都顶到同一个电平，听感是「念经」不像说话。"""
    if not notes:
        return y
    rms = []
    spans = []
    for note in notes:
        i0 = int(max(0, round(float(note["start"]) * sr)))
        i1 = int(min(len(y), round((float(note["start"]) + float(note["dur"])) * sr)))
        if i1 - i0 < int(0.03 * sr):
            continue
        seg = y[i0:i1]
        r = float(np.sqrt(np.mean(seg ** 2) + 1e-12))
        rms.append(r)
        spans.append((i0, i1))
    if len(rms) < 3:
        return y
    med = float(np.median(rms))
    if med <= 1e-7:
        return y
    gain = np.ones(len(y), dtype=np.float32)
    for (i0, i1), r in zip(spans, rms):
        g = float(np.clip((med / max(r, 1e-7)) ** power, lo, hi))
        gain[i0:i1] *= g
    # 20 ms 平滑，免得字与字之间出现台阶
    if len(gain) > 16:
        from scipy.ndimage import uniform_filter1d
        gain = uniform_filter1d(gain.astype(np.float64),
                                size=max(3, int(0.02 * sr)), mode="nearest").astype(np.float32)
    return y * gain


def speech(project_path, section=None, intensity=0.8, strength=0.95, even=True,
           ab=True, dry_run=False, seed=None, tag=None):
    """把工程里的人声（唱出来的或外部导入的）改成语调。

    intensity 控制声调幅度；dry_run=True 只出计划表，不做 WORLD 合成（秒回）。
    """
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    project = load_project(base)
    notes, score_src = load_notes(base)
    vocal = _vocal_file(base, section)
    run_tag = tag or section or "lead"
    intensity = float(np.clip(intensity, 0.1, 1.5))
    seed = int(seed if seed is not None else project.get("seed") or 7)

    data, _sr0 = dsp.load(vocal, SAMPLE_RATE)
    if data.ndim == 2:
        data = data.mean(axis=1)
    x = np.asarray(data, dtype=np.float32)
    sr = SAMPLE_RATE
    t = np.arange(len(x), dtype=np.float64) / sr

    target_hz, note_hz, gain_db, phrase_of, plan = _design_speech(
        notes, np.arange(0, len(x) / sr, FRAME_MS / 1000.0), intensity, seed)

    summary = {
        "vocal": str(vocal), "score": score_src, "chars": len(plan),
        "phrases": len(phrase_of), "intensity": intensity,
        "toneHistogram": {},
        "questionPhrases": sum(1 for p in plan if p["question"]),
    }
    for row in plan:
        key = row["toneName"]
        summary["toneHistogram"][key] = summary["toneHistogram"].get(key, 0) + 1

    if dry_run:
        jdump(base / "analysis" / f"yutiao-plan-{run_tag}.json",
              {"style": "speech", "plan": plan, "summary": summary})
        return {"ok": True, "dryRun": True, "plan": plan, "summary": summary}

    f0, tf, sp, ap = _analyze(x, sr)
    # _design_speech 用的是另一条更粗的网格；统一到 WORLD 帧时间上
    target_hz = np.interp(tf, np.arange(len(target_hz)) * (FRAME_MS / 1000.0), target_hz,
                          left=0.0, right=0.0)
    note_hz = np.interp(tf, np.arange(len(note_hz)) * (FRAME_MS / 1000.0), note_hz,
                        left=0.0, right=0.0)
    f0_new = _apply_pitch(f0, note_hz, target_hz, float(np.clip(strength, 0.1, 1.0)))

    import pyworld as pw
    y = pw.synthesize(np.ascontiguousarray(f0_new, dtype=np.float64),
                      np.ascontiguousarray(sp, dtype=np.float64),
                      np.ascontiguousarray(ap, dtype=np.float64),
                      sr, frame_period=FRAME_MS).astype(np.float32)
    if len(y) < len(x):
        y = np.pad(y, (0, len(x) - len(y)))
    y = y[:len(x)]

    rms_x = float(np.sqrt(np.mean(x ** 2) + 1e-12))
    rms_y = float(np.sqrt(np.mean(y ** 2) + 1e-12))
    if rms_y > 1e-9:
        y *= rms_x / rms_y

    # 力度曲线在帧网格上：和 harvest 的帧数可能差一帧，按索引对齐后插值到采样网格
    if len(gain_db) != len(tf):
        idx = np.linspace(0.0, max(0.0, len(gain_db) - 1.0), len(tf))
        gain_db = np.interp(idx, np.arange(len(gain_db)), gain_db)
    y *= (10.0 ** (np.interp(t, tf, gain_db) / 20.0)).astype(np.float32)

    y = _taper(y, sr, notes)
    if even:
        y = _even_loudness(y, sr, notes)

    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0.94:
        y *= 0.94 / peak

    out_dir = base / "audio" / "vocal"
    ensure_dir(out_dir)
    dry_path = out_dir / f"vocal-yutiao-{run_tag}.wav"
    dsp.save(dry_path, y.astype(np.float32), sr, subtype="PCM_24")

    stats = {"notes": len(notes), "phrases": len(phrase_of), "chars": len(plan),
             "score": score_src, "vocal": str(vocal),
             "duration": round(len(x) / sr, 2),
             "lufsYutiao": _loudness(y, sr),
             "pitchBefore": _pitch_stats(f0, tf, notes, target_hz),
             "pitchAfter": _pitch_stats(f0_new, tf, notes, target_hz),
             "toneHistogram": summary["toneHistogram"]}
    result = {"ok": True, "project": str(base), "style": "speech",
              "intensity": intensity, "dry": str(dry_path), "stats": stats,
              "plan": plan}

    if ab and len(x) > 10 * sr:
        start = max(0.0, notes[0]["start"] - 0.3) if notes else 1.0
        i0 = int(start * sr)
        if i0 + 9 * sr > len(x):
            i0 = max(0, len(x) - 9 * sr)
        a = x[i0:i0 + int(8 * sr)]
        b = y[i0:i0 + int(8 * sr)]
        fade = int(0.4 * sr)
        if len(a) > fade and len(b) >= len(a):
            b = b[:len(a)].copy()
            b[:fade] *= np.linspace(0, 1, fade)
            ra = float(np.sqrt(np.mean(a ** 2) + 1e-12))
            rb = float(np.sqrt(np.mean(b ** 2) + 1e-12))
            if rb > 1e-6:
                b *= ra / rb
            ab_sig = np.concatenate([a, b])
            pk = float(np.max(np.abs(ab_sig)))
            if pk > 0.95:
                ab_sig = ab_sig * (0.95 / pk)
            ab_path = out_dir / f"vocal-yutiao-ab-{run_tag}.wav"
            dsp.save(ab_path, ab_sig, sr, subtype="PCM_24")
            result["ab"] = str(ab_path)

    jdump(base / "analysis" / f"yutiao-plan-{run_tag}.json",
          {"style": "speech", "plan": plan, "summary": stats})
    jdump(base / "report" / f"yutiao-{run_tag}.json", result)
    return result
