import json
import mimetypes
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import CONFIG, SAMPLE_RATE
from .project import load_project
from .util import ensure_dir, jdump, jload


def _base_url():
    return (CONFIG.get("voiceBaseUrl") or "http://127.0.0.1:9880").rstrip("/")


def _key():
    candidates = []
    p = CONFIG.get("voiceApiKeyFile")
    if p:
        candidates.append(Path(p).expanduser())
    home = Path.home()
    candidates += [
        home / "feiyu" / "server" / ".runtime" / "voice-service" / "api_key.txt",
        home / ".local" / "share" / "feiyu-voice" / ".runtime" / "voice-service" / "api_key.txt",
        Path.cwd() / ".runtime" / "voice-service" / "api_key.txt",
    ]
    for c in candidates:
        if c.exists():
            return c.read_text(encoding="utf-8").strip()
    return ""


def _request(method, path, data=None, headers=None, timeout=30):
    url = _base_url() + path
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                return r.status, json.loads(body.decode("utf-8"))
            return r.status, body
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"声音服务 HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上声音服务 {_base_url()}：{e.reason}")


def _get_json(path, auth=True):
    headers = {"authorization": "Bearer " + _key()} if auth else {}
    return _request("GET", path, headers=headers)[1]


def _post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    return _request("POST", path, data=data, headers={
        "authorization": "Bearer " + _key(),
        "content-type": "application/json",
    })[1]


def _upload(path):
    p = Path(path)
    boundary = "----dshmusic" + secrets.token_hex(8)
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + p.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    status, res = _request("POST", "/v1/assets", data=body, headers={
        "authorization": "Bearer " + _key(),
        "content-type": f"multipart/form-data; boundary={boundary}",
    }, timeout=120)
    return res["id"]


# 模型音素表把 ü 写成 u:（律 lu:4、女 nu:3），j/q/x 后直接写 u。
# pypinyin 给的是 lv4/nv3，不转换会被服务拒绝。
# 服务的拼音校验只接受 [a-zv]+声调，ü 无法表达，所以 lv/nv 降级成 lu/nu
# （律唱成「路」的音）；要更准就在工程里放 lyrics/pinyin.txt 手写。
_PINYIN_FIX = (("lv", "lu"), ("nv", "nu"))


def _fix_pinyin(token):
    out = token.lower()
    for bad, good in _PINYIN_FIX:
        if out.startswith(bad):
            return good + out[len(bad):]
    return out


_PHONESET_CACHE = {}


def _phoneset():
    """读音词表：肥鱼服务原样校验每个拼音，缺一个就整首失败。

    词表不在（换了机器）就返回 None，退回「让服务自己猜」的旧行为。
    """
    if "set" in _PHONESET_CACHE:
        return _PHONESET_CACHE["set"]
    home = Path.home()
    cands = []
    cfg = CONFIG.get("voiceRoot") or ""
    if cfg:
        cands.append(Path(cfg).expanduser() / "assets" / "singing" / "phone_set.json")
    for root in (home / "feiyu_repo", home / "Music" / "feiyu_repo", home / "feiyu"):
        cands.append(root / "server" / "assets" / "singing" / "phone_set.json")
        cands.append(root / "assets" / "singing" / "phone_set.json")
    found = None
    for c in cands:
        if c.exists():
            try:
                data = jload(c, None)
                if isinstance(data, list) and data:
                    found = set(str(x) for x in data)
            except Exception:
                found = None
            if found:
                break
    _PHONESET_CACHE["set"] = found
    return found


