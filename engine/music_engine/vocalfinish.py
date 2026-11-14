"""S5 人声收尾：分和声 → 体检 → 修补 → 重唱建议（可选调教）。

标准化编曲的 S5 原本到 music_tune 就结束：音准、气口、A/B 都验过了，
但「分了和声之后有没有哑音、重叠、缺口气」这件事没人管。这个模块把
music_harmony + music_vocal_doctor 收成一步，产出一份 report/vocal-finish-<tag>.json，
gate 的 S5 段直接读它。

顺序是有讲究的：
  1) 先分和声——和声要在主唱定稿的谱面上编，改一次谱就得重排一次；
  2) 再体检主唱——和声是另一条轨，主唱的问题不该被和声掩盖；
  3) 能补的当场补（pitch/tail/cut/fill），补完复扫一遍，用「修后」的数字说话；
  4) 起音哑音补不了（那是 SVS 咬字问题），只能给改谱建议 + 重唱；
  5) 调教放最后，因为调教本身会重新合成一轮 F0，先调教再修补等于白修。
"""

from __future__ import annotations

from pathlib import Path

from . import dsp, harmonize, tune, vocaldoctor
from .util import jdump, jload
from .project import load_project, save_project

# 时域修补能解决的动作；resing 不在其中
FIXABLE = ("pitch", "tail", "cut", "fill", "debleed")
# 这些留在「修后」报告里就算没过（silent / dead-onset 补不了，另算重唱）
HARD = ("dead-body", "overlap", "tail", "gap")


def _base(project_path):
    p = Path(project_path)
    return p.parent if p.is_file() else p


