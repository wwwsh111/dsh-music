"""人声体检与修补（vocaldoctor）。

参考「AI 翻唱」后期的手工流程，把这些听感问题变成可测的指标：

* 哑音起音（第一个音节没声 / 咬字被吞）——音符开头 60 ms 的能量比音身低太多；
* 混响造成的哑音——长音中段被残响/重叠音盖住，出现 40 ms 以上的能量坑；
* 重叠音——上一个音还没收尾，下一个音已经开口，两个元音叠在一起；
* 拖尾——音结束后还有明显的残响，进下一句时糊成一片；
* 缺音（补音）——同一句里两个音之间空出 60–300 ms，本该连起来；
* 走音——音身里实测 F0 的中心偏离谱面音高超过阈值（改调的候选）。

前五类能用 DSP 直接修（切尾、去重叠、补气口、拉音高）；起音哑音没法凭空
造出辅音，只能给出「改谱重唱」的建议：把该音提前 40 ms、拉长 40 ms 再唱一遍。

所有阈值都在报告里，不猜：扫出来的每个问题都带实测 dB/音分。
"""

from pathlib import Path

import numpy as np

from . import tune
from .config import SAMPLE_RATE
from .project import load_project, save_project
from .util import jdump, jload

HOP_MS = 10
FLOOR_DB = -48.0


# ---------------------------------------------------------------- 读入

def _base(project_path):
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    return base


def _load_vocal(base, section=None, vocal=None):
    from . import dsp
    if vocal:
        path = Path(vocal)
        if not path.is_file():
            path = base / vocal
        if not path.is_file():
            raise ValueError(f"找不到人声文件: {vocal}")
    else:
        path = tune._vocal_file(base, section)
    data, sr = dsp.load(path, sr=SAMPLE_RATE)
    if data.ndim == 1:
        data = data[:, None]
    return data.astype("float32"), sr, path


