#!/usr/bin/env python3
"""music_gc.py —— dsh-music 工程空间清理器 / disk reclaimer.

默认 dry-run：只读，不动磁盘；给出按类别、按工程的回收报告。
确认范围后加 --apply：把文件「隔离」搬到隔离区并保留相对路径 + manifest.json，
任何一步都能原样搬回。只有 --purge（或 --purge-now）才真正删除。

隔离区放在**另一块盘**（--trash-dir X:\\_music_trash，X ≠ 工程所在盘）时，文件是复制后删除，
原盘空间当场腾出来，同时还能 --restore —— 这是「既省空间又能反悔」的唯一办法。
注意工程根常常是 junction（例如 C:\\Users\\你的用户名\\Music 指向另一块盘），
默认隔离区 <root>\\_trash 与工程**同盘**，此时 --apply 只是改目录项、不释放空间，必须再 --purge。

分类（宁可少删，绝不误删）：
  keep         工程元数据与不可再生产物：project.json / midi / lyrics / report /
               export / design / source / analysis / session / logs / ref / deliver，
               以及滚动目录里最新的几份 master 与 tuned 人声。
  regenerable  能由 midi/ 重跑 music_render 得到的音频：audio/render-*/、audio/raw-*/、
               preview/、frames/、audio/ 下散落的 wav。
  history      同类文件里超出保留份数的旧版本：audio/mix/*、audio/master/*、audio/vocal/*
  junk         环境垃圾：__pycache__ / *.pyc / *.log / .tmp / *.bak / *.tgz /
               工程里误装的 cpu_torch、dl、venv、site-packages、node_modules

手搭工作区（没有 project.json 的目录，例如 projects/biye-luori）默认**只报告**；
加 --orphans-junk 才清它里面的环境垃圾（cpu_torch/dl/__pycache__/*.whl 等），
绝不碰 .py/.wav/.mid/.lrc/.md。

用法：
  python tools/music_gc.py                        # dry-run，完整报告
  python tools/music_gc.py --summary              # 只看合计
  python tools/music_gc.py --projects a,b         # 只处理指定工程（名字或相对路径）
  python tools/music_gc.py --apply                # 隔离（可还原），写 manifest
  python tools/music_gc.py --apply --only junk    # 只清垃圾
  python tools/music_gc.py --apply --trash-dir X:\\_music_trash  # 跨盘隔离（X 不是工程所在盘）：原盘立刻腾空
  python tools/music_gc.py --apply --orphans-junk # 连手搭工作区里的环境垃圾一起清
  python tools/music_gc.py --apply --purge-now    # 隔离完立刻真删（不可还原）
  python tools/music_gc.py --restore "<隔离区>\\manifest.json"
  python tools/music_gc.py --purge --older-than 0  # 立刻清空隔离区（真删）
  python tools/music_gc.py --dupes                # >4MB 的完全重复文件
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 元数据与不可再生产物：怎么都不能自动删
KEEP_DIRS = {"midi", "lyrics", "report", "export", "design", "deliver",
             "source", "analysis", "session", "logs", "ref", "tools"}
KEEP_FILES = {"project.json"}

# 环境垃圾目录：命中即 junk（整棵子树）
JUNK_DIR_NAMES = {"__pycache__", ".tmp", ".cache", "node_modules",
                  "cpu_torch", "dl", "venv", ".venv", "site-packages", "dist-info"}
JUNK_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".tgz", ".whl", ".egg-info"}

# 手搭工作区里允许清的垃圾：只认「环境」，不认「内容」
ORPHAN_JUNK_SUFFIXES = {".pyc", ".pyo", ".whl", ".bak", ".tgz", ".tmp", ".egg-info"}

# 能由 midi 重渲的目录（前缀匹配，render-v1 / raw-backup 都算）
REGEN_DIRS = ("render", "preview", "frames", "scratch", "raw", "raw-backup", "stems")
# 滚动目录：同一目录里保留最新 N 份，其余算历史
ROLLING_DIRS = {"mix", "master", "vocal"}

ROOT_JUNK_NAMES = {"__pycache__", ".tmp"}
MIN_DUPE_MB = 4

COLS = ("regenerable", "history", "junk")
LABEL = {"regenerable": "可再生", "history": "历史版本", "junk": "垃圾"}


def human(n: float) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{int(n)} B"


def free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1 << 30)
    except OSError:
        return float("nan")


_VER_RE = re.compile(r"-v(\d+)$")


def ver_key(name: str) -> tuple[int, float]:
    """文件名里的版本序号；manual-v8 > manual-v2，没有序号就看 0。"""
    m = _VER_RE.search(Path(name).stem)
    if m:
        return (int(m.group(1)), 0.0)
    m = re.search(r"(\d+)$", Path(name).stem)
    if m:
        return (int(m.group(1)), 0.0)
    return (0, 0.0)


def classify(project: Path, f: Path) -> str:
    """单个文件的静态分类；滚动目录的 keep/history 由 scan_project 统一裁决。"""
    rel = f.relative_to(project)
    parts = rel.parts

    # 1. 环境垃圾目录优先（哪怕在 export/ 里）
    if any(p in JUNK_DIR_NAMES for p in parts[:-1]) or rel.name in ROOT_JUNK_NAMES:
        return "junk"

    # 2. 元数据与不可再生资产 —— 在扩展名判定之前，免得 logs/ 里的 .log 被当垃圾
    if rel.name in KEEP_FILES or (parts and parts[0] in KEEP_DIRS):
        return "keep"

    if f.suffix.lower() in JUNK_SUFFIXES:
        return "junk"

    # 3. audio/ 下细分
    if len(parts) >= 2 and parts[0] == "audio":
        sub = parts[1]
        if sub in ROLLING_DIRS:
            return f"rolling:{sub}"
        if any(sub.startswith(prefix) for prefix in REGEN_DIRS):
            return "regenerable"
        return "regenerable" if f.suffix.lower() in {".wav", ".flac", ".mp3", ".raw"} else "keep"

    # 4. 其它位置的音频：能重渲的派生物
    if parts and parts[0] in REGEN_DIRS:
        return "regenerable"
    if f.suffix.lower() in {".wav", ".flac"} and len(parts) <= 2:
        return "regenerable"

    return "keep"


def referenced_audio(project: Path) -> set[Path]:
    """report/*.json 里登记过的音频路径 —— 报告点名过的成品永不自动删。

    report/master-v1.json、report/mix-v1.json 里存的是绝对路径（有时是相对路径），
    它们就是「这一版交付到底在哪」的唯一凭据。只按版本号保留最新 N 份会把这些文件
    删掉，报告还在却指不到文件，等于把交付链接弄断了。
    """
    out: set[Path] = set()
    rdir = project / "report"
    if not rdir.is_dir():
        return out
    exts = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
    for jf in rdir.glob("*.json"):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
            elif isinstance(cur, str) and cur.lower().endswith(exts):
                p = Path(cur)
                if not p.is_absolute():
                    p = project / cur
                try:
                    out.add(p.resolve())
                except OSError:
                    pass
    return out


def scan_project(project: Path, keep_mix: int, keep_master: int) -> dict:
    """扫一个工程：返回分类后的文件清单与体积。"""
    out = {"project": project, "keep": [], "regenerable": [], "history": [],
           "junk": [], "rolling": {}, "protected": []}
    if not project.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(project):
        here = Path(dirpath)
        # 跳过隔离区，以及嵌套的子工程（它自己会被单独统计）
        dirnames[:] = [d for d in dirnames
                       if d != "_trash" and not (here / d != project and (here / d / "project.json").exists())]
        for name in filenames:
            f = here / name
            try:
                f.stat()
            except OSError:
                continue
            cat = classify(project, f)
            if cat.startswith("rolling:"):
                out["rolling"].setdefault(cat.split(":", 1)[1], []).append(f)
            else:
                out[cat].append(f)

    refs = referenced_audio(project)
    # 滚动目录内部按「文件名序号」保留最新 N 份，其余算历史；
    # 被 report/*.json 点名的文件先拎出来，永远算 keep。
    for sub, files in out["rolling"].items():
        limit = keep_master if sub == "master" else keep_mix
        pinned, rest = [], []
        for p in files:
            try:
                (pinned if p.resolve() in refs else rest).append(p)
            except OSError:
                rest.append(p)
        rest.sort(key=lambda p: (ver_key(p.name), p.stat().st_mtime), reverse=True)
        out["keep"].extend(pinned)
        out["protected"].extend(pinned)
        out["keep"].extend(rest[:limit])
        out["history"].extend(rest[limit:])
    out["rolling"] = {}

    for key in COLS + ("keep",):
        out[f"{key}_bytes"] = sum(f.stat().st_size for f in out[key] if f.exists())
    return out


def iter_projects(root: Path) -> list[Path]:
    """递归找出所有带 project.json 的工程（深度 4 以内）。"""
    found: list[Path] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        if depth >= 4:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d != "_trash"]
        if "project.json" in filenames:
            found.append(here)
            # 仍然向下走，免得漏掉嵌套在工程里的子工程（如 export/v1）
    return sorted(found)


def match_project(project: Path, root: Path, filt: set[str] | None) -> bool:
    if not filt:
        return True
    rel = str(project.relative_to(root)).replace("\\", "/")
    low = {x.lower() for x in filt}
    return project.name.lower() in low or rel.lower() in low or rel.replace("/", "\\").lower() in low


def scan_orphans(root: Path, known: list[Path]) -> list[dict]:
    """没有 project.json 的目录：只挑环境垃圾，内容一律不碰。"""
    out = []
    known_s = {str(p) for p in known}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name in ROOT_JUNK_NAMES or entry.name.startswith("_"):
            continue
        if str(entry) in known_s:
            continue
        if any(str(p).startswith(str(entry) + os.sep) for p in known):
            continue  # 是某个工程的祖先目录，交给工程扫描
        junk: list[Path] = []
        total = 0
        for dirpath, _dirnames, filenames in os.walk(entry):
            rel_parts = Path(dirpath).relative_to(entry).parts
            in_junk_dir = any(p in JUNK_DIR_NAMES for p in rel_parts)
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                total += sz
                if in_junk_dir or p.suffix.lower() in ORPHAN_JUNK_SUFFIXES:
                    junk.append(p)
        if total > 10 * (1 << 20):
            out.append({"project": entry, "keep": [], "regenerable": [], "history": [],
                        "junk": junk, "rolling": {},
                        "keep_bytes": 0, "regenerable_bytes": 0, "history_bytes": 0,
                        "junk_bytes": sum(p.stat().st_size for p in junk if p.exists()),
                        "total_bytes": total})
    return out


def print_report(scan: list[dict], root: Path, summary_only: bool, orphans: list[dict]) -> None:
    totals = {c: 0 for c in COLS}
    if not summary_only:
        header = f"{'工程':<34} {'可再生':>10} {'历史版本':>10} {'垃圾':>10} {'合计':>10}"
        print(header)
        print("-" * 78)
    rows = sorted(scan, key=lambda s: -(s["regenerable_bytes"] + s["history_bytes"] + s["junk_bytes"]))
    for s in rows:
        row = [s[f"{c}_bytes"] for c in COLS]
        for c, v in zip(COLS, row):
            totals[c] += v
        if summary_only:
            continue
        rel = str(s["project"].relative_to(root)).replace("\\", "/")
        print(f"{rel:<34} {human(row[0]):>10} {human(row[1]):>10} "
              f"{human(row[2]):>10} {human(sum(row)):>10}")
    if not summary_only:
        print("-" * 78)
    grand = sum(totals.values())
    print(f"{'工程合计':<34} {human(totals['regenerable']):>10} "
          f"{human(totals['history']):>10} {human(totals['junk']):>10} {human(grand):>10}")

    pinned = [p for s in scan for p in (s.get("protected") or [])]
    if pinned:
        pb = sum(p.stat().st_size for p in pinned if p.exists())
        print(f"{'其中报告点名的成品（永不删）':<34} {len(pinned)} 个 {human(pb):>10}")

    if orphans and not summary_only:
        print("\n---- 无 project.json 的手搭工作区（默认只报告；--orphans-junk 才清环境垃圾）----")
        print(f"{'目录':<34} {'总计':>10} {'其中环境垃圾':>14}")
        for s in sorted(orphans, key=lambda x: -x["total_bytes"]):
            print(f"{s['project'].name:<34} {human(s['total_bytes']):>10} {human(s['junk_bytes']):>14}")
        oj = sum(s["junk_bytes"] for s in orphans)
        if oj:
            print(f"{'可清环境垃圾':<34} {human(oj):>10}")


def cmd_dupes(root: Path) -> None:
    buckets: dict[tuple[int, str], list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "_trash"]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < MIN_DUPE_MB * (1 << 20):
                continue
            h = hashlib.md5()
            try:
                with p.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            buckets.setdefault((st.st_size, h.hexdigest()), []).append(p)

    wasted = 0
    for (size, _), paths in sorted(buckets.items(), key=lambda kv: -kv[0][0]):
        if len(paths) < 2:
            continue
        wasted += size * (len(paths) - 1)
        print(f"\n{human(size)} × {len(paths)} 可省 {human(size * (len(paths) - 1))}")
        for p in paths:
            print(f"   {p.relative_to(root)}")
    print(f"\n重复浪费合计：{human(wasted)}")


def apply_quarantine(scan: list[dict], root: Path, only: set[str], stamp: str,
                     trash_base: Path | None = None) -> Path:
    trash = (trash_base or (root / "_trash")) / stamp
    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "root": str(root), "trash": str(trash),
                "mode": "quarantine", "items": []}
    mpath = trash / "manifest.json"

    def flush() -> None:
        trash.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    moved = 0
    moved_bytes = 0
    per_cat: dict[str, int] = {}
    errors = 0
    cross = bool(trash.drive and root.drive and trash.drive.upper() != root.drive.upper())
    if cross:
        print(f"隔离区在另一个盘（{trash.drive}），复制后删除：{root.drive} 的空间会真正腾出来，且仍可还原。")
    for s in scan:
        for cat in COLS:
            if only and cat not in only:
                continue
            for f in s.get(cat, []):
                try:
                    rel = f.relative_to(root)
                    size = f.stat().st_size
                    dst = trash / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dst))
                except OSError as e:
                    print(f"跳过 {f}: {e}", file=sys.stderr)
                    errors += 1
                    continue
                manifest["items"].append({"cat": cat, "rel": str(rel), "bytes": size})
                moved += 1
                moved_bytes += size
                per_cat[cat] = per_cat.get(cat, 0) + 1
                if moved % 25 == 0:
                    flush()
                    print(f"  …已移动 {moved} 个 / {human(moved_bytes)}", flush=True)
    flush()
    print(f"\n已隔离 {moved} 个文件 / {human(moved_bytes)} → {trash}")
    if per_cat:
        print("分类：" + "，".join(f"{LABEL.get(k, k)} {v} 个" for k, v in per_cat.items()))
    if errors:
        print(f"有 {errors} 个文件跳过（权限/占用），见上面的 stderr。")
    print(f"清单：{mpath}")
    print(f"还原：python tools/music_gc.py --restore \"{mpath}\"")
    if cross:
        print(f"原盘剩余空间：{free_gb(root):.1f} GB")
    else:
        print("注意：隔离区在同一块盘上，这一步还没腾出空间；确认无误后再 --purge 才是真删。")
    return trash


def cmd_restore(manifest_path: Path) -> None:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(data["root"])
    n = 0
    miss = 0
    for item in data["items"]:
        src = manifest_path.parent / item["rel"]
        dst = root / item["rel"]
        if not src.exists():
            print(f"缺失，跳过：{item['rel']}", file=sys.stderr)
            miss += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        n += 1
    print(f"已还原 {n} 个文件到 {root}" + (f"（{miss} 个缺失）" if miss else ""))


def cmd_purge(base: Path, older_than: int) -> None:
    trash_root = base if base.name == "_trash" else base / "_trash"
    if not trash_root.exists():
        print(f"没有隔离区：{trash_root}")
        return
    cutoff = time.time() - older_than * 86400
    freed = 0
    reports = trash_root.parent / "_gc_reports"
    for bucket in sorted(trash_root.iterdir()):
        if not bucket.is_dir() or bucket.stat().st_mtime > cutoff:
            continue
        size = sum(f.stat().st_size for f in bucket.rglob("*") if f.is_file())
        # 真删之前先把 manifest 压一份留档，事后还能查到删了什么
        mani = bucket / "manifest.json"
        if mani.exists():
            try:
                reports.mkdir(parents=True, exist_ok=True)
                with gzip.open(reports / f"{bucket.name}.manifest.json.gz", "wt", encoding="utf-8") as fh:
                    fh.write(mani.read_text(encoding="utf-8"))
            except OSError as e:
                print(f"manifest 留档失败（不影响删除）：{e}", file=sys.stderr)
        shutil.rmtree(bucket)
        freed += size
        print(f"已删除 {bucket.name}  ({human(size)})")
    print(f"\n本次释放：{human(freed)}")
    print(f"隔离区剩余：{free_gb(trash_root):.1f} GB 可用（该盘）")


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
    ap = argparse.ArgumentParser(description="dsh-music 工程空间清理器")
    ap.add_argument("--root", default=None, help="工程根（默认读 ~/.dsh/music/engine.json 的 root）")
    ap.add_argument("--projects", default=None, help="只处理这些工程，逗号分隔（名字或相对路径）")
    ap.add_argument("--keep-mix", type=int, default=2, help="mix/vocal 目录保留最新几份（默认 2）")
    ap.add_argument("--keep-master", type=int, default=2, help="master 目录保留最新几份（默认 2）")
    ap.add_argument("--apply", action="store_true", help="执行隔离（可还原）")
    ap.add_argument("--trash-dir", default=None,
                    help="隔离区放哪：默认 <root>\\_trash；给另一个盘（如 D:\\_music_trash）会立刻腾出原盘空间")
    ap.add_argument("--purge-now", action="store_true",
                    help="配合 --apply：隔离完立刻清空这一批（不保留还原能力，但空间立即释放）")
    ap.add_argument("--only", default=None, help="只处理这些类别：junk,history,regenerable")
    ap.add_argument("--orphans-junk", action="store_true",
                    help="连手搭工作区（无 project.json）里的环境垃圾一起清（只清垃圾，不碰内容）")
    ap.add_argument("--purge", action="store_true", help="删除隔离区里过期的内容")
    ap.add_argument("--older-than", type=int, default=7, help="--purge 的天数阈值（默认 7；0 = 立刻）")
    ap.add_argument("--restore", default=None, help="按 manifest 还原")
    ap.add_argument("--dupes", action="store_true", help="只报告完全重复的大文件")
    ap.add_argument("--summary", action="store_true", help="只打印汇总")
    args = ap.parse_args()

    if args.restore:
        cmd_restore(Path(args.restore))
        return 0

    root = Path(args.root) if args.root else default_root()
    if not root.exists():
        print(f"工程根不存在：{root}", file=sys.stderr)
        return 2
    root = root.resolve()

    if args.purge:
        cmd_purge(Path(args.trash_dir) if args.trash_dir else root, args.older_than)
        return 0

    if args.dupes:
        cmd_dupes(root)
        return 0

    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else set()
    if only - set(COLS):
        print("--only 只接受 junk,history,regenerable", file=sys.stderr)
        return 2

    filt = {x.strip() for x in args.projects.split(",") if x.strip()} if args.projects else None
    projects = [p for p in iter_projects(root) if match_project(p, root, filt)]
    if not projects:
        print(f"没找到带 project.json 的工程：{root}", file=sys.stderr)
        return 2

    print(f"工程根：{root}   可用空间 {free_gb(root):.1f} GB   工程数 {len(projects)}\n")
    scan = [scan_project(p, args.keep_mix, args.keep_master) for p in projects]

    orphans = scan_orphans(root, iter_projects(root)) if not filt else []
    print_report(scan, root, args.summary, orphans)

    total = sum(s["regenerable_bytes"] + s["history_bytes"] + s["junk_bytes"] for s in scan)
    oj = sum(s["junk_bytes"] for s in orphans) if args.orphans_junk else 0
    print(f"\n可回收合计：{human(total)}"
          + (f"（另加手搭工作区垃圾 {human(oj)} → {human(total + oj)}）" if oj else "")
          + "（dry-run，未改动磁盘）")

    if args.apply:
        targets = list(scan)
        if args.orphans_junk:
            targets += [o for o in orphans if o["junk"]]
        trunk_base = Path(args.trash_dir) if args.trash_dir else None
        trash = apply_quarantine(targets, root, only, time.strftime("%Y%m%d-%H%M%S"), trunk_base)
        if args.purge_now:
            size = sum(f.stat().st_size for f in trash.rglob("*") if f.is_file())
            shutil.rmtree(trash)
            print(f"--purge-now：已真删本批隔离内容，释放 {human(size)}")
            print(f"原盘剩余空间：{free_gb(root):.1f} GB")
    else:
        print("确认后执行：python tools/music_gc.py --apply [--only junk]")
        print("            隔离区与工程同盘时，--apply 不释放空间；腾空间再跑 --purge --older-than 0")
        print("            要「当场腾空又能反悔」，把 --trash-dir 指到**另一块盘**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
