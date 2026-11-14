"""音效库：把硬盘上散落的音效变成能秒搜、能试听、能直接进工程的库。

移植自 xy040402/SoundVault（音匣）的思路，不是把它的 WPF 界面搬过来：

* **不搬文件**。索引只是指向原文件的路径，删库不会删素材，改库不会动素材。
* **元数据进 SQLite**（采样率/声道/位深/时长/波形峰值/拼音），搜索走 SQL + 内存过滤。
* **拼音搜索**：文件名是中文时，`枪声` `qiangsheng` `qs` 都能命中（pypinyin）。
* **过滤语法**沿用音匣：`#tag star:>=4 dur:<2s sr:>48k ch:2 type:wav`。

和本插件其它模块的关系：库里的文件可以直接 `use` 进工程——挂成采样器轨，
或者按模板批量导出到别处。索引本身不带音频数据，只有 240 点波形。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import sampler
from .config import ROOT
from .util import ensure_dir, jdump, jload, slugify

# ---------------------------------------------------------------- 常量

AUDIO_EXT = {
    ".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".mp3", ".mp2",
    ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".caf", ".w64", ".rf64",
}
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "node_modules", "__pycache__",
    ".git", ".svn", "lost+found",
}
PEAK_POINTS = 240
PEAK_MAX_SECONDS = 120.0
DEFAULT_TEMPLATE = "{n:00}_{name}"
DB_VERSION = 1

_LOCK = threading.RLock()


def lib_root() -> Path:
    """库数据目录。环境变量 MUSIC_LIBRARY 可以把索引放到别处（比如素材盘）。"""
    env = os.environ.get("MUSIC_LIBRARY")
    return Path(env).expanduser() if env else Path.home() / ".dsh" / "music" / "library"


def _db_path() -> Path:
    return lib_root() / "index.db"


def _store_path() -> Path:
    return lib_root() / "store.json"


def _now() -> float:
    return time.time()


def _iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dur(sec) -> str:
    if sec is None:
        return ""
    sec = float(sec)
    if sec < 60:
        return f"{sec:.2f}s"
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


# ---------------------------------------------------------------- 规范化

def _norm(path) -> str:
    """绝对路径 + 统一分隔符（Windows 用反斜杠，Linux 用正斜杠）。"""
    p = Path(str(path)).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = p.absolute()
    return os.path.normpath(str(p))


def _key(path) -> str:
    """大小写无关的索引键：Windows 上 D:\\SFX 和 d:\\sfx 是同一个目录。"""
    return os.path.normcase(_norm(path))


def _ident(path) -> str:
    return hashlib.sha1(_key(path).encode("utf-8")).hexdigest()[:12]


def _safe_name(name: str) -> str:
    """导出/复制用的安全文件名：非法字符换下划线，去掉尾部点和空格。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "").strip())
    s = s.strip(" .")
    return s or "untitled"


def _clean_display(stem: str) -> str:
    """显示名：去掉开头的轨号、括号里的平台标记，分隔符变空格。

    `01_枪声_近距离` → `枪声 近距离`。只影响显示和搜索，不改文件名。
    """
    s = str(stem or "")
    s = re.sub(r"^\s*\d{1,3}\s*[._\-、)]\s*", "", s)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip() or str(stem or "")


# ---------------------------------------------------------------- 拼音

_PY = None


def _pinyin():
    global _PY
    if _PY is None:
        try:
            from pypinyin import Style, lazy_pinyin
            _PY = (lazy_pinyin, Style)
        except Exception:
            _PY = (None, None)
    return _PY


def _pinyin_text(text: str) -> tuple[str, str]:
    """返回 (全拼, 首字母)。非中文原样保留，所以 `gun snd` 也能搜到。"""
    text = str(text or "")
    lazy_pinyin, Style = _pinyin()
    if lazy_pinyin is None:
        return text.lower(), text.lower()
    full = "".join(lazy_pinyin(text, style=Style.NORMAL, errors=lambda x: list(x))).lower()
    init = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER, errors=lambda x: list(x))).lower()
    return full, init


def _is_latin(word: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9]+", word or ""))


# ---------------------------------------------------------------- 库根目录

def _default_store() -> dict:
    return {"version": DB_VERSION, "roots": [], "settings": {
        "template": DEFAULT_TEMPLATE,
        "previewLoop": True,
    }, "exports": []}


def _store() -> dict:
    data = jload(_store_path(), None)
    if not isinstance(data, dict):
        data = _default_store()
    data.setdefault("roots", [])
    data.setdefault("settings", _default_store()["settings"])
    data.setdefault("exports", [])
    return data


def _save_store(data: dict):
    ensure_dir(lib_root())
    jdump(_store_path(), data)


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  key TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  root TEXT,
  dir TEXT,
  name TEXT,
  display TEXT,
  ext TEXT,
  size INTEGER DEFAULT 0,
  mtime REAL DEFAULT 0,
  sr INTEGER DEFAULT 0,
  ch INTEGER DEFAULT 0,
  bits INTEGER DEFAULT 0,
  dur REAL DEFAULT 0,
  peaks BLOB,
  py_full TEXT,
  py_init TEXT,
  ok INTEGER DEFAULT 1,
  err TEXT,
  missing INTEGER DEFAULT 0,
  scanned REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);
CREATE INDEX IF NOT EXISTS idx_files_dir ON files(dir);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_missing ON files(missing);

CREATE TABLE IF NOT EXISTS tags(name TEXT PRIMARY KEY COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS asset_tags(key TEXT, tag TEXT COLLATE NOCASE,
  PRIMARY KEY(key, tag));
CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag);

