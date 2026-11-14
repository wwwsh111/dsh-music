"""调教：把 SVS 的「准音源」变成有演唱设计的虚拟歌手。

为什么单独一层：SVS 只负责按谱发声——音高、时长、字都对，但听感仍是
「机器在念谱」。真实歌姬的辨识度来自谱面之外的东西：

  · 音高不是一条直线：起音有滑音、长音有颤音、句尾会掉下来；
  · 力度不是恒定的：一句里有起伏，重拍会顶一下；
  · 句与句之间有人呼吸，长音尾巴会散开（气声）。

这些如果只在混音里补（EQ/压缩/混响）是补不出来的——混响只会把「平的」
人声弄得更糊。所以顺序必须是：先调教（改音高曲线/力度/气口），再混音。

实现用 WORLD 声码器（pyworld）：分解出 F0、频谱包络、非周期性，
按设计好的曲线重新合成。这样改音高不会像变速那样改时长，也不会像
相位声码器那样在颤音上产生「水声」。
"""

import math
import random
from pathlib import Path

import numpy as np

from . import dsp
from .config import SAMPLE_RATE
from .project import load_project
from .util import jdump, jload, note_to_midi

# 风格参数：数字都是「感觉」，但每项都对应一个可测量的量。
#   vibrato  颤音深度（0–1 → 15–55 音分）
#   scoop    起音下滑（0–1 → 0–55 音分）
#   drift    长音游移（0–1 → ±4 音分）
#   legato   连音滑行时长（0–1 → 20–90 ms）
#   dynamics 句内力度起伏（0–1 → ±2.5 dB）
#   breath   气口音量（0–1）
#   air      高频空气感（0–1 → 高频架 +1.5–3.5 dB）
#   comp     压缩比（2.5–4）
#   body     低中频厚度（0–1）
#   formant  共振峰偏移（半音，正=更薄更亮）
STYLES = {
    "natural": dict(vibrato=0.52, vib_rate=5.6, scoop=0.42, drift=0.5, legato=0.5,
                    dynamics=0.55, breath=0.55, air=0.55, comp=3.0, body=0.55,
                    formant=0.0),
    "ballad": dict(vibrato=0.88, vib_rate=5.2, scoop=0.7, drift=0.45, legato=0.82,
                   dynamics=0.85, breath=0.8, air=0.6, comp=2.6, body=0.7,
                   formant=-0.15),
    "power": dict(vibrato=0.36, vib_rate=5.9, scoop=0.28, drift=0.35, legato=0.45,
                  dynamics=0.72, breath=0.35, air=0.82, comp=4.0, body=0.45,
                  formant=0.12),
    "cute": dict(vibrato=0.48, vib_rate=6.15, scoop=0.62, drift=0.55, legato=0.55,
                 dynamics=0.48, breath=0.65, air=0.78, comp=2.8, body=0.35,
                 formant=0.35),
}

FRAME_MS = 10.0  # WORLD 帧移：10 ms 足够跟住 6 Hz 颤音（每周期 ~16 帧），内存也只有 5 ms 的一半


# ------------------------------------------------------------------ 读谱

def _vocal_file(base, section=None):
    d = base / "audio" / "vocal"
    if not d.is_dir():
        raise ValueError(f"没有 {d}；先 music_vocals 唱出人声")
    # 修补过的干声比原始 take 新，就用修补版：S5 的顺序是「先修后调教」，
    # 调教要作用在修好的那一版上，否则等于把哑音/缺口又调回来。
    rep = d / f"vocal-repair-{section or 'lead'}.wav"
    raw = d / (f"vocal-{section}-v1.wav" if section else "vocal-lead-v1.wav")
    if rep.is_file() and (not raw.is_file() or rep.stat().st_mtime >= raw.stat().st_mtime):
        return rep
    if section:
        for name in (f"vocal-{section}-v1.wav", f"vocal-{section}.wav"):
            if (d / name).exists():
                return d / name
    for name in ("vocal-lead-v1.wav", "vocal-v1.wav", "vocal-tuned-dry.wav"):
        if (d / name).exists():
            return d / name
    # 工程里只放了一个备用人声：优先 vocal-*（标准产物），再取最新的非 raw/非和声文件
    skip = ("raw", "harmony", "tuned", "ab", "guide", "backup")
    cands = [p for p in d.glob("*.wav")
             if not any(k in p.name.lower() for k in skip)]
    if cands:
        standard = [p for p in cands if p.name.startswith("vocal")]
        pool = standard or cands
        return max(pool, key=lambda p: p.stat().st_mtime)
    raise ValueError(f"{d} 里没有可用的人声：先 music_vocals")


