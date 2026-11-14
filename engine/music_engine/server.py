import argparse
import json
import os
import shutil
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from . import genres as genres_mod
from . import lyrics as lyrics_mod
from . import compose as compose_mod
from . import render as render_mod
from . import mix as mix_mod
from . import master as master_mod
from . import export as export_mod
from . import vocals as vocals_mod
from . import preview as preview_mod
from . import remix as remix_mod
from . import analyze as analyze_mod
from . import tune as tune_mod
from . import theory as theory_mod
from . import soundcheck as soundcheck_mod
from . import design as design_mod
from . import voicebanks as voicebanks_mod
from . import automation as automation_mod
from . import pianoroll as pianoroll_mod
from . import patterns as patterns_mod
from . import sampler as sampler_mod
from . import fxchain as fxchain_mod
from . import routing as routing_mod
from . import live as live_mod
from . import panel as panel_mod
from . import library as library_mod
from .config import ROOT, SOUNDFONT, TOKEN
from .project import create_project, list_projects, load_project
from .util import jload


def _doctor():
    import importlib.util
    mods = {}
    for m in ("numpy", "scipy", "soundfile", "librosa", "pedalboard",
              "pyloudnorm", "pretty_midi", "mido", "demucs", "torch", "pyworld"):
        mods[m] = importlib.util.find_spec(m) is not None
    try:
        usage = shutil.disk_usage(str(ROOT))
        disk = round(usage.free / 1e9)
    except Exception:
        disk = None
    return {
        "ok": True, "version": __version__, "python": sys.version.split()[0],
        "root": str(ROOT), "soundfont": {"path": str(SOUNDFONT), "exists": Path(SOUNDFONT).exists()},
        "ffmpeg": shutil.which("ffmpeg"), "fluidsynth": shutil.which("fluidsynth"),
        "modules": mods, "separateBackend": ("demucs" if mods.get("demucs") else "center"),
        "diskFreeGB": disk, "voice": vocals_mod.capabilities(),
        "soundcheck": soundcheck_mod.status(),
        "library": library_mod.state(),
    }


def _resolve_project(p):
    if not p:
        raise ValueError("缺少 project")
    path = Path(p).expanduser()
    if not path.is_absolute() and not path.exists():
        path = ROOT / path
    return path


def _projects(args):
    op = args.get("op") or "create"
    if op == "list":
        return {"ok": True, "projects": list_projects()}
    if op == "open":
        p = _resolve_project(args.get("project"))
        return {"ok": True, "project": str(p), "data": load_project(p)}
    res = create_project(name=args.get("name"), genre=args.get("genre") or "citypop",
                         bpm=args.get("bpm"), key=args.get("key"), bars=args.get("bars"))
    return res


def _lyrics(args):
    p = _resolve_project(args.get("project"))
    text = args.get("text") or ""
    if (args.get("op") or "set") == "validate":
        return {"ok": True, **lyrics_mod.validate_text(text)}
    return lyrics_mod.set_lyrics(p, text, args.get("section"))


def _todo(name):
    raise ValueError(name + " 尚未实现（M1/M2 里程碑）")


def _vocals(a):
    mode = (a.get("mode") or "sing").lower()
    if mode == "import":
        return vocals_mod.import_vocal(_resolve_project(a.get("project")), a.get("source"),
                                       part=a.get("part") or "lead",
                                       section=a.get("section"),
                                       label=a.get("label"),
                                       align=a.get("align", True))
    if mode == "speak":
        return vocals_mod.speak(a.get("text") or "", a.get("style") or "cute",
                                voice=a.get("voice") or "feiyu")
    return vocals_mod.sing(_resolve_project(a.get("project")), a.get("section"),
                           part=a.get("part") or "lead", job_id=a.get("jobId"),
                           wait=a.get("wait", True), voice=a.get("voice"))


def _yutiao(a):
    # 延迟导入：语调模块即使有毛病，也只坏这一个接口，不拖垮整个引擎
    from . import yutiao as yutiao_mod
    return yutiao_mod.speech(_resolve_project(a.get("project")),
                             section=a.get("section"),
                             intensity=a.get("intensity", 0.8),
                             strength=a.get("strength", 0.95),
                             even=a.get("even", True),
                             ab=a.get("ab", True),
                             dry_run=bool(a.get("dryRun")),
                             seed=a.get("seed"))


def _tunefile(a):
    # 与洛天依（SynthV / VOCALOID）的工程文件互通：导出 / 读回
    from . import tunefile as tunefile_mod
    action = (a.get("action") or "export").lower()
    if action == "import":
        return tunefile_mod.import_tune(_resolve_project(a.get("project")),
                                        a.get("source"),
                                        part=a.get("part") or "lead")
    return tunefile_mod.export_tune(_resolve_project(a.get("project")),
                                    a.get("format") or "svp", a.get("out"))


