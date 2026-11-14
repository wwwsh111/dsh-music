"""音效库自测：不依赖引擎进程，直接跑模块。

用法（Windows）：
  set PYTHONIOENCODING=utf-8
  .venv\\Scripts\\python.exe tools\\test_library.py

会在临时目录里造几个 wav，走一遍 加库 → 扫描 → 搜索 → 标签/评分 → 重复 → 导出 → 进工程。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "engine"))

TMP = Path(tempfile.mkdtemp(prefix="dsh-lib-test-"))
os.environ["MUSIC_LIBRARY"] = str(TMP / "libdata")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from music_engine import library  # noqa: E402

PASS = []


def ok(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        raise SystemExit(1)


def make_wav(path: Path, sec=0.5, sr=48000, ch=2, freq=440.0):
    t = np.arange(int(sr * sec)) / sr
    sig = (0.4 * np.sin(2 * np.pi * freq * t)).astype("float32")
    data = np.stack([sig] * ch, axis=1) if ch > 1 else sig
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sr)


def main():
    root = TMP / "SFX"
    make_wav(root / "枪声_近距离.wav", 0.4)
    make_wav(root / "01_Whoosh_Soft.wav", 0.8, freq=220)
    make_wav(root / "Kick_Heavy.wav", 0.3, ch=1, freq=60)
    make_wav(root / "sub" / "Rain_Loop.wav", 2.0, ch=1, freq=100)

    r = library.roots(op="add", path=str(root), name="测试库")
    ok("加库", r.get("ok") and r["root"]["path"], r)
    rid = r["root"]["id"]

    s = library.scan(root=rid, max_seconds=30)
    ok("扫描", s["files"] == 4 and s["complete"], s)
    ok("扫描幂等", library.scan(root=rid, max_seconds=30)["added"] == 0)

    r = library.search("枪")
    ok("中文搜", r["total"] == 1 and r["items"][0]["display"].startswith("枪声"), r)
    ok("全拼搜", library.search("qiangsheng")["total"] == 1)
    ok("首字母搜", library.search("qs")["total"] == 1)

    r = library.search("dur:<1s")
    ok("时长过滤", r["total"] == 3, r)
    ok("采样率过滤", library.search("sr:>=48000")["total"] == 4)
    ok("声道过滤", library.search("ch:1")["total"] == 2)
    ok("格式过滤", library.search("type:wav")["total"] == 4)

    kick = library.search("Kick_Heavy")["items"][0]
    library.update(ident=kick["id"], rating=5, favorite=True,
                   tags_add=["鼓", "重击"], collections_add=["常用"])
    r = library.search("star:>=4")
    ok("评分过滤", r["total"] == 1 and r["items"][0]["favorite"], r)
    ok("标签过滤", library.search("#鼓")["total"] == 1)
    ok("收藏过滤", library.search(favorites=True)["total"] == 1)
    ok("集合过滤", library.search(collection="常用")["total"] == 1)

    got = library.get(ident=kick["id"])["item"]
    ok("波形峰值", len(got["peaks"]) == library.PEAK_POINTS, len(got["peaks"]))
    ok("标签回读", set(got["tags"]) == {"鼓", "重击"}, got["tags"])

    shutil.copy2(root / "Kick_Heavy.wav", root / "Kick_Heavy_copy.wav")
    library.scan(root=rid, max_seconds=30)
    dup = library.duplicates()
    ok("重复检测", dup["groups"] >= 1 and dup["files"] >= 2, dup)

    dest = TMP / "out"
    ex = library.export(query="type:wav", dest=str(dest), template="{n:00}_{name}")
    ok("批量导出", ex["copied"] == 5, ex)
    ok("导出命名", any(p.name.startswith("01_") for p in dest.glob("*.wav")),
       [p.name for p in dest.glob("*.wav")])
    ok("导出清单", (dest / "_export.csv").exists())

    proj = TMP / "proj"
    proj.mkdir()
    (proj / "project.json").write_text(json.dumps({
        "title": "libtest", "bpm": 120, "samplerate": 48000, "samplers": [],
        "tracks": [], "sections": [], "bars": 8,
    }), encoding="utf-8")
    u = library.use(project=str(proj), ident=kick["id"], track="sfx_kick")
    pj = json.loads((proj / "project.json").read_text(encoding="utf-8"))
    ok("进工程-采样器", u["ok"] and len(pj["samplers"]) == 1, u)
    ok("进工程-轨道", any(t["id"] == "sfx_kick" for t in pj["tracks"]), pj["tracks"])
    u2 = library.use(project=str(proj), ident=kick["id"], sampler_id="kick2",
                     track="sfx_kick2", copy=True)
    ok("进工程-复制", u2["copied"] and Path(u2["file"]).exists(), u2)

    st = library.stats()
    ok("统计", st["files"] == 5 and st["topTags"], {k: st[k] for k in
                                                   ("files", "missing", "topTags")})
    fd = library.folders(dir=str(root))
    ok("文件夹视图", any(e["name"] == "sub" for e in fd["entries"]), fd)

    (root / "sub" / "Rain_Loop.wav").unlink()
    library.scan(root=rid, max_seconds=30)
    ok("标记缺失", library.stats()["missing"] == 1, library.stats()["missing"])

    library.roots(op="remove", root_id=rid)
    ok("删库不删文件", library.stats()["files"] == 0 and root.is_dir())

    print(f"\n全部通过：{len(PASS)} 项。临时目录 {TMP}")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