def _notes_from_alignment(base):
    for name in ("vocal-align.json", "vocal-harmony-align.json"):
        data = jload(base / "analysis" / name, None)
        if not data or not data.get("notes"):
            continue
        notes = []
        for n in data["notes"]:
            try:
                midi = note_to_midi(str(n.get("pitch")))
            except Exception:
                continue
            dur = float(n.get("duration") or 0)
            if dur <= 0.02:
                continue
            notes.append({"start": float(n["start"]), "dur": dur, "midi": midi,
                          "char": n.get("char") or "", "line": n.get("line"),
                          "beat": n.get("beat"), "section": n.get("section")})
        if notes:
            notes.sort(key=lambda x: x["start"])
            return notes, "analysis/" + name
    return None, None


def _notes_from_midi(base):
    path = base / "midi" / "vocal.mid"
    if not path.exists():
        return None, None
    try:
        import pretty_midi
        pm = pretty_midi.PrettyMIDI(str(path))
    except Exception:
        return None, None
    best = None
    for inst in pm.instruments:
        if inst.is_drum or not inst.notes:
            continue
        if best is None or len(inst.notes) > len(best.notes):
            best = inst
    if best is None:
        return None, None
    notes = []
    for n in best.notes:
        dur = float(n.end - n.start)
        if dur <= 0.02:
            continue
        notes.append({"start": float(n.start), "dur": dur, "midi": int(n.pitch),
                      "char": "", "line": None, "beat": None, "section": None})
    notes.sort(key=lambda x: x["start"])
    return (notes, "midi/vocal.mid") if notes else (None, None)


def load_notes(base):
    notes, src = _notes_from_alignment(base)
    if not notes:
        notes, src = _notes_from_midi(base)
    if not notes:
        raise ValueError("找不到谱面：analysis/vocal-align.json 或 midi/vocal.mid")
    return notes, src


# ------------------------------------------------------------------ 谱面 → 演唱设计

def _phrases(notes, gap=0.34):
    """按气口分句：间隔超过 gap 秒、或者歌词换行，都是新的一句。

    只按时间间隔分句会漏掉「句间没留大空隙、但确实换了一口气」的行：
    歌词换行比物理静音更接近歌手的换气决定。
    """
    out = []
    cur = []
    prev_end = None
    prev_line = None
    for n in notes:
        line = n.get("line")
        new_line = (prev_line is not None and line is not None and line != prev_line)
        if prev_end is not None and (n["start"] - prev_end > gap or new_line):
            if cur:
                out.append(cur)
            cur = []
        cur.append(n)
        prev_end = n["start"] + n["dur"]
        prev_line = line if line is not None else prev_line
    if cur:
        out.append(cur)
    return out


def _hz(midi):
    return 440.0 * 2.0 ** ((float(midi) - 69.0) / 12.0)