def finish(project_path, section=None, harmony="auto", tune_style=None,
           fix=True, density="full", cut="default", dry_run=False):
    """跑完 S5 尾段，返回一份汇总。harmony: auto/off/<strategy>。

    tune_style 给了就跑一遍 music_tune（会重合成 F0，所以排在修补之后）；
    不给就沿用工程里已有的 report/tune-*.json，只把它的音准读数抄进汇总。
    """
    base = _base(project_path)
    project = load_project(base)
    vocal = project.get("vocal") or {}
    tag = section or "lead"
    stages = {}
    notes = []

    # ---- 1) 和声编排 ----
    want_harm = (harmony != "off") and (vocal.get("autoHarmony", True) is not False)
    if want_harm:
        strat = None if harmony in ("auto", True, None) else str(harmony)
        try:
            hrep = harmonize.plan(base, sections=[section] if section else None,
                                  strategy=strat, cut=cut, density=density)
            stages["harmony"] = {"ok": True, "notes": hrep.get("harmonyNotes", 0),
                                 "leadNotes": hrep.get("leadNotes", 0),
                                 "cut": hrep.get("cut") or {},
                                 "perSection": hrep.get("perSection") or {},
                                 "strategies": hrep.get("strategies") or {}}
            notes.append(f"和声 {hrep.get('harmonyNotes', 0)} 音 / 主唱 "
                         f"{hrep.get('leadNotes', 0)} 音")
            if not hrep.get("harmonyNotes"):
                notes.append("没有编出和声：先查段落 role 与和弦进行")
        except Exception as exc:  # 和声失败不该挡住人声收尾
            stages["harmony"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            notes.append(f"和声编排失败：{type(exc).__name__}")
    else:
        stages["harmony"] = {"ok": True, "skipped": "off"}

    # ---- 2) 体检主唱 ----
    scan = vocaldoctor.scan(base, section=section)
    counts = dict(scan.get("counts") or {})
    scan_before_severe = dict(scan.get("countsSevere") or {})
    stages["scan"] = {"ok": True, "notes": scan.get("notes"), "seconds": scan.get("seconds"),
                      "counts": counts, "alignmentBiasMs": scan.get("alignmentBiasMs"),
                      "resing": len(scan.get("resing") or [])}

    # ---- 3) 修补 + 复扫 ----
    # 注意：这里要按 issue 的 action（pitch/tail/cut/fill/debleed）传，不是按 issue 的 kind。
    # 补完要复扫：补一个缺口可能露出下一个缺口（缺口是相对静音判的），所以要迭代到稳定。
    after = dict(counts)
    severe = dict(scan.get("countsSevere") or {})
    fixed = {}
    stage_fix = {"ok": True}
    cur = None
    passes = 0
    try:
        for _i in range(3):
            todo = sorted({it.get("action") for it in (scan.get("issues") or [])
                           if it.get("action") in FIXABLE})
            if not fix or not todo or dry_run:
                break
            passes += 1
            rep = vocaldoctor.fix(base, section=section, vocal=cur, actions=todo)
            for k, v in (rep.get("done") or {}).items():
                fixed[k] = fixed.get(k, 0) + int(v or 0)
            cur = rep.get("output") or cur
            stage_fix["output"] = cur
            if not cur or not Path(cur).exists():
                break
            rescan = vocaldoctor.scan(base, section=section, vocal=cur)
            scan = rescan
            after = dict(rescan.get("counts") or {})
            severe = dict(rescan.get("countsSevere") or {})
            stage_fix["after"] = after
            stage_fix["afterSeconds"] = rescan.get("seconds")
            if not any(it.get("action") in FIXABLE for it in (rescan.get("issues") or [])):
                break
    except Exception as exc:  # 修补失败不该挡住后面的重唱建议与报告
        stage_fix = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        notes.append(f"修补失败：{type(exc).__name__}: {exc}")
    if dry_run and fix:
        stage_fix = {"ok": True, "dryRun": True,
                     "wouldDo": sorted({it.get("action") for it in (scan.get("issues") or [])
                                        if it.get("action") in FIXABLE})}
    elif not passes:
        stage_fix = {"ok": True, "skipped": "no-issues" if not fix else "nothing-fixable"}
    else:
        stage_fix["passes"] = passes
        stage_fix["done"] = fixed
    stages["fix"] = stage_fix

    # ---- 4) 重唱建议（起音哑音补不了） ----
    try:
        sug = vocaldoctor.suggest_score(base, section=section, apply=False)
        plan = sug.get("suggestions") or []
        stages["resing"] = {"ok": True, "notes": len(plan),
                            "detail": [{"char": p.get("char"), "start": p.get("start")}
                                       for p in plan[:12]]}
        if plan:
            notes.append(f"{len(plan)} 个起音哑音要重唱（改谱建议已写 "
                         f"analysis/vocal-fix-score-{tag}.json）")
    except Exception as exc:
        stages["resing"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---- 5) 可选调教 / 抄已有调教读数 ----
    if tune_style:
        try:
            src = None
            try:
                src = str(tune._vocal_file(base, section))
            except Exception:
                pass
            trep = tune.tune(base, section=section, style=tune_style)
            st = trep.get("stats") or {}
            stages["tune"] = {"ok": True, "style": tune_style, "input": src,
                              "pitchBefore": st.get("pitchBefore"),
                              "pitchAfter": st.get("pitchAfter"),
                              "breaths": st.get("breaths"), "phrases": st.get("phrases"),
                              "mixed": trep.get("mixed")}
        except Exception as exc:
            stages["tune"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        prev = None
        for p in sorted((base / "report").glob("tune-*.json")):
            prev = jload(p, None)
            break
        if prev:
            st = prev.get("stats") or {}
            stages["tune"] = {"ok": True, "source": "report/tune-*.json",
                              "pitchBefore": st.get("pitchBefore"),
                              "pitchAfter": st.get("pitchAfter"),
                              "breaths": st.get("breaths"), "phrases": st.get("phrases")}
        else:
            stages["tune"] = {"ok": True, "skipped": "no-tune"}

    # ---- 判定 ----
    left = {k: after.get(k, 0) for k in HARD if after.get(k)}
    # 只有严重档（晚 > 120 ms）才算「必须重唱」，75–120 ms 只提示
    resing_left = (severe.get("dead-onset") or 0) + (after.get("silent") or 0)
    if left:
        verdict = "needs-fix"
    elif resing_left:
        verdict = "needs-resing"
    else:
        verdict = "ok"

    report = {"ok": verdict == "ok", "verdict": verdict, "project": str(base),
              "section": section, "harmony": harmony, "tuneStyle": tune_style,
              "stages": stages, "issuesBefore": counts, "issuesAfter": after,
              "severeBefore": scan_before_severe, "severeAfter": severe,
              "remaining": left, "resingNotes": resing_left,
              "output": (stages.get("fix") or {}).get("output"),
              "notes": notes}
    if not dry_run:
        jdump(base / "report" / f"vocal-finish-{tag}.json", report)
        try:
            project.setdefault("vocal", {})["finish"] = {
                "verdict": verdict, "harmony": harmony,
                "issuesBefore": counts, "issuesAfter": after}
            save_project(base, project)
        except Exception:
            pass
    return report