def _lev(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _nearest_syllable(token):
    """在词表里找读音最接近、且确实存在的那一个（先同调、再同韵母）。"""
    pool = [t for t in _phoneset() if t.startswith("zh_")]
    if not pool:
        return token
    body = token[3:] if token.startswith("zh_") else token
    # 拆出「音节 + 声调」
    tone = body[-1] if body and body[-1] in "12345" else "5"
    syl = body[:-1] if body and body[-1] in "12345" else body
    same_tone = [t for t in pool if t.endswith(tone)]
    best, best_d = None, 99
    for t in same_tone:
        d = _lev(syl, t[3:-1])
        if d < best_d:
            best, best_d = t, d
    if best and best_d <= 2:
        return best
    best2, best_d2 = None, 99
    for t in pool:
        d = _lev(syl, t[3:-1]) * 2 + (0 if t.endswith(tone) else 1)
        if d < best_d2:
            best2, best_d2 = t, d
    return best2 or token


def _valid_pinyin(token):
    ps = _phoneset()
    if not ps:
        return True
    return token in ps or ("zh_" + token) in ps if not token.startswith("zh_") else token in ps


def _pinyin_tokens(chars):
    """按对齐表里每个音符的字生成拼音，避免模型自己猜多音字。"""
    if not chars:
        return []
    try:
        from pypinyin import Style, lazy_pinyin
    except Exception:
        return []
    try:
        raw = lazy_pinyin("".join(chars), style=Style.TONE3,
                          neutral_tone_with_five=True, errors="ignore")
    except Exception:
        return []
    out = [_fix_pinyin(t) for t in raw if t]
    if len(out) != len(chars):
        return []  # 数量对不上就让服务自己按歌词猜，别让整首失败
    ps = _phoneset()
    if ps:
        fixed = []
        for t in out:
            key = t if t in ps else ("zh_" + t if ("zh_" + t) in ps else None)
            if key is None:
                key = _nearest_syllable("zh_" + t)
            fixed.append(key[3:] if key.startswith("zh_") else key)
        out = fixed
    return out


def _pinyin_text(base, part="lead"):
    """工程里放了 lyrics/pinyin.txt 就用它，否则按歌词自动生成。"""
    manual = base / "lyrics" / ("pinyin-harmony.txt" if part == "harmony" else "pinyin.txt")
    if manual.exists():
        return manual.read_text(encoding="utf-8").strip()
    align_name = "vocal-harmony-align.json" if part == "harmony" else "vocal-align.json"
    align = jload(base / "analysis" / align_name, {"notes": []})
    chars = [n.get("char", "") for n in align.get("notes", []) if n.get("char")]
    return " ".join(_pinyin_tokens(chars))


def _upload_text(text, name):
    tmp = Path.home() / "Music" / "dsh-music" / ".tmp"
    ensure_dir(tmp)
    f = tmp / name
    f.write_text(text, encoding="utf-8")
    return _upload(f)


def capabilities():
    out = {"baseUrl": _base_url(), "key": bool(_key())}
    try:
        out["health"] = _get_json("/health", auth=False)
    except Exception as e:
        out["health"] = {"ok": False, "error": str(e)}
        return out
    try:
        out["capabilities"] = _get_json("/v1/capabilities")
    except Exception as e:
        out["capabilities"] = {"error": str(e)}
    return out


def _wait(job_id, timeout=2400):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _get_json(f"/v1/jobs/{job_id}")
        if row.get("status") in ("success", "failed", "interrupted"):
            if row["status"] == "success":
                return row
            # 长歌会撞上服务端 30 分钟上限而被标 failed，但音频其实已经写全。
            # 先探一下 /audio，能拉到就当成功用，只是记一条警告。
            if row.get("audioReady") or _local_audio(job_id) or _audio_ready(job_id):
                row = dict(row)
                row["status"] = "success"
                row["warning"] = f"任务被标记 {row.get('statusText') or 'failed'}（{row.get('error')}），但音频已完整生成"
                return row
            raise RuntimeError(f"声音任务 {job_id} {row['status']}: {row.get('error')}")
        time.sleep(2)
    raise TimeoutError(f"声音任务 {job_id} 仍在队列中；稍后用 job 查询")


def _audio_ready(job_id):
    try:
        status, data = _request("GET", f"/v1/jobs/{job_id}/audio",
                                headers={"authorization": "Bearer " + _key()}, timeout=300)
        return status == 200 and len(data) > 40000
    except Exception:
        return False


_JOB_ROOTS = ("voiceRoot", "feiyuRoot")


def _job_dir(job_id):
    """声音服务就在本机：长任务被服务端按 30 分钟上限标 failed 时，
    产物其实已经写在 job 目录里，可以直接取。"""
    cands = []
    cfg = CONFIG or {}
    for key in _JOB_ROOTS:
        root = cfg.get(key)
        if root:
            cands.append(Path(root))
    home = Path.home()
    cands += [home / "Music" / "feiyu_repo" / "server", home / "feiyu_repo" / "server",
              home / "feiyu" / "server", home / "Music" / "feiyu" / "server"]
    for root in cands:
        d = root / ".runtime" / "voice-service" / "jobs" / job_id
        if d.is_dir():
            return d
    return None


def _local_audio(job_id):
    d = _job_dir(job_id)
    if not d:
        return None
    for rel in ("output/svs/generated.wav", "output/speech/generated.wav"):
        f = d / rel
        if f.is_file() and f.stat().st_size > 40000:
            return f
    return None


def _download(job_id, out_path):
    local = _local_audio(job_id)
    if local is not None:
        shutil.copyfile(local, out_path)
        return out_path
    status, data = _request("GET", f"/v1/jobs/{job_id}/audio",
                            headers={"authorization": "Bearer " + _key()}, timeout=120)
    Path(out_path).write_bytes(data)
    return out_path


def _resample(src, dst):
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ar", str(SAMPLE_RATE), "-ac", "1",
           "-c:a", "pcm_s24le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError("重采样失败：" + r.stderr[-300:])


def _align_to_score(path, base, part="lead"):
    """SVS 输出普遍带 50–150 ms 的起唱延迟；按谱面量出来，直接挪回去。

    不修的话混音阶段人声会比伴奏晚一点点：单听不明显，一起放像「抢拍」。
    只在置信度 ≥ 2 且偏移 15–400 ms 时动手，量不准宁可不动。
    """
    try:
        from . import timing as tm, dsp
        align_name = "vocal-harmony-align.json" if part == "harmony" else "vocal-align.json"
        notes = (jload(Path(base) / "analysis" / align_name, {}) or {}).get("notes") or []
        if len(notes) < 4:
            return None
        data, sr = dsp.load(path, SAMPLE_RATE)
        off, conf = tm.measure_offset(data, sr, tm.score_onsets(notes))
        if conf >= 2.0 and 15.0 <= abs(off) <= 400.0:
            data = tm.shift(data, sr, -off)
            dsp.save(path, data, sr, subtype="PCM_24")
            return off
    except Exception:
        return None
    return None


def _lyrics_string(base, part="lead"):
    align_name = "vocal-harmony-align.json" if part == "harmony" else "vocal-align.json"
    align = jload(base / "analysis" / align_name, {"notes": []})
    chars = [n.get("char", "") for n in align.get("notes", []) if n.get("char")]
    if chars:
        return "".join(chars)
    data = jload(base / "lyrics" / "lyrics.json", {"sections": []})
    out = []
    for sec in data.get("sections", []):
        for line in sec.get("lines", []):
            out += [c for c in line.get("text", "") if "\u4e00" <= c <= "\u9fff"]
    return "".join(out)


def _pick_voice(project, voice=None):
    """音色选择顺序：显式传入 > 工程里的 vocal.voice > feiyu。"""
    if voice:
        return str(voice).strip() or "feiyu"
    try:
        chosen = (project.get("vocal") or {}).get("voice")
    except AttributeError:
        chosen = None
    return str(chosen).strip() if chosen else "feiyu"


def sing(project_path, section=None, part="lead", job_id=None, wait=True, voice=None):
    """用肥鱼 SVS 唱出人声。

    part=harmony 唱和声轨：谱面和歌词都取自 music_harmony 排好的
    midi/vocal-harmony.mid 与 analysis/vocal-harmony-align.json（每句跟哪些音、跟几度都在里面）。
    """
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    project = load_project(base)
    name = "vocal-harmony" if part == "harmony" else "vocal"
    midi = base / "midi" / f"{name}.mid"
    if not midi.exists():
        if part == "harmony":
            raise ValueError("没有 midi/vocal-harmony.mid；先 music_harmony 排和声")
        raise ValueError(f"没有 midi/{name}.mid；先 music_compose 生成人声旋律")
    if not _key():
        raise ValueError("读不到声音服务 API Key；配置 voiceApiKeyFile 或先部署服务")
    lyrics = _lyrics_string(base, part)
    if not lyrics:
        raise ValueError("没有歌词；先 music_lyrics")
    # job_id 可以直接续查一个已经在跑的任务（SVS 在 CPU 上一段要 6 分钟）
    if not job_id:
        score_id = _upload(midi)
        # track/channel 交给服务自己挑：mido 写出的第一轨是速度/拍号，
        # 主旋律在第 1 轨第 0 通道，硬写 0/0 会找不到音符。
        payload = {"voice": _pick_voice(project, voice), "score_id": score_id, "lyrics": lyrics,
                   "bpm": project["tempo"], "segment_seconds": 20}
        pinyin = _pinyin_text(base, part)
        if pinyin:
            payload["pinyin_id"] = _upload_text(pinyin, f"pinyin-{part}.txt")
        row = _post_json("/v1/singing", payload)
        job_id = row["id"]
    if not wait:
        return {"ok": True, "submitted": True, "job": job_id, "part": part}
    _wait(job_id)
    raw = base / "audio" / "vocal" / f"raw-{job_id}.wav"
    ensure_dir(raw.parent)
    _download(job_id, raw)
    tag = section or part
    out = base / "audio" / "vocal" / f"vocal-{tag}-v1.wav"
    _resample(raw, out)
    aligned = _align_to_score(out, base, part)
    report = {"job": job_id, "score": str(midi), "part": part,
              "voice": _pick_voice(project, voice),
              "lyricsChars": len(lyrics), "output": str(out), "sampleRate": SAMPLE_RATE}
    if aligned is not None:
        report["alignedMs"] = round(float(aligned), 1)
    jdump(base / "report" / f"vocal-{tag}-v1.json", report)
    return {"ok": True, "vocal": str(out), "part": part, "job": job_id, "report": report}


def speak(text, style="cute", voice="feiyu"):
    if not _key():
        raise ValueError("读不到声音服务 API Key")
    row = _post_json("/v1/speech", {"voice": voice or "feiyu", "text": text, "style": style})
    job_id = row["id"]
    _wait(job_id)
    out = Path("speech-" + job_id + ".wav")
    _download(job_id, out)
    return {"ok": True, "speech": str(out), "job": job_id}

def _duration_of(src):
    # 先看 wav 头（soundfile 是本引擎的老依赖，比调外部程序稳）；
    # 非 wav 再退回 ffmpeg 的 stderr —— 很多机器只装了 ffmpeg，没有 ffprobe。
    try:
        import soundfile as sf
        info = sf.info(str(src))
        if info.samplerate:
            return float(info.frames) / float(info.samplerate)
    except Exception:
        pass
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src)],
                             capture_output=True, text=True, timeout=60, errors="replace")
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


