"""声库识别接口：把磁盘上的声库/换声模型认出来，并如实说明本引擎能不能用。

设计原则：
  * 只读。不复制、不解密、不改动用户文件。
  * 认得出就说出是什么（格式、体积、能读到的元数据）。
  * 能用/不能用分开讲清楚，不能用的给出确切原因，而不是含糊地"不支持"。

本引擎（肥鱼声音服务）实际能加载的只有 feiyu-rvc-release-v1 包；
Vocaloid 的 .vdb/.ddb、Synthesizer V 的 .svpk 属于厂商私有格式，必须由对应引擎
解包并做授权校验——这不是本文件能绕过的，所以它们会被标成 loadable=False。
"""

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

# 认得的扩展名 → (kind, 说明)
EXT_KINDS = {
    ".vdb": ("vocaloid-voicebank", "Vocaloid 声库库文件（厂商私有，需 Vocaloid Editor 授权激活）"),
    ".ddb": ("vocaloid-database", "VOCALOID3 时代的声库数据库（厂商私有）"),
    ".svpk": ("synthv-package", "Synthesizer V 声库/资源包（需 SynthV Studio 授权激活）"),
    ".svp": ("synthv-project", "Synthesizer V 工程文件（不是声库）"),
    ".pth": ("rvc-model", "RVC 换声模型权重（需要配 .index 与包清单才能被本引擎使用）"),
    ".index": ("rvc-index", "RVC 检索索引（与 .pth 配对）"),
    ".ust": ("utau-project", "UTAU 工程文件"),
    ".ustx": ("openutau-project", "OpenUtau 工程文件"),
    ".zip": ("archive", "压缩包（可能是上面任一种的打包）"),
}

FEIYU_RVC_SCHEMA = "feiyu-rvc-release-v1"
FEIYU_TTS_SCHEMA = "feiyu-voice-local-release-v1"

# 目录里的说明/脚本不算声库，扫的时候直接跳过，免得报告里全是噪音
SKIP_EXTS = {".md", ".txt", ".py", ".json", ".ini", ".log", ".gitkeep"}


def _sha256(path, chunk=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _size_mb(path):
    try:
        return round(Path(path).stat().st_size / (1024 * 1024), 2)
    except OSError:
        return None


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _probe_sqlite(path):
    """Vocaloid 的 .vdb 是加密数据库；能打开就说明是明文 SQLite。"""
    info = {"sqlite": False}
    try:
        with open(path, "rb") as stream:
            head = stream.read(16)
        if not head.startswith(b"SQLite format 3\x00"):
            info["header"] = head[:8].hex()
            return info
        info["sqlite"] = True
    except OSError as exc:
        info["error"] = str(exc)
        return info
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                "select name from sqlite_master where type='table' limit 40"
            ).fetchall()
            info["tables"] = [r[0] for r in rows]
        finally:
            conn.close()
    except Exception as exc:
        info["note"] = "SQLite 头存在但打不开：" + type(exc).__name__
    return info


def _probe_zip(path):
    """svpk / pth(torch zip) / zip 都能用这一点看出容器结构。"""
    info = {"zip": False}
    if not zipfile.is_zipfile(path):
        return info
    info["zip"] = True
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            info["entries"] = len(names)
            info["sample"] = names[:12]
            for want in ("manifest.json", "config.json", "voice.json",
                         "model.json", "README.md", "LICENSE.txt", "NOTICE.md"):
                if want in names:
                    info.setdefault("found", []).append(want)
    except Exception as exc:
        info["note"] = type(exc).__name__
    return info


def _classify_dir(path):
    manifest = _read_json(path / "manifest.json")
    entry = {"path": str(path), "name": path.name, "kind": "directory"}
    if manifest:
        schema = manifest.get("schema")
        entry["manifest"] = {"schema": schema, "id": manifest.get("id"),
                             "version": manifest.get("version")}
        if schema == FEIYU_RVC_SCHEMA:
            script = path / "run_rvc.py"
            runtime = path / "runtime.local.json"
            hashes = path / "files_sha256.json"
            model = manifest.get("model") or {}
            pth = path / (model.get("pth") or "")
            idx = path / (model.get("index") or "")
            missing = [p.name for p, ok in
                       ((script, script.is_file()), (runtime, runtime.is_file()),
                        (hashes, hashes.is_file()), (pth, pth.is_file()),
                        (idx, idx.is_file())) if not ok]
            entry.update(kind="feiyu-rvc-package", format=FEIYU_RVC_SCHEMA,
                         sizeMB=_round_dir_mb(path), loadable=not missing,
                         reason=("可以直接用：music_vocals 里把 voice 设成这个包注册的名字"
                                 if not missing else
                                 "包结构不完整，缺：" + "、".join(missing)))
            entry["model"] = {"pth": model.get("pth"), "index": model.get("index")}
        elif schema == FEIYU_TTS_SCHEMA:
            entry.update(kind="feiyu-tts-package", format=schema,
                         sizeMB=_round_dir_mb(path), loadable=True,
                         reason="念白（TTS）包；唱歌用不到它")
        else:
            entry.update(kind="unknown-dir", format=schema, sizeMB=_round_dir_mb(path),
                         loadable=False, reason="manifest.schema 不认识")
        return entry
    pths = list(path.glob("*.pth"))
    if pths:
        entry.update(kind="rvc-model-dir", format="rvc", sizeMB=_round_dir_mb(path),
                     loadable=False,
                     reason="只有权重文件，还没有 feiyu-rvc-release-v1 包结构（manifest/校验/运行配置）")
        entry["model"] = {"pth": [p.name for p in pths]}
        return entry
    entry.update(kind="directory", sizeMB=_round_dir_mb(path), loadable=False,
                 reason="目录里没有 manifest.json，也没有 .pth 权重")
    return entry