CREATE TABLE IF NOT EXISTS marks(
  key TEXT PRIMARY KEY,
  rating INTEGER DEFAULT 0,
  favorite INTEGER DEFAULT 0,
  used REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_marks_rating ON marks(rating);

CREATE TABLE IF NOT EXISTS collections(name TEXT PRIMARY KEY COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS collection_items(
  collection TEXT COLLATE NOCASE,
  key TEXT,
  PRIMARY KEY(collection, key)
);

CREATE TABLE IF NOT EXISTS saved_queries(
  name TEXT PRIMARY KEY COLLATE NOCASE,
  query TEXT
);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _db() -> sqlite3.Connection:
    ensure_dir(lib_root())
    conn = sqlite3.connect(str(_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- 探测音频

_SUBTYPE_BITS = {
    "PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32,
    "FLOAT": 32, "DOUBLE": 64, "ALAW": 8, "ULAW": 8, "IMA_ADPCM": 4,
    "MS_ADPCM": 4, "VORBIS": 0, "MPEG_LAYER_III": 0, "OPUS": 0,
}


def _probe(path: str) -> dict:
    """只读文件头拿元数据，不解码。soundfile 失败再退回 ffprobe。"""
    out = {"ok": 1, "err": "", "sr": 0, "ch": 0, "bits": 0, "dur": 0.0}
    try:
        import soundfile as sf
        info = sf.info(path)
        out.update(sr=int(info.samplerate), ch=int(info.channels),
                   dur=float(info.frames) / float(info.samplerate or 1),
                   bits=int(_SUBTYPE_BITS.get(str(info.subtype).upper(), 0)))
        return out
    except Exception as e:
        out["ok"] = 0
        out["err"] = f"{type(e).__name__}: {e}"
    # ffprobe 兜底（mp3/aac 的某些封装 libsndfile 读不了）
    try:
        import subprocess
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels,duration,bits_per_sample",
             "-of", "json", path],
            capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
            out.update(ok=1, err="", sr=int(st.get("sample_rate") or 0),
                       ch=int(st.get("channels") or 0),
                       bits=int(st.get("bits_per_sample") or 0),
                       dur=float(st.get("duration") or 0))
    except Exception:
        pass
    return out


def state():
    """库的全局状态：根目录、条目数、数据库大小。给工具和面板做入口。"""
    store = _store()
    with _LOCK, _db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        missing = conn.execute("SELECT COUNT(*) FROM files WHERE missing=1").fetchone()[0]
        dur = conn.execute("SELECT COALESCE(SUM(dur),0) FROM files WHERE missing=0").fetchone()[0]
        size = conn.execute("SELECT COALESCE(SUM(size),0) FROM files WHERE missing=0").fetchone()[0]
    return {
        "ok": True,
        "root": str(lib_root()),
        "db": str(_db_path()),
        "dbBytes": _db_path().stat().st_size if _db_path().exists() else 0,
        "roots": len(store["roots"]),
        "files": n,
        "missing": missing,
        "duration": round(float(dur), 2),
        "bytes": int(size),
        "settings": store.get("settings"),
    }


# ---------------------------------------------------------------- 根目录管理

def roots(op="list", path=None, name=None, root_id=None, root=None):
    if op == "list":
        store = _store()
        items = []
        with _LOCK, _db() as conn:
            for r in store["roots"]:
                row = conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(size),0) b, COALESCE(SUM(dur),0) d "
                    "FROM files WHERE root=? AND missing=0", (r["id"],)).fetchone()
                items.append({**r, "files": row["n"], "bytes": int(row["b"]),
                              "duration": round(float(row["d"]), 2),
                              "exists": Path(r["path"]).is_dir()})
        return {"ok": True, "roots": items}

    if op == "add":
        if not path:
            raise ValueError("add 需要 path")
        p = Path(str(path)).expanduser()
        if not p.is_dir():
            raise ValueError(f"目录不存在：{p}")
        p = Path(_norm(p))
        store = _store()
        for r in store["roots"]:
            if _key(r["path"]) == _key(p):
                return {"ok": True, "root": r, "already": True}
        rid = slugify(p.name) + "-" + hashlib.sha1(_key(p).encode()).hexdigest()[:6]
        item = {"id": rid, "name": name or p.name or str(p), "path": str(p),
                "added": _iso(), "scanned": None}
        store["roots"].append(item)
        _save_store(store)
        return {"ok": True, "root": item}

    if op == "remove":
        # root 可传 id 或路径（工具层/面板只传 root），path 保留给旧调用。
        ref = root_id or root
        store = _store()
        target = None
        for r in store["roots"]:
            if ref and (r["id"] == ref or _key(r["path"]) == _key(ref)):
                target = r
                break
            if path and _key(r["path"]) == _key(path):
                target = r
                break
        if not target:
            raise ValueError("找不到这个库目录")
        store["roots"] = [r for r in store["roots"] if r["id"] != target["id"]]
        _save_store(store)
        with _LOCK, _db() as conn:
            keys = [x[0] for x in conn.execute(
                "SELECT key FROM files WHERE root=?", (target["id"],)).fetchall()]
            conn.execute("DELETE FROM files WHERE root=?", (target["id"],))
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                q = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM asset_tags WHERE key IN ({q})", chunk)
                conn.execute(f"DELETE FROM marks WHERE key IN ({q})", chunk)
                conn.execute(f"DELETE FROM collection_items WHERE key IN ({q})", chunk)
        return {"ok": True, "removed": target, "files": len(keys),
                "note": "只删索引，硬盘上的音频文件没有动"}

    if op == "rename":
        store = _store()
        for r in store["roots"]:
            if r["id"] == root_id:
                r["name"] = name or r["name"]
                _save_store(store)
                return {"ok": True, "root": r}
        raise ValueError("找不到这个库目录")

    raise ValueError(f"不支持的 op：{op}")


# ---------------------------------------------------------------- 扫描

def _walk(root_path: str, deadline: float):
    """生成 (path, stat)，超时停下。返回是否走完。"""
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield full, st
            if time.time() > deadline:
                return False
    return True


def scan(root=None, max_seconds=20.0, probe_limit=0):
    """增量扫描：size/mtime 没变就跳过，只解新文件；有时间预算，可反复调用。"""
    store = _store()
    targets = [r for r in store["roots"]
               if (root is None or r["id"] == root or _key(r["path"]) == _key(root or ""))]
    if not targets:
        return {"ok": True, "scanned": 0, "added": 0, "updated": 0,
                "note": "还没有库目录，先 roots op=add"}

    deadline = time.time() + float(max_seconds or 20.0)
    t0 = time.time()
    added = probed = skipped = failed = 0
    complete = True
    seen_total = 0

    for r in targets:
        base = r["path"]
        if not Path(base).is_dir():
            complete = False
            continue
        seen = []
        done = True
        with _LOCK, _db() as conn:
            known = {row["key"]: (row["size"], row["mtime"]) for row in conn.execute(
                "SELECT key,size,mtime FROM files WHERE root=?", (r["id"],))}
            for full, st in _walk(base, deadline):
                key = _key(full)
                seen.append(key)
                old = known.get(key)
                if old and int(old[0]) == int(st.st_size) and abs(float(old[1]) - st.st_mtime) < 0.001:
                    conn.execute("UPDATE files SET missing=0 WHERE key=?", (key,))
                    skipped += 1
                    continue
                if probe_limit and probed >= int(probe_limit):
                    done = False
                    break
                info = _probe(full)
                probed += 1
                stem = Path(full).stem
                display = _clean_display(stem)
                full_py, init_py = _pinyin_text(display + " " + stem)
                conn.execute(
                    "INSERT INTO files(key,path,root,dir,name,display,ext,size,mtime,sr,ch,bits,dur,"
                    "peaks,py_full,py_init,ok,err,missing,scanned) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,0,?) "
                    "ON CONFLICT(key) DO UPDATE SET path=excluded.path,root=excluded.root,"
                    "dir=excluded.dir,name=excluded.name,display=excluded.display,ext=excluded.ext,"
                    "size=excluded.size,mtime=excluded.mtime,sr=excluded.sr,ch=excluded.ch,"
                    "bits=excluded.bits,dur=excluded.dur,py_full=excluded.py_full,"
                    "py_init=excluded.py_init,ok=excluded.ok,err=excluded.err,missing=0,"
                    "peaks=CASE WHEN files.size=excluded.size AND files.mtime=excluded.mtime "
                    "THEN files.peaks ELSE NULL END,scanned=excluded.scanned",
                    (key, _norm(full), r["id"], os.path.dirname(_norm(full)), stem, display,
                     Path(full).suffix.lower(), int(st.st_size), float(st.st_mtime),
                     info["sr"], info["ch"], info["bits"], info["dur"], full_py, init_py,
                     info["ok"], info["err"], _now()))
                if info["ok"]:
                    added += 1
                else:
                    failed += 1
                if time.time() > deadline:
                    done = False
                    break
            if done and seen:
                # 只有走完整棵目录才敢判定「没见到的文件丢了」，
                # 中途超时停下的那次不能标 missing，否则一次扫描把库打出一片红。
                for i in range(0, len(seen), 500):
                    chunk = seen[i:i + 500]
                    q = ",".join("?" * len(chunk))
                    conn.execute(
                        f"UPDATE files SET missing=1 WHERE root=? AND key NOT IN ({q})",
                        [r["id"]] + chunk)
            seen_total += len(seen)
        if done:
            r["scanned"] = _iso()
        else:
            complete = False
        if time.time() > deadline:
            break
    _save_store(store)

    with _LOCK, _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        missing = conn.execute("SELECT COUNT(*) FROM files WHERE missing=1").fetchone()[0]

    return {"ok": True, "seen": seen_total, "added": added, "probed": probed,
            "skipped": skipped, "failed": failed, "missing": missing,
            "files": total, "complete": complete,
            "spent": round(time.time() - t0, 2),
            "note": "" if complete else "还有目录没扫完，再调一次 scan 会接着往下走"}


# ---------------------------------------------------------------- 搜索语法

def parse_query(q: str) -> dict:
    """音匣的过滤语法。语义照着 SearchQuery.cs 抄，不做「更聪明」的改动。

    `star:>4` 是严格大于（等于 5），`star:>=4` 是包含 4；`dur:<2s` 是小于；
    `sr:>48k` 严格大于 48000。认不出的 key 就当普通搜索词。
    """
    out = {"words": [], "tags": [], "star_min": None, "dur_min": None,
           "dur_max": None, "sr_min": None, "sr_max": None, "ch": None,
           "ext": None, "sort": None}
    for tok in str(q or "").split():
        if tok.startswith("#") and len(tok) > 1:
            out["tags"].append(tok[1:].strip())
            continue
        m = re.match(r"^([^:：]+)[:：](.+)$", tok)
        if not m:
            out["words"].append(tok.lower())
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key in ("star", "rating", "评分", "星级"):
            op = ">="
            for cand in (">=", "<=", ">", "<", "="):
                if val.startswith(cand):
                    op, val = cand, val[len(cand):]
                    break
            try:
                n = int(float(val))
            except ValueError:
                out["words"].append(tok.lower())
                continue
            if op == ">":
                n += 1
            out["star_min"] = max(int(out["star_min"] or 0), n)
        elif key in ("dur", "duration", "时长", "长度"):
            out.update(_range(val, "dur"))
        elif key in ("sr", "samplerate", "采样率"):
            out.update(_range(val, "sr", scale="k"))
        elif key in ("ch", "channels", "声道"):
            try:
                out["ch"] = int(float(re.sub(r"[^0-9.]", "", val)))
            except ValueError:
                out["words"].append(tok.lower())
        elif key in ("type", "format", "ext", "格式"):
            v = val.strip().lower().lstrip(".")
            out["ext"] = "." + v if v else None
        else:
            out["words"].append(tok.lower())
    return out


def _range(val: str, field: str, scale=None):
    """把 `>=2s` `48k` `0.5` 这类值翻成区间端点。"""
    op = ">="
    for cand in (">=", "<=", ">", "<", "="):
        if val.startswith(cand):
            op, val = cand, val[len(cand):]
            break
    raw = val.strip().lower().replace("hz", "")
    mult = 1.0
    if raw.endswith("ms"):
        raw, mult = raw[:-2], 0.001
    elif raw.endswith("s"):
        raw = raw[:-1]
    elif raw.endswith("k"):
        raw, mult = raw[:-1], 1000.0
    try:
        num = float(raw) * mult
    except ValueError:
        return {}
    lo, hi = "dur_min", "dur_max"
    if field == "sr":
        lo, hi = "sr_min", "sr_max"
        num = int(round(num))
    if op in ("<", "<="):
        return {hi: num}
    if op == ">":
        return {lo: num + (1 if field == "sr" else 0.001)}
    return {lo: num}


# ---------------------------------------------------------------- 搜索

def _rows_to_items(rows) -> list[dict]:
    items = []
    for r in rows:
        items.append({
            "id": _ident(r["path"]),
            "path": r["path"],
            "name": r["name"],
            "display": r["display"] or r["name"],
            "ext": r["ext"],
            "dur": round(float(r["dur"] or 0), 3),
            "durText": _fmt_dur(r["dur"]),
            "sr": int(r["sr"] or 0),
            "ch": int(r["ch"] or 0),
            "bits": int(r["bits"] or 0),
            "size": int(r["size"] or 0),
            "mtime": float(r["mtime"] or 0),
            "dir": r["dir"],
            "root": r["root"],
            "missing": bool(r["missing"]),
            "ok": bool(r["ok"]),
            "url": f"/v1/library/audio?id={_ident(r['path'])}",
            "rating": int(r["rating"] or 0),
            "favorite": bool(r["favorite"]),
            "used": float(r["used"] or 0),
            "tags": [t for t in (r["tags"] or "").split("\x1f") if t],
        })
    return items


def _fetch_items(conn, where, params=()) -> list[dict]:
    sql = ("SELECT f.*, COALESCE(m.rating,0) rating, COALESCE(m.favorite,0) favorite, "
           "COALESCE(m.used,0) used, "
           "(SELECT GROUP_CONCAT(tag, char(31)) FROM asset_tags at WHERE at.key=f.key) tags "
           "FROM files f LEFT JOIN marks m ON m.key=f.key WHERE " + where)
    return _rows_to_items(conn.execute(sql, params).fetchall())


def _select_sql(f, collection=None, favorites=False, recent=False):
    where, params = [], []
    if f.get("ext"):
        where.append("LOWER(f.ext)=?")
        params.append(f["ext"])
    if f.get("ch"):
        where.append("f.ch=?")
        params.append(int(f["ch"]))
    if f.get("sr_min") is not None:
        where.append("f.sr>=?")
        params.append(float(f["sr_min"]))
    if f.get("sr_max") is not None:
        where.append("f.sr<?")
        params.append(float(f["sr_max"]))
    if f.get("dur_min") is not None:
        where.append("f.dur>=?")
        params.append(float(f["dur_min"]))
    if f.get("dur_max") is not None:
        where.append("f.dur<?")
        params.append(float(f["dur_max"]))
    if f.get("star_min") is not None:
        where.append("COALESCE(m.rating,0)>=?")
        params.append(int(f["star_min"]))
    if favorites:
        where.append("COALESCE(m.favorite,0)=1")
    if collection:
        where.append("f.key IN (SELECT key FROM collection_items WHERE collection=?)")
        params.append(collection)
    for tag in f.get("tags") or []:
        where.append("f.key IN (SELECT key FROM asset_tags WHERE tag=?)")
        params.append(tag)
    if recent:
        where.append("COALESCE(m.used,0)>0")
    where.append("f.missing=0")
    sql = (
        "SELECT f.*, COALESCE(m.rating,0) rating, COALESCE(m.favorite,0) favorite, "
        "COALESCE(m.used,0) used, "
        "(SELECT GROUP_CONCAT(tag, char(31)) FROM asset_tags at WHERE at.key=f.key) tags "
        "FROM files f LEFT JOIN marks m ON m.key=f.key "
        "WHERE " + " AND ".join(where)
    )
    return sql, params


def _match_words(item: dict, words: list[str]) -> bool:
    """每个词都要命中：显示名 / 文件名 / 路径；拉丁词再看全拼和首字母。"""
    hay = " ".join([item.get("display") or "", item.get("name") or "",
                    item.get("path") or ""]).lower()
    for w in words:
        if w in hay:
            continue
        if _is_latin(w) and (w in (item.get("py") or "")):
            continue
        return False
    return True


def search(q=None, root=None, collection=None, tags=None, favorites=False,
           recent=False, limit=60, offset=0, sort="relevance", min_rating=0):
    if isinstance(q, dict):
        f = parse_query(q.get("q") or "")
        collection = collection or q.get("collection")
        favorites = favorites or bool(q.get("favorites"))
        recent = recent or bool(q.get("recent"))
        root = root or q.get("root")
        sort = q.get("sort") or sort
        limit = int(q.get("limit") or limit)
        offset = int(q.get("offset") or 0)
    else:
        f = parse_query(q or "")
    if tags:
        f["tags"] = list(f["tags"]) + list(tags)
    if min_rating:
        f["star_min"] = max(int(f["star_min"] or 0), int(min_rating))

    sql, params = _select_sql(f, collection=collection, favorites=favorites, recent=recent)
    if root:
        sql += " AND f.root=?"
        params.append(root)
    sql += " LIMIT 20000"
    with _LOCK, _db() as conn:
        rows = conn.execute(sql, params).fetchall()

    items = []
    for r in rows:
        it = _rows_to_items([r])[0]
        py_full, py_init = _pinyin_text((it["display"] or "") + " " + (it["name"] or ""))
        it["py_full"] = py_full
        it["py_init"] = py_init
        it["py"] = py_full + " " + py_init
        if f["words"] and not _match_words(it, f["words"]):
            continue
        items.append(it)

    words = f["words"]
    for it in items:
        score = 0.0
        d = (it["display"] or "").lower()
        if words:
            for w in words:
                if d == w:
                    score += 60
                elif d.startswith(w):
                    score += 30
                elif w in d:
                    score += 12
                elif w in it["py_init"]:
                    score += 5
        score += it["rating"] * 6 + (10 if it["favorite"] else 0)
        it["score"] = round(score, 1)

    if sort in ("name", "display"):
        items.sort(key=lambda x: (x["display"] or "").lower())
    elif sort == "dur":
        items.sort(key=lambda x: x["dur"])
    elif sort == "dur_desc":
        items.sort(key=lambda x: -x["dur"])
    elif sort == "sr":
        items.sort(key=lambda x: -x["sr"])
    elif sort == "rating":
        items.sort(key=lambda x: (-x["rating"], (x["display"] or "").lower()))
    elif sort == "recent":
        items.sort(key=lambda x: -x["mtime"])
    elif sort == "used":
        items.sort(key=lambda x: -x["used"])
    elif sort == "random":
        import random
        random.shuffle(items)
    else:
        items.sort(key=lambda x: (-x["score"], (x["display"] or "").lower()))

    total = len(items)
    page = items[int(offset):int(offset) + int(limit)]
    for it in page:
        it.pop("py", None)
    return {"ok": True, "query": q, "total": total, "offset": int(offset),
            "limit": int(limit), "items": page,
            "parsed": {k: v for k, v in f.items() if v}}


def _resolve(ident=None, path=None):
    """按 id / 路径 / `lib:` 前缀找一行。找不到给明确的错。"""
    raw = str(path or ident or "").strip()
    if raw.startswith("lib:"):
        raw = raw[4:]
    if not raw:
        raise ValueError("需要 id 或 path")
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM files WHERE key=? OR path=?",
                           (_key(raw), _norm(raw))).fetchone()
        if row is None:
            # id 是 key 的 sha1 前 12 位，SQL 里反算不了；几万条扫一遍也就几毫秒
            for cand in conn.execute("SELECT key,path FROM files"):
                if _ident(cand["path"]) == raw:
                    row = conn.execute("SELECT * FROM files WHERE key=?",
                                       (cand["key"],)).fetchone()
                    break
        if row is None:
            raise ValueError(f"库里找不到：{path or ident}")
        return dict(row)


