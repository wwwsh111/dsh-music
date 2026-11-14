#!/usr/bin/env python3
"""开发用假声音服务：验证 dsh-music 的唱歌/说话桥，不需要 GPU。

    python scripts/mock_voice_service.py --port 9880 --key testkey

只实现 /health、/v1/capabilities、/v1/assets、/v1/singing、/v1/speech、
/v1/jobs/{id}、/v1/jobs/{id}/audio；返回一段正弦波，用来验证上传/轮询/下载/对齐。
"""
import argparse
import io
import json
import math
import secrets
import struct
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "testkey"
JOBS = {}


def tone(seconds=6.0, freq=440.0, sr=40000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(seconds * sr)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / (0.05 * sr), (n - i) / (0.2 * sr))
            v = int(12000 * env * math.sin(2 * math.pi * freq * i / sr))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get("authorization") == "Bearer " + KEY

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "version": "mock"})
        if not self._auth():
            return self._json(401, {"detail": "unauthorized"})
        if self.path == "/v1/capabilities":
            return self._json(200, {
                "tts": [{"voice": "feiyu", "available": True, "styles": ["cute", "gentle"]}],
                "rvc": [{"voice": "feiyu", "available": True}],
                "singing": {"available": True},
                "queue": {"waiting": 0, "running": False},
            })
        if self.path.endswith("/audio"):
            job_id = self.path.split("/")[3]
            data = tone(6.0)
            self.send_response(200)
            self.send_header("content-type", "audio/wav")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/v1/jobs/"):
            job_id = self.path.split("/")[3]
            job = JOBS.get(job_id)
            if not job:
                return self._json(404, {"detail": "job not found"})
            status = "success" if time.time() >= job["ready"] else "running"
            return self._json(200, {"id": job_id, "status": status, "error": None})
        return self._json(404, {"detail": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._json(401, {"detail": "unauthorized"})
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        if self.path == "/v1/assets":
            return self._json(201, {"id": secrets.token_hex(16)})
        if self.path in ("/v1/singing", "/v1/speech"):
            try:
                payload = json.loads(body.decode() or "{}")
            except Exception:
                payload = {}
            job_id = secrets.token_hex(16)
            JOBS[job_id] = {"ready": time.time() + 1.0, "payload": payload}
            return self._json(202, {"id": job_id})
        return self._json(404, {"detail": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--key", default="testkey")
    args = ap.parse_args()
    global KEY
    KEY = args.key
    print(f"mock voice service on 127.0.0.1:{args.port} key={KEY}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
