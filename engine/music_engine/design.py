"""作品方案：把一个音乐工程做成可交付的设计页面。

设计语言来自 歸藏设计（dsh-guizang-design）的 token 与排版规则：
瑞士风（Inter / 克莱因蓝 / 直角网格）或杂志风（衬线 / 纸色）。
这里只复用它的色值与规则（themes.json 是数据），HTML/CSS 由本插件生成，
不复制它的样式表；装了 dsh-guizang-design 时会优先读它的 themes.json。
"""
import json
import time
from pathlib import Path

SCHEMA = 1

# 兜底 token：与 dsh-guizang-design 0.1.4 themes.json 对齐
THEMES = {
    "swiss": {
        "default": "ikb",
        "colors": [
            {"id": "ikb", "name": "克莱因蓝", "paper": "#fafaf8", "ink": "#0a0a0a", "accent": "#002FA7"},
            {"id": "lemon", "name": "柠檬黄", "paper": "#fafaf8", "ink": "#0a0a0a", "accent": "#FFD500"},
            {"id": "lemon-green", "name": "柠檬绿", "paper": "#fafaf8", "ink": "#0a0a0a", "accent": "#C5E803"},
            {"id": "safety-orange", "name": "安全橙", "paper": "#fafaf8", "ink": "#0a0a0a", "accent": "#FF6B35"},
        ],
    },
    "magazine": {
        "default": "ink-classic",
        "colors": [
            {"id": "ink-classic", "name": "墨水经典", "paper": "#f1efea", "ink": "#0a0a0b", "accent": "#111111"},
            {"id": "indigo-porcelain", "name": "靛蓝瓷", "paper": "#f1f3f5", "ink": "#0a1f3d", "accent": "#315d93"},
            {"id": "forest-ink", "name": "森林墨", "paper": "#f5f1e8", "ink": "#1a2e1f", "accent": "#2e6b4f"},
            {"id": "kraft-paper", "name": "牛皮纸", "paper": "#eedfc7", "ink": "#2a1e13", "accent": "#9b5a2e"},
            {"id": "dune", "name": "沙丘", "paper": "#f0e6d2", "ink": "#1f1a14", "accent": "#8f7650"},
            {"id": "midnight-ink", "name": "午夜墨", "paper": "#0e0d0c", "ink": "#ece2cf", "accent": "#d4a04a"},
        ],
    },
}


def _np():
    import numpy as np
    return np


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _project_dirs(project_path):
    base = Path(project_path)
    if not base.is_dir():
        raise ValueError(f"工程不存在: {base}")
    if not (base / "project.json").exists():
        raise ValueError(f"缺少 project.json: {base}")
    return base