def get(ident=None, path=None, peaks=True):
    row = _resolve(ident, path)
    item = _rows_to_items([dict(row, rating=0, favorite=0, used=0, tags="")])[0]
    with _LOCK, _db() as conn:
        m = conn.execute("SELECT * FROM marks WHERE key=?", (row["key"],)).fetchone()
        if m:
            item["rating"] = int(m["rating"] or 0)
            item["favorite"] = bool(m["favorite"])
            item["used"] = float(m["used"] or 0)
        item["tags"] = [r["tag"] for r in conn.execute(
            "SELECT tag FROM asset_tags WHERE key=? ORDER BY tag", (row["key"],))]
        item["collections"] = [r["collection"] for r in conn.execute(
            "SELECT collection FROM collection_items WHERE key=? ORDER BY collection",
            (row["key"],))]
        if peaks:
            item["peaks"] = _ensure_peaks(conn, row)
    item["url"] = f"/v1/library/audio?id={item['id']}"
    return {"ok": True, "item": item, "parsed": None}


def _ensure_peaks(conn, row) -> list[int]:
    """波形峰值按需算并缓存：扫 10000 个文件时不能给每个文件都解码一遍。"""
    if row.get("peaks"):
        return list(row["peaks"])
    try:
        data = _peaks(row["path"])
    except Exception:
        data = b""
    if data:
        conn.execute("UPDATE files SET peaks=? WHERE key=?", (data, row["key"]))
        conn.commit()
    return list(data)


