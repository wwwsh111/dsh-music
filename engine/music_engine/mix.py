"""混音：分轨 → 总线。

0.3.0 的问题：所有轨道直接相加，project 里的 pan 被完全忽略，
没有 send、没有总线处理，最后归一化到 -6 dBFS 就算完。

现在的总线结构：

    drums ─┐
    bass ──┤
    gtr L/R┼─→ (sidechain: 底鼓压贝斯/键盘) ─→ mix bus ─→ 总线胶水压缩 ─→ mix
    keys ──┤                                   ↑
    pads ──┤                          reverb / delay returns
    vocal ─┘

人声在这里才挂链（render 阶段不碰它），因为它来自肥鱼 SVS 的真实人声，
不是 MIDI 合成的。
"""

import math
from pathlib import Path

import numpy as np

from . import automation as automation_mod
from . import fxchain as fxchain_mod
from . import routing as routing_mod
from . import dsp
from .project import load_project, save_project
from .util import ensure_dir, jdump, jload

SIDECHAIN = {"bass": 3.5, "keys": 2.0, "pad": 2.0, "gtr_rhythm_l": 0.8,
             "gtr_rhythm_r": 0.8}

DEFAULT_GAIN = {"drums": 0.5, "bass": 0.0, "gtr_rhythm_l": -1.5, "gtr_rhythm_r": -1.5,
                "gtr_lead": -2.5, "guitar": -3.0, "keys": -3.0, "pad": -6.0,
                "lead": -3.0, "vocal": 2.0, "vocal-harmony": -7.5}

DEFAULT_PAN = {"drums": 0.0, "bass": 0.0, "gtr_rhythm_l": -0.55, "gtr_rhythm_r": 0.55,
               "gtr_lead": 0.0, "guitar": -0.2, "keys": -0.25, "pad": 0.0,
               "lead": 0.15, "vocal": 0.0, "vocal-harmony": 0.0}


def _collect_vocals(base):
    """找肥鱼 SVS 出的人声：lead 和 harmony 分开。

    调教之后 audio/vocal 里会同时躺着原声、调教干声、A/B 拼贴和成品：
    按「成品调教 > 调教干声 > 原始 SVS」的优先级挑，别让 _ab 拼贴被当成主唱。
    """
    out = {}
    vdir = base / "audio" / "vocal"
    if not vdir.exists():
        return out
    skip = ("ab", "dry", "guide", "raw", "backup")
    files = [p for p in sorted(vdir.glob("*.wav"))
             if not any(k in p.stem.lower() for k in skip)]
    leads, harmony = [], []
    for f in files:
        name = f.stem.lower()
        if "harmony" in name:
            harmony.append(f)
        elif "lead" in name or "vocal-" in name:
            leads.append(f)

    def rank(p):
        n = p.stem.lower()
        if "tuned" in n:
            return 0
        if "v1" in n or "feiyu" in n:
            return 1
        return 2

    if leads:
        best = sorted(leads, key=lambda p: (rank(p), -p.stat().st_mtime))[0]
        out["vocal"] = str(best)
    if harmony:
        out["vocal-harmony"] = str(sorted(harmony, key=lambda p: (rank(p), -p.stat().st_mtime))[0])
    return out


def _track_meta(project):
    meta = {}
    for tr in project.get("tracks") or []:
        meta[tr["id"]] = tr
    return meta


# 段落能量 → 总线增益：摇滚的副歌要靠推子（和真实现场一样）再抬 2 dB，
# 主歌退 1–2 dB，否则 300–800 Hz 堆满之后整首歌听起来是「一条直线」。
SECTION_DB_PER_ENERGY = 5.0
SECTION_DB_RANGE = (-2.5, 2.0)


def _section_dbs(project):
    out = []
    for a in (project.get("arrangement") or {}).values():
        e = float(a.get("energy") or 0.5)
        out.append(float(np.clip((e - 0.5) * SECTION_DB_PER_ENERGY, *SECTION_DB_RANGE)))
    return out