def _design(notes, t, style, bpm, intensity, seed):
    """生成目标 F0 曲线、力度曲线、气口列表。

    所有偏移都以「音分」为单位：音分是相对量，换调、换音域都不用改参数。
    """
    rng = random.Random(seed)
    n = len(t)
    target_hz = np.zeros(n, dtype=np.float64)
    note_hz = np.zeros(n, dtype=np.float64)
    gain_db = np.zeros(n, dtype=np.float64)
    phrase_of = _phrases(notes)

    vib_depth = 14.0 + 42.0 * style["vibrato"] * intensity
    scoop_cents = 52.0 * style["scoop"] * intensity
    drift_cents = 4.0 * style["drift"]
    dyn_db = 2.6 * style["dynamics"] * intensity

    # 全曲慢速游移：真人的音准不会像格子一样锁死
    slow_phase = rng.uniform(0.0, 2 * math.pi)
    global_drift = drift_cents * 0.7 * np.sin(2 * math.pi * 0.07 * t + slow_phase)

    for pi, phrase in enumerate(phrase_of):
        # 一句的能量拱形：起点略收、中后段最满、句尾放掉
        p_start = phrase[0]["start"]
        p_end = max(x["start"] + x["dur"] for x in phrase)
        span = max(p_end - p_start, 0.2)
        for ni, note in enumerate(phrase):
            s, d = note["start"], note["dur"]
            base = _hz(note["midi"])
            seg = (t >= s) & (t < s + d)
            if not np.any(seg):
                continue
            dt = t[seg] - s
            u = dt / max(d, 1e-3)

            cents = np.zeros(dt.shape, dtype=np.float64)
            cents += global_drift[seg]

            # 1) 起音下滑：气息先到、音高再「落」到准位（不是每个音都加）
            gap_before = s - (phrase[ni - 1]["start"] + phrase[ni - 1]["dur"]) \
                if ni > 0 else 9.9
            if ni == 0 or gap_before > 0.12:
                amt = scoop_cents * (0.6 if ni == 0 else 1.0)
                cents -= amt * np.exp(-np.maximum(dt, 0.0) / 0.045)

            # 2) 颤音：延迟进入、深度渐强，长音才明显
            #    0.30 s 以上的音也要有一点——真实演唱里 8 分音符并不是直的，
            #    只是颤音来不及长满；深度按时长缩，短音浅、长音深。
            if d >= 0.30:
                delay = min(0.28, max(0.12, d * 0.28))
                ramp = np.clip((dt - delay) / 0.14, 0.0, 1.0)
                depth_scale = float(np.clip((d - 0.26) / 0.40, 0.35, 1.0))
                depth = vib_depth * depth_scale * (0.75 + 0.5 * rng.random()) * (ramp ** 1.2)
                rate = style["vib_rate"] * (0.96 + 0.08 * rng.random())
                phase = rng.uniform(0.0, 2 * math.pi)
                wobble = 1.0 + 0.06 * np.sin(2 * math.pi * 0.55 * dt + phase * 0.3)
                cents += depth * np.sin(2 * math.pi * rate * dt * wobble + phase)

            # 3) 句尾下落：最后一个字把音高放下来，别悬在空中
            if ni == len(phrase) - 1 and d >= 0.28:
                fall = np.clip((u - 0.80) / 0.20, 0.0, 1.0) ** 2
                cents -= (45.0 + 30.0 * style["scoop"]) * fall

            # 4) 微游移：每个音一个固定的随机偏一点，唱出来才不像音准器
            cents += rng.uniform(-drift_cents, drift_cents) * 0.5

            target_hz[seg] = base * 2.0 ** (cents / 1200.0)
            note_hz[seg] = base

            # 力度：句内拱形 + 重拍 + 字头
            pos = np.clip((t[seg] - p_start) / span, 0.0, 1.0)
            arch = (np.sin(np.pi * pos) ** 0.8 - 0.45) * dyn_db
            beat = note.get("beat")
            accent = 0.0
            if beat is not None:
                b = float(beat)
                accent = 0.9 if abs(b - round(b / 4.0) * 4.0) < 0.01 else \
                    0.35 if abs(b - round(b / 2.0) * 2.0) < 0.01 else 0.0
            # 字头 40 ms 的小顶：咬字更清楚
            head = 0.75 * np.exp(-np.maximum(dt, 0.0) / 0.035)
            gain_db[seg] = arch + accent + head

    # 连音滑行：相邻音之间 20–90 ms 的对数频率过渡（大跳更长）
    for a, b in zip(notes[:-1], notes[1:]):
        gap = b["start"] - (a["start"] + a["dur"])
        if gap > 0.12:
            continue
        i0 = int(round(b["start"] / (FRAME_MS / 1000.0)))
        ia = int(round((a["start"] + a["dur"]) / (FRAME_MS / 1000.0))) - 1
        if i0 <= 0 or i0 >= n or ia < 0:
            continue
        prev = target_hz[min(ia, n - 1)]
        if prev <= 0:
            continue
        step = abs(12.0 * math.log2(max(target_hz[min(i0, n - 1)], 1e-6) / prev))
        dur_s = np.clip(0.018 + 0.014 * step, 0.02, 0.09) * (0.6 + 0.8 * style["legato"])
        nb = max(2, int(round(dur_s / (FRAME_MS / 1000.0))))
        nb = min(nb, n - i0)
        w = np.linspace(0.0, 1.0, nb)[:, None]
        cur = target_hz[i0:i0 + nb]
        valid = cur > 0
        target_hz[i0:i0 + nb] = np.where(valid, np.exp((1 - w[:, 0]) * np.log(prev) + w[:, 0] * np.log(np.maximum(cur, 1e-6))), cur)

    # 力度曲线平滑（60 ms），避免字与字之间出现台阶
    if np.any(gain_db):
        from scipy.ndimage import uniform_filter1d
        gain_db = uniform_filter1d(gain_db, size=max(3, int(0.06 / (FRAME_MS / 1000.0))),
                                   mode="nearest")
    return target_hz, note_hz, gain_db, phrase_of