def _peaks(path: str, points=PEAK_POINTS, max_seconds=PEAK_MAX_SECONDS) -> bytes:
    import numpy as np
    import soundfile as sf
    vals = []
    with sf.SoundFile(path) as f:
        sr = int(f.samplerate or 1)
        total = min(len(f), int(max_seconds * sr))
        block = 4096
        while f.tell() < total:
            d = f.read(min(block, total - f.tell()), dtype="float32", always_2d=True)
            if not len(d):
                break
            vals.append(float(np.max(np.abs(d))))
    if not vals:
        return b""
    arr = np.asarray(vals, dtype="float32")
    if len(arr) > points:
        idx = np.linspace(0, len(arr) - 1, points)
        arr = np.interp(idx, np.arange(len(arr)), arr)
    elif len(arr) < points:
        arr = np.interp(np.linspace(0, len(arr) - 1, points),
                        np.arange(len(arr)), arr)
    arr = np.clip(arr, 0.0, 1.0) ** 0.65
    return (arr * 255.0).astype("uint8").tobytes()


# ---------------------------------------------------------------- 标记 / 标签

def update(ident=None, path=None, rating=None, favorite=None, tags_add=None,
           tags_remove=None, tags_set=None, collections_add=None,
           collections_remove=None, mark_used=False):
    row = _resolve(ident, path)
    key = row["key"]
    with _LOCK, _db() as conn:
        conn.execute("INSERT OR IGNORE INTO marks(key) VALUES(?)", (key,))
        if rating is not None:
            conn.execute("UPDATE marks SET rating=? WHERE key=?",
                         (max(0, min(5, int(rating))), key))
        if favorite is not None:
            conn.execute("UPDATE marks SET favorite=? WHERE key=?",
                         (1 if favorite else 0, key))
        if mark_used:
            conn.execute("UPDATE marks SET used=? WHERE key=?", (_now(), key))
        if tags_set is not None:
            conn.execute("DELETE FROM asset_tags WHERE key=?", (key,))
            for t in tags_set:
                _add_tag(conn, key, t)
        for t in tags_add or []:
            _add_tag(conn, key, t)
        for t in tags_remove or []:
            conn.execute("DELETE FROM asset_tags WHERE key=? AND tag=?", (key, str(t)))
        for c in collections_add or []:
            conn.execute("INSERT OR IGNORE INTO collections(name) VALUES(?)", (str(c),))
            conn.execute("INSERT OR IGNORE INTO collection_items(collection,key) VALUES(?,?)",
                         (str(c), key))
        for c in collections_remove or []:
            conn.execute("DELETE FROM collection_items WHERE collection=? AND key=?",
                         (str(c), key))
    return get(ident=key)


