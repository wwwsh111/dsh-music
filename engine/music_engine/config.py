import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"

def _cfg():
    try:
        return json.loads(os.environ.get("MUSIC_CONFIG") or "{}")
    except Exception:
        return {}

CONFIG = _cfg()
ROOT = Path(os.environ.get("MUSIC_ROOT") or (Path.home() / "Music" / "dsh-music"))


def _find_font(filename, env_name):
    """找音源文件。Linux 有系统路径，Windows 上 fluidsynth 的 SF2 通常散落在
    别处（msys2 的 usr/share、DSH 的 bash 包里），所以按候选列表逐个试，
    最后再兜底扫几个常见目录。找不到就报清楚，别让渲染阶段莫名其妙失败。
    """
    env = os.environ.get(env_name)
    cands = [env] if env else []
    cands += [
        str(ASSETS / "soundfonts" / filename),
        f"/usr/share/sounds/sf2/{filename}",
        f"/usr/share/soundfonts/{filename}",
        str(Path.home() / ".local/share/dsh-music" / filename),
        "C:\\usr\\share\\sounds\\sf2\\" + filename,
        "C:\\Program Files\\fluidsynth\\soundfonts\\" + filename,
    ]
    for pat in (f"*/usr/share/sounds/sf2/{filename}",
                f"*/usr/share/soundfonts/{filename}"):
        home = Path.home()
        for root in (home / ".dsh", home / "AppData/Local", Path("C:/msys64"), Path("C:/tools")):
            if root.exists():
                try:
                    cands += [str(p) for p in root.glob(pat)]
                except Exception:
                    pass
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    return Path(env or f"/usr/share/sounds/sf2/{filename}")


# 鼓用 FluidR3：它有 31 套鼓组、30 个 kick 采样，比 GeneralUser 的 11 套完整。
SOUNDFONT = _find_font("FluidR3_GM.sf2", "MUSIC_SOUNDFONT")
# 旋律乐器用 GeneralUser GS：吉他采样 22 → 71，贝斯层次更细，整包只有 31 MB。
# 找不到时静默回退到 FluidR3，功能不退化。
_GUGS = _find_font("GeneralUser.sf2", "MUSIC_SOUNDFONT_MELODIC")
MELODIC_SOUNDFONT = _GUGS if _GUGS.exists() else SOUNDFONT
TOKEN = os.environ.get("MUSIC_TOKEN") or ""
SAMPLE_RATE = int(CONFIG.get("sampleRate") or 48000)