def _section_curve(project, sr, n):
    """按 arrangement 的段落边界和 energy 生成逐样本增益曲线。"""
    dbs = _section_dbs(project)
    if not dbs:
        return None
    spb = 60.0 / float(project.get("tempo") or 120.0)
    starts, bar = [], 1
    for a in project["arrangement"].values():
        starts.append(int((bar - 1) * 4 * spb * sr))
        bar += int(a.get("bars") or 0)
    gain = np.ones(n, dtype="float32")
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else n
        s, e = min(max(s, 0), n), min(max(e, 0), n)
        if e > s:
            gain[s:e] = dsp.db_to_gain(dbs[i])
    w = max(1, int(0.06 * sr))
    if w > 1 and n > w:
        gain = np.convolve(gain, np.ones(w, dtype="float32") / w, mode="same").astype("float32")
    return gain


def mix(project_path=None, stems=None, preset=None, reference=None, target_lufs=None):
    base = None
    project = None
    if project_path:
        project = load_project(project_path)
        base = Path(project_path)
        if base.is_file():
            base = base.parent
        render_dir = base / "audio" / (project.get("renders", {}).get("current") or "render-v1")
        found = {p.stem: str(p) for p in sorted(render_dir.glob("*.wav"))}
        found.update(_collect_vocals(base))
        stems = found
        if not stems:
            raise ValueError("没有可混的分轨；先 music_render")
        sr = int(project.get("sampleRate") or 48000)
        tmeta = _track_meta(project)
    else:
        if not stems:
            raise ValueError("需要 project 或 stems")
        sr = 48000
        tmeta = {}

    reference = reference or (project or {}).get("mix", {}).get("reference")
    ref_profile = None
    if reference:
        try:
            ref_profile = dsp.reference_profile(reference, sr)
        except Exception as e:
            ref_profile = {"error": str(e)}

    loaded, report = {}, {"stems": {}, "sampleRate": sr,
                          "preset": preset or (project or {}).get("mix", {}).get("preset")}
    for tid, path in stems.items():
        data, _sr0 = dsp.load(path, sr)
        data = dsp.ensure_stereo(data)
        fx = (tmeta.get(tid) or {}).get("fx") or "generic"
        if tid.startswith("vocal"):
            fx = "vocal"
        if fx == "vocal":
            # DAW 式对齐：先量这条人声离网格/乐谱的偏移，再决定要不要 nudge。
            # 只做诊断 + 可选位移，不重渲人声（重渲一次 30 分钟）。
            timing_info = _vocal_timing(project, base, tid, data, sr)
            if timing_info:
                report.setdefault("timing", {})[tid] = timing_info
                nudge = timing_info.get("appliedMs") or 0.0
                if nudge:
                    from . import timing as _timing
                    data = _timing.shift(data, sr, nudge)
            # 调教成品已经过真实感链（去齿音/压缩/板式混响/加倍），再套一遍
            # vocal_chain 就是重复处理：高频二次提亮、压缩叠压缩，人声会「炸」
            if "tuned" not in str(path).lower():
                data = dsp.vocal_chain(data, sr, presence=0.6, warmth=0.4)
            # 人声经常是全曲峰最高的那轨：归一化时先留 −4 dB，给总线/增益再留余量，
            # 否则 vocal stem 的 peak 会顶到 0 dBFS（调教成品带板式和加倍，峰更尖）
            data = _norm(data, -18.0, ceiling=-4.0)
        # 插入槽：预设音色处理之后、推子之前（和 DAW 的 channel strip 同位置）
        chain = (tmeta.get(tid) or {}).get("fxChain") or []
        if not chain:
            # autoFx：没挂链的轨按角色套预设（鼓=鼓、贝斯=贝斯、键盘=键盘）
            chain = routing_mod.auto_chain(project, tid, fx)
            if chain:
                report.setdefault("autoFx", {})[tid] = routing_mod.role_preset(tid, fx)
        if chain:
            data, chain_report = fxchain_mod.apply_chain(data, sr, chain)
            report.setdefault("fxChain", {})[tid] = chain_report
        db = float((tmeta.get(tid) or {}).get("gainDb", DEFAULT_GAIN.get(tid, -6.0)))
        pv = float((tmeta.get(tid) or {}).get("pan", DEFAULT_PAN.get(tid, 0.0)))
        data = dsp.pan(data, pv)
        data = data * dsp.db_to_gain(db)
        # 轨道自动化：和 FL 的 automation clip 一样，挂在推子之后、进总线之前
        data, auto = automation_mod.apply_track(project, tid, data, sr)
        if auto:
            report.setdefault("automation", {})[tid] = auto
        loaded[tid] = data
        report["stems"][tid] = {
            "file": str(path), "fx": fx, "gainDb": db, "pan": pv,
            "rms": round(20 * math.log10(float(np.sqrt(np.mean(data ** 2) + 1e-12))), 2),
            "peak": dsp.true_peak(data, sr),
        }

    n = max(d.shape[0] for d in loaded.values())
    for tid in list(loaded):
        loaded[tid] = dsp.fit_length(loaded[tid], n)

    ducked = {}
    if "drums" in loaded or "drums" in stems:
        drums = loaded.get("drums")
        if drums is None:
            drums, _ = dsp.load(stems["drums"], sr)
        env = dsp.kick_envelope(drums, sr)
        for tid, depth in SIDECHAIN.items():
            if tid in loaded:
                loaded[tid] = dsp.duck(loaded[tid], env, depth)
                ducked[tid] = depth
    report["sidechain"] = ducked

    # 分组总线：被 route 走的轨不再直接进主输出，只经 bus 出来一次。
    # FL 的 group 里还能再套 group，所以 bus 自己也带一个 "bus" 字段（父总线）。
    bus_defs = [b for b in ((project or {}).get("buses") or []) if b.get("id")]
    nested_send = {}
    if bus_defs:
        by_id = {str(b["id"]): b for b in bus_defs}
        direct, accum, members, parent_of = {}, {}, {}, {}
        for tid, data in loaded.items():
            bid = str((tmeta.get(tid) or {}).get("bus") or "")
            if bid and bid in by_id:
                if bid not in accum:
                    accum[bid] = np.zeros_like(data)
                accum[bid] = accum[bid] + data
                members.setdefault(bid, []).append(tid)
            else:
                direct[tid] = data
        # 嵌套路由：先整理父子关系，成环直接报错（否则混音会静默丢一条总线）
        for bid, b in by_id.items():
            p = str(b.get("bus") or "")
            if p and p in by_id:
                if p == bid:
                    raise ValueError("总线路由成环：%s 路由到了自己" % bid)
                parent_of[bid] = p
        order, pending = [], dict(by_id)
        while pending:
            # 父总线必须等子总线全部算完：pending 里还有人把它当父级，就先放着
            ready = [k for k in pending if not any(parent_of.get(o) == k for o in pending)]
            if not ready:
                raise ValueError("总线路由成环：%s" % " -> ".join(pending))
            for bid in ready:
                order.append(bid)
                pending.pop(bid)
        # 子总线要先算完再并进父级，所以按 order 处理（order 已保证父在后）
        for bid in order:
            data = accum.pop(bid, None)
            if data is None:
                continue
            b = by_id[bid]
            chain = b.get("fxChain") or []
            if chain:
                data, bus_chain = fxchain_mod.apply_chain(data, sr, chain)
                report.setdefault("busFxChain", {})[bid] = bus_chain
            data = dsp.pan(data, float(b.get("pan") or 0.0))
            data = data * dsp.db_to_gain(float(b.get("gainDb") or 0.0))
            data, bus_auto = automation_mod.apply_track(project, bid, data, sr)
            if bus_auto:
                report.setdefault("automationBus", {})[bid] = bus_auto
            tmeta[bid] = b                   # 让下面的 send 循环能读到 bus 自己的 send
            report.setdefault("buses", {})[bid] = {
                "tracks": members.get(bid, []),
                "gainDb": round(float(b.get("gainDb") or 0.0), 2),
                "fxChain": [str(s.get("type")) for s in chain],
                "bus": parent_of.get(bid),
                "peak": dsp.true_peak(data, sr),
            }
            p = parent_of.get(bid)
            if p:
                # 并进父级之前把要送出去的量截下来，否则内层 bus 的 send 会丢
                snd = b.get("send") or {}
                if float(snd.get("reverb") or 0.0) > 0 or float(snd.get("delay") or 0.0) > 0:
                    nested_send[bid] = data
                if p not in accum:
                    accum[p] = np.zeros_like(data)
                accum[p] = accum[p] + data
            else:
                direct[bid] = data
        loaded = direct

    # send 效果器总线：所有轨共用一套空间，房间才是同一个房间
    sends = {"reverb": None, "delay": None}
    send_amount = {"reverb": {}, "delay": {}}
    send_src = dict(loaded)
    send_src.update(nested_send)          # 已被父级吸收的子总线，送出量仍然要算
    for tid, data in send_src.items():
        s = (tmeta.get(tid) or {}).get("send") or {}
        send_amount["reverb"][tid] = float(s.get("reverb") or 0.0)
        send_amount["delay"][tid] = float(s.get("delay") or 0.0)
    # 空间参数按工程走（styles._space 给每首歌一套），延迟时间跟拍速对齐
    space = ((project or {}).get("profile") or {}).get("space") or {}
    room = float(space.get("room", 0.62))
    damping = float(space.get("damping", 0.45))
    predelay = float(space.get("predelayMs", 18.0))
    rev_ret = float(space.get("reverbReturn", 0.35))
    dly_ret = float(space.get("delayReturn", 0.22))
    bpm = float((project or {}).get("tempo") or 120.0)
    delay_s = 0.75 * 60.0 / bpm          # 附点八分：跟歌走，而不是固定 0.34 s
    for bus in ("reverb", "delay"):
        acc = np.zeros((n, 2), dtype=np.float32)
        used = 0
        for tid, data in send_src.items():
            amt = send_amount[bus].get(tid, 0.0)
            if amt <= 0:
                continue
            acc += data * amt
            used += 1
        if used:
            if bus == "reverb":
                sends[bus] = dsp.reverb(acc, sr, wet=1.0, room=room, damping=damping,
                                        predelay_ms=predelay)
            else:
                sends[bus] = dsp.delay(acc, sr, time_s=delay_s, feedback=0.3, wet=1.0)
    report["sendTracks"] = {k: sum(1 for v in d.values() if v > 0)
                            for k, d in send_amount.items()}

    total = np.zeros((n, 2), dtype=np.float32)
    for tid, data in loaded.items():
        total += data
    if sends["reverb"] is not None:
        total += sends["reverb"] * rev_ret
    if sends["delay"] is not None:
        total += sends["delay"] * dly_ret

    total = dsp.master_bus(total, sr, glue=0.45)
    # 段落落差：编曲给的 energy 差（0.25→0.9）在实测里只剩 1.7 dB，
    # 副歌听着「没起来」。按 energy 给整体一条缓变增益曲线（摇滚现场的推子手法），
    # 主歌压一点、副歌顶一点，边界做 60 ms 斜坡防止爆音。
    section = None
    if base is not None and project.get("arrangement"):
        section = _section_curve(project, sr, total.shape[0])
        if section is not None:
            total = total * section[:, None]
            report["sectionAutomation"] = {
                "db": [round(v, 2) for v in _section_dbs(project)],
                "sections": list(project["arrangement"]),
            }
    # 总线自动化（track 写 master/*）：段落曲线之后、参考匹配之前，
    # 相当于 FL 主推子的推子自动化——最后的淡出/渐强都在这里
    total, amaster = automation_mod.apply_master(project, total, sr)
    if amaster:
        report["automationMaster"] = amaster
    if ref_profile and not ref_profile.get("error"):
        total, match = dsp.match_reference(total, sr, ref_profile)
        report["reference"] = {"profile": ref_profile, **match}
    elif ref_profile:
        report["reference"] = ref_profile

    # 混音只留 -3 dBFS 余量，响度交给母带。
    # 但母带要从「混音响度」推到目标响度：如果混音太轻（比如 -17 LUFS），
    # 母带就得加 8 dB 以上，软限幅把瞬态全削平（实测 crest 从 17 掉到 5.9）。
    # 所以混音先把整体推到「目标响度 - 5 dB」，留 5 dB 给母带做。
    tgt_mix = -9.0
    try:
        tgt_mix = float(((project or {}).get("mix") or {}).get("targets") or {}).get("lufs", -9.0)
    except Exception:
        pass
    want = tgt_mix - 5.0
    try:
        cur = dsp.lufs(total, sr)
        if cur > -70 and abs(want - cur) > 0.3:
            gain = dsp.db_to_gain(want - cur)
            cand = total * gain
            pk = float(np.max(np.abs(cand))) or 1.0
            lim = dsp.db_to_gain(-1.5)
            if pk > lim:
                gain *= lim / pk
            total = total * gain
            report["mixLevelTrimDb"] = round(float(dsp.gain_to_db(gain)), 2)
    except Exception:
        pass

    peak = float(np.max(np.abs(total))) or 1.0
    ceil = dsp.db_to_gain(-3.0)
    if peak > ceil:
        # 只削峰不压响度：整体缩放会把上面刚推起来的响度又还回去
        total = dsp.soft_clip(total, ceiling_db=-3.0, knee_db=6.0)
        peak = float(np.max(np.abs(total))) or 1.0
        if peak > ceil:
            total = total * (ceil / peak)

    out = ensure_dir(base / "audio" / "mix") / "mix-v1.wav" if base else Path("mix-v1.wav")
    dsp.save(out, total, sr, subtype="FLOAT")

    metrics = {
        "lufs": dsp.lufs(total, sr),
        "truePeak": dsp.true_peak(total, sr),
        "crest": dsp.crest_factor(total),
        "bands": dsp.band_energies(total, sr),
        "centroidHz": dsp.spectral_centroid(total, sr),
        "stereoCorr": dsp.stereo_correlation(total),
        "duration": round(total.shape[0] / sr, 2),
    }
    metrics.update(dsp.silence_stats(total, sr))
    report.update({"mix": str(out), **metrics})
    if base is not None:
        project.setdefault("renders", {})["mix"] = "mix-v1"
        save_project(base, project)
        jdump(base / "report" / "mix-v1.json", report)
    return {"ok": True, "mix": str(out),
            "metrics": {k: metrics[k] for k in ("lufs", "truePeak", "crest", "duration")},
            "analysis": {k: metrics[k] for k in ("bands", "centroidHz", "stereoCorr",
                                                 "longestSilence") if k in metrics},
            "report": report, "next": ["music_master"]}


