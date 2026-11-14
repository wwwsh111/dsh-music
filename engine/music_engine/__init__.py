"""dsh-music engine."""

import json
from pathlib import Path


def _detect_version(default="0.0.0"):
    """版本只认插件根目录的 package.json，别再手写第二个常量。

    旧写法是这里写死 0.16.0、package.json 写 1.0.2，结果 /v1/doctor 与
    /health 报的版本和用户装的包永远对不上（发版时只改了一处）。
    打包脚本会重写 package.json 的 version，所以这里读到的就是实际装的版本。
    """
    pkg = Path(__file__).resolve().parents[2] / "package.json"
    try:
        return json.loads(pkg.read_text(encoding="utf-8-sig")).get("version") or default
    except Exception:
        return default


__version__ = _detect_version()