def _add_tag(conn, key, tag):
    t = str(tag or "").strip()
    if not t:
        return
    conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (t,))
    conn.execute("INSERT OR IGNORE INTO asset_tags(key,tag) VALUES(?,?)", (key, t))


def tags(op="list", name=None, new=None, ident=None, path=None):
    with _LOCK, _db() as conn:
        if op == "list":
            rows = conn.execute(
                "SELECT t.name, COUNT(at.key) n FROM tags t "
                "LEFT JOIN asset_tags at ON at.tag=t.name GROUP BY t.name "
                "ORDER BY n DESC, t.name").fetchall()
            return {"ok": True, "tags": [{"name": r["name"], "files": r["n"]} for r in rows]}
        if op == "rename":
            old, target = str(name or ""), str(new or ident or "").strip()
            if not old or not target:
                raise ValueError("rename 需要 name=旧名 和 new=新名")
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (target,))
            conn.execute("INSERT OR IGNORE INTO asset_tags(key,tag) "
                         "SELECT key,? FROM asset_tags WHERE tag=?", (target, old))
            conn.execute("DELETE FROM asset_tags WHERE tag=?", (old,))
            conn.execute("DELETE FROM tags WHERE name=?", (old,))
            return tags("list")
        if op == "delete":
            conn.execute("DELETE FROM asset_tags WHERE tag=?", (str(name or ""),))
            conn.execute("DELETE FROM tags WHERE name=?", (str(name or ""),))
            return tags("list")
    raise ValueError(f"不支持的 op：{op}")


