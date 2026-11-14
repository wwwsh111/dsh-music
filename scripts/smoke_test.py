#!/usr/bin/env python3
"""端到端冒烟测试：引擎 + 假声音服务，走完 建工程→歌词→编曲→渲染→混音→母带→导出→唱歌桥。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ENGINE = HERE / "engine"
PORT = 17891
VOICE = 9881
TOKEN = "smoketoken"
KEY = "testkey"


def post(path, payload, port=PORT):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST",
                                headers={"content-type": "application/json",
                                         "authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def wait(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dsh-music-smoke-"))
    keyfile = tmp / "voice-key.txt"
    keyfile.write_text(KEY)
    cfg = {"voiceBaseUrl": f"http://127.0.0.1:{VOICE}", "voiceApiKeyFile": str(keyfile),
           "sampleRate": 48000}
    env = dict(os.environ, MUSIC_TOKEN=TOKEN, MUSIC_ROOT=str(tmp / "projects"),
               MUSIC_CONFIG=json.dumps(cfg), PYTHONUNBUFFERED="1")
    mock = subprocess.Popen([sys.executable, str(HERE / "scripts" / "mock_voice_service.py"),
                             "--port", str(VOICE), "--key", KEY])
    eng = subprocess.Popen([sys.executable, "-m", "music_engine.server", "--port", str(PORT)],
                           cwd=ENGINE, env=env)
    try:
        assert wait(f"http://127.0.0.1:{PORT}/health"), "engine 未启动"
        assert wait(f"http://127.0.0.1:{VOICE}/health"), "mock 未启动"
        print("doctor:", json.dumps(post("/v1/doctor", {}), ensure_ascii=False)[:300])

        p = post("/v1/projects", {"op": "create", "name": "Smoke City", "genre": "citypop",
                                  "bpm": 112, "key": "F#m", "bars": 32})
        proj = p["project"]
        print("project:", proj)

        ly = post("/v1/lyrics", {"op": "set", "project": proj,
                                 "text": "[verse]\n霓虹在雨后慢慢亮起\n便利店的灯还醒着\n[chorus]\n带我回到那条街\n风把旧唱片吹得很远"})
        print("lyrics:", ly["totalSyllables"], "warnings:", ly["warnings"][:3])

        c = post("/v1/compose", {"project": proj})
        print("compose:", list(c["written"]), "vocalNotes:", c["vocalNotes"], "dur:", c["durationSec"])

        r = post("/v1/render", {"project": proj})
        print("render:", {k: Path(v).name for k, v in r["stems"].items()})

        v = post("/v1/vocals", {"mode": "sing", "project": proj})
        print("vocals:", v["vocal"], "job:", v["job"])

        m = post("/v1/mix", {"project": proj})
        print("mix:", m["metrics"])

        ms = post("/v1/master", {"project": proj, "targetLufs": -10})
        print("master:", ms["metrics"])

        ex = post("/v1/export", {"project": proj, "daw": "generic"})
        print("export:", ex["export"], "files:", len(ex["files"]))
        print("SMOKE OK", tmp)
    finally:
        for p in (eng, mock):
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
