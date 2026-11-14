#!/usr/bin/env python3
"""从 hf-mirror 下载肥鱼声库（TTS / RVC），支持断点续传。

用法：
    python scripts/download_voice_models.py --dest <你的模型目录>
    python scripts/download_voice_models.py --list
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPOS = {
    "tts": "Esmerubphire/FeiyuVoice-CosyVoice3",
    "rvc": "Esmerubphire/FeiyuVoice-RVC",
}
DEFAULT_ENDPOINT = "https://hf-mirror.com"


def api(repo, endpoint):
    url = f"{endpoint}/api/models/{repo}?blobs=true"
    req = urllib.request.Request(url, headers={"user-agent": "dsh-music/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def verify(root):
    manifest = root / "files_sha256.json"
    if not manifest.exists():
        print(f"  (no manifest in {root})")
        return True
    data = json.loads(manifest.read_text(encoding="utf-8"))
    bad = []
    for rel, meta in data.items():
        p = root / rel
        if not p.exists():
            bad.append((rel, "missing")); continue
        if meta.get("bytes") is not None and p.stat().st_size != meta["bytes"]:
            bad.append((rel, f"size {p.stat().st_size} != {meta['bytes']}")); continue
        h = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != meta.get("sha256"):
            bad.append((rel, "sha256"))
    if bad:
        print(f"  verify FAILED: {len(bad)} file(s)")
        for rel, why in bad[:8]:
            print("   ", rel, why)
        return False
    print(f"  verify ok ({len(data)} files)")
    return True


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(url, dest, size=0):
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    if size and have == size:
        print(f"  skip {dest.name} ({human(size)})")
        return
    headers = {"user-agent": "dsh-music/0.1"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    mode = "ab" if have else "wb"
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, mode) as f:
        total = size or int(r.headers.get("content-length") or 0) + have
        got, last = have, time.time()
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            f.write(block)
            got += len(block)
            if time.time() - last > 1:
                pct = 100 * got / total if total else 0
                print(f"\r  {dest.name}: {pct:5.1f}%  {human(got)}/{human(total)}", end="")
                last = time.time()
    print(f"\r  {dest.name}: done ({human(dest.stat().st_size)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="models", help="模型根目录（服务端 server/models）")
    ap.add_argument("--only", choices=["all", "tts", "rvc"], default="all")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    names = ["tts", "rvc"] if args.only == "all" else [args.only]
    for key in names:
        repo = REPOS[key]
        info = api(repo, args.endpoint)
        files = info.get("siblings") or []
        total = sum(f.get("size") or 0 for f in files)
        print(f"[{key}] {repo}: {len(files)} files, {human(total)}")
        root = Path(args.dest) / repo.split("/")[-1]
        if args.verify_only:
            verify(root)
            continue
        if args.list:
            for f in files:
                print(f"  {human(f.get('size') or 0):>8}  {f['rfilename']}")
            continue
        for f in files:
            name = f["rfilename"]
            if name.endswith(".gitattributes"):
                continue
            url = f"{args.endpoint}/{repo}/resolve/main/{urllib.parse.quote(name)}"
            download(url, root / name, f.get("size") or 0)
        verify(root)
        print(f"[{key}] -> {root}")


if __name__ == "__main__":
    sys.exit(main())