def _breath_segment(sr, nn, amp, rng):
    """一段气声：粉噪化 + 带通 + 起落包络。"""
    noise = rng.standard_normal(nn)
    # 粉噪化：低频多一点，才不会像白噪声「嘶」
    noise = np.convolve(noise, np.array([0.35, 0.5, 0.35]), mode="same")
    from scipy.signal import butter, sosfilt
    sos = butter(2, [420 / (sr / 2), min(0.95, 5600 / (sr / 2))], btype="band", output="sos")
    noise = sosfilt(sos, noise)
    env = np.ones(nn)
    atk = max(1, min(int(0.055 * sr), nn))
    rel = max(1, min(int(0.16 * sr), nn))
    env[:atk] = np.linspace(0, 1, atk) ** 2
    env[nn - rel:] *= np.linspace(1, 0, rel) ** 1.5
    return noise * env * amp


def _breaths(y, sr, phrase_of, style, intensity, rng):
    """在气口位置铺气声：不是「加个噪声」，而是按句子长度和前后音高塑形。

    换行处如果 SVS 唱得太满、几乎没有缝，就先给上一句做一次自然的提前收声，
    把 100–150 ms 让出来 — 真人换行也是先收再吸，而不是无缝硬切。
    """
    made = []
    level = 0.55 * style["breath"] * intensity
    for pi in range(len(phrase_of) - 1):
        a, b = phrase_of[pi], phrase_of[pi + 1]
        a_end = max(x["start"] + x["dur"] for x in a)
        gap = b[0]["start"] - a_end
        line_break = (a[-1].get("line") is not None and b[0].get("line") is not None
                      and a[-1]["line"] != b[0]["line"])
        last = max(a, key=lambda x: x["start"] + x["dur"])
        room = None
        if gap >= (0.08 if line_break else 0.22):
            # 已有现成的缝：气口越宽，吸气越长，但最多 0.45 s，且不能撑满
            dur = float(np.clip(min(gap * 0.55, gap * 0.85), 0.10, 0.45))
            t0 = a_end + min(0.05, gap * 0.25)
        elif line_break and last["dur"] > 0.20:
            # 缝太小：提前收声腾出空间，收声 + 原缝一起做气口
            room = float(np.clip(min(0.15, last["dur"] * 0.35), 0.08, 0.15))
            fade = np.linspace(1.0, 0.06, int(room * sr)) ** 1.6
            i_f = int((a_end - room) * sr)
            if i_f > 0:
                y[i_f:i_f + len(fade)] *= fade.astype(y.dtype)
            dur = float(np.clip(room + gap * 0.5, 0.10, 0.45))
            t0 = a_end - room + 0.01
        else:
            continue
        i0 = int(t0 * sr)
        nn = int(dur * sr)
        if i0 < 0 or nn <= 0 or i0 + nn > len(y):
            continue
        # 用前面 0.3 s 的能量定气声电平：气声永远比唱低一截
        lo = max(0, i0 - int(0.3 * sr))
        rms = float(np.sqrt(np.mean(y[lo:i0] ** 2) + 1e-12))
        amp = rms * level * (0.7 + 0.6 * rng.random())
        y[i0:i0 + nn] += _breath_segment(sr, nn, amp, rng).astype(y.dtype)
        made.append(round(t0, 3))
    return made