def import_vocal(project_path, source, part="lead", section=None, label=None, align=True):
    """把外部软件渲染好的人声导进工程 —— 洛天依这条线的通用接口。

    本引擎自己只会用肥鱼 SVS/RVC 合成；洛天依的合法干声通常来自
    用户手上的商业/公共模型（ACE Studio、Synthesizer V、GPT-SoVITS、
    或者用户自己的开源程序）。这里只做四件事：
      1. 重采样成工程规格（48 kHz / 单声道 / 24 bit）
      2. 按谱面量出起唱延迟并挪回去（和 SVS 输出同一个对齐逻辑）
      3. 落成标准产物名 audio/vocal/vocal-<tag>-v1.wav
      4. 写 report，标明 engine=external 与音色标签
    之后 tune / yutiao / mix / master / design / export 全部照常吃它。
    """
    base = Path(project_path)
    if base.is_file():
        base = base.parent
    load_project(base)
    src = Path(str(source)).expanduser()
    if not src.exists():
        raise ValueError(f"人声文件不存在: {src}")
    tag = section or part
    out = base / "audio" / "vocal" / f"vocal-{tag}-v1.wav"
    ensure_dir(out.parent)
    before = _duration_of(src)
    _resample(src, out)
    duration = _duration_of(out)
    aligned = None
    if align:
        aligned = _align_to_score(out, base, part)
    report = {
        "engine": "external",
        "source": str(src),
        "voiceLabel": (label or src.stem),
        "part": part,
        "output": str(out),
        "sampleRate": SAMPLE_RATE,
        "duration": round(duration, 3) if duration else None,
        "sourceDuration": round(before, 3) if before else None,
    }
    if aligned is not None:
        report["alignedMs"] = round(float(aligned), 1)
    jdump(base / "report" / f"vocal-{tag}-v1.json", report)
    return {"ok": True, "vocal": str(out), "part": part, "report": report,
            "next": ("接着 music_tune（演唱设计）/ music_yutiao（语调）"
                     " / music_mix（混音）")}


def convert_voice(src, out_path, voice="feiyu"):
    """RVC 换声：上传干声 → /v1/voice-conversion → 下载结果（48 kHz 单声道）。"""
    src = Path(src)
    if not src.exists():
        raise ValueError(f"待换声音频不存在: {src}")
    if not _key():
        raise ValueError("读不到声音服务 API Key；配置 voiceApiKeyFile 或先部署服务")
    asset = _upload(src)
    row = _post_json("/v1/voice-conversion", {"input_id": asset, "voice": voice})
    job_id = row.get("id")
    if not job_id:
        raise RuntimeError(f"声音服务没有返回任务 id：{row}")
    _wait(job_id)
    out_path = Path(out_path)
    ensure_dir(out_path.parent)
    raw = out_path.with_suffix(".raw.wav")
    _download(job_id, raw)
    _resample(raw, out_path)
    jdump(out_path.parent.parent.parent / "report" / "voice-conversion.json",
          {"job": job_id, "input": str(src), "output": str(out_path), "voice": voice})
    return str(out_path)
