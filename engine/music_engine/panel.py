"""面板用的引擎侧接口。

浏览器里的粉板（dsh-music-panel）直连本机引擎，不走 Bearer token：
token 文件读不到浏览器里，而引擎只绑 127.0.0.1，所以这几条只认回环 + 回环 Origin。
这里只做三件事：列工程/看状态、改「构思阶段」的参数、代跑轻量动作。
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import ROOT
from .genres import list_genres
from .project import load_project, save_project, create_project, list_projects
from .util import key_name, parse_key

VOICES = ["feiyu"]
KEY_CHOICES = ["C", "G", "D", "A", "E", "F", "Bb", "Eb", "Am", "Em", "Bm", "F#m",
               "Cm", "Gm", "Dm"]
METER_CHOICES = ["4/4", "3/4", "6/8"]


def _resolve(name):
    """工程路径或工程名 → 绝对路径；找不到抛错。"""
    if not name:
        return None
    p = Path(str(name))
    if (p / "project.json").exists():
        return p
    cand = ROOT / str(name)
    if (cand / "project.json").exists():
        return cand
    raise ValueError(f"工程不存在: {name}")


def _newest():
    try:
        items = list_projects()
    except Exception:
        items = []
    if not items:
        return None
    best = max(items, key=lambda d: (d.get("updatedAt") or "", d.get("path") or ""))
    return Path(best["path"])


def _bars(project):
    return sum(int(s.get("bars") or 0) for s in project.get("sections") or [])


def snapshot(path):
    project = load_project(path)
    tracks = project.get("tracks") or []
    return {
        "project": str(path),
        "id": project.get("id"),
        "title": project.get("title"),
        "genre": project.get("genre"),
        "tempo": project.get("tempo"),
        "key": key_name(project.get("key") or {"tonic": "C", "pc": 0, "mode": "major"}),
        "meter": project.get("meter"),
        "bars": _bars(project),
        "seed": project.get("seed"),
        "vocal": {
            "enabled": bool((project.get("vocal") or {}).get("enabled")),
            "voice": (project.get("vocal") or {}).get("voice"),
            "hasLyrics": bool((project.get("vocal") or {}).get("lyrics")),
        },
        "tracks": [{"id": t.get("id"), "bus": t.get("bus"),
                    "fxChain": [f.get("type") for f in (t.get("fxChain") or [])],
                    "send": t.get("send") or {}} for t in tracks],
        "buses": project.get("buses") or [],
        "autoFx": bool((project.get("mix") or {}).get("autoFx")),
        "renders": project.get("renders") or {},
    }


def _catalog():
    try:
        genres = [{"id": g.get("id"), "name": g.get("name") or g.get("id"),
                   "bpm": g.get("bpm")} for g in list_genres()]
    except Exception:
        genres = []
    return {"genres": genres, "voices": VOICES,
            "keys": KEY_CHOICES, "meters": METER_CHOICES}


def state(args):
    """返回面板要的全部状态：工程列表、当前工程快照、可选曲风/歌手/调性。"""
    args = args or {}
    path = None
    if args.get("project"):
        try:
            path = _resolve(args.get("project"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    if path is None:
        path = _newest()
    out = {"ok": True}
    out.update(_catalog())
    try:
        items = list_projects()
    except Exception:
        items = []
    out["projects"] = [{"id": d.get("id"), "title": d.get("title"),
                        "genre": d.get("genre"), "tempo": d.get("tempo"),
                        "path": d.get("path")} for d in items]
    if path is None:
        out["project"] = None
        out["note"] = "还没有工程：选好曲风/BPM 后点「新建」。"
        return out
    try:
        out["project"] = snapshot(path)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


def quick(args):
    """「构思阶段」的参数：新建工程 / 改当前工程的 BPM、调性、拍号、歌手、种子。

    op="new" 建新工程；op="set" 改现有工程（曲风是结构性的，只能新建时换）。
    """
    args = args or {}
    op = (args.get("op") or "set").strip()
    voice = args.get("voice")
    if op == "new":
        genre = args.get("genre") or "citypop"
        name = args.get("name") or f"panel-{genre}-{int(time.time()) % 10000}"
        made = create_project(name=name, genre=genre,
                              bpm=args.get("bpm"), key=args.get("key"),
                              bars=args.get("bars"))
        path = Path(made["project"])
        project = made["data"]
        if voice:
            project.setdefault("vocal", {})["voice"] = voice
        save_project(path, project)
        out = {"ok": True, "created": str(path), "note": f"已新建 {name}"}
        out.update(state({"project": str(path)}))
        out["created"] = str(path)
        out["note"] = f"已新建 {name}"
        return out

    path = _resolve(args.get("project")) if args.get("project") else _newest()
    if path is None:
        return {"ok": False, "error": "还没有工程，先用 op=new 新建一个"}
    project = load_project(path)
    changed = []
    if args.get("bpm"):
        try:
            bpm = int(round(float(args["bpm"])))
        except (TypeError, ValueError):
            return {"ok": False, "error": "BPM 要是数字"}
        if not 40 <= bpm <= 260:
            return {"ok": False, "error": "BPM 超出 40–260"}
        project["tempo"] = bpm
        changed.append("BPM")
    if args.get("key"):
        project["key"] = parse_key(str(args["key"]))
        changed.append("调性")
    if args.get("meter") in METER_CHOICES:
        project["meter"] = args["meter"]
        changed.append("拍号")
    if voice:
        project.setdefault("vocal", {})["voice"] = str(voice)
        changed.append("歌手")
    if args.get("seed") not in (None, ""):
        try:
            project["seed"] = int(args["seed"])
            changed.append("种子")
        except (TypeError, ValueError):
            pass
    save_project(path, project)
    out = {"ok": True, "changed": changed, "note": ("已改：" + "、".join(changed)) if changed else "没有改动"}
    out.update(state({"project": str(path)}))
    out["changed"] = changed
    out["note"] = ("已改：" + "、".join(changed)) if changed else "没有改动"
    return out


def act(args):
    """代跑动作：compose / render / mix / master / compare。都在引擎进程里做。"""
    args = args or {}
    op = (args.get("op") or "").strip()
    path = _resolve(args.get("project")) if args.get("project") else _newest()
    if path is None:
        return {"ok": False, "error": "还没有工程"}
    if op == "compose":
        from . import compose as compose_mod

        res = compose_mod.compose(path, seed=args.get("seed"))
        out = {"ok": True, "result": res}
        out.update(state({"project": str(path)}))
        return out
    if op == "render":
        from . import render as render_mod

        res = render_mod.render(path, args.get("quality"))
        out = {"ok": True, "result": res}
        out.update(state({"project": str(path)}))
        return out
    if op == "mix":
        from . import mix as mix_mod

        res = mix_mod.mix(project_path=path, preset=args.get("preset"))
        out = {"ok": True, "result": {k: v for k, v in res.items() if k != "report"}}
        out["metrics"] = res.get("report")
        out.update(state({"project": str(path)}))
        return out
    if op == "master":
        from . import master as master_mod

        res = master_mod.master(project_path=path, target_lufs=args.get("targetLufs"),
                                true_peak=args.get("truePeak", -1.0),
                                preset=args.get("preset"))
        out = {"ok": True, "result": {k: v for k, v in res.items() if k != "report"}}
        out["metrics"] = res.get("metrics")
        out.update(state({"project": str(path)}))
        return out
    return {"ok": False, "error": f"不认识的动作: {op or '（空）'}"}

def bus(args):
    """总线路由/推子/送出量：面板的「自动接线」和推子都走这里。"""
    args = args or {}
    path = _resolve(args.get("project")) if args.get("project") else _newest()
    if path is None:
        return {"ok": False, "error": "还没有工程"}
    from . import routing as routing_mod

    res = routing_mod.edit(path, args.get("op") or "list",
                           bus=args.get("bus"), track=args.get("track"),
                           sends=args.get("sends"), auto=args.get("auto"))
    if isinstance(res, dict) and res.get("ok") is False:
        return res
    out = {"ok": True, "edit": res}
    out.update(state({"project": str(path)}))
    out["buses"] = (out.get("project") or {}).get("buses") or []
    return out

def library(args):
    """音效库：面板的「音效」页签走这里，动作直接转给 library.dispatch。

    面板只认工程名不认路径，所以 use 这一支由这里把名字解成真实工程目录。
    """
    from . import library as library_mod

    args = dict(args or {})
    op = args.pop("op", "search") or "search"
    try:
        if op == "use":
            path = _resolve(args.get("project")) if args.get("project") else _newest()
            if path is None:
                return {"ok": False, "error": "还没有工程，先在「构思」里建一个"}
            res = library_mod.dispatch(op, dict(args, project=str(path)))
        else:
            res = library_mod.dispatch(op, args)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if isinstance(res, dict):
        return {"ok": True, **res}
    return {"ok": True, "result": res}