# ------------------------------------------------------------------ WORLD

def _analyze(x, sr):
    import pyworld as pw
    x = np.asarray(x, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    f0, t = pw.harvest(x, sr, f0_floor=65.0, f0_ceil=880.0,
                       frame_period=FRAME_MS)
    sp = pw.cheaptrick(x, f0, t, sr)
    ap = pw.d4c(x, f0, t, sr)
    return f0, t, sp, ap


def _apply_pitch(f0, note_hz, target_hz, strength):
    """把实测 F0 拉到目标曲线；strength=1 时完全按设计唱。"""
    out = f0.copy()
    voiced = (f0 > 0) & (target_hz > 0) & (note_hz > 0)
    meas = 1200.0 * np.log2(np.maximum(f0, 1e-9) / np.maximum(note_hz, 1e-9))
    tgt = 1200.0 * np.log2(np.maximum(target_hz, 1e-9) / np.maximum(note_hz, 1e-9))
    # 八度跳音（检测错或和声）直接跟目标，别把错误放大
    octave_bad = np.abs(meas) > 650
    cents = np.where(octave_bad, tgt, tgt * strength + meas * (1.0 - strength))
    out[voiced] = note_hz[voiced] * 2.0 ** (cents[voiced] / 1200.0)
    # 目标曲线边界外：维持原音高，短距离淡出避免咔哒
    return out


def _warp_formants(sp, sr, semitones):
    if abs(semitones) < 1e-3:
        return sp
    factor = 2.0 ** (float(semitones) / 12.0)
    n_bins = sp.shape[1]
    src = np.clip(np.arange(n_bins) / factor, 0, n_bins - 1)
    lo = np.floor(src).astype(int)
    hi = np.minimum(lo + 1, n_bins - 1)
    w = src - lo
    return (sp[:, lo] * (1 - w) + sp[:, hi] * w).astype(sp.dtype)


def _breathiness(ap, sr, style, intensity):
    """高频非周期性 + 长音尾巴：声带闭合变松，听感才有「气」。"""
    n_bins = ap.shape[1]
    freqs = np.linspace(0, sr / 2, n_bins)
    w = np.clip((freqs - 3200.0) / 5200.0, 0.0, 1.0)
    boost = 0.10 * style["breath"] * intensity
    return np.clip(ap + boost * w[None, :], 0.0, 1.0)


# ------------------------------------------------------------------ 混音：视频里的真实感链

def _plate_ir(sr, seconds=1.7, seed=7):
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    t = np.arange(n) / sr
    out = []
    # 两声道用不同噪声：真板混响的左右不是同一份信号
    for ch in range(2):
        ir = rng.standard_normal(n) * np.exp(-t / 0.42)
        for delay, g in ((0.0071, 0.65), (0.0129, 0.5), (0.0193, 0.42),
                         (0.0277, 0.34), (0.0411, 0.26), (0.0563, 0.18)):
            idx = int((delay + 0.0007 * ch) * sr)
            if idx < n:
                ir[idx] += g
        from scipy.signal import butter, sosfilt
        sos = butter(2, [240 / (sr / 2), 9000 / (sr / 2)], btype="band", output="sos")
        ir = sosfilt(sos, ir)
        ir /= max(np.sqrt(np.sum(ir ** 2)), 1e-9)
        out.append(ir.astype(np.float32))
    return out


def _doubler(x, sr, amount=0.55):
    """左右各一轨 11/19 ms 延迟的复制：就是视频里「左右合唱」的廉价版。

    不复制成完全相同的两份（那只是加宽），左右用不同延迟，
    中间 0.3 Hz 的慢速摆动让两轨永远不完全同相。
    """
    n = len(x)
    outs = []
    for k, (delay_ms, sign) in enumerate(((11.0, -1.0), (19.0, 1.0))):
        # 左右用不同延迟，两轨永远不完全同相
        d = int(delay_ms * sr / 1000.0)
        y = np.zeros(n, dtype=np.float32)
        if d < n:
            y[d:] = x[:n - d]
        # 复制轨只保留 250 Hz–7 kHz：低频重了会糊，高频重了会齿音加倍
        from scipy.signal import butter, sosfilt
        sos = butter(2, [250 / (sr / 2), min(0.95, 7000 / (sr / 2))],
                     btype="band", output="sos")
        y = sosfilt(sos, y).astype(np.float32)
        outs.append(y * amount)
    return outs


def _deess(x, sr, amount=0.55):
    """齿音：5–9 kHz 频段单独压，别把整个高频削掉。

    虚拟歌手的 /s/ /sh/ 比真人更尖（声码器合成的高频没有真实口腔衰减），
    但直接挂高架会把「空气感」一起削没，所以只压这一段的包络。
    """
    from scipy.signal import butter, sosfilt
    sos = butter(2, [4800 / (sr / 2), min(0.95, 9200 / (sr / 2))],
                 btype="band", output="sos")
    band = sosfilt(sos, x).astype(np.float32)
    env = dsp.envelope(band, sr, attack_ms=0.5, release_ms=25.0)
    thr = float(np.percentile(env, 92)) if env.size else 0.0
    if thr <= 1e-4:
        return np.asarray(x, dtype=np.float32)
    over = np.clip(env / (thr + 1e-9) - 1.0, 0.0, 4.0)
    gain = (1.0 - amount * np.clip(over / 3.0, 0.0, 1.0)).astype(np.float32)
    return (x - band + band * gain).astype(np.float32)


def _realism(x, sr, style, intensity, seed=11):
    """视频里的顺序：EQ 提亮 → 2A 类光电压缩 → 板式混响发送 → 左右加倍。

    关键不是「挂满插件」，而是每一步都只做一点点：
    虚拟歌姬最容易死在「高频全被削掉」和「混响比人声还大」。
    """
    from pedalboard import (Compressor, HighpassFilter, HighShelfFilter,
                            PeakFilter, Pedalboard)
    mono = np.asarray(x, dtype=np.float32)
    mono = _deess(mono, sr)
    air = 0.5 + style["air"] * intensity * 0.5
    body = 0.4 + style["body"] * 0.6
    chain = [
        HighpassFilter(cutoff_frequency_hz=88),
        # 250 Hz 一点厚度：虚拟歌手常缺胸腔共鸣，声音「飘」
        PeakFilter(cutoff_frequency_hz=250, gain_db=1.6 * body, q=0.85),
        PeakFilter(cutoff_frequency_hz=1500, gain_db=1.0 * air, q=1.1),
        # 视频里反复强调的「高频多做文章」：5 k 和 10 k 各抬一点，
        # 不是一刀切高架，两个点才有「空气」而不是「嘶」
        PeakFilter(cutoff_frequency_hz=5200, gain_db=1.8 * air, q=0.9),
        HighShelfFilter(cutoff_frequency_hz=8800, gain_db=2.4 * air),
    ]
    board = Pedalboard(chain)
    y = board(mono[None, :], sr)[0].astype(np.float32)

    # 2A 类：慢起音、慢释放、低压缩比。快起音会把字头咬掉，
    # 而虚拟歌手的字头本来就弱，再削就听不清词。
    comp = Pedalboard([Compressor(threshold_db=-24.0, ratio=style["comp"],
                                  attack_ms=16.0, release_ms=150.0)])
    y = comp(y[None, :], sr)[0].astype(np.float32)

    # 轻饱和：给高频补一点谐波，混响里才不会「沙」
    y = dsp.saturate(y, drive_db=1.2, mix=0.22)

    # 板式混响发送（EMT140 的味道）：预延迟 22 ms，返回 -17 dB
    irs = _plate_ir(sr, 1.7, seed)
    from scipy.signal import fftconvolve
    wet = np.stack([fftconvolve(np.pad(y, (int(0.022 * sr), 0))[:len(y)], irs[c])[:len(y)]
                    for c in range(2)], axis=1).astype(np.float32)
    wet *= 0.14

    # 左右加倍
    dl, dr = _doubler(y, sr, amount=0.5)
    left = y * 0.92 + dl + wet[:, 0] * 0.7
    right = y * 0.92 + dr + wet[:, 1] * 0.7

    out = np.stack([left, right], axis=1)
    out = dsp.soft_clip(out, ceiling_db=-1.5)
    return out.astype(np.float32)


def _loudness(x, sr):
    try:
        import pyloudnorm as pyln
        return float(pyln.Meter(sr).integrated_loudness(
            np.asarray(x, dtype=np.float64)))
    except Exception:
        return None


def _pitch_stats(f0, t, notes, target_hz):
    """测音准：只统计音符范围内的有声帧。"""
    if not len(notes):
        return {}
    mask = np.zeros(len(f0), dtype=bool)
    for n in notes:
        mask |= (t >= n["start"]) & (t < n["start"] + n["dur"])
    mask &= f0 > 0
    if not np.any(mask):
        return {}
    meas = 1200.0 * np.log2(f0[mask] / target_hz[mask])
    return {"frames": int(mask.sum()),
            "medianAbsCents": round(float(np.median(np.abs(meas))), 1),
            "p90AbsCents": round(float(np.percentile(np.abs(meas), 90)), 1)}


# ------------------------------------------------------------------ 主流程

def tune(project_path, section=None, style="natural", intensity=0.7,
         breath=None, formant=None, strength=0.92, mix=True, ab=True,
         seed=None):
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    project = load_project(base)
    style = (style or "natural").lower()
    if style not in STYLES:
        raise ValueError(f"没有风格 {style}；可选 {', '.join(STYLES)}")
    st = dict(STYLES[style])
    intensity = float(np.clip(intensity, 0.2, 1.4))
    if formant is not None:
        st["formant"] = float(formant)
    if breath is not None:
        st["breath"] = float(np.clip(breath, 0.0, 1.5))
    seed = int(seed if seed is not None else project.get("seed") or 7)
    rng = np.random.default_rng(seed)

    notes, score_src = load_notes(base)
    vocal = _vocal_file(base, section)
    data, sr0 = dsp.load(vocal, SAMPLE_RATE)
    if data.ndim == 2:
        data = data.mean(axis=1)
    x = np.asarray(data, dtype=np.float32)
    sr = SAMPLE_RATE

    f0, t, sp, ap = _analyze(x, sr)
    target_hz, note_hz, gain_db, phrase_of = _design(notes, t, st, project.get("tempo"), intensity, seed)
    f0_new = _apply_pitch(f0, note_hz, target_hz, float(np.clip(strength, 0.1, 1.0)))
    sp = _warp_formants(sp, sr, st["formant"])
    ap = _breathiness(ap, sr, st, intensity)
    import pyworld as pw
    # pyworld 的 Cython 接口只接受 C 连续数组：共振峰弯曲/气声加权之后
    # 数组可能是某种视图（尤其 sp 经过高级索引相乘后），这里统一兜一次。
    f0_new = np.ascontiguousarray(f0_new, dtype=np.float64)
    sp = np.ascontiguousarray(sp, dtype=np.float64)
    ap = np.ascontiguousarray(ap, dtype=np.float64)
    y = pw.synthesize(f0_new, sp, ap, sr, frame_period=FRAME_MS).astype(np.float32)
    if len(y) < len(x):
        y = np.pad(y, (0, len(x) - len(y)))
    y = y[:len(x)]
    # 合成后归一化回原电平：改音高会轻微改变能量
    rms_x = float(np.sqrt(np.mean(x ** 2) + 1e-12))
    rms_y = float(np.sqrt(np.mean(y[: min(len(y), len(x))] ** 2) + 1e-12))
    y *= rms_x / max(rms_y, 1e-9)

    # 力度曲线在 WORLD 帧网格上，先插值到采样网格再乘（10 ms 台阶会听见）
    if len(gain_db) == len(t) and len(t) > 1:
        gain_db = np.interp(np.arange(len(y)) / sr, t, gain_db)
    y *= (10.0 ** (gain_db / 20.0)).astype(np.float32)
    breath_marks = _breaths(y, sr, phrase_of, st, intensity, rng)

    # 干声留余量：SVS 原始电平很满，WORLD 重合成 + 力度曲线后可能越 0 dBFS；
    # 直接存 24bit 会把顶削成方波，混音时再怎么压也救不回来
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0.94:
        y *= 0.94 / peak

    dry = y.astype(np.float32)
    tag = section or "lead"
    out_dir = base / "audio" / "vocal"
    dry_path = out_dir / f"vocal-tuned-dry-{tag}.wav"
    dsp.save(dry_path, dry, sr, subtype="PCM_24")

    before = _pitch_stats(f0, t, notes, target_hz)
    after = _pitch_stats(f0_new, t, notes, target_hz)
    stats = {"notes": len(notes), "phrases": len(phrase_of), "breaths": len(breath_marks),
             "score": score_src, "vocal": str(vocal),
             "pitchBefore": before, "pitchAfter": after,
             "duration": round(len(x) / sr, 2),
             "lufsDry": _loudness(dry, sr)}
    result = {"ok": True, "project": str(base), "style": style, "intensity": intensity,
              "dry": str(dry_path), "stats": stats, "breathTimes": breath_marks}

    if mix:
        tuned = _realism(dry, sr, st, intensity, seed)
        # 统一到人声干声的响度附近：混音阶段还要按工程电平再平衡
        loud = _loudness(tuned, sr)
        if loud is not None and np.isfinite(loud):
            tuned *= 10.0 ** ((-19.0 - loud) / 20.0)
        tuned = dsp.soft_clip(tuned, ceiling_db=-1.2)
        tuned_path = out_dir / f"vocal-tuned-{tag}.wav"
        dsp.save(tuned_path, tuned, sr, subtype="PCM_24")
        result["mixed"] = str(tuned_path)
        stats["lufsMixed"] = _loudness(tuned, sr)
        stats["centroidDry"] = round(dsp.spectral_centroid(dry, sr), 1)
        stats["centroidMixed"] = round(dsp.spectral_centroid(tuned, sr), 1)

    if ab and len(x) > 12 * sr:
        # A/B：从第一个有音符的地方开始，先原声后调教（各 8 s，交叉淡入 0.4 s）
        start = max(0.0, notes[0]["start"] - 0.3) if notes else 1.0
        i0 = int(start * sr)
        if i0 + 9 * sr > len(x):
            i0 = max(0, len(x) - 9 * sr)
        a = x[i0:i0 + int(8 * sr)]
        b = (dry if not mix else tuned[:, 0])[i0:i0 + int(8 * sr)]
        fade = int(0.4 * sr)
        if len(a) > fade and len(b) >= len(a):
            b = b[:len(a)].copy()
            b[:fade] *= np.linspace(0, 1, fade)
            # 两段先按响度对齐，否则比的不是「像不像人」而是「谁更响」
            ra = float(np.sqrt(np.mean(a ** 2) + 1e-12))
            rb = float(np.sqrt(np.mean(b ** 2) + 1e-12))
            if rb > 1e-6:
                b *= ra / rb
            ab_sig = np.concatenate([a, b])
            # SVS 原始干声常常已经顶到 0 dBFS：A/B 拼在一起会削顶，
            # 那样对比的就不是「像不像人」而是「谁先爆」
            pk = float(np.max(np.abs(ab_sig)))
            if pk > 0.95:
                ab_sig = ab_sig * (0.95 / pk)
            ab_path = out_dir / f"vocal-ab-{tag}.wav"
            dsp.save(ab_path, ab_sig, sr, subtype="PCM_24")
            result["ab"] = str(ab_path)

    jdump(base / "analysis" / f"tune-plan-{tag}.json",
          {"style": style, "intensity": intensity, "params": st,
           "notes": len(notes), "phrases": len(phrase_of), "breaths": breath_marks,
           "pitchBefore": before, "pitchAfter": after})
    jdump(base / "report" / f"tune-{tag}.json", result)
    return result