def collections(op="list", name=None, ident=None, path=None):
    with _LOCK, _db() as conn:
        if op == "list":
            rows = conn.execute(
                "SELECT c.name, COUNT(ci.key) n FROM collections c "
                "LEFT JOIN collection_items ci ON ci.collection=c.name "
                "GROUP BY c.name ORDER BY c.name").fetchall()
            return {"ok": True, "collections": [{"name": r["name"], "files": r["n"]}
                                                for r in rows]}
        if op == "create":
            conn.execute("INSERT OR IGNORE INTO collections(name) VALUES(?)", (str(name),))
            return collections("list")
        if op == "delete":
            conn.execute("DELETE FROM collection_items WHERE collection=?", (str(name),))
            conn.execute("DELETE FROM collections WHERE name=?", (str(name),))
            return collections("list")
        if op in ("add", "remove"):
            row = _resolve(ident, path)
            if op == "add":
                conn.execute("INSERT OR IGNORE INTO collections(name) VALUES(?)", (str(name),))
                conn.execute("INSERT OR IGNORE INTO collection_items(collection,key) VALUES(?,?)",
                             (str(name), row["key"]))
            else:
                conn.execute("DELETE FROM collection_items WHERE collection=? AND key=?",
                             (str(name), row["key"]))
            return collections("list")
    raise ValueError(f"不支持的 op：{op}")


def saved_queries(op="list", name=None, q=None):
    with _LOCK, _db() as conn:
        if op == "list":
            return {"ok": True, "queries": [{"name": r["name"], "query": r["query"]}
                                            for r in conn.execute(
                                                "SELECT * FROM saved_queries ORDER BY name")]}
        if op == "save":
            if not name:
                raise ValueError("save 需要 name")
            conn.execute("INSERT INTO saved_queries(name,query) VALUES(?,?) "
                         "ON CONFLICT(name) DO UPDATE SET query=excluded.query",
                         (str(name), str(q or "")))
            return saved_queries("list")
        if op == "delete":
            conn.execute("DELETE FROM saved_queries WHERE name=?", (str(name),))
            return saved_queries("list")
    raise ValueError(f"不支持的 op：{op}")


# ---------------------------------------------------------------- 浏览

def folders(dir=None, root=None):
    """文件夹视图：不传 dir 列根目录，传 dir 列直属子目录与文件数。"""
    with _LOCK, _db() as conn:
        if not dir:
            store = _store()
            out = []
            for r in store["roots"]:
                n = conn.execute("SELECT COUNT(*) FROM files WHERE root=? AND missing=0",
                                 (r["id"],)).fetchone()[0]
                out.append({"name": r["name"], "path": r["path"], "root": r["id"],
                            "files": n, "isRoot": True})
            return {"ok": True, "dir": None, "entries": out}
        d = _norm(dir)
        rows = conn.execute(
            "SELECT dir, COUNT(*) n, COALESCE(SUM(dur),0) d FROM files "
            "WHERE missing=0 AND dir LIKE ? GROUP BY dir",
            (d.rstrip("\\/") + os.sep + "%",)).fetchall()
        kids = {}
        for r in rows:
            rest = str(r["dir"])[len(d.rstrip("\\/")) + 1:]
            first = rest.split(os.sep)[0]
            if not first:
                continue
            k = kids.setdefault(first, {"name": first, "path": os.path.join(d, first),
                                        "files": 0, "duration": 0.0, "isDir": True})
            k["files"] += r["n"]
            k["duration"] += float(r["d"])
        files = conn.execute(
            "SELECT * FROM files WHERE missing=0 AND dir=?", (d,)).fetchall()
        entries = sorted(kids.values(), key=lambda x: x["name"].lower())
        entries += [{"name": f["display"], "path": f["path"], "files": 0,
                     "duration": float(f["dur"] or 0), "isDir": False,
                     "ext": f["ext"], "id": _ident(f["path"]), "dur": float(f["dur"] or 0)}
                    for f in files]
        return {"ok": True, "dir": d, "entries": entries}


# ---------------------------------------------------------------- 统计

def stats():
    with _LOCK, _db() as conn:
        base = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) b, COALESCE(SUM(dur),0) d "
            "FROM files WHERE missing=0").fetchone()
        missing = conn.execute("SELECT COUNT(*) FROM files WHERE missing=1").fetchone()[0]
        bad = conn.execute("SELECT COUNT(*) FROM files WHERE ok=0").fetchone()[0]
        fav = conn.execute("SELECT COUNT(*) FROM marks WHERE favorite=1").fetchone()[0]
        rated = conn.execute("SELECT COUNT(*) FROM marks WHERE rating>0").fetchone()[0]
        by_ext = conn.execute(
            "SELECT ext, COUNT(*) n FROM files WHERE missing=0 GROUP BY ext "
            "ORDER BY n DESC").fetchall()
        by_root = conn.execute(
            "SELECT root, COUNT(*) n, COALESCE(SUM(size),0) b FROM files "
            "WHERE missing=0 GROUP BY root").fetchall()
        top_tags = conn.execute(
            "SELECT tag, COUNT(*) n FROM asset_tags GROUP BY tag ORDER BY n DESC LIMIT 12"
        ).fetchall()
        hist = conn.execute(
            "SELECT rating, COUNT(*) n FROM marks WHERE rating>0 GROUP BY rating").fetchall()
        longest = conn.execute(
            "SELECT display,dur FROM files WHERE missing=0 ORDER BY dur DESC LIMIT 1"
        ).fetchone()
    store = _store()
    names = {r["id"]: r["name"] for r in store["roots"]}
    return {
        "ok": True,
        "files": base["n"], "bytes": int(base["b"]),
        "duration": round(float(base["d"]), 1),
        "durationText": _fmt_dur(base["d"]),
        "missing": missing, "unreadable": bad,
        "favorites": fav, "rated": rated,
        "avgDur": round(float(base["d"]) / base["n"], 2) if base["n"] else 0,
        "byExt": [{"ext": r["ext"], "files": r["n"]} for r in by_ext],
        "byRoot": [{"root": r["root"], "name": names.get(r["root"], r["root"]),
                    "files": r["n"], "bytes": int(r["b"])} for r in by_root],
        "topTags": [{"tag": r["tag"], "files": r["n"]} for r in top_tags],
        "ratingHist": {str(r["rating"]): r["n"] for r in hist},
        "longest": {"name": longest["display"], "dur": longest["dur"]} if longest else None,
        "indexBytes": _db_path().stat().st_size if _db_path().exists() else 0,
        "root": str(lib_root()),
    }


# ---------------------------------------------------------------- 重复

