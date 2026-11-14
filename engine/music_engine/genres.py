from pathlib import Path
from .config import ASSETS
from .util import jload

GENRE_DIR = ASSETS / "genres"


def list_genres():
    out = []
    for p in sorted(GENRE_DIR.glob("*.json")):
        g = jload(p, {})
        out.append({
            "id": g.get("id", p.stem),
            "name": g.get("name", p.stem),
            "bpm": g.get("bpm", [90, 120]),
            "vocal": (g.get("vocal") or {}).get("range"),
            "lufs": g.get("lufs"),
        })
    return out


def get_genre(genre_id):
    p = GENRE_DIR / f"{genre_id}.json"
    if not p.exists():
        raise ValueError(f"没有曲风 {genre_id}；可用: " + ", ".join(g["id"] for g in list_genres()))
    return jload(p, {})


def default_bars(name):
    table = {
        "intro": 8, "verse": 16, "prechorus": 8, "chorus": 16, "bridge": 8,
        "outro": 8, "loop_a": 8, "loop_b": 8, "a": 8, "b": 8, "c": 8,
        "hook": 8, "build": 8, "drop": 16, "break": 8, "solo": 8,
        "interlude": 8, "release": 8, "climax": 8,
    }
    return table.get(str(name).lower(), 8)
