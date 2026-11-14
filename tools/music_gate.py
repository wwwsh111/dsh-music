#!/usr/bin/env python3
"""music_gate.py —— dsh-music 编曲流程的验收闸门（离线，不需要引擎在线）。

它只做一件事：把一个工程目录里的真实产物（project.json / midi / report/*.json）
翻出来，按 S2–S8 逐条核对 PIPELINE.md 的验收阈值，打印 实测值 → 门限 → 结论，
最后给出「下一步该回哪个阶段」。PASS 只给有依据的项；没数据的一律 `—`，不猜。

用法：
  python tools/music_gate.py <工程目录>              # 单工程详细报告
  python tools/music_gate.py <工程目录> --json       # 机器可读
  python tools/music_gate.py <工程目录> --stage S4,S7
  python tools/music_gate.py --all [--root <工程根>]  # 全部工程一行摘要

退出码：0 全部通过（允许 WARN）/ 1 有 FAIL / 2 用法或数据错误。

依赖：标准库 + mido + numpy + soundfile（都在引擎 venv 里）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import unicodedata
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---- 门限：改这里就是改 SOP，PIPELINE.md §4 与它一一对应 ----
TH = {
    "peak_max": 0.0,          # 分轨峰值上限 dBFS（超过即离削波只差一步）
    "rms_tol": 1.5,           # 分轨 RMS 与目标差 dB
    # 唯一小节占比不设门限：它是「复制粘贴检测」，不是质量分。见 S4 那一行。
    "tune_cents": 12.0,       # 调教后音准中位绝对偏差 cents
    "tune_cents_warn": 20.0,
    "breath_ratio": 0.80,     # 气口数 / 乐句数
    "ab_lu": 1.0,             # A/B 响度差
    "mix_clip": 0,            # 混音削波样本数
    "mix_tp": -1.0,           # 混音真峰值 dBTP
    "mix_crest": 7.0,
    "mix_silence": 0.05,      # 静音占比
    "vocal_pan": 0.15,        # 主唱偏离中轴
    "lufs_tol": 0.5,          # 母带 LUFS 与目标差
    "master_tp": -1.0,
    "master_crest": 7.0,
    "vocal_grid_ms": 40.0,
}

OK, WARN, FAIL, NA = "PASS", "WARN", "FAIL", "—"


class Row:
    __slots__ = ("stage", "item", "value", "limit", "verdict", "hint")

    def __init__(self, stage, item, value, limit, verdict, hint=""):
        self.stage, self.item, self.value, self.limit = stage, item, value, limit
        self.verdict, self.hint = verdict, hint

    def as_dict(self):
        return {"stage": self.stage, "item": self.item, "value": self.value,
                "limit": self.limit, "verdict": self.verdict, "hint": self.hint}


def grade(x, limit, *, warn=None, higher_is_better=False, band=False):
    """按门限判定。band=True 表示 |x| ≤ limit。"""
    if x is None:
        return NA
    if band:
        if abs(x) <= limit:
            return OK
        return WARN if warn is not None and abs(x) <= warn else FAIL
    if higher_is_better:
        if x >= limit:
            return OK
        return WARN if warn is not None and x >= warn else FAIL
    if x <= limit:
        return OK
    return WARN if warn is not None and x <= warn else FAIL


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def num(x, nd=1, unit=""):
    if x is None:
        return "—"
    if isinstance(x, bool):
        return ("是" if x else "否") + unit
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not math.isfinite(v):
        return "—"
    return f"{v:.{nd}f}{unit}"


# ---------------- 唯一小节占比：从 MIDI 直接算，不靠报告字段 ----------------
def bar_stats(mid_path: Path) -> tuple[float, float] | None:
    """返回 (唯一小节占比, 每小节音符数中位数)。

    占比 = 去重后的小节数 / 总小节数。它的含义是「有没有整段照抄」：
      接近 1.0 → 每个小节都不一样（可能是每小节重新写，也可能缺动机记忆点）
      接近 0.1 → 整段在复制（lo-fi/极简编曲的正常写法，也可能是没发展）
    两头都不算错，所以只报数不下判决，不设门限。
    """
    try:
        import mido
    except ImportError:
        return None
    if not mid_path.exists():
        return None
    try:
        mid = mido.MidiFile(str(mid_path))
    except Exception:
        return None
    tpb = mid.ticks_per_beat or 480
    sigs: dict[int, list] = {}
    per_bar_count: dict[int, int] = {}
    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                bar = tick // (tpb * 4)
                pos = round((tick % (tpb * 4)) / tpb, 2)
                sigs.setdefault(bar, []).append((msg.note, pos))
                per_bar_count[bar] = per_bar_count.get(bar, 0) + 1
    if not sigs:
        return None
    prof = {b: tuple(sorted(v)) for b, v in sigs.items()}
    counts = sorted(per_bar_count.values())
    median_n = counts[len(counts) // 2] if counts else 0
    return len(set(prof.values())) / len(prof), float(median_n)


def lyric_lines(pj: dict, proj: Path):
    """歌词句数。lyrics.json 的真实结构是 {language,text,sections,warnings,totalSyllables}，
    sections 是 [{section, lines:[{text,syllables,suggestedBars}], syllables}]。"""
    ref = (pj.get("vocal") or {}).get("lyrics")
    if not ref:
        return None, None
    path = proj / ref
    if not path.exists():
        return None, ref
    if path.suffix.lower() == ".json":
        data = read_json(path)
        if isinstance(data, list):
            return len(data), ref
        if isinstance(data, dict):
            for key in ("lines", "lyrics", "sentences"):
                if isinstance(data.get(key), list):
                    return len(data[key]), ref
            secs = data.get("sections")
            if isinstance(secs, list) and secs:
                n = 0
                for s in secs:
                    if isinstance(s, dict) and isinstance(s.get("lines"), list):
                        n += len(s["lines"])
                    elif isinstance(s, str):
                        n += 1
                if n:
                    return n, ref
            text = data.get("text")
            if isinstance(text, str):
                n = len([ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("[")])
                if n:
                    return n, ref
        return None, ref
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    return len(lines), ref


def ab_loudness(proj: Path):
    """vocal-ab-*.wav 通常是 A、B 两段前后拼在一起：量前后半段的 RMS 差。"""
    cands = sorted((proj / "audio" / "vocal").glob("vocal-ab-*.wav")) if (proj / "audio" / "vocal").is_dir() else []
    if not cands:
        return None, None
    try:
        import numpy as np
        import soundfile as sf

        data, sr = sf.read(str(cands[0]), dtype="float32", always_2d=True)
    except Exception:
        return None, cands[0].name
    half = len(data) // 2
    if half < sr // 4:
        return None, cands[0].name

    def rms_db(a):
        r = float(np.sqrt(np.mean(np.square(a, dtype=np.float64))))
        return 20 * math.log10(r) if r > 0 else -120.0

    return abs(rms_db(data[:half]) - rms_db(data[half:])), cands[0].name


def wav_peak_db(path) -> float | None:
    """量真实 WAV 的采样峰值（dBFS）。

    引擎在 report/render-*.json 里自报的 `peak` 是「处理链上量到的」，不等于落盘文件的峰值
    （实测 lofi 工程报告写 drums +2.25 dB，文件其实是 −1.00 dBFS）。闸门以文件为准。
    """
    try:
        import numpy as np
        import soundfile as sf

        peak = 0.0
        for block in sf.blocks(str(path), blocksize=1 << 20, always_2d=True, dtype="float32"):
            peak = max(peak, float(np.abs(block).max()))
        if peak <= 0.0:
            return None
        return 20 * math.log10(peak)
    except Exception:
        return None


def true_peak_db(path) -> float | None:
    """4x 过采样估计真峰值（dBTP）。

    report/*.json 里的 truePeakOut 是限幅器反馈值，不一定是文件的实际峰值：
    实测母带报告 −1.05 dBTP、落盘文件采样峰 −5.02 dBFS。闸门以文件为准。
    """
    if not path:
        return None
    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly

        peak = 0.0
        with sf.SoundFile(str(path)) as f:
            sr = f.samplerate
            for block in f.blocks(blocksize=max(sr * 10, 1024), always_2d=True, dtype="float32"):
                peak = max(peak, float(np.abs(resample_poly(block, 4, 1, axis=0)).max()))
        if peak <= 0.0:
            return None
        return 20 * math.log10(peak)
    except Exception:
        return None


def is_vocal_track(t: dict) -> bool:
    """人声轨（vocal / vocal-harmony / lead-vocal）不按器乐轨的门限判：
    它们的 MIDI 可能不存在（旋律走 vocal.melody），也不进 stems。"""
    tid = str(t.get("id") or "").lower()
    if "vocal" in tid or "vox" in tid:
        return True
    return not t.get("midi") and ("vocal" in str(t.get("name") or "").lower() or "人声" in str(t.get("name") or ""))


def check(proj: Path, stages: set[str] | None) -> tuple[list[Row], dict]:
    rows: list[Row] = []
    info = {"project": proj.name, "path": str(proj)}

    def want(stage):
        return stages is None or stage in stages

    pj = read_json(proj / "project.json")
    if pj is None:
        rows.append(Row("S2", "project.json", "缺失或损坏", "必须存在", FAIL, "工程没建好：music_project {op:\"create\"}"))
        return rows, info

    tempo = pj.get("tempo")
    key = (pj.get("key") or {})
    sections = pj.get("sections") or []
    tracks = pj.get("tracks") or []
    vocal = pj.get("vocal") or {}
    info.update({"title": pj.get("title"), "genre": pj.get("genre"), "tempo": tempo,
                 "key": f"{key.get('tonic', '?')} {key.get('mode', '')}".strip(),
                 "bars": pj.get("bars"), "sections": len(sections), "tracks": len(tracks),
                 "seed": pj.get("seed"), "vocal": bool(vocal.get("enabled"))})

    # ---------------- S2 工程骨架 ----------------
    total_bars = sum(int(s.get("bars") or 0) for s in sections)
    bars = pj.get("bars")
    info["bars"] = bars if bars is not None else (f"{total_bars}*" if total_bars else None)
    if want("S2"):
        if bars is None:
            rows.append(Row("S2", "段落总长 match bars", f"{total_bars}（bars 未登记，按段落求和）", "相等", OK,
                            "老工程常不写顶层 bars；门限按 section 求和放行，不影响后面小节号"))
        else:
            rows.append(Row("S2", "段落总长 match bars", f"{total_bars} / {bars}", "相等",
                            OK if total_bars == int(bars) else FAIL,
                            "段长和 bars 对不上，后续小节号全会错位"))
        gaps = []
        for prev, cur in zip(sections, sections[1:]):
            if int(cur.get("startBar") or 0) != int(prev.get("startBar") or 0) + int(prev.get("bars") or 0):
                gaps.append(cur.get("id"))
        rows.append(Row("S2", "段落首尾相接", f"{len(gaps)} 处断档", "0", OK if not gaps else FAIL,
                        f"断档段落：{', '.join(map(str, gaps[:5]))}"))
        music_tracks = [t for t in tracks if not is_vocal_track(t)]
        miss = [t.get("id") for t in music_tracks if not (proj / str(t.get("midi") or "")).exists()]
        rows.append(Row("S2", "器乐轨 MIDI 就位", f"{len(music_tracks) - len(miss)}/{len(music_tracks)}", "全部",
                        OK if not miss else FAIL, f"缺：{', '.join(map(str, miss[:5]))}"))
        if vocal.get("enabled"):
            vtracks = [t for t in tracks if is_vocal_track(t)]
            vmiss = [str(t.get("id")) for t in vtracks if not (proj / str(t.get("midi") or "")).exists()]
            # 和声轨常常是渲染时按 vocal.harmony 生成的，不需要单独 MIDI
            only_harmony = vmiss and all("harmony" in m.lower() for m in vmiss)
            rows.append(Row("S2", "人声轨 MIDI", f"{len(vtracks) - len(vmiss)}/{len(vtracks)}", "登记了就要有",
                            OK if vtracks and not vmiss else (OK if only_harmony else (NA if not vtracks else WARN)),
                            (f"和声轨 {', '.join(vmiss[:3])} 没 MIDI —— 和声由 vocal.harmony 生成时可以没有"
                             if only_harmony else
                             f"缺 {', '.join(vmiss[:3])}：人声旋律至少要有 vocal.melody")
                            if vmiss else "主唱/和声 MIDI 都在"))
        chords = {s.get("id"): tuple(s.get("chords") or []) for s in sections}
        distinct = len(set(chords.values()))
        rows.append(Row("S2", "段落和声有差别", f"{distinct} 种 / {len(sections)} 段", "≥ 3 且不全同",
                        OK if distinct >= 3 else (WARN if distinct == 2 else FAIL),
                        "整首一套和弦 = 没写旋转/终止式，回 S4 重新 compose" if distinct < 2 else
                        "2 套对 lo-fi / hiphop / trap 是正常写法；其他曲风回 S4 重新 compose（旋转/终止式）"))

    # ---------------- S3 歌词 ----------------
    n_lines = None
    if want("S3") and vocal.get("enabled"):
        n_lines, ref = lyric_lines(pj, proj)
        rows.append(Row("S3", "歌词文件", ref or "未登记", "必须存在并可读",
                        OK if n_lines else FAIL, "music_lyrics {op:\"set\"} 后 warnings 要清空"))
        rows.append(Row("S3", "乐句数", num(n_lines, 0), "≥ 4", OK if (n_lines or 0) >= 4 else FAIL,
                        "句子太少，副歌撑不起来"))

    # ---------------- S4 编曲 ----------------
    render = read_json(proj / "report" / "render-v1.json")
    if want("S4"):
        if render is None:
            rows.append(Row("S4", "渲染报告", "缺 report/render-v1.json", "必须存在", NA,
                            "还没渲染，或分轨刚被清理：music_render {project} 重跑即可"))
        else:
            notes = render.get("notes") or {}
            files = render.get("files") or {}
            peaks, from_report = {}, []
            for t, n in notes.items():
                fp = files.get(t)
                v = wav_peak_db(fp) if (fp and Path(str(fp)).exists()) else None
                if v is None and n.get("peak") is not None:
                    v, _ = float(n["peak"]), from_report.append(t)
                if v is not None:
                    peaks[t] = v
            over = [(t, p) for t, p in peaks.items() if p > TH["peak_max"]]
            hint = ("超限：" + ", ".join(f"{t} {p:+.1f}" for t, p in over[:4])) if over else ""
            if from_report:
                hint = (hint + "  " if hint else "") + (
                    f"（{', '.join(from_report[:3])} 没量到 wav，用的是引擎自报值）")
            rows.append(Row("S4", "分轨峰值", f"超限 {len(over)} 轨", f"≤ {TH['peak_max']:.0f} dBFS",
                            OK if not over else FAIL, hint))
            off = [(t, (n.get("rms") or 0) - (n.get("rmsTarget") or 0)) for t, n in notes.items()]
            worst = max((abs(d) for _, d in off), default=0.0)
            rows.append(Row("S4", "分轨 RMS 贴合", f"最大偏离 {num(worst, 2, ' dB')}", f"≤ {TH['rms_tol']} dB",
                            grade(worst, TH["rms_tol"], warn=TH["rms_tol"] * 2),
                            "偏离最大：" + ", ".join(f"{t} {d:+.1f}" for t, d in
                                                  sorted(off, key=lambda x: -abs(x[1]))[:3]) if off else ""))
            secs = render.get("seconds")
            expect = total_bars * 4 * 60 / (tempo or 120) if total_bars else None
            delta = (secs - expect) if (secs is not None and expect) else None
            rows.append(Row("S4", "渲染时长", f"{num(secs, 1, ' s')} / 应约 {num(expect, 0, ' s')}",
                            "偏差 ≤ 2 s", grade(delta, 2.0, warn=8.0, band=True),
                            "对不上说明中途停渲或段落没铺满"))

        ratios = {}
        for t in tracks:
            if is_vocal_track(t):
                continue
            mp = proj / str(t.get("midi") or "")
            if not mp.exists():
                continue
            st = bar_stats(mp)
            if st is not None:
                ratios[t.get("id")] = st  # (唯一小节占比, 每小节音符中位数)
        if ratios:
            lo_t = min(ratios, key=lambda k: ratios[k][0])
            hi_t = max(ratios, key=lambda k: ratios[k][0])
            rows.append(Row("S4", "唯一小节占比", f"{num(ratios[lo_t][0], 2)}–{num(ratios[hi_t][0], 2)}", "参考值", NA,
                            f"最低 {lo_t} / 最高 {hi_t}：≈1.0 每小节都在变（可能缺动机重复），"
                            "≈0.1 整段照抄（lo-fi 正常）。只判「像不像 loop」，不是质量分"))

    # ---------------- S5 人声 ----------------
    mix = read_json(proj / "report" / "mix-v1.json")
    tunes = sorted((proj / "report").glob("tune-*.json")) if (proj / "report").is_dir() else []
    if want("S5"):
        if not vocal.get("enabled"):
            rows.append(Row("S5", "人声", "未启用（纯音乐）", "—", NA, ""))
        elif not tunes:
            rows.append(Row("S5", "调教报告", "缺 report/tune-*.json", "必须存在", NA,
                            "music_vocals → music_tune，然后回读 report/tune-<tag>.json"))
        else:
            t = read_json(tunes[0]) or {}
            st = t.get("stats") or {}
            after = (st.get("pitchAfter") or {}).get("medianAbsCents")
            before = (st.get("pitchBefore") or {}).get("medianAbsCents")
            rows.append(Row("S5", "音准（调教后）", f"{num(after, 1, ' cents')}（调教前 {num(before, 0)}）",
                            f"≤ {TH['tune_cents']:.0f} cents",
                            grade(after, TH["tune_cents"], warn=TH["tune_cents_warn"]),
                            "唱不准回 lyrics/pinyin.txt 改咬字，别用调教硬压"))
            br, ph = st.get("breaths"), st.get("phrases")
            rows.append(Row("S5", "气口密度", f"{num(br, 0)} 气口 / {num(ph, 0)} 句",
                            f"≥ {TH['breath_ratio']:.0%} 句数",
                            grade((br / ph) if br and ph else None, TH["breath_ratio"], higher_is_better=True),
                            "气口太少会像机器人；太多会喘"))
            diff, name = ab_loudness(proj)
            rows.append(Row("S5", "A/B 响度差", f"{num(diff, 2, ' dB(RMS)')} {name or ''}",
                            f"≤ {TH['ab_lu']} LU(近似)", grade(diff, TH["ab_lu"], warn=2.0) if diff is not None else NA,
                            "A/B 差太多说明处理链没对齐，用户会以为「修坏了」"))
        # S5 收尾（music_vocal_finish）：分和声 + 去哑音/补音/剪重叠
        fin = None
        for p in sorted((proj / "report").glob("vocal-finish-*.json")) if (proj / "report").is_dir() else []:
            fin = read_json(p)
            break
        if not vocal.get("enabled"):
            pass
        elif fin is None:
            rows.append(Row("S5", "人声收尾", "缺 report/vocal-finish-*.json", "建议存在", NA,
                            "music_vocal_finish {project}：一步跑完分和声 → 体检 → 修补 → 重唱建议"))
        else:
            before = fin.get("issuesBefore") or {}
            after = fin.get("issuesAfter") or {}
            hard = ("dead-body", "overlap", "tail", "gap")
            left = {k: after.get(k, 0) for k in hard if after.get(k)}
            resing = fin.get("resingNotes")
            if resing is None:
                resing = (after.get("dead-onset", 0) or 0) + (after.get("silent", 0) or 0)
            nbefore = sum((before.get(k) or 0) for k in hard + ("dead-onset", "silent"))
            value = ("修后 0" if not left else "修后 " + "、".join(f"{k} {v}" for k, v in left.items()))
            value += f"（修前 {nbefore}" + (f"，另 {resing} 个严重起音哑音/整音无声" if resing else "") + "）"
            if left:
                verdict = FAIL
                hint = "还有能补的问题：确认 music_vocal_finish fix:true（修补文件是 vocal-repair-*.wav）"
            elif resing:
                verdict = WARN
                hint = "起音哑音补不了：music_vocal_doctor op=suggest 改谱（音符提前 40 ms）后重唱"
            else:
                verdict = OK
                hint = "哑音、拖尾、重叠、缺口都清过了"
            rows.append(Row("S5", "哑音/重叠/缺口", value, "修后 0", verdict, hint))
            hst = (fin.get("stages") or {}).get("harmony") or {}
            if vocal.get("harmony"):
                hn = hst.get("notes") or 0
                rows.append(Row("S5", "和声编排", f"{num(hn, 0)} 和声音 / 剪掉 "
                                f"{sum((hst.get('cut') or {}).values())} 个候选",
                                "≥ 1", OK if hn else FAIL,
                                "music_harmony 重排，或 music_vocals part=harmony 唱出来"))
            else:
                rows.append(Row("S5", "和声编排", "未启用", "—", NA,
                                "副歌想厚一点就跑 music_harmony；不需要则忽略"))

        tim = ((mix or {}).get("timing") or {}).get("vocal") or {}
        med = tim.get("medianMs")
        if vocal.get("enabled") and (want("S6") or True):
            conf = tim.get("confidence")
            hint = "起音离 16 分网格的中位距离；超过 40 ms 才听得出来拖"
            if tim.get("offsetMs") is not None and conf is not None and float(conf) < 2.0:
                hint = (f"offsetMs {num(tim.get('offsetMs'), 1)} 的置信只有 {num(conf, 2)}（< 2 别照它挪），"
                        "先按这里的中位数判")
            rows.append(Row("S5", "人声离拍中位", num(med, 1, " ms"), f"≤ {TH['vocal_grid_ms']} ms",
                            grade(med, TH["vocal_grid_ms"]), hint))

    # ---------------- S6 混音 ----------------
    mix = read_json(proj / "report" / "mix-v1.json")
    if want("S6"):
        if mix is None:
            rows.append(Row("S6", "混音报告", "缺 report/mix-v1.json", "必须存在", NA,
                            "music_mix {project} 后回读报告"))
        else:
            clipped = mix.get("clippedSamples")
            rows.append(Row("S6", "削波样本", num(clipped, 0), "0",
                            NA if clipped is None else (OK if clipped == 0 else FAIL),
                            "mix 里有削波：先降总线 1–2 dB 再重混" if clipped else
                            "老报告没记这个字段，看不出有没有削波"))
            tp_rep = mix.get("truePeak")
            tp_file = true_peak_db(mix.get("mix"))
            tp_show = tp_file if tp_file is not None else tp_rep
            tp_hint = "留够余量给母带"
            if tp_file is not None and tp_rep is not None and abs(tp_file - tp_rep) > 2.0:
                tp_hint = f"报告 {num(tp_rep, 2)} 与文件实测差 {abs(tp_file - tp_rep):.1f} dB，以文件为准"
            rows.append(Row("S6", "真峰值", num(tp_show, 2, " dBTP"), f"≤ {TH['mix_tp']}",
                            grade(tp_show, TH["mix_tp"]), tp_hint))
            rows.append(Row("S6", "波峰因数", num(mix.get("crest"), 1, " dB"), f"≥ {TH['mix_crest']}",
                            grade(mix.get("crest"), TH["mix_crest"], higher_is_better=True),
                            "crest 太低 = 混音阶段已经压扁"))
            sr = mix.get("silentRatio")
            rows.append(Row("S6", "静音占比", num(sr * 100 if sr is not None else None, 1, "%"),
                            f"≤ {TH['mix_silence']:.0%}",
                            grade(sr, TH["mix_silence"]),
                            f"最长静音 {num(mix.get('longestSilence'), 1, ' s')}"))
            stems = mix.get("stems") or {}
            music_ids = [str(t.get("id")) for t in tracks if not is_vocal_track(t)]
            sung = [k for k in music_ids if k not in stems]
            n_v = len([k for k in stems if "vocal" in k.lower() or "vox" in k.lower()])
            rows.append(Row("S6", "参与混音轨道", f"{len(music_ids) - len(sung)}/{len(music_ids)}"
                            + (f"（另有 {n_v} 条人声）" if n_v else ""), "器乐轨全进",
                            OK if music_ids and not sung else (NA if not music_ids else FAIL),
                            f"没进混音：{', '.join(sung[:4])}——音乐听着「缺一块」" if sung else ""))
            if vocal.get("enabled"):
                vst = [(k, v) for k, v in stems.items()
                       if ("vocal" in k.lower() or "vox" in k.lower()) and isinstance(v, dict)]
                pan = vst[0][1].get("pan") if vst else None
                if pan is not None:
                    rows.append(Row("S6", "主唱位置", num(pan, 2), f"|pan| ≤ {TH['vocal_pan']}",
                                    grade(pan, TH["vocal_pan"], band=True), "人声别偏到一边"))
                elif not vst:
                    rows.append(Row("S6", "主唱位置", f"无独立人声通道（{len(stems)} 条 stem）", "应有人声", WARN,
                                    "混音报告里没人声通道：可能人声根本没进混音，或走了独立总线。去听 music/mix 确认"))
                else:
                    rows.append(Row("S6", "主唱位置", "—", f"|pan| ≤ {TH['vocal_pan']}", NA,
                                    "人声通道没记 pan，判不了偏移"))
            lufs_in = mix.get("lufs")
            rows.append(Row("S6", "混音响度", num(lufs_in, 1, " LUFS"), "参考性", NA,
                            "粗混不用追响度，S7 才收口"))

    # ---------------- S7 母带 ----------------
    master = read_json(proj / "report" / "master-v1.json")
    if want("S7"):
        if master is None:
            rows.append(Row("S7", "母带报告", "缺 report/master-v1.json", "必须存在", NA,
                            "music_master {project, targetLufs, truePeak}"))
        else:
            tgt, out = master.get("targetLufs"), master.get("lufsOut")
            miss = (out - tgt) if (out is not None and tgt is not None) else None
            rows.append(Row("S7", "综合响度", f"{num(out, 1)} / 目标 {num(tgt, 1)} LUFS",
                            f"偏差 ≤ {TH['lufs_tol']} LU",
                            grade(miss, TH["lufs_tol"], band=True), "欠得太多就再推 0.5–1 LU，别硬顶"))
            tpm_rep = master.get("truePeakOut")
            tpm_file = true_peak_db(master.get("master"))
            tpm_show = tpm_file if tpm_file is not None else tpm_rep
            tpm_hint = "超过 −1 dBTP 的平台会失真"
            if tpm_file is not None and tpm_rep is not None and abs(tpm_file - tpm_rep) > 2.0:
                tpm_hint = f"报告 {num(tpm_rep, 2)} 与文件实测差 {abs(tpm_file - tpm_rep):.1f} dB，以文件为准"
            rows.append(Row("S7", "真峰值", num(tpm_show, 2, " dBTP"),
                            f"≤ {TH['master_tp']}", grade(tpm_show, TH["master_tp"]), tpm_hint))
            rows.append(Row("S7", "波峰因数", num(master.get("crestOut"), 1, " dB"), f"≥ {TH['master_crest']}",
                            grade(master.get("crestOut"), TH["master_crest"], higher_is_better=True),
                            "低于 7 dB 说明压平了，降 0.5–1 LU 重渲"))
            oc = master.get("overCompressed")
            rows.append(Row("S7", "过压缩", num(oc), "否",
                            NA if oc is None else (OK if not oc else WARN),
                            "老报告没记 overCompressed，只能看 crest" if oc is None else "overCompressed 为真要复查"))
            # 母带文件：报告里的绝对路径优先（那是引擎写入的真实位置），
            # 文件名被改名/清理过时退回扫 audio/master，并说明报告链接已断。
            declared = []
            mdir = proj / "audio" / "master"
            for key, ext in (("master", "wav"), ("mp3", "mp3")):
                p = master.get(key)
                if p:
                    declared.append(Path(str(p)))
            if not declared:
                declared = [mdir / f"master-v1.wav", mdir / f"master-v1.mp3"]
            have = [p for p in declared if p.is_file()]
            mp3_file = next((p for p in have if p.suffix.lower() == ".mp3"), None)
            loose_mp3 = sorted(p for p in mdir.glob("*.mp3")) if mdir.is_dir() else []
            any_mp3 = mp3_file or (loose_mp3[0] if loose_mp3 else None)
            mp3_ok = master.get("mp3Ok")
            if mp3_ok is None:
                mp3_ok = bool(any_mp3)
            mp3_verdict = OK if (mp3_ok and any_mp3) else FAIL
            mp3_hint = ""
            if not any_mp3:
                mp3_hint = "mp3 没出来，交付缺一件"
            elif not mp3_ok:
                mp3_hint = "mp3 文件在，但报告说编码没通过"
            elif not mp3_file:
                mp3_hint = "报告登记的 mp3 已不在原路径，audio/master 里有别的 mp3（可用）"
            rows.append(Row("S7", "MP3 交付", num(mp3_ok), "是", mp3_verdict, mp3_hint))
            loose = sorted(p.name for p in mdir.glob("*") if p.suffix.lower() in (".wav", ".mp3")) \
                if mdir.is_dir() else []
            if len(have) == len(declared) and declared:
                rows.append(Row("S7", "母带文件", "wav+mp3 都在", "wav+mp3 都在", OK, have[0].name))
            else:
                miss_names = [p.name for p in declared if not p.is_file()]
                rows.append(Row("S7", "母带文件", f"{len(have)}/{len(declared)} 命中", "wav+mp3 都在", FAIL,
                                "报告指向的 " + ", ".join(miss_names[:2]) + " 不在了；audio/master 实际有："
                                + (", ".join(loose[:4]) if loose else "（空）")
                                + " —— 音频没丢，链接断了：gc 以后会保留报告引用的文件"))

    # ---------------- S8 交付 ----------------
    if want("S8"):
        exp = proj / "export"
        n_exp = len([p for p in exp.rglob("*") if p.is_file()]) if exp.is_dir() else 0
        rows.append(Row("S8", "DAW 包", f"{n_exp} 文件", "按需", OK if n_exp else NA,
                        "music_export {project, daw} 随时可再出"))
        des = sorted((proj / "design").glob("*.html")) if (proj / "design").is_dir() else []
        rows.append(Row("S8", "方案页", des[0].name if des else "无", "按需", OK if des else NA, ""))
        prev = sorted((proj / "preview").glob("*.png")) if (proj / "preview").is_dir() else []
        rows.append(Row("S8", "波形/段落图", f"{len(prev)} 张", "按需", OK if prev else NA,
                        "music_preview 出图，试听点和方案页都要用"))

    return rows, info


def next_step(rows: list[Row]) -> str:
    fails = [r for r in rows if r.verdict == FAIL]
    # 「参考值」类检查（如唯一小节占比）按设计就是 NA，不算「没数据」
    nas = [r for r in rows if r.verdict == NA and r.limit != "参考值"]
    if not fails:
        warns = [r for r in rows if r.verdict == WARN]
        if warns:
            return "可以往下走，但要跟用户说明：" + "；".join(f"{r.stage} {r.item}" for r in warns[:3])
        if nas:
            return (f"本阶段没有 FAIL，但有 {len(nas)} 项没数据（比如 "
                    + "、".join(f"{r.stage} {r.item}" for r in nas[:2])
                    + "）——没数据不等于通过，要么补跑对应工具，要么在交付说明里写明")
        return "本阶段全过，按 PIPELINE 进入下一阶段"
    first = fails[0]
    return f"先回 {first.stage}：{first.hint or first.item}"


def _dw(s: str) -> int:
    """显示宽度：中日韩全角字符按 2 列算，表格才对得齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s, width: int) -> str:
    s = str(s)
    if _dw(s) > width:                       # 截断时留一个省略号，别把列挤歪
        out, w = "", 0
        for ch in s:
            cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if w + cw > width - 1:
                break
            out, w = out + ch, w + cw
        s = out + "…"
    return s + " " * max(0, width - _dw(s))


def print_detail(rows: list[Row], info: dict) -> None:
    print(f"工程 {info.get('title') or info['project']}   {info.get('genre')}   "
          f"{info.get('tempo')} BPM   {info.get('key')}   {info.get('bars')} 小节   "
          f"{info.get('sections')} 段 / {info.get('tracks')} 轨   seed {info.get('seed')}")
    print(f"路径 {info['path']}\n")
    cols = ["阶段", "检查项", "实测", "门限", "结论"]
    widths = [5, 18, 30, 20, 6]
    for r in rows:
        for i, v in enumerate((r.stage, r.item, str(r.value), str(r.limit), r.verdict)):
            widths[i] = max(widths[i], _dw(str(v)))
    widths = [min(w, 40) for w in widths]
    gutter = "  "
    line = (sum(widths) + len(gutter) * (len(widths) - 1))
    print(gutter.join(_pad(c, widths[i]) for i, c in enumerate(cols)).rstrip())
    print("-" * line)
    for r in rows:
        vals = (r.stage, r.item, str(r.value), str(r.limit), r.verdict)
        print(gutter.join(_pad(v, widths[i]) for i, v in enumerate(vals)).rstrip())
    print("-" * line)
    tally = {k: sum(1 for r in rows if r.verdict == k) for k in (OK, WARN, FAIL, NA)}
    print(f"通过 {tally[OK]} / 警告 {tally[WARN]} / 失败 {tally[FAIL]} / 无数据 {tally[NA]}")
    print(f"下一步：{next_step(rows)}")
    hints = [r.hint for r in rows if r.verdict in (WARN, FAIL) and r.hint]
    if hints:
        print("\n待办：")
        for h in dict.fromkeys(hints):
            print(f"  · {h}")


def summary_line(rows: list[Row], info: dict) -> str:
    by_stage: dict[str, list[str]] = {}
    for r in rows:
        by_stage.setdefault(r.stage, []).append(r.verdict)
    stages = []
    for st in ("S2", "S3", "S4", "S5", "S6", "S7", "S8"):
        v = by_stage.get(st)
        if not v:
            continue
        if FAIL in v:
            stages.append(f"{st}✗")
        elif WARN in v:
            stages.append(f"{st}!")
        elif set(v) == {NA}:
            stages.append(f"{st}·")
        else:
            stages.append(f"{st}✓")
    name = info.get("rel") or info["project"]
    return f"{name:<34}{'/'.join(stages):<26}"


def iter_projects(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in Path(root).walk():
        dirnames[:] = [d for d in dirnames if d not in ("_trash", "_gc_reports", "_backup", "__pycache__")]
        if "project.json" in filenames:
            out.append(Path(dirpath))
    return sorted(out)


def default_root() -> Path:
    cfg = Path.home() / ".dsh" / "music" / "engine.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if data.get("root"):
                return Path(data["root"])
        except Exception:
            pass
    return Path.home() / "Music" / "dsh-music"


def main() -> int:
    ap = argparse.ArgumentParser(description="dsh-music 验收闸门（离线）")
    ap.add_argument("project", nargs="?", help="工程目录")
    ap.add_argument("--all", action="store_true", help="扫描工程根下所有工程，一行一个摘要")
    ap.add_argument("--root", default=None, help="工程根（默认读 ~/.dsh/music/engine.json）")
    ap.add_argument("--stage", default=None, help="只查这些阶段，如 S4,S7")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    stages = {s.strip().upper() for s in args.stage.split(",")} if args.stage else None

    if args.all:
        root = Path(args.root) if args.root else default_root()
        projects = iter_projects(root)
        if not projects:
            print(f"{root} 下没有工程", file=sys.stderr)
            return 2
        print(f"{'工程':<34}{'阶段状态（✓通过 !警告 ✗失败 ·无数据）':<26}")
        print("-" * 70)
        payload = []
        failing = 0
        for p in projects:
            rows, info = check(p, stages)
            try:
                info["rel"] = str(p.relative_to(root))
            except ValueError:
                info["rel"] = p.name
            print(summary_line(rows, info))
            if any(r.verdict == FAIL for r in rows):
                failing += 1
            payload.append({"project": info["project"], "rel": info["rel"], "path": str(p),
                            "rows": [r.as_dict() for r in rows]})
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("-" * 70)
            print(f"共 {len(projects)} 个工程，{failing} 个有 FAIL（调试残留的工程不必返工，只看你要交付的那个）")
        return 1 if failing else 0

    if not args.project:
        ap.print_help()
        return 2
    proj = Path(args.project)
    if (proj / "project.json").exists() is False and (proj / proj.name / "project.json").exists():
        proj = proj / proj.name
    if not proj.is_dir():
        print(f"不是目录：{proj}", file=sys.stderr)
        return 2

    rows, info = check(proj, stages)
    if args.json:
        print(json.dumps({"info": info, "rows": [r.as_dict() for r in rows],
                          "tally": {k: sum(1 for r in rows if r.verdict == k) for k in (OK, WARN, FAIL, NA)},
                          "next": next_step(rows)}, ensure_ascii=False, indent=2))
    else:
        print_detail(rows, info)
    return 1 if any(r.verdict == FAIL for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