def _harmony(a):
    # 和声编排：照 AI 翻唱的「分和声」，按段落给策略、剪掉会糊成一片的音
    from . import harmonize as harmonize_mod
    op = (a.get("op") or "plan").lower()
    proj = _resolve_project(a.get("project"))
    if op == "show":
        return harmonize_mod.show(proj)
    if op == "clear":
        return harmonize_mod.clear(proj)
    return harmonize_mod.plan(proj, sections=a.get("sections"),
                              strategy=a.get("strategy"),
                              cut=a.get("cut") or "default",
                              density=a.get("density") or "full",
                              sing=a.get("sing"))


def _vocaldoctor(a):
    # 人声体检：哑音 / 拖尾 / 重叠 / 缺口 / 走音，再按结果修
    from . import vocaldoctor as doctor_mod
    op = (a.get("op") or "scan").lower()
    proj = _resolve_project(a.get("project"))
    if op == "fix":
        return doctor_mod.fix(proj, section=a.get("section"), vocal=a.get("vocal"),
                              actions=a.get("actions"), dry_run=bool(a.get("dryRun")),
                              pitch_strength=a.get("pitchStrength", 0.9),
                              pitch_cents=a.get("pitchCents", 45.0))
    if op.startswith("suggest") or op == "score":
        return doctor_mod.suggest_score(proj, section=a.get("section"),
                                        apply=bool(a.get("apply")))
    return doctor_mod.scan(proj, section=a.get("section"), vocal=a.get("vocal"),
                           pitch_cents=a.get("pitchCents", 45.0),
                           range_min=a.get("rangeMin", 50),
                           range_max=a.get("rangeMax", 86))


def _vocalfinish(a):
    # S5 收尾一步到位：分和声 → 体检 → 修补 → 重唱建议（可选调教）
    from . import vocalfinish as finish_mod
    proj = _resolve_project(a.get("project"))
    return finish_mod.finish(proj, section=a.get("section"),
                             harmony=a.get("harmony") or "auto",
                             tune_style=a.get("tune") or None,
                             fix=bool(a.get("fix", True)),
                             density=a.get("density") or "full",
                             cut=a.get("cut") or "default",
                             dry_run=bool(a.get("dryRun")))