def duplicates(deep=False, by="content", limit=200):
    """找重复：先按「大小+采样率+声道+时长」分桶，桶内再可选哈希确认。"""
    groups = []
    with _LOCK, _db() as conn:
        if by == "name":
            rows = conn.execute(
                "SELECT LOWER(display) k, COUNT(*) n FROM files WHERE missing=0 "
                "GROUP BY k HAVING n>1 ORDER BY n DESC LIMIT ?", (int(limit),)).fetchall()
            for g in rows:
                groups.append(_fetch_items(
                    conn, "f.missing=0 AND LOWER(f.display)=?", (g["k"],)))
        else:
            rows = conn.execute(
                "SELECT size, sr, ch, ROUND(dur,1) d, COUNT(*) n FROM files "
                "WHERE missing=0 AND size>2048 GROUP BY size, sr, ch, ROUND(dur,1) "
                "HAVING n>1 ORDER BY n DESC LIMIT ?", (int(limit),)).fetchall()
            for g in rows:
                groups.append(_fetch_items(
                    conn, "f.missing=0 AND f.size=? AND f.sr=? AND f.ch=? AND ROUND(f.dur,1)=?",
                    (g["size"], g["sr"], g["ch"], g["d"])))
    if deep:
        # 深查只留「真的重复」的组：同桶里首尾各 256KB 的 sha256 相同才算。
        exact = []
        for g in groups:
            seen = {}
            for it in g:
                try:
                    h = _partial_hash(it["path"])
                except OSError:
                    continue
                it["hash"] = h[:16]
                seen.setdefault(h, []).append(it)
            for h, same in seen.items():
                if len(same) > 1:
                    for x in same[1:]:
                        x["duplicateOf"] = same[0]["id"]
                    exact.append(same)
        groups = exact
    total = sum(len(g) for g in groups)
    return {"ok": True, "groups": len(groups), "files": total, "by": by,
            "deep": bool(deep), "sets": groups}


def _partial_hash(path, size=262144):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(size))
        try:
            f.seek(-size, os.SEEK_END)
            h.update(f.read(size))
        except OSError:
            pass
    return h.hexdigest()


# ---------------------------------------------------------------- 导出

def _fill_template(tpl: str, n: int, item: dict, index: int, lib: str) -> str:
    stem = _safe_name(item.get("display") or item.get("name") or "sound")
    ext = item.get("ext") or Path(item.get("path") or "").suffix
    values = {
        "n": str(n), "name": stem, "index": str(index),
        "dur": f"{float(item.get('dur') or 0):.2f}",
        "sr": str(int(item.get("sr") or 0)), "ch": str(int(item.get("ch") or 0)),
        "lib": _safe_name(lib or ""),
    }
    out = str(tpl or DEFAULT_TEMPLATE)

    def repl(m):
        key = m.group(1)
        pad = m.group(2)
        if key not in values:
            return m.group(0)
        v = values[key]
        if pad:
            width = len(pad)
            try:
                return str(int(float(v))).zfill(width)
            except ValueError:
                return v
        return v

    out = re.sub(r"\{([a-zA-Z]+)(?::(0+))?\}", repl, out)
    out = _safe_name(out)
    if ext and not out.lower().endswith(ext.lower()):
        out += ext
    return out


def export(idents=None, query=None, dest=None, template=None, keep_structure=True,
           csv_name=None, include_missing=False, limit=500):
    """批量导出：复制（不是移动），可选保留原目录结构，附 CSV 清单。"""
    if not dest:
        raise ValueError("export 需要 dest（目标目录）")
    dest = Path(str(dest)).expanduser()
    ensure_dir(dest)

    items = []
    if query is not None:
        res = search(q=query, limit=int(limit), sort="name")
        items = res["items"]
    elif idents:
        for ident in idents:
            try:
                items.append(get(ident=ident, peaks=False)["item"])
            except ValueError:
                continue
    if not items:
        raise ValueError("没有要导出的文件（query 或 idents 至少给一个）")
    if not include_missing:
        items = [it for it in items if not it.get("missing")]

    store = _store()
    tpl = template or store["settings"].get("template") or DEFAULT_TEMPLATE
    roots = {r["id"]: r for r in store["roots"]}
    used = {}
    out = []
    rows = []
    for i, it in enumerate(items, 1):
        src = Path(it["path"])
        if not src.exists():
            out.append({"src": it["path"], "ok": False, "error": "源文件不存在"})
            continue
        sub = ""
        if keep_structure:
            r = roots.get(it.get("root"))
            if r:
                try:
                    sub = os.path.relpath(it["dir"], r["path"])
                except ValueError:
                    sub = ""
                if sub.startswith(".."):
                    sub = ""
        target_dir = ensure_dir(dest / sub) if sub else dest
        name = _fill_template(tpl, i, it, i, (roots.get(it.get("root")) or {}).get("name", ""))
        stem, ext = os.path.splitext(name)
        final = target_dir / name
        guard = 2
        while final.exists() or str(final).lower() in used:
            final = target_dir / f"{stem}-{guard}{ext}"
            guard += 1
        used[str(final).lower()] = True
        try:
            shutil.copy2(src, final)
            out.append({"src": it["path"], "dst": str(final), "ok": True})
            rows.append((it, str(final)))
        except OSError as e:
            out.append({"src": it["path"], "ok": False, "error": str(e)})

    manifest = dest / (csv_name or "_export.csv")
    try:
        with manifest.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["name", "src", "dst", "dur", "sr", "ch", "rating", "tags"])
            for it, dst in rows:
                w.writerow([it.get("display"), it.get("path"), dst,
                            it.get("dur"), it.get("sr"), it.get("ch"),
                            it.get("rating"), " ".join(it.get("tags") or [])])
    except OSError:
        manifest = None

    ok = sum(1 for r in out if r.get("ok"))
    store["exports"].append({"at": _iso(), "dest": str(dest), "files": ok,
                             "template": tpl})
    store["exports"] = store["exports"][-30:]
    _save_store(store)
    return {"ok": True, "dest": str(dest), "copied": ok,
            "failed": len(out) - ok, "manifest": str(manifest) if manifest else None,
            "template": tpl, "items": out}


# ---------------------------------------------------------------- 进工程