def _vocal_timing(project, base, tid, data, sr):
    """量一条人声的「卡点」情况，并给出建议位移。

    DAW 里这一步是手动盯着波形拖 clip；这里自动量：
      * gridDistance —— 起音离最近 16 分线的中位距离与 ±30 ms 命中率；
      * offsetMs —— 相对乐谱（align.json 的音符起点）的整体偏移；
      * appliedMs —— 这次实际会挪多少（用户显式给了 nudgeMs 就用它，否则
        只在置信度 ≥ 2 且偏移 ≥ 12 ms 时自动挪）。
    """
    if base is None or not project:
        return None
    try:
        from . import timing as tm
    except Exception:
        return None
    part = "harmony" if "harmony" in tid else "lead"
    name = "vocal-harmony-align.json" if part == "harmony" else "vocal-align.json"
    p = Path(base) / "analysis" / name
    if not p.exists():
        return None
    notes = (jload(p, {}) or {}).get("notes") or []
    if len(notes) < 4:
        return None
    bpm = float(project.get("tempo") or 120)
    try:
        before = tm.grid_distance(data, sr, bpm)
        offset, conf = tm.measure_offset(data, sr, tm.score_onsets(notes))
    except Exception as e:
        return {"error": str(e)[:120]}
    manual = float(((project.get("vocal") or {}).get("nudgeMs")) or 0.0)
    # 默认不自作主张挪音频：自动测出来的偏移偶尔会锁到相邻音符（±1~2 个音的
    # 周期），挪错比不挪更难听。只把建议值写进报告，nudgeMs 由用户/上层决定。
    applied = manual
    out = dict(before or {})
    out.update({"offsetMs": offset, "confidence": conf,
                "suggestedMs": offset if conf >= 2.0 else 0.0,
                "appliedMs": round(float(applied), 1), "manualMs": manual,
                "part": part})
    return out


def _norm(data, target_db, ceiling=-2.0):
    rms = float(np.sqrt(np.mean(data ** 2) + 1e-12))
    if rms <= 1e-9:
        return data
    out = data * (dsp.db_to_gain(target_db) / rms)
    peak = float(np.max(np.abs(out))) or 1.0
    c = dsp.db_to_gain(ceiling)
    return (out * min(1.0, c / peak)).astype("float32")