ROUTES = {
    "/v1/doctor": lambda a: _doctor(),
    "/v1/genres": lambda a: (genres_mod.get_genre(a["id"]) if a.get("id") and a.get("op") == "get"
                             else {"ok": True, "genres": genres_mod.list_genres()}),
    "/v1/projects": _projects,
    "/v1/lyrics": _lyrics,
    "/v1/compose": lambda a: compose_mod.compose(_resolve_project(a.get("project")),
                                                 seed=a.get("seed"), track=a.get("track")),
    "/v1/render": lambda a: render_mod.render(_resolve_project(a.get("project")), a.get("quality")),
    "/v1/mix": lambda a: mix_mod.mix(project_path=(_resolve_project(a["project"]) if a.get("project") else None),
                                     stems=a.get("stems"), preset=a.get("preset"),
                                     reference=a.get("reference"), target_lufs=a.get("targetLufs")),
    "/v1/master": lambda a: master_mod.master(project_path=(_resolve_project(a["project"]) if a.get("project") else None),
                                              mix_path=a.get("mix"), target_lufs=a.get("targetLufs"),
                                              true_peak=a.get("truePeak", -1.0), preset=a.get("preset")),
    "/v1/export": lambda a: export_mod.export(_resolve_project(a.get("project")),
                                              a.get("daw") or "generic",
                                              a.get("sampleRate") or 48000,
                                              a.get("bitDepth") or 24),
    "/v1/vocals": _vocals,
    "/v1/harmony": _harmony,
    "/v1/vocaldoctor": _vocaldoctor,
    "/v1/vocalfinish": _vocalfinish,
    "/v1/yutiao": _yutiao,
    "/v1/tunefile": _tunefile,
    "/v1/analyze": lambda a: analyze_mod.analyze(a.get("audio"), a.get("stems") or "off",
                                                a.get("targetBpm"), a.get("targetKey"),
                                                deep=bool(a.get("deep")), asr=a.get("asr"),
                                                deep_stems=a.get("deepStems") or "off",
                                                start=a.get("start"), end=a.get("end")),
    "/v1/remix": lambda a: remix_mod.remix(a.get("input"), mode=a.get("mode") or "remix", brief=a.get("brief"), keep=a.get("keep"), output=a.get("output"), target_genre=a.get("targetGenre"), bpm=a.get("bpm"), key=a.get("key"), bars=a.get("bars"), seed=a.get("seed"), backend=a.get("backend") or "auto"),
    "/v1/preview": lambda a: preview_mod.preview(_resolve_project(a.get("project")) if a.get("project") else None,
                                                 a.get("audio")),
    "/v1/tune": lambda a: tune_mod.tune(_resolve_project(a.get("project")),
                                         section=a.get("section"),
                                         style=a.get("style") or "natural",
                                         intensity=a.get("intensity", 0.7),
                                         breath=a.get("breath"),
                                         formant=a.get("formant"),
                                         strength=a.get("strength", 0.92),
                                         mix=a.get("mix", True),
                                         ab=a.get("ab", True),
                                         seed=a.get("seed")),
    "/v1/theory": lambda a: theory_mod.read(a.get("topic"), a.get("section")),
    "/v1/voicebanks": lambda a: voicebanks_mod.scan(a.get("paths"), bool(a.get("hash")),
                                                   a.get("serviceConfig")),
    "/v1/automation": lambda a: automation_mod.edit(_resolve_project(a.get("project")),
                                                    a.get("op") or "list", a.get("lane")),
    "/v1/notes": lambda a: pianoroll_mod.edit(_resolve_project(a.get("project")),
                                              a.get("track"),
                                              a.get("op") or "get",
                                              notes=a.get("notes"),
                                              sel=a.get("sel"),
                                              barDelta=a.get("barDelta", 0),
                                              beatDelta=a.get("beatDelta", 0),
                                              pitchDelta=a.get("pitchDelta", 0),
                                              semitones=a.get("semitones", 0),
                                              value=a.get("value"),
                                              scale=a.get("scale", 1.0),
                                              grid=a.get("grid", 0.5),
                                              strength=a.get("strength", 1.0),
                                              durations=bool(a.get("durations")),
                                              timingMs=a.get("timingMs", 6.0),
                                              velocity=a.get("velJitter", 10.0),
                                              seed=a.get("seed")),
    "/v1/patterns": lambda a: patterns_mod.edit(_resolve_project(a.get("project")),
                                                a.get("op") or "list",
                                                name=a.get("name"),
                                                start_bar=a.get("startBar"),
                                                bars=a.get("bars"),
                                                tracks=a.get("tracks"),
                                                pattern=a.get("pattern"),
                                                repeat=a.get("repeat", 1),
                                                index=a.get("index")),
    "/v1/sampler": lambda a: (sampler_mod.add_track(_resolve_project(a.get("project")),
                                                    a.get("track") or a.get("trackId"),
                                                    a.get("sampler") or a.get("samplerId"))
                              if (a.get("op") or "list") == "addTrack"
                              else sampler_mod.edit(_resolve_project(a.get("project")),
                                                    a.get("op") or "list",
                                                    sampler=a.get("sampler"),
                                                    sid=a.get("id"))),
    "/v1/bus": lambda a: routing_mod.edit(_resolve_project(a.get("project")),
                                         a.get("op") or "list",
                                         bus=a.get("bus"), track=a.get("track"),
                                         sends=a.get("sends"), auto=a.get("auto")),
    "/v1/panel/state": lambda a: panel_mod.state(a),
    "/v1/panel/quick": lambda a: panel_mod.quick(a),
    "/v1/panel/act": lambda a: panel_mod.act(a),
    "/v1/panel/bus": lambda a: panel_mod.bus(a),
    "/v1/panel/library": lambda a: panel_mod.library(a),
    "/v1/fx": lambda a: fxchain_mod.edit(_resolve_project(a.get("project")),
                                         a.get("op") or "list",
                                         track=a.get("track"), chain=a.get("chain"),
                                         preset=a.get("preset")),
    "/v1/design": lambda a: design_mod.design(_resolve_project(a.get("project")),
                                              kind=a.get("kind") or "release",
                                              style=a.get("style") or "swiss",
                                              theme=a.get("theme"),
                                              deep=bool(a.get("deep"))),
    "/v1/live/probe": lambda a: live_mod.probe(),
    "/v1/live/snapshot": lambda a: live_mod.snapshot(seconds=a.get("seconds") or 8,
                                                     grab=bool(a.get("grab")),
                                                     out=a.get("out") or "",
                                                     analyze=a.get("analyze", True)),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-music/" + __version__

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        origin = self._origin()
        if origin:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("vary", "Origin")
            self.send_header("access-control-allow-headers", "content-type")
            self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
            self.send_header("access-control-max-age", "600")
        self.end_headers()
        self.wfile.write(body)

    def _origin(self):
        """只认本机界面：回环 Origin，以及不用 http 载入的桌面壳（dsh-app://app / Origin: null）。

        桌面端（Electron）的页面是 `dsh-app://app/`，不是 http://127.0.0.1，
        所以它的 Origin 既不是回环也不是空，按老规则一律 401 —— 表现就是
        面板永远显示「连不上音乐引擎」。这两个 Origin 只有本机桌面壳/本地文件
        才能产生，普通网站伪造不了，放开它们不会把接口暴露给外网页面。
        引擎本身仍然只绑 127.0.0.1。
        """
        raw = self.headers.get("origin") or ""
        if not raw:
            return ""
        if raw == "null" or raw.startswith("dsh-app://"):
            return raw
        host = raw.split("//", 1)[-1].split("/", 1)[0]
        host = host.split(":")[0].strip("[]").lower()
        return raw if host in ("127.0.0.1", "localhost", "::1") else ""

    def _panel_path(self):
        """面板和音效库接口都算「本机界面」：库里要能直接播音频、点星标。"""
        return self.path.startswith("/v1/panel/") or self.path.startswith("/v1/library/")

    def _panel_allowed(self):
        """面板接口免 token：没有 Origin（curl / 同进程）放行，有 Origin 必须是回环。"""
        origin = self.headers.get("origin")
        return True if not origin else bool(self._origin())

    def _auth(self):
        if not TOKEN:
            return True
        if self._panel_path() and self._panel_allowed():
            return True
        return self.headers.get("authorization", "") == "Bearer " + TOKEN

    def do_OPTIONS(self):
        if not self._origin():
            self._send(403, {"ok": False, "error": "forbidden origin"})
            return
        self._send(200, {"ok": True})

    def _live_stream(self):
        """把系统音频的律动按 SSE 推给面板。

        只推标量（响度 / 三频 / 起音 / BPM / 拍内相位）：PCM 不出引擎进程，
        和 dsh-bgm 一样是 loopback-only —— 只绑 127.0.0.1，浏览器同源直连。
        """
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(self.path).query)

        def num(key, default):
            try:
                return float((q.get(key) or [str(default)])[0])
            except ValueError:
                return float(default)

        fps = max(5.0, min(60.0, num("fps", 30)))
        seconds = max(5.0, min(6 * 3600.0, num("seconds", 3600)))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache, no-store")
        self.send_header("x-accel-buffering", "no")
        self.send_header("connection", "close")
        # EventSource 是跨端口请求，CORS 头不能少，否则浏览器直接断流。
        origin = self._origin()
        if origin:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("vary", "Origin")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            for ev in live_mod.stream(fps=fps, session_seconds=seconds):
                line = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:  # 别让一条坏流把整个引擎带下来
            try:
                msg = {"error": f"{type(e).__name__}: {e}"}
                self.wfile.write(("event: error\ndata: " + json.dumps(msg, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def _serve_audio(self):
        """按 Range 流式送库里的音频：面板要能拖播放头、循环试听。

        只送索引里登记过的音频文件（audio_path 会查库），不是任意路径下载器。
        """
        import mimetypes
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(self.path).query)
        try:
            p = library_mod.audio_path(q.get("id", [None])[0], q.get("path", [None])[0])
        except ValueError as e:
            self._send(404, {"ok": False, "error": str(e)})
            return
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        start, end, code = 0, max(0, size - 1), 200
        rng = (self.headers.get("range") or "").strip()
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "":
                    start, end = max(0, size - int(b)), size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
            except ValueError:
                self.send_response(416)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            code = 206
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("accept-ranges", "bytes")
        self.send_header("content-length", str(end - start + 1))
        if code == 206:
            self.send_header("content-range", f"bytes {start}-{end}/{size}")
        origin = self._origin()
        if origin:
            self.send_header("access-control-allow-origin", origin)
            self.send_header("access-control-expose-headers", "content-range, accept-ranges")
        self.send_header("cache-control", "no-store")
        self.end_headers()
        try:
            with p.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": __version__})
            return
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path.startswith("/v1/panel/live/stream"):
            self._live_stream()
            return
        if self.path.startswith("/v1/library/audio"):
            self._serve_audio()
            return
        if self.path.startswith("/v1/jobs/"):
            self._send(404, {"ok": False, "error": "同步接口下没有该任务；长任务请等待返回"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            args = json.loads(raw.decode("utf-8") or "{}")
            if self.path.startswith("/v1/library/"):
                op = self.path[len("/v1/library/"):].strip("/")
                self._send(200, library_mod.dispatch(op, args))
                return
            route = ROUTES.get(self.path)
            if not route:
                self._send(404, {"ok": False, "error": f"未知接口 {self.path}"})
                return
            result = route(args)
            self._send(200, result if isinstance(result, dict) else {"ok": True, "result": result})
        except ValueError as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17890)
    ns = ap.parse_args(argv)
    ROOT.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", ns.port), Handler)
    print(f"dsh-music engine {__version__} on 127.0.0.1:{ns.port} root={ROOT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