def _guizang_dirs():
    import os
    cands = []
    env = os.environ.get("MUSIC_GUIZANG_PLUGIN")
    if env:
        cands.append(Path(env))
    cands.append(Path(__file__).absolute().parents[3] / "dsh-guizang-design")
    cands.append(Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-guizang-design")
    return [p for p in cands if p.is_dir()]


def _load_themes():
    for base in _guizang_dirs():
        f = base / "skills" / "guizang-page-skill" / "assets" / "themes.json"
        data = _load_json(f)
        if data and "swiss" in data and "magazine" in data:
            out = {}
            for fam in ("swiss", "magazine"):
                out[fam] = {
                    "default": data[fam].get("default"),
                    "colors": [
                        {"id": c["id"], "name": c.get("name", c["id"]),
                         "paper": c.get("paper"), "ink": c.get("ink"), "accent": c.get("accent")}
                        for c in data[fam].get("colors", [])
                    ],
                }
            return out, f
    return THEMES, None


def _latest(base, pattern, require=()):
    files = sorted((base / "report").glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        return None, None
    for p in reversed(files):
        try:
            data = _load_json(p) or {}
        except Exception:
            continue
        if all(data.get(k) is not None for k in require):
            return p, data
    return files[-1], _load_json(files[-1])


def _fmt(x, nd=1):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _measure_mix(path):
    """从混音文件补测报告里缺的响度/峰值/重心/立体声相关。"""
    import numpy as np
    from . import dsp
    try:
        data, sr = dsp.load(str(path))
    except Exception:
        return {}
    if data.size == 0:
        return {}
    mono = data.mean(axis=1)
    out = {}
    try:
        import pyloudnorm as pyln
        out["lufs"] = float(pyln.Meter(sr).integrated_loudness(mono))
    except Exception:
        pass
    peak = float(np.max(np.abs(data)))
    out["truePeak"] = float(20.0 * np.log10(max(peak, 1e-9)))
    rms = float(np.sqrt(np.mean(mono ** 2)))
    out["crest"] = out["truePeak"] - float(20.0 * np.log10(max(rms, 1e-9)))
    try:
        import librosa
        out["centroidHz"] = float(np.median(librosa.feature.spectral_centroid(y=mono, sr=sr)))
    except Exception:
        pass
    if data.shape[1] > 1:
        l, r = data[:, 0], data[:, 1]
        if float(np.std(l)) > 1e-6 and float(np.std(r)) > 1e-6:
            out["stereoCorr"] = float(np.corrcoef(l, r)[0, 1])
    return out


def _key_str(key):
    if not key:
        return "—"
    tonic = key.get("tonic", "?")
    mode = key.get("mode", "")
    if isinstance(key.get("pc"), int):
        tonic = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][key["pc"] % 12]
    return f"{tonic} {'小调' if mode == 'minor' else '大调'}" if mode else tonic


def _pitch_rows(stats):
    rows = []
    for label, key in (("调教前", "pitchBefore"), ("调教后", "pitchAfter")):
        p = (stats or {}).get(key) or {}
        med = p.get("medianAbsCents")
        p90 = p.get("p90AbsCents")
        if med is None:
            continue
        rows.append({"label": label, "median": med, "p90": p90})
    return rows


def _rel(base, path):
    if not path:
        return None
    p = Path(path)
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return p.as_uri()


def _audio_block(base, path, label):
    rel = _rel(base, path)
    if not rel:
        return ""
    # 页面落在 <工程>/design/ 下，音频在 <工程>/audio/，所以要 ../ 上去；
    # 工程外的文件 _rel 给的是 file:// URI，不能再加前缀。
    src = rel if rel.startswith("file:") else "../" + rel
    return (
        f'<div class="audio"><span class="kicker">{label}</span>'
        f'<audio controls preload="none" src="{src}"></audio></div>'
    )


CSS = r"""
:root{
  --paper:#fafaf8; --paper-2:#f0f0ee; --ink:#0a0a0a; --muted:#737373;
  --line:#d4d4d2; --line-subtle:#e0e0e0; --accent:#002FA7; --accent-soft:#d5deef;
  --font-display:'Inter','Noto Sans SC','PingFang SC','Hiragino Sans GB','Microsoft YaHei',system-ui,sans-serif;
  --font-sans:var(--font-display);
  --font-mono:'JetBrains Mono','Noto Sans SC',ui-monospace,'SF Mono',Menlo,monospace;
  --font-serif:'Noto Serif SC','Source Han Serif SC','Songti SC',STSong,Georgia,serif;
}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--font-sans);
  font-size:17px;line-height:1.8;letter-spacing:0;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:0 80px}
.kicker{font-family:var(--font-mono);font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted)}
.accent{color:var(--accent)}
.rule{height:1px;background:var(--line);border:0;margin:0}
.hero{padding:56px 0 56px;border-bottom:2px solid var(--ink)}
.mast{display:flex;justify-content:space-between;align-items:center;gap:24px;
  padding:18px 0 0;border-top:6px solid var(--accent)}
.hero .top{display:flex;justify-content:space-between;align-items:baseline;gap:24px;margin-top:24px}
.hero h1{font-size:76px;line-height:1.02;letter-spacing:-.02em;margin:28px 0 16px;
  font-weight:300;max-width:14em}
.hero h1::after{content:"";display:block;width:96px;height:6px;background:var(--accent);margin-top:28px}
.pull{font-size:26px;line-height:1.5;font-weight:300;max-width:26em;margin:32px 0 0;
  border-left:2px solid var(--accent);padding-left:20px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 0}
.chip{font-family:var(--font-mono);font-size:10px;letter-spacing:.06em;border:1px solid var(--line);
  padding:3px 8px;color:var(--muted)}
.chip b{color:var(--ink);font-weight:500}
.status{font-family:var(--font-mono);font-size:10px;letter-spacing:.08em;border:1px solid var(--accent);
  color:var(--accent);padding:2px 7px;margin-left:10px}
.status.warn{border-color:var(--line-strong);color:var(--muted)}
.hero .sub{font-size:19px;color:var(--muted);max-width:34em;margin:0 0 40px}
.meta{display:flex;gap:40px;flex-wrap:wrap;font-family:var(--font-mono);font-size:12px}
.meta b{display:block;font-weight:500;color:var(--muted);font-size:10px;letter-spacing:.14em;
  text-transform:uppercase;margin-bottom:4px}
.hero .badge{font-family:var(--font-mono);font-size:11px;border:1px solid var(--ink);
  padding:4px 10px;white-space:nowrap}
.hero .meta{margin-top:40px}
section{padding:56px 0 0}
.sec-head{display:flex;align-items:baseline;gap:20px;margin-bottom:28px}
.sec-head .no{font-family:var(--font-mono);font-size:12px;color:var(--accent)}
.sec-head h2{font-size:13px;letter-spacing:.16em;text-transform:uppercase;
  font-weight:600;margin:0}
.big{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
  border:1px solid var(--line);margin-bottom:28px}
.big div{background:var(--paper);padding:24px 20px}
.big b{display:block;font-size:44px;font-weight:300;line-height:1;letter-spacing:-.02em}
.big span{display:block;margin-top:10px;font-size:11px;color:var(--muted);
  font-family:var(--font-mono);letter-spacing:.1em;text-transform:uppercase}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:56px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:24px}
p{margin:0 0 1em;max-width:36em;color:#252524}
p.lead{font-size:19px;color:var(--ink);max-width:32em}
.def{border-left:2px solid var(--accent);padding-left:18px;margin:0 0 18px}
.def b{font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;color:var(--muted);
  display:block;margin-bottom:4px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:14px;margin:0 0 24px}
th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line-subtle)}
th{font-family:var(--font-mono);font-size:10px;letter-spacing:.12em;color:var(--muted);
  text-transform:uppercase;border-bottom:1px solid var(--line)}
td.num{font-family:var(--font-mono);font-variant-numeric:tabular-nums;text-align:right}
.bar{position:relative;height:12px;background:var(--paper-2);margin:6px 0 6px}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--accent)}
.def > em{display:block;font-family:var(--font-mono);font-size:11px;font-style:normal;
  color:var(--muted);margin:0 0 14px}
.bars{display:flex;align-items:flex-end;gap:2px;height:72px;margin:8px 0 6px}
.bars i{flex:1;background:var(--accent);min-height:2px;opacity:.85}
.bars .cap{display:none}
.timeline{display:flex;gap:2px;margin:8px 0 4px}
.timeline i{height:8px;background:var(--accent);opacity:calc(.25 + .75*var(--e))}
.legend{display:flex;justify-content:space-between;font-family:var(--font-mono);
  font-size:10px;color:var(--muted);margin-top:6px;letter-spacing:.08em}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-top:8px}
.card{border-top:2px solid var(--ink);padding-top:16px}
.card .kicker{margin-bottom:8px;display:block}
.card h3{font-size:20px;margin:0 0 8px;font-weight:500}
.card p{font-size:14px;color:#525252;margin-bottom:12px}
.card code{font-family:var(--font-mono);font-size:11px;background:var(--paper-2);
  padding:2px 6px;display:inline-block}
.audio{display:flex;align-items:center;gap:14px;margin:10px 0 4px}
.audio audio{height:34px;width:100%;max-width:420px}
.note{font-size:13px;color:var(--muted);max-width:38em}
footer{margin-top:72px;border-top:2px solid var(--ink);padding:24px 0 56px;
  display:flex;justify-content:space-between;gap:32px;flex-wrap:wrap;
  font-family:var(--font-mono);font-size:10px;color:var(--muted);letter-spacing:.06em}
.swatches{display:flex;gap:6px;align-items:center}
.swatches i{width:18px;height:18px;border:1px solid var(--line)}
html[data-theme="midnight-ink"]{--paper:#0e0d0c;--paper-2:#1b1a17;--ink:#ece2cf;
  --muted:#a99e8b;--line:#3a352c;--line-subtle:#2a261f;--accent:#d4a04a;--accent-soft:#3b2f1a}
html[data-theme="midnight-ink"] p{color:#d8cdb9}
html[data-theme="midnight-ink"] .card p{color:#b8ad9a}
.mag h1,.mag h2,.mag h3,.mag p,.mag .sub{font-family:var(--font-serif)}
.mag h1{font-weight:400}
@media (max-width:1024px){
  .wrap{padding:0 40px}
  .hero h1{font-size:52px}
  .grid2,.grid3,.cards{grid-template-columns:1fr;gap:32px}
  .big{grid-template-columns:repeat(2,1fr)}
}
@media print{
  .wrap{padding:0 32px}
  section{break-inside:avoid}
  .audio{display:none}
  @page{margin:16mm}
}
"""


def _bars(values, max_v):
    """段落能量柱：直接给像素高度，避免百分比在 flex 容器里被拉伸。"""
    out = []
    for v in values:
        h = 6 if not v else max(6, int(round(64 * min(1.0, v / max_v))))
        out.append(f'<i style="height:{h}px"></i>')
    return "".join(out)


def design(project_path, kind="release", style="swiss", theme=None, deep=False):
    np = _np()
    base = _project_dirs(project_path)
    proj = _load_json(base / "project.json") or {}
    themes, theme_file = _load_themes()
    fam = "magazine" if style == "magazine" else "swiss"
    ids = [c["id"] for c in themes[fam]["colors"]]
    theme_id = theme if theme in ids else themes[fam]["default"]
    theme = next(c for c in themes[fam]["colors"] if c["id"] == theme_id)

    tune_path, tune = _latest(base, "tune-*.json", ("stats",))
    mix_path, mix = _latest(base, "mix-*.json", ("lufs",))
    stats = (tune or {}).get("stats") or {}

    align = _load_json(base / "analysis" / "vocal-align.json") or {}
    notes = align.get("notes") or []
    lyric_lines = []
    vocal = proj.get("vocal") or {}
    lyric_file = vocal.get("lyrics")
    if lyric_file:
        lyr = _load_json(base / lyric_file)
        if lyr:
            if lyr.get("text"):
                for ln in str(lyr["text"]).splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("[") and not ln.startswith("#"):
                        lyric_lines.append(ln)
            elif lyr.get("sections"):
                for sec in lyr["sections"]:
                    for ln in sec.get("lines") or []:
                        if ln.get("text"):
                            lyric_lines.append(str(ln["text"]))
    lyric_line = lyric_lines[0] if lyric_lines else ""
    for n in notes:
        if n.get("lineText"):
            lyric_line = str(n["lineText"])
            break
    lyric_sub = lyric_lines[1] if len(lyric_lines) > 1 else ""
    title = proj.get("title") or base.name
    if lyric_line and (title == base.name or title.lower() in ("tune-test", "untitled", "demo")):
        title = lyric_line
        lyric_sub = lyric_lines[1] if len(lyric_lines) > 1 else ""

    tempo = proj.get("tempo") or (mix or {}).get("bpm")
    duration = (mix or {}).get("duration") or stats.get("duration")
    key = _key_str(proj.get("key"))
    sections = proj.get("sections") or []
    notes_n = stats.get("notes") or (len(notes) or None)

    deep_info = None
    if deep:
        try:
            from . import soundcheck as soundcheck_mod
            mix_file = (mix or {}).get("mix")
            if mix_file and Path(mix_file).exists():
                raw = soundcheck_mod.analyze(mix_file, stems="4s")
                deep_info = soundcheck_mod.condense(raw)
        except Exception as exc:  # 深分析失败不阻断方案
            deep_info = {"error": str(exc)}

    pitch = _pitch_rows(stats)
    p_max = max([r["p90"] or r["median"] for r in pitch] + [1])

    def _pitch_bar(r):
        w = max(3.0, min(100.0, 100.0 * (r["p90"] or r["median"] or 0) / p_max))
        return (f'<div class="def"><b>{r["label"]}</b>'
                f'<div class="bar"><i style="width:{w:.1f}%"></i></div>'
                f'<em>{_fmt(r["median"])} 音分中位 · p90 {_fmt(r["p90"])}</em></div>')

    pitch_html = "".join(_pitch_bar(r) for r in pitch) \
        or '<p class="note">还没有调教报告：先跑 music_tune。</p>'

    energies = [float(s.get("energy") or 0) for s in sections]
    tl = "".join(f'<i style="flex:{max(1, int((s.get("bars") or 1))) };--e:{min(1.0, e)}"></i>'
                 for s, e in zip(sections, energies))
    sec_rows = "".join(
        f'<tr><td>{s.get("role", s.get("id"))}</td><td class="num">{s.get("startBar")}</td>'
        f'<td class="num">{s.get("bars")}</td><td class="num">{_fmt((s.get("energy") or 0) * 100, 0)}%</td></tr>'
        for s in sections
    ) or '<tr><td colspan="4" class="note">没有段落数据</td></tr>'

    lufs = (mix or {}).get("lufs")
    tp = (mix or {}).get("truePeak") or (mix or {}).get("peakDb")
    crest = (mix or {}).get("crest")
    centroid = (mix or {}).get("centroidHz")
    corr = (mix or {}).get("stereoCorr")
    tim = ((mix or {}).get("timing") or {}).get("vocal") or {}
    offset = tim.get("offsetMs")
    med = tim.get("medianMs")
    conf = tim.get("confidence")

    mix_file = (mix or {}).get("mix")
    if mix_file and (lufs is None or tp is None or crest is None or centroid is None or corr is None):
        mp = Path(mix_file) if Path(mix_file).is_absolute() else base / mix_file
        if mp.exists():
            mm = _measure_mix(mp)
            lufs = lufs if lufs is not None else mm.get("lufs")
            tp = tp if tp is not None else mm.get("truePeak")
            crest = crest if crest is not None else mm.get("crest")
            centroid = centroid if centroid is not None else mm.get("centroidHz")
            corr = corr if corr is not None else mm.get("stereoCorr")

    deep_html = ""
    if deep_info:
        if deep_info.get("error"):
            deep_html = f'<p class="note">深分析不可用：{deep_info["error"]}</p>'
        else:
            lo = deep_info.get("loudness") or {}
            stems = (deep_info.get("stems") or {}).get("ratios") or {}
            stem_txt = " · ".join(f"{k} {v * 100:.0f}%" for k, v in stems.items()) or "—"
            bounds = deep_info.get("structureBoundaries") or []
            b_txt = " · ".join(f'{b.get("sec")}s 变化 {b.get("jumpDb"):+.1f} dB' for b in bounds) or "无显著变化点"
            deep_html = (
                '<div class="grid2"><div>'
                f'<table><tr><th>深分析（整首/片段）</th><th>值</th></tr>'
                f'<tr><td>LUFS（真）</td><td class="num">{_fmt(lo.get("lufs"), 2)}</td></tr>'
                f'<tr><td>真峰值 dBTP</td><td class="num">{_fmt(lo.get("truePeakDb"), 2)}</td></tr>'
                f'<tr><td>削波</td><td class="num">{"有" if lo.get("clipped") else "无"}</td></tr>'
                f'<tr><td>分轨占比</td><td class="num">{stem_txt}</td></tr>'
                '</table></div><div>'
                f'<p class="note">结构变化：{b_txt}</p>'
                f'<p class="note">拍速：{_fmt((deep_info.get("bpm") or {}).get("primary"), 1)} BPM；'
                f'调性：{((deep_info.get("key") or {}).get("best") or "—")}</p>'
                '</div></div>'
            )

    h = time.strftime("%Y-%m-%d %H:%M")
    style_cls = "mag" if fam == "magazine" else ""
    css = CSS + f"\n:root{{--paper:{theme['paper']};--ink:{theme['ink']};--accent:{theme['accent']};}}\n"

    ab = (tune or {}).get("ab")
    mixed = (tune or {}).get("mixed")
    mix_file = (mix or {}).get("mix")
    audio_html = "".join([
        _audio_block(base, ab, "A/B 对照"),
        _audio_block(base, mixed, "调教混音"),
        _audio_block(base, mix_file, "整首混音"),
    ])

    tracks = proj.get("tracks") or []
    chips = "".join(
        '<span class="chip"><b>{}</b> {}dB {}</span>'.format(
            t.get("id", "?"),
            _fmt(t.get("gainDb"), 1),
            "C" if abs(float(t.get("pan") or 0)) < 0.01 else
            ("L" if float(t.get("pan") or 0) < 0 else "R") + str(int(abs(float(t.get("pan") or 0)) * 100)),
        )
        for t in tracks
    )

    def _status(label, ok):
        return f'<span class="status{" warn" if not ok else ""}">{label}</span>'

    lufs_ok = lufs is not None and abs(float(lufs) + 14.0) <= 1.0
    tp_ok = tp is not None and float(tp) <= -1.0
    # 对拍看「离 16 分网格的中位距离」（medianMs，逐音起音实量）；
    # offsetMs 是相对乐谱的整体偏移，低置信度时常锁到相邻音符，只作附注。
    off_ok = med is not None and float(med) <= 40.0
    mix_status = "".join(
        s for s in [
            _status(f"{_fmt(lufs, 1)} LUFS", lufs_ok) if lufs is not None else "",
            _status("峰值留 ≥1 dB", tp_ok) if tp is not None else "",
            _status("人声离拍中位 ≤40 ms", off_ok) if med is not None else "",
        ] if s
    )

    html = f"""<!DOCTYPE html>
<html lang="zh" data-theme="{theme_id}" class="{style_cls}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 作品方案</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@200..700&family=Noto+Sans+SC:wght@200..700&family=JetBrains+Mono:wght@400;500&family=Noto+Serif+SC:wght@300..700&display=swap">
<style>{css}</style>
</head>
<body>
<div class="wrap">
  <div class="mast">
    <span class="kicker accent">DSH Music / Release Brief</span>
    <span class="kicker">{h}</span>
  </div>
  <header class="hero">
    <div class="top">
      <span class="kicker">作品方案 · 01</span>
      <span class="badge">{theme['name']} · {fam.upper()}</span>
    </div>
    <h1>{title}</h1>
    <p class="sub">{lyric_sub or '一首用真实 SVS 人声、调教与混音链做出来的作品。'}</p>
    <p class="pull">先把「唱得准」做成「会唱」，再把「会唱」放进伴奏里。</p>
    <div class="meta">
      <div><b>曲风</b>{proj.get('genre') or '—'}</div>
      <div><b>速度</b>{_fmt(tempo, 0)} BPM</div>
      <div><b>调性</b>{key}</div>
      <div><b>时长</b>{_fmt(duration, 1)} s</div>
      <div><b>歌词音符</b>{_fmt(notes_n, 0)}</div>
      <div><b>主题</b>{theme['name']}</div>
    </div>
  </header>

  <section>
    <div class="sec-head"><span class="no">01</span><h2>曲目档案</h2></div>
    <div class="big">
      <div><b>{_fmt(tempo, 0)}</b><span>BPM</span></div>
      <div><b>{key.replace(' 小调', 'm').replace(' 大调', '')}</b><span>KEY</span></div>
      <div><b>{_fmt(duration, 1)}</b><span>秒</span></div>
      <div><b>{len(sections) or '—'}</b><span>段落</span></div>
    </div>
    <div class="grid2">
      <div>
        <p class="lead">这不是「点一下生成」的伴奏：人声由本地 SVS 按谱唱出，再在音频层加滑音、颤音、句尾下落和换气，最后走一遍真实感混音链。</p>
        <p>方案页把可验证的数字放在一起：唱得准不准、有没有气口、混音有没有余量、段落什么能量。数字都来自工程里的报告文件，不是估的。</p>
      </div>
      <div>
        <table>
          <tr><th>项目</th><th>值</th></tr>
          <tr><td>拍号</td><td class="num">{proj.get('meter') or '—'}</td></tr>
          <tr><td>采样率</td><td class="num">{_fmt(proj.get('sampleRate'), 0)} Hz</td></tr>
          <tr><td>工程 ID</td><td class="num">{proj.get('id') or base.name}</td></tr>
          <tr><td>工程目录</td><td class="num">{base.name}</td></tr>
        </table>
        <span class="kicker">编制</span>
        <div class="chips">{chips}</div>
      </div>
    </div>
  </section>

  <section>
    <div class="sec-head"><span class="no">02</span><h2>演唱设计</h2></div>
    <div class="grid2">
      <div>
        <p>调教目标是把「音高正确」变成「会唱」：起音有滑音、长音有颤音、句尾有下落、换行有气口。评测用与目标音高的音分偏差，中位数看准不准，p90 看最松的那一截。</p>
        {pitch_html}
        <table>
          <tr><th>统计</th><th>值</th></tr>
          <tr><td>音符 / 乐句</td><td class="num">{_fmt(stats.get('notes'), 0)} / {_fmt(stats.get('phrases'), 0)}</td></tr>
          <tr><td>气口</td><td class="num">{_fmt(stats.get('breaths'), 0)}</td></tr>
          <tr><td>风格 / 强度</td><td class="num">{(tune or {}).get('style', '—')} / {_fmt((tune or {}).get('intensity'), 2)}</td></tr>
        </table>
      </div>
      <div>
        <p class="note">参考的混音链：共振峰塑形 → 高频 EQ → 2A 类光电压缩 → 板式混响发送 → 左右加倍。插件里对应 <code>music_tune</code> 的 <code>_realism</code> 阶段。</p>
        {audio_html or '<p class="note">还没有可试听的成品：先跑 music_tune。</p>'}
      </div>
    </div>
  </section>

  <section>
    <div class="sec-head"><span class="no">03</span><h2>混音验收</h2>{mix_status}</div>
    <div class="big">
      <div><b>{_fmt(lufs, 1)}</b><span>LUFS</span></div>
      <div><b>{_fmt(tp, 1)}</b><span>TRUE PEAK dB</span></div>
      <div><b>{_fmt(crest, 1)}</b><span>CREST dB</span></div>
      <div><b>{_fmt(med, 1)}</b><span>人声离拍中位 ms</span></div>
    </div>
    <div class="grid2">
      <div>
        <table>
          <tr><th>指标</th><th>值</th></tr>
          <tr><td>频谱重心</td><td class="num">{_fmt(centroid, 0)} Hz</td></tr>
          <tr><td>立体声相关</td><td class="num">{_fmt(corr, 2)}</td></tr>
          <tr><td>混音文件</td><td class="num">{(Path(mix_file).name if mix_file else '—')}</td></tr>
          <tr><td>调教报告</td><td class="num">{(tune_path.name if tune_path else '—')}</td></tr>
        </table>
      </div>
      <div>{deep_html or '<p class="note">加 <code>deep: true</code> 会再跑一次声检深分析：真峰值、削波、分轨占比和结构变化点。</p>'}</div>
    </div>
  </section>

  <section>
    <div class="sec-head"><span class="no">04</span><h2>结构</h2></div>
    <div class="bars">{_bars(energies, 1.0)}</div>
    <div class="legend"><span>{_fmt(0, 0)}s</span><span>段落能量</span><span>{_fmt(duration, 1)}s</span></div>
    <div class="timeline">{tl}</div>
    <table style="margin-top:24px">
      <tr><th>段落</th><th>起始小节</th><th>小节数</th><th>能量</th></tr>
      {sec_rows}
    </table>
  </section>

  <section>
    <div class="sec-head"><span class="no">05</span><h2>发布计划</h2></div>
    <div class="cards">
      <div class="card"><span class="kicker">01 / WEB PAGE</span><h3>精排单页</h3>
        <p>把这首的制作笔记排成能发出去的网页：杂志风或瑞士风，和本方案同一套 token。</p>
        <code>/guizang-page-skill</code></div>
      <div class="card"><span class="kicker">02 / SOCIAL CARDS</span><h3>图文卡片</h3>
        <p>3:4 组图、21:9 头图、1:1 分享卡，一次出齐，标题从方案里的数据来。</p>
        <code>/guizang-social-card-skill</code></div>
      <div class="card"><span class="kicker">03 / SLIDES</span><h3>网页 PPT</h3>
        <p>发布会/复盘用横向翻页稿，封面、数据大字报和讲稿备注都在一个 HTML 里。</p>
        <code>/guizang-ppt-skill</code></div>
    </div>
    <p class="note" style="margin-top:24px">这套物料建议同一主题：本页用的是 <b>{theme['name']}</b>（{theme['accent']}）。做卡片和 PPT 时把同一个主题名带过去，字体与色值就还是同一家人。</p>
  </section>

  <footer>
    <span>DSH Music · 作品方案 v{SCHEMA} · {h}</span>
    <span class="swatches">TOKENS
      <i style="background:{theme['paper']}"></i>
      <i style="background:{theme['ink']}"></i>
      <i style="background:{theme['accent']}"></i>
      <i style="background:#d4d4d2"></i>
    </span>
    <span>设计语言参考 歸藏设计 dsh-guizang-design（AGPL-3.0）的 token 与排版规则{'; 主题取自 ' + theme_file.name if theme_file else '；内置兜底 token'}</span>
  </footer>
</div>
</body>
</html>
"""

    out_dir = base / "design"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() or c in "-_." else "-" for c in base.name)
    out = out_dir / f"{slug}-{fam}-{theme_id}-plan.html"
    out.write_text(html, encoding="utf-8")
    meta = {
        "html": str(out),
        "project": str(base),
        "title": title,
        "kind": kind,
        "style": fam,
        "theme": theme_id,
        "themeSource": str(theme_file) if theme_file else None,
        "duration": duration,
        "sources": {"tune": str(tune_path) if tune_path else None,
                    "mix": str(mix_path) if mix_path else None},
        "bytes": out.stat().st_size,
    }
    (out_dir / f"{slug}-{fam}-{theme_id}-plan.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
