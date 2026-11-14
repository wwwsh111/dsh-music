"""混音台路由：分组总线、每轨 send 量、按角色自动套效果链。

FL 的 Mixer 是两层：通道机架里每个乐器占一条通道 → 通道可以选中另一条
mixer track（"Send to"）→ 都汇进 Master。鼓一般是 route 到一条 bus 上，
bus 上挂压缩/饱和，推子一动整组跟着动。

这里对应：

    project["tracks"][i]["bus"]  = "drums_bus"      # 送进哪条分组，空=直连主输出
    project["tracks"][i]["send"] = {"reverb": 0.2, "delay": 0.1}   # 0–1
    project["buses"] = [{"id", "name", "gainDb", "pan", "fxChain", "send"}]
    project["mix"]["autoFx"] = True                 # 没挂链的轨按角色套预设

分组不是"再复制一份声音"：被 route 走的轨道不再直接进主输出，
它只经 bus 出来一次——和真实调音台的内部总线一样。
"""

from pathlib import Path

from . import fxchain
from .project import load_project, save_project

# 通道机架里的角色 → 预设链。没有 fx 的按 fx 名兜底（render 的 fx 字段）
ROLE_PRESET = {
    "drums": "drum-punch",
    "bass": "bass-tight",
    "keys": "keys-air",
    "pad": "keys-air",
    "lead": "keys-air",
    "gtr_lead": "guitar-bite",
    "gtr_rhythm_l": "guitar-bite",
    "gtr_rhythm_r": "guitar-bite",
    "guitar": "guitar-bite",
}

FX_PRESET = {
    "drum_bus": "drum-punch",
    "bass": "bass-tight",
    "amp": "guitar-bite",
    "keys": "keys-air",
    "pad": "keys-air",
    "synth": "keys-air",
}

# 人声已经有 vocal_chain + 调教链，再叠一遍会「炸」（见 mix.py 的注释）
NO_AUTO = ("vocal",)

# 曲风布局：一键把通道机架接成「分组总线 + 父总线」的两层结构（FL 的 Group 里套 Group）。
# 增益偏移按曲风微调：摇滚/电子把鼓推一点，抒情把铺底收一点。
GENRE_LAYOUT = {
    "default": {"drums": 0.0, "bass": 0.0, "music": -1.0, "gtr": -0.5},
    "citypop": {"drums": -1.0, "bass": -0.5, "music": -1.5, "gtr": -1.0},
    "rock": {"drums": 1.0, "bass": 0.0, "music": -1.0, "gtr": 1.0},
    "edm": {"drums": 1.5, "bass": 0.5, "music": -1.0, "gtr": 0.0},
    "ballad": {"drums": -2.0, "bass": -1.0, "music": -1.0, "gtr": -1.5},
    "lofi": {"drums": -2.0, "bass": -1.0, "music": -2.0, "gtr": -1.5},
    "guofeng": {"drums": -0.5, "bass": -0.5, "music": -1.0, "gtr": -0.5},
}

# 角色 → 叶子总线
BUS_OF_ROLE = {
    "drum_bus": "drums", "bass": "bass", "amp": "gtr",
    "keys": "music", "pad": "music", "synth": "music",
}
BUS_OF_TRACK = {
    "drums": "drums", "bass": "bass",
    "gtr_lead": "gtr", "gtr_rhythm_l": "gtr", "gtr_rhythm_r": "gtr", "guitar": "gtr",
    "keys": "music", "pad": "music", "lead": "music", "vocal-harmony": "music",
}
# 每类轨默认送出量（0–1）：鼓给一点点房间，键盘铺底最多
DEFAULT_SEND = {
    "drums": {"reverb": 0.06},
    "bass": {},
    "gtr": {"reverb": 0.10, "delay": 0.05},
    "music": {"reverb": 0.22, "delay": 0.10},
}


def _base(project_path):
    base = Path(project_path)
    return base.parent if base.is_file() else base