def _frame_db(x, sr):
    """10 ms 一帧的 RMS（dBFS），长度对齐 tune 的帧率。"""
    mono = x.mean(axis=1) if x.ndim > 1 else x
    hop = int(sr * HOP_MS / 1000)
    n = len(mono) // hop
    if n <= 0:
        return np.array([FLOOR_DB])
    rms = np.sqrt((mono[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    return 20.0 * np.log10(np.maximum(rms, 1e-12))


def _rel_db(level, ref):
    """相对电平（dB），两者都是线性 RMS。"""
    return 20.0 * float(np.log10(max(level, 1e-9) / max(ref, 1e-9)))


# ---------------------------------------------------------------- 扫描

def _plan_from_alignment(base):
    align = jload(base / "analysis" / "vocal-align.json", None)
    if not align or not align.get("notes"):
        align = jload(base / "analysis" / "vocal-harmony-align.json", None)
    return align


def scan(project_path, section=None, vocal=None, pitch_cents=45.0,
         range_min=50, range_max=86):
    """逐音体检。返回问题清单，不改任何文件。

    起音先按「比音身峰值低 12 dB 的第一帧」量出实际开口时间，再用全曲中位数
    校正整体对齐偏差——否则一个系统性的 40 ms 位移会让每个音都被判成哑音。
    """
    base = _base(project_path)
    data, sr, path = _load_vocal(base, section, vocal)
    notes, src = tune.load_notes(base)
    f0, t, sp, ap = tune._analyze(data, sr)
    db = _frame_db(data, sr)
    nframes = len(db)
    total = nframes * HOP_MS / 1000.0

    def frames(t0, t1):
        a = max(0, int(round(t0 * 1000.0 / HOP_MS)))
        b = min(nframes, int(round(t1 * 1000.0 / HOP_MS)))
        return a, max(a + 1, b)

    def rms_range(t0, t1):
        a, b = frames(t0, t1)
        seg = db[a:b]
        return 10.0 ** (float(seg.mean()) / 20.0) if len(seg) else 1e-6

    rows = []
    for i, n in enumerate(notes):
        s, e = n["start"], n["start"] + n["dur"]
        dur = float(n["dur"])
        a, b = frames(s, e)
        span = db[a:b]
        body_peak = float(span.max()) if len(span) else FLOOR_DB
        # 音身参考：中间 60% 的均值（去掉起音与收尾）
        ca, cb = frames(s + 0.2 * dur, e - 0.2 * dur)
        body = 10.0 ** (float(db[ca:cb].mean()) / 20.0)

        # 实际开口时间：越过「峰值 - 10 dB」并连续 3 帧保持的第一帧。
        # 窗口从音符前 20 ms 开始，且「一上来就已经在响」记 0——连音/气口接得
        # 上的音不该被判成哑音，只有真的晚开口才算。
        thresh = body_peak - 10.0
        wa, wb = frames(s - 0.02, s + 0.32)
        seg = db[wa:wb]
        attack = 0.9
        if len(seg) >= 4:
            above = seg > thresh
            run = np.convolve(above.astype(int), np.ones(3, dtype=int), mode="valid")
            idx = np.where(run >= 3)[0]
            if len(idx):
                j = int(idx[0])
                attack = max(-0.02, (wa + j) * HOP_MS / 1000.0 - s)
            if above[0] and above[1]:
                attack = 0.0

        # 音身能量坑（混响/重叠造成的哑音）
        dip_db, dip_ms = 0.0, 0
        if dur >= 0.32:
            da, db_ = frames(s + 0.15 * dur, e - 0.10 * dur)
            seg = db[da:db_]
            if len(seg) >= 8:
                win = 4
                mins = np.array([seg[j:j + win].mean() for j in range(0, len(seg) - win)])
                if len(mins):
                    dip_db = float(mins.min()) - float(seg.max())
                    dip_ms = int(win * HOP_MS) if dip_db < -10.0 else 0

        tail = rms_range(e + 0.012, min(e + 0.20, total - 0.01)) if e + 0.05 < total else 0.0

        # 音高：只看音身中段，避开滑音与颤音边缘
        fa, fb = frames(max(s, s + 0.15 * dur), min(e, s + 0.9 * dur))
        seg_f0 = f0[fa:fb]
        voiced = seg_f0[seg_f0 > 0]
        cents = None
        if len(voiced) >= 5:
            hz = 440.0 * 2.0 ** ((n["midi"] - 69) / 12.0)
            cents = float(1200.0 * np.log2(np.median(voiced) / hz))

        prev_end = notes[i - 1]["start"] + notes[i - 1]["dur"] if i else 0.0
        nxt = notes[i + 1] if i + 1 < len(notes) else None
        gap = (nxt["start"] - e) if nxt else None
        gap_db = None
        if gap is not None and 0.03 <= gap <= 0.32:
            gap_db = _rel_db(rms_range(e, nxt["start"]), max(body, 1e-6))

        rows.append({
            "index": i, "char": n.get("char") or "", "section": n.get("section"),
            "line": n.get("line"), "start": round(s, 3), "dur": round(dur, 3),
            "midi": n["midi"], "pitch": _note_name(n["midi"]),
            "bodyDb": round(20.0 * np.log10(max(body, 1e-9)), 1),
            "peakDb": round(body_peak, 1),
            "attackMs": int(round(attack * 1000)),
            "tailDb": round(_rel_db(tail, max(body, 1e-6)), 1),
            "dipDb": round(dip_db, 1), "dipMs": dip_ms,
            "cents": None if cents is None else round(cents, 1),
            "overlapMs": int(round((prev_end - s) * 1000)) if prev_end > s + 0.05 else 0,
            "gapMs": int(round(gap * 1000)) if gap is not None and 0 <= gap < 1.0 else None,
            "gapDb": None if gap_db is None else round(gap_db, 1)})

    # 整体对齐偏差：全曲起音中位数（只统计够长的音）
    long_notes = [r["attackMs"] for r in rows if r["dur"] >= 0.18 and r["attackMs"] < 800]
    bias = float(np.median(long_notes)) if long_notes else 0.0
    if abs(bias) > 80:
        bias = 0.0

    issues = []
    for row in rows:
        i = row["index"]
        late = row["attackMs"] - bias
        dur = row["dur"]

        def add(kind, sev, detail, action, **extra):
            it = {"index": i, "char": row["char"], "start": row["start"],
                  "section": row["section"], "kind": kind, "severity": sev,
                  "detail": detail, "action": action}
            it.update(extra)
            issues.append(it)

        if row["bodyDb"] < FLOOR_DB:
            add("silent", "info", f"音身 {row['bodyDb']} dBFS 基本没声", "resing")
            continue
        if dur >= 0.18 and late > 75:
            # 75–120 ms 只是「比别人晚一点」，多半是慢起音，标 info；
            # 超过 120 ms 才是真的吞字（一个十六分音符都快到了），标 warn，
            # 计进「必须重唱」。两个档分开，gate 只对后者 FAIL。
            add("dead-onset", "warn" if late > 120 else "info",
                f"开口比谱面晚 {int(late)} ms（哑音/吞字）", "resing",
                lateMs=int(late))
        if row["dipMs"] >= 50 and row["dipDb"] <= -15.0:
            add("dead-body", "warn",
                f"音身中段掉了 {abs(row['dipDb']):.0f} dB / {row['dipMs']} ms", "debleed")
        if row["tailDb"] > -14.0 and (row["gapMs"] is None or row["gapMs"] > 100):
            # 音间区如果本来就不安静（gapDb 高），响的是下一个音的慢起音，不是本音拖尾；
            # 那种情况要按 dead-onset 处理，去切本音尾巴反而会把这个音的收尾切坏。
            if row["gapDb"] is None or row["gapDb"] < -12.0:
                add("tail", "warn", f"音尾残响 {row['tailDb']:.0f} dB", "tail")
        if row["overlapMs"] > 50:
            add("overlap", "warn", f"与上一音重叠 {row['overlapMs']} ms", "cut")
        if row["gapMs"] is not None and 50 <= row["gapMs"] <= 140 and \
                row["gapDb"] is not None and row["gapDb"] < -30.0:
            add("gap", "info", f"句内空档 {row['gapMs']} ms / {row['gapDb']:.0f} dB",
                "fill")
        if row["cents"] is not None and abs(row["cents"]) > pitch_cents:
            add("off-pitch", "warn", f"偏离谱面 {row['cents']:+.0f} 音分", "pitch")
        if row["midi"] < range_min or row["midi"] > range_max:
            add("out-of-range", "info",
                f"{_note_name(row['midi'])} 超出常用音域 {_note_name(range_min)}–"
                f"{_note_name(range_max)}", "transpose")

    counts = {}
    severe = {}
    for it in issues:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
        if it.get("severity") == "warn":
            severe[it["kind"]] = severe.get(it["kind"], 0) + 1
    out = {"ok": True, "vocal": str(path), "score": src, "notes": len(notes),
           "seconds": round(total, 2), "alignmentBiasMs": int(bias),
           "issues": issues, "counts": counts, "countsSevere": severe, "rows": rows,
           "resing": [it for it in issues if it["action"] == "resing"]}
    tag = section or "lead"
    jdump(base / "analysis" / f"vocal-doctor-{tag}.json", out)
    return out


def _note_name(midi):
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    m = int(midi)
    return f"{names[m % 12]}{m // 12 - 1}"


# ---------------------------------------------------------------- 修补

def _fill_gap(x, sr, t0, t1, src_len=0.10, level=None):
    """用音符尾巴最后一小段补气口：交叉淡入淡出，避免咔哒。

    level 是目标 RMS：尾巴本身是衰减段，直接复制会把缺口补成「还是很安静」，
    复扫仍是 gap。给了 level 就把补进去的片段拉到该电平（默认目标 = 音身 -6 dB）。
    """
    a, b = int(t0 * sr), int(t1 * sr)
    fill = b - a
    if fill <= 0:
        return x
    n = max(64, int(src_len * sr))
    start = max(0, a - n)
    seg = x[start:a].copy()
    if len(seg) < 64:
        return x
    # 循环铺满 + 首尾 8 ms 交叉淡化
    reps = int(np.ceil(fill / len(seg))) + 1
    body = np.tile(seg, (reps, 1))[:fill] if seg.ndim > 1 else np.tile(seg, reps)[:fill]
    if level:
        cur = float(np.sqrt(np.mean((body.mean(axis=1) if body.ndim > 1 else body) ** 2) + 1e-12))
        if cur > 1e-6:
            body = body * float(np.clip(level / cur, 0.25, 12.0))
    fade = max(8, int(0.008 * sr))
    if fade * 2 < fill:
        ramp = np.linspace(0.0, 1.0, fade)[:, None] if body.ndim > 1 else np.linspace(0.0, 1.0, fade)
        body[:fade] *= ramp
        body[-fade:] *= ramp[::-1]
    out = x.copy()
    out[a:b] = body
    return out


def _fade_out(x, sr, t0, t1):
    a, b = max(0, int(t0 * sr)), min(len(x), int(t1 * sr))
    if b - a < 16:
        return x
    out = x.copy()
    env = np.linspace(1.0, 0.0, b - a) ** 1.5
    out[a:b] *= env[:, None] if out.ndim > 1 else env
    return out


def _splice_pitch(x, y, notes, targets, sr, pad=0.03, fade_ms=15):
    """把重合成的音窗拼回原音频：只动改过的音，并按原响度包络回贴。

    WORLD 重合成会磨钝起音、把音间气口压深，整段替换会连带把没问题的音也弄哑；
    所以只替换目标音窗，且用原音频的逐帧 RMS 校正合成段的包络（限制在 ±6 dB）。
    """
    out = x.copy()
    mono_out = x.mean(axis=1) if x.ndim > 1 else x
    hop = max(1, int(sr * HOP_MS / 1000.0))
    fade = max(8, int(sr * fade_ms / 1000.0))
    for it in targets:
        n = notes[it["index"]]
        a = max(0, int((n["start"] - pad) * sr))
        b = min(len(y), int((n["start"] + n["dur"] + pad) * sr))
        if b - a < 4 * hop:
            continue
        seg_y = y[a:b].astype("float32")
        env_x = _frame_rms(mono_out[a:b], hop)
        env_y = _frame_rms(seg_y, hop)
        if env_x is None or env_y is None:
            continue
        gain = np.clip((env_x + 1e-6) / (env_y + 1e-6), 0.5, 2.0)
        gain = np.repeat(gain, hop)[: b - a]
        if len(gain) < b - a:
            gain = np.pad(gain, (0, b - a - len(gain)), mode="edge")
        seg_y = seg_y * gain
        # 两端交叉淡化，避免拼缝咔哒
        k = min(fade, (b - a) // 3)
        if k > 1:
            ramp = np.linspace(0.0, 1.0, k, dtype="float32")
            seg_y[:k] *= ramp
            seg_y[-k:] *= ramp[::-1]
        if out.ndim > 1:
            for ch in range(out.shape[1]):
                src = out[a:b, ch].copy()
                blend = src.copy()
                if k > 1:
                    blend[:k] = src[:k] * (1 - ramp) + seg_y[:k] * ramp
                    blend[-k:] = src[-k:] * ramp + seg_y[-k:] * (1 - ramp)
                    blend[k:-k] = seg_y[k:-k]
                else:
                    blend = seg_y
                out[a:b, ch] = blend
        else:
            src = out[a:b].copy()
            blend = src.copy()
            if k > 1:
                blend[:k] = src[:k] * (1 - ramp) + seg_y[:k] * ramp
                blend[-k:] = src[-k:] * ramp + seg_y[-k:] * (1 - ramp)
                blend[k:-k] = seg_y[k:-k]
            else:
                blend = seg_y
            out[a:b] = blend
    return out


def _frame_rms(seg, hop):
    """按 hop 取 RMS 包络；段太短返回 None。"""
    n = int(len(seg) // hop)
    if n < 2:
        return None
    body = seg[: n * hop].astype("float32")
    if body.ndim > 1:
        body = body.mean(axis=1)
    return np.sqrt(np.mean(body.reshape(n, hop) ** 2, axis=1) + 1e-12)


def fix(project_path, section=None, vocal=None, actions=None, dry_run=False,
        pitch_strength=0.9, pitch_cents=45.0):
    """按体检结果修补：拉音高（WORLD 重合成）→ 切残响 → 剪重叠 → 补缺口。"""
    from . import dsp

    base = _base(project_path)
    report = scan(project_path, section=section, vocal=vocal, pitch_cents=pitch_cents)
    want = set(actions or ["pitch", "tail", "cut", "fill", "debleed"])
    todo = [it for it in report["issues"] if it["action"] in want
            and it["action"] != "resing"]
    data, sr, path = _load_vocal(base, section, vocal)
    x = data.copy().astype("float32")
    try:
        lufs_before = float(dsp.lufs(x))
    except Exception:
        lufs_before = None
    notes, _src = tune.load_notes(base)
    done = {"pitch": 0, "tail": 0, "overlap": 0, "gap": 0, "debleed": 0}

    # 1) 走音：改 F0 后整段重合成（只做一次，避免多次 WORLD 往返掉音质）
    if "pitch" in want:
        targets = [it for it in report["issues"] if it["action"] == "pitch"]
        if targets:
            f0, t, sp, ap = tune._analyze(x, sr)
            for it in targets:
                n = notes[it["index"]]
                hz = 440.0 * 2.0 ** ((n["midi"] - 69) / 12.0)
                a = int(n["start"] * 1000.0 / HOP_MS)
                b = int((n["start"] + n["dur"]) * 1000.0 / HOP_MS)
                a, b = max(0, a), min(len(f0), b)
                if b - a < 3:
                    continue
                seg = f0[a:b]
                voiced = seg > 0
                if np.count_nonzero(voiced) < 5:
                    continue
                med = float(np.median(seg[voiced]))
                shift = float(np.log2(hz / med) * 12.0)
                if abs(shift) > 1.0:
                    shift = 0.0 if abs(shift) > 7.0 else shift
                if abs(shift) * 100.0 < pitch_cents * 0.5:
                    continue
                new = seg.copy()
                new[voiced] *= 2.0 ** (shift * float(pitch_strength) / 12.0)
                fade = min(5, max(1, (b - a) // 6))
                w = np.linspace(0.0, 1.0, fade)
                new[:fade] = seg[:fade] * (1 - w) + new[:fade] * w
                new[-fade:] = seg[-fade:] * w + new[-fade:] * (1 - w)
                f0[a:b] = new
                done["pitch"] += 1
            if done["pitch"]:
                import pyworld as pw
                mono = x.mean(axis=1) if x.ndim > 1 else x
                y = pw.synthesize(f0, sp, ap, sr, HOP_MS).astype("float32")
                y = dsp.fit_length(y, len(mono))
                # 只把改过的音窗拼回去，并且按原音频的响度包络回贴。
                # 整段换掉（原来的做法）会把 SVS 的起音磨钝、音间气口压深——
                # 实测同一轨：全段替换后 dead-onset 40→45、gap 20→26；
                # 改成按音窗拼接 + 包络保持后，两者都不再变差。
                x = _splice_pitch(x, y, notes, targets, sr)

    # 2) 切残响 / 剪重叠 / 补缺口：时域编辑，逐通道做
    for it in todo:
        n = notes[it["index"]]
        s, e = n["start"], n["start"] + n["dur"]
        nxt = notes[it["index"] + 1] if it["index"] + 1 < len(notes) else None
        if it["action"] == "tail":
            limit = (nxt["start"] - 0.02) if nxt else (e + 0.16)
            x = _fade_out(x, sr, max(s + 0.03, e - 0.006), min(limit, e + 0.16))
            done["tail"] += 1
        elif it["action"] == "cut" and nxt:
            x = _fade_out(x, sr, max(s, nxt["start"] - 0.035), nxt["start"])
            done["overlap"] += 1
        elif it["action"] == "debleed":
            x = _fade_out(x, sr, max(s, e - 0.05), min(e + 0.02, (nxt["start"] if nxt else e + 0.05)))
            done["debleed"] += 1
        elif it["action"] == "fill" and nxt:
            row = next((r for r in report["rows"] if r["index"] == it["index"]), None)
            lvl = 10.0 ** ((row["bodyDb"] - 6.0) / 20.0) if row else None
            x = _fill_gap(x, sr, e, nxt["start"], level=lvl)
            done["gap"] += 1

    # 修补后把整体响度拉回原样，别让「修一个音」顺手改掉整轨的电平
    if lufs_before is not None:
        try:
            gain_db = max(-3.0, min(3.0, lufs_before - float(dsp.lufs(x))))
            if abs(gain_db) > 0.05:
                x = x * (10.0 ** (gain_db / 20.0))
        except Exception:
            pass

    tag = section or "lead"
    out = base / "audio" / "vocal" / f"vocal-repair-{tag}.wav"
    if not dry_run:
        dsp.save(out, x, sr=sr)
        report["output"] = str(out)
        jdump(base / "report" / f"vocal-doctor-{tag}.json",
              {"done": done, "issues": len(report["issues"]), "output": str(out),
               "resing": report["resing"]})
    report["done"] = done
    report["dryRun"] = bool(dry_run)
    return report


# ---------------------------------------------------------------- 改谱建议

def suggest_score(project_path, section=None, apply=False):
    """起音哑音只能靠重唱：给出「提前 40 ms、拉长 40 ms」的谱面修改。"""
    base = _base(project_path)
    rep = scan(project_path, section=section)
    picks = [it for it in rep["issues"] if it["action"] == "resing"]
    tag = section or "lead"
    plan = []
    for it in picks:
        plan.append({"index": it["index"], "char": it["char"],
                     "start": round(max(0.0, it["start"] - 0.04), 3),
                     "note": "起音哑音：把音符提前 40 ms，时值 +40 ms 再唱"})
    jdump(base / "analysis" / f"vocal-fix-score-{tag}.json", {"notes": plan})
    if not apply or not plan:
        return {"ok": True, "suggestions": plan, "applied": 0}
    align_path = base / "analysis" / "vocal-align.json"
    align = jload(align_path, {"notes": []})
    by_char = {}
    for n in align.get("notes", []):
        by_char.setdefault(n.get("char"), []).append(n)
    applied = 0
    for it in plan:
        cands = by_char.get(it["char"]) or []
        for n in cands:
            if abs(float(n.get("start", -9)) - it["start"] - 0.04) < 0.2:
                n["start"] = round(max(0.0, float(n["start"]) - 0.04), 3)
                n["duration"] = round(float(n.get("duration", 0.2)) + 0.04, 3)
                applied += 1
                break
    if applied:
        jdump(align_path, align)
    return {"ok": True, "suggestions": plan, "applied": applied}