def use(project, ident=None, path=None, sampler_id=None, track=None, mode=None,
        root_note=60, copy=False, gain_db=0.0, name=None):
    """把库里的音效变成工程里的一条采样器轨。默认只引用，不复制。"""
    if not project:
        raise ValueError("use 需要 project")
    item = get(ident=ident, path=path, peaks=False)["item"]
    proj = Path(str(project)).expanduser()
    if proj.is_file():
        proj = proj.parent
    if not (proj / "project.json").exists():
        raise ValueError(f"工程不存在：{proj}")

    src = Path(item["path"])
    file_path = src
    if copy:
        samples = ensure_dir(proj / "samples")
        file_path = samples / _safe_name(name or item["display"] + item["ext"])
        if not file_path.exists() or file_path.stat().st_size != src.stat().st_size:
            shutil.copy2(src, file_path)

    sid = sampler_id or slugify(item["display"] or Path(item["path"]).stem)[:40]
    tid = track or sid
    auto_mode = mode or ("oneshot" if float(item.get("dur") or 0) <= 4.0 else "pitch")
    defn = {
        "id": sid,
        "file": str(file_path),
        "rootNote": int(root_note),
        "mode": auto_mode,
        "gainDb": float(gain_db or 0.0),
        "attackMs": 2.0 if auto_mode == "oneshot" else 3.0,
        "releaseMs": 40.0,
        "libraryKey": item["id"],
        "libraryPath": item["path"],
    }
    sampler.edit(proj, op="set", sampler=defn)
    sampler.add_track(proj, tid, sid)
    with _LOCK, _db() as conn:
        conn.execute("INSERT INTO marks(key,used) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET used=excluded.used",
                     (_key(item["path"]), _now()))
    hint = (f"已在工程里加好采样器轨 `{tid}`（{auto_mode}）。"
            f"用 music_notes 往这条轨写音符就会发声；oneshot 模式按下即播完整段。")
    return {"ok": True, "sampler": defn, "track": tid, "mode": auto_mode,
            "copied": bool(copy), "file": str(file_path), "hint": hint}


# ---------------------------------------------------------------- 从音匣导入

def import_soundvault(path=None, scan_now=False):
    """读音匣的 store.json，把里面的库目录接过来（只认目录，不动文件）。"""
    candidates = []
    if path:
        candidates.append(Path(str(path)).expanduser())
    else:
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local:
            candidates.append(Path(local) / "SoundVault" / "store.json")
        candidates.append(Path.home() / "AppData" / "Local" / "SoundVault" / "store.json")
        here = Path(__file__).resolve().parents[2]
        candidates.append(here / "SoundVault" / "store.json")
    src = next((c for c in candidates if c.is_file()), None)
    if not src:
        return {"ok": False, "error": "没找到音匣的 store.json",
                "looked": [str(c) for c in candidates],
                "hint": "如果音匣用的是便携模式，路径是 <exe目录>/data/store.json，用 path 指过去"}

    try:
        data = json.loads(src.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise ValueError(f"读不了 {src}：{e}")

    found = []

    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, str):
            if len(node) > 2 and len(node) < 400 and Path(node).is_dir():
                found.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(data)
    added = []
    for d in dict.fromkeys(found):
        r = roots(op="add", path=d)
        if r.get("root"):
            added.append(r["root"])
    if scan_now and added:
        scan(max_seconds=15)
    return {"ok": True, "store": str(src), "imported": added,
            "found": len(added), "note": "只导入了库目录，音匣的标签/评分没有一并迁移"}


# ---------------------------------------------------------------- 其它

def reveal(ident=None, path=None):
    """在资源管理器里选中文件。这只是给本机用户看的便利操作。"""
    row = _resolve(ident, path)
    p = row["path"]
    try:
        if os.name == "nt":
            os.startfile(os.path.dirname(p))  # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["xdg-open", os.path.dirname(p)])
        return {"ok": True, "path": p}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": p}


def audio_path(ident=None, path=None) -> Path:
    row = _resolve(ident, path)
    p = Path(row["path"])
    if not p.is_file():
        raise ValueError(f"文件不在了：{p}")
    if p.suffix.lower() not in AUDIO_EXT:
        raise ValueError(f"不是音频文件：{p.name}")
    return p


def settings(op="get", **kw):
    store = _store()
    if op == "get":
        return {"ok": True, "settings": store["settings"]}
    if op == "set":
        for k, v in kw.items():
            if v is not None and k in ("template", "previewLoop"):
                store["settings"][k] = v
        _save_store(store)
        return {"ok": True, "settings": store["settings"]}
    raise ValueError(f"不支持的 op：{op}")


_ALIASES = {
    "id": "ident", "ids": "idents", "libraryId": "ident", "tagsAdd": "tags_add",
    "tagsRemove": "tags_remove", "tagsSet": "tags_set",
    "collectionsAdd": "collections_add", "collectionsRemove": "collections_remove",
    "rootId": "root_id", "maxSeconds": "max_seconds", "keepStructure": "keep_structure",
    "csvName": "csv_name", "samplerId": "sampler_id", "rootNote": "root_note",
    "minRating": "min_rating", "markUsed": "mark_used", "includeMissing": "include_missing",
    "probeLimit": "probe_limit", "scanNow": "scan_now", "saved": "saved_queries",
}


def dispatch(op: str, args: dict):
    """给 server.py 一个统一入口，省得每个动作都写一条 lambda。

    工具层用 camelCase（JS 习惯），这里统一翻成 Python 的 snake_case，
    免得两边各写一份映射表、改一个字段漏一边。
    """
    op = {"import": "import_soundvault", "saved": "saved_queries"}.get(op, op)
    args = dict(args or {})
    for k, v in list(args.items()):
        if k in _ALIASES:
            args[_ALIASES[k]] = v
            args.pop(k)
    fn = {
        "state": state,
        "stats": stats,
        "roots": roots,
        "scan": scan,
        "search": search,
        "get": get,
        "update": update,
        "tags": tags,
        "collections": collections,
        "saved_queries": saved_queries,
        "folders": folders,
        "duplicates": duplicates,
        "export": export,
        "use": use,
        "import_soundvault": import_soundvault,
        "reveal": reveal,
        "settings": settings,
    }.get(op)
    if not fn:
        raise ValueError(f"未知的库操作：{op}")
    args.pop("op", None)
    # roots/tags/collections/saved 这些还有一层子动作（add/remove/rename），
    # 工具层用 action 传，这里翻成函数自己的 op 参数。
    sub = args.pop("action", None)
    if op == "use" and args.get("project"):
        proj = Path(str(args["project"])).expanduser()
        if not proj.is_absolute() and not (proj / "project.json").exists():
            args["project"] = str(Path(ROOT) / proj)
    return fn(op=sub, **args) if sub else fn(**args)