def _round_dir_mb(path, limit=2000):
    total, seen = 0, 0
    for child in path.rglob("*"):
        if seen >= limit:
            break
        seen += 1
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)


def _classify_file(path):
    ext = path.suffix.lower()
    kind, note = EXT_KINDS.get(ext, ("unknown", "扩展名不认识"))
    entry = {"path": str(path), "name": path.name, "kind": kind,
             "format": ext.lstrip(".") or "none", "sizeMB": _size_mb(path),
             "note": note, "loadable": False}
    meta = {}
    if ext in (".vdb", ".ddb"):
        meta = _probe_sqlite(path)
        entry["reason"] = ("厂商私有格式：需要 Vocaloid Editor 解包并做授权激活，本引擎不读这种容器"
                           if not meta.get("sqlite")
                           else "是明文 SQLite，但 Vocaloid 声库表结构仍需厂商引擎解释")
        entry["hint"] = "想用这个音色：在装有授权的 Vocaloid 里唱，导出干声后接进工程"
    elif ext == ".svpk":
        meta = _probe_zip(path)
        entry["reason"] = "Synthesizer V 的资源包：需要 SynthV Studio 解包并授权激活"
        entry["hint"] = "同上——在 SynthV 里唱，导出干声再接进来"
    elif ext == ".svp":
        entry["reason"] = "这是工程文件不是声库，里面通常只有音符与参数，不含音色"
    elif ext == ".pth":
        meta = _probe_zip(path)
        meta.pop("zip", None)
        entry["reason"] = "是 RVC 权重，但还缺 .index 与 feiyu 包清单；本引擎不接受裸权重"
        entry["hint"] = "只有你拥有该音色使用权的模型，才适合打包进来"
    elif ext == ".index":
        entry["reason"] = "检索索引，需与同名 .pth 一起打包"
    elif ext in (".ust", ".ustx"):
        entry["reason"] = "UTAU/OpenUtau 工程，不含音色"
    elif ext == ".zip":
        meta = _probe_zip(path)
        entry["reason"] = "压缩包；里面若是 .svpk/.vdb 仍受同样的授权限制"
    else:
        entry["reason"] = note
    if meta:
        entry["meta"] = meta
    return entry


def _registered_feiyu(service_config):
    """已注册在声音服务里的包，标成可用。"""
    names = {}
    try:
        cfg = _read_json(service_config) or {}
    except Exception:
        return names
    base = Path(service_config).resolve().parent.parent
    for section in ("rvc", "tts"):
        for voice, item in (cfg.get(section) or {}).items():
            pkg = Path(item.get("package") or "")
            if not pkg.is_absolute():
                pkg = (base / pkg).resolve()
            names[str(pkg).lower()] = (voice, section)
    return names


def default_service_config():
    """声音服务的配置文件一般在肥鱼仓库里；找不到就返回 None（跳过"已注册"标注）。"""
    import os
    cands = []
    env = os.environ.get("FEIYU_CONFIG")
    if env:
        cands.append(Path(env))
    home = Path.home()
    cands += [
        home / "feiyu" / "server" / "configs" / "voice_service.json",
        home / "Music" / "feiyu_repo" / "server" / "configs" / "voice_service.json",
    ]
    for cand in cands:
        if cand.is_file():
            return cand
    return None


def scan(paths=None, do_hash=False, service_config=None):
    roots = []
    for item in (paths or []):
        p = Path(item).expanduser()
        if p.exists():
            roots.append(p)
    if not roots:
        default = Path(r"D:\DSH\voicebanks")
        default.mkdir(parents=True, exist_ok=True)
        roots = [default]

    registered = {}
    if service_config is None:
        service_config = default_service_config()
    if service_config:
        registered = _registered_feiyu(service_config)

    found = []
    for root in roots:
        if root.is_file():
            found.append(_classify_file(root))
            continue
        for child in sorted(root.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_file() and child.suffix.lower() in SKIP_EXTS:
                continue
            entry = _classify_dir(child) if child.is_dir() else _classify_file(child)
            voice = registered.get(str(child.resolve()).lower())
            if voice and entry.get("loadable"):
                entry["registeredAs"] = voice[0]
                entry["usage"] = f'music_vocals(project=..., voice="{voice[0]}")'
            if do_hash and child.is_file():
                try:
                    entry["sha256"] = _sha256(child)
                except OSError:
                    pass
            found.append(entry)

    by_format = {}
    for item in found:
        key = item.get("kind") or "unknown"
        by_format[key] = by_format.get(key, 0) + 1

    usable = [item["name"] for item in found if item.get("loadable")]
    return {
        "ok": True,
        "scanned": [str(r) for r in roots],
        "found": found,
        "summary": {"total": len(found), "loadable": len(usable),
                    "usableNames": usable, "byKind": by_format},
        "engineLoads": [FEIYU_RVC_SCHEMA, FEIYU_TTS_SCHEMA],
        "note": ("本引擎只加载 feiyu 包格式。Vocaloid / Synthesizer V 声库是厂商私有且需授权，"
                 "识别得到但加载不了；要把那些音色用进编曲，走外部引擎导出干声的桥接。"),
    }