def _track(project, tid):
    for tr in project.get("tracks") or []:
        if str(tr.get("id")) == str(tid):
            return tr
    return None


def _buses(project):
    return [b for b in (project.get("buses") or []) if b.get("id")]


def role_preset(tid, fx):
    """这条轨自动套哪个预设；返回 None 表示不套。"""
    if str(tid).startswith(NO_AUTO):
        return None
    name = ROLE_PRESET.get(str(tid))
    if name is None:
        name = FX_PRESET.get(str(fx or ""))
    return name if name in fxchain.PRESETS else None


def auto_chain(project, tid, fx):
    """autoFx 打开且该轨没显式挂链时，给出预设链（拷贝，不改 PRESETS）。"""
    mix = (project or {}).get("mix") or {}
    if not mix.get("autoFx"):
        return []
    tr = _track(project, tid) or {}
    if tr.get("fxChain"):
        return []
    name = role_preset(tid, fx)
    if not name:
        return []
    return [dict(stage) for stage in fxchain.PRESETS[name]]


def _norm_chain(chain):
    """只留认识的类型，每级补齐默认值——和 fxchain.edit 里的规则一致。"""
    out = []
    for st in chain or []:
        t = str((st or {}).get("type") or "")
        if t in fxchain.TYPES:
            out.append(dict(fxchain.DEFAULTS.get(t, {}), **st))
    return out


def _norm_sends(raw, base=None):
    out = dict(base or {})
    for k in ("reverb", "delay"):
        if k in (raw or {}):
            out[k] = round(max(0.0, min(1.0, float(raw[k]))), 3)
    return out


def _parent_of(buses, bid):
    entry = next((b for b in buses if b["id"] == bid), None)
    return str((entry or {}).get("bus") or "")


def _would_cycle(buses, bid, parent):
    """把 bid 挂到 parent 下面会不会成环（父链一路往上走）。"""
    seen = set()
    cur = str(parent or "")
    while cur:
        if cur == bid or cur in seen:
            return True
        seen.add(cur)
        cur = _parent_of(buses, cur)
    return False


def _genre_layout(project, buses, genre=None):
    """按曲风建一套两层总线，并把现有轨按角色接上去。返回 (buses, routed, sends)。"""
    g = str(genre or ((project.get("genrePack") or {}).get("id")) or project.get("genre") or "default")
    offsets = GENRE_LAYOUT.get(g) or GENRE_LAYOUT["default"]
    keep = [b for b in buses if b["id"] not in ("drums", "bass", "music", "gtr", "mix")]
    made = [
        {"id": "mix", "name": "总分组", "gainDb": 0.0, "fxChain": []},
        {"id": "drums", "name": "鼓组", "bus": "mix", "gainDb": offsets["drums"],
         "fxChain": [dict(p) for p in fxchain.PRESETS["drum-punch"]]},
        {"id": "bass", "name": "贝斯", "bus": "mix", "gainDb": offsets["bass"],
         "fxChain": [dict(p) for p in fxchain.PRESETS["bass-tight"]]},
        {"id": "music", "name": "键盘/铺底", "bus": "mix", "gainDb": offsets["music"],
         "fxChain": [dict(p) for p in fxchain.PRESETS["keys-air"]]},
        {"id": "gtr", "name": "吉他", "bus": "mix", "gainDb": offsets["gtr"],
         "fxChain": [dict(p) for p in fxchain.PRESETS["guitar-bite"]]},
    ]
    routed, sends = {}, {}
    for tr in project.get("tracks") or []:
        tid = str(tr.get("id"))
        if tid.startswith("vocal") or tr.get("sampler"):
            continue
        bid = BUS_OF_TRACK.get(tid) or BUS_OF_ROLE.get(str(tr.get("fx") or ""))
        if not bid:
            continue
        tr["bus"] = bid
        routed[tid] = bid
        snd = DEFAULT_SEND.get(bid) or {}
        if snd:
            tr["send"] = _norm_sends(snd, tr.get("send"))
            sends[tid] = tr["send"]
    return keep + made, routed, sends


def edit(project_path, op="list", bus=None, track=None, sends=None, auto=None):
    project = load_project(project_path)
    base = _base(project_path)
    buses = _buses(project)

    if op == "list":
        routes = {}
        for tr in project.get("tracks") or []:
            routes[str(tr.get("id"))] = {
                "bus": tr.get("bus") or "",
                "send": tr.get("send") or {},
                "fxChain": len(tr.get("fxChain") or []),
            }
        return {"ok": True,
                "buses": [{"id": b["id"], "gainDb": float(b.get("gainDb") or 0.0),
                           "pan": float(b.get("pan") or 0.0),
                           "fxChain": [s.get("type") for s in (b.get("fxChain") or [])],
                           "send": b.get("send") or {},
                           "bus": b.get("bus") or "",
                           "tracks": [t for t, r in routes.items() if r["bus"] == b["id"]],
                           "children": [c["id"] for c in buses if (c.get("bus") or "") == b["id"]]}
                          for b in buses],
                "tracks": routes,
                "autoFx": bool(((project.get("mix") or {}).get("autoFx")))}

    if op == "set":
        if not isinstance(bus, dict) or not bus.get("id"):
            raise ValueError("set 需要 bus={id, gainDb, pan, fxChain, send}")
        bid = str(bus["id"])
        entry = next((b for b in buses if b["id"] == bid), None)
        chain = bus.get("fxChain")
        if chain is not None:
            chain = _norm_chain(chain)
        if entry is None:
            entry = {"id": bid, "name": str(bus.get("name") or bid)}
            buses.append(entry)
        if "gainDb" in bus:
            entry["gainDb"] = round(float(bus["gainDb"]), 2)
        if "pan" in bus:
            entry["pan"] = round(max(-1.0, min(1.0, float(bus["pan"]))), 3)
        if chain is not None:
            entry["fxChain"] = chain
        if "send" in bus:
            entry["send"] = _norm_sends(bus.get("send"), entry.get("send"))
        if "bus" in bus:
            parent = str(bus.get("bus") or "")
            if parent:
                if parent == bid:
                    raise ValueError("总线不能路由到自己")
                if parent not in [b["id"] for b in buses]:
                    raise ValueError(f"父总线不存在：{parent}（先建父总线再挂）")
                if _would_cycle(buses, bid, parent):
                    raise ValueError(f"总线路由成环：{bid} → {parent}")
            if parent:
                entry["bus"] = parent
            else:
                entry.pop("bus", None)
        project["buses"] = buses
        save_project(base, project)
        return {"ok": True, "bus": entry,
                "note": f"总线 {bid}：{len(entry.get('fxChain') or [])} 级效果，"
                        f"推子 {float(entry.get('gainDb') or 0.0):+.1f} dB"}

    if op == "remove":
        bid = str((bus or {}).get("id") if isinstance(bus, dict) else (bus or ""))
        if not bid:
            raise ValueError("remove 需要 bus（id 或 {id}）")
        buses = [b for b in buses if b["id"] != bid]
        project["buses"] = buses
        cleared = []
        for tr in project.get("tracks") or []:
            if str(tr.get("bus")) == bid:
                tr.pop("bus", None)
                cleared.append(str(tr.get("id")))
        save_project(base, project)
        return {"ok": True, "removed": bid, "unrouted": cleared,
                "note": f"删掉总线 {bid}，{len(cleared)} 条轨回主输出"}

    if op == "route":
        tid = str(track or "")
        tr = _track(project, tid)
        if tr is None:
            # 总线也能挂到父总线上（两层分组）
            parent_entry = next((b for b in buses if b["id"] == tid), None)
            if parent_entry is not None:
                target = str(bus.get("id") if isinstance(bus, dict) else (bus or ""))
                if target and target not in [b["id"] for b in buses]:
                    raise ValueError(f"没有这条总线：{target}")
                if target and _would_cycle(buses, tid, target):
                    raise ValueError(f"总线路由成环：{tid} → {target}")
                if target:
                    parent_entry["bus"] = target
                else:
                    parent_entry.pop("bus", None)
                project["buses"] = buses
                save_project(base, project)
                return {"ok": True, "track": tid, "bus": target, "isBus": True,
                        "note": f"总线 {tid} → {target or '主输出'}"}
            raise ValueError(f"没有这条轨：{tid}")
        bid = str(bus.get("id") if isinstance(bus, dict) else (bus or ""))
        if bid and bid not in [b["id"] for b in buses]:
            raise ValueError(f"没有这条总线：{bid}（先用 op=set 建）")
        if bid:
            tr["bus"] = bid
        else:
            tr.pop("bus", None)
        save_project(base, project)
        return {"ok": True, "track": tid, "bus": bid,
                "note": f"{tid} → {bid or '主输出'}"}

    if op == "send":
        tid = str(track or "")
        tr = _track(project, tid)
        if tr is None:
            # 总线自己也能往混响/延迟里送（FL 里 bus 一样有 send 旋钮）
            entry = next((b for b in buses if b["id"] == tid), None)
            if entry is None:
                raise ValueError(f"没有这条轨或总线：{tid}")
            entry["send"] = _norm_sends(sends or {}, entry.get("send"))
            save_project(base, project)
            return {"ok": True, "track": tid, "send": entry["send"], "bus": True,
                    "note": f"总线 {tid} 送出：混响 {entry['send'].get('reverb', 0):.2f} / "
                            f"延迟 {entry['send'].get('delay', 0):.2f}"}
        tr["send"] = _norm_sends(sends or {}, tr.get("send"))
        save_project(base, project)
        return {"ok": True, "track": tid, "send": tr["send"],
                "note": f"{tid} 送出：混响 {tr['send'].get('reverb', 0):.2f} / "
                        f"延迟 {tr['send'].get('delay', 0):.2f}"}

    if op == "auto":
        mix = dict(project.get("mix") or {})
        mix["autoFx"] = bool(auto)
        project["mix"] = mix
        save_project(base, project)
        preview = {}
        for tr in project.get("tracks") or []:
            tid = str(tr.get("id"))
            name = role_preset(tid, tr.get("fx"))
            if name:
                preview[tid] = name
        return {"ok": True, "autoFx": mix["autoFx"], "roles": preview,
                "note": ("已打开：没挂链的轨按角色自动套预设" if mix["autoFx"]
                         else "已关闭：只用显式 fxChain")}

    if op in ("layout", "genre"):
        genre = (bus or {}).get("genre") if isinstance(bus, dict) else (bus or None)
        new_buses, routed, sends = _genre_layout(project, buses, genre)
        project["buses"] = new_buses
        save_project(base, project)
        return {"ok": True, "buses": [b["id"] for b in new_buses], "routed": routed,
                "sends": sends,
                "note": (f"按曲风接好 {len(routed)} 条轨："
                         f"鼓/贝斯/键盘/吉他 → mix → 主输出")}

    if op == "clear":
        cleared = []
        for tr in project.get("tracks") or []:
            if tr.get("bus"):
                tr.pop("bus", None)
                cleared.append(str(tr.get("id")))
            tr.pop("send", None)          # 送出量也一起拆，否则清空后混音还是有混响尾巴
        project["buses"] = []
        save_project(base, project)
        return {"ok": True, "unrouted": cleared,
                "note": f"清空全部总线，{len(cleared)} 条轨回主输出，送出量已归零"}

    raise ValueError(f"不认识的 op：{op}（list/set/remove/route/send/auto/layout/clear）")
