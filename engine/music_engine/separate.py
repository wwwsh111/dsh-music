"""人声/伴奏分离。

两种后端：
- demucs：装了 torch + demucs 时使用，质量最好（PC 上推荐）。
- center：手机/无 GPU 环境的回退方案 —— 中心声道相干性掩码 + HPSS + 带限。
  能拿到"以人声为主"的近似结果，但鼓/贝斯的中心成分会残留，不能当成专业分离。

输出始终是 48 kHz 立体声 float WAV：vocals.wav / instrumental.wav。
"""
import shutil
import subprocess
import sys
from pathlib import Path

from .util import ensure_dir

SR = 48000


def _resample(data, sr, target=SR):
    import numpy as np
    from scipy.signal import resample_poly
    import math
    if sr == target:
        return data
    g = math.gcd(int(sr), int(target))
    up, down = int(target // g), int(sr // g)
    return np.stack([resample_poly(data[:, c], up, down) for c in range(data.shape[1])], axis=1).astype("float32")


def load_stereo(path, sr=SR):
    import numpy as np
    import soundfile as sf
    data, rate = sf.read(str(path), always_2d=True, dtype="float32")
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    return _resample(data, rate, sr), sr


def demucs_available():
    try:
        import importlib.util
        return importlib.util.find_spec("demucs") is not None and \
            importlib.util.find_spec("torch") is not None
    except Exception:
        return False


def _center_split(data, sr):
    import numpy as np
    import librosa
    from scipy.signal import butter, sosfilt

    # 分离在 22.05 kHz 上做：人声内容都在 11 kHz 以下，速度约快 2 倍
    work_sr = 22050 if sr > 22050 else sr
    work = _resample(data, sr, work_sr) if work_sr != sr else data

    n_fft, hop = 4096, 1024
    left = librosa.stft(work[:, 0], n_fft=n_fft, hop_length=hop)
    right = librosa.stft(work[:, 1], n_fft=n_fft, hop_length=hop)
    mid = (left + right) * 0.5
    side = (left - right) * 0.5
    coherence = np.abs(mid) / (np.abs(mid) + np.abs(side) + 1e-9)
    mask = np.clip((coherence - 0.55) / 0.45, 0.0, 1.0) ** 1.5
    vocal = librosa.istft(mid * mask, hop_length=hop, length=work.shape[0])

    # 去掉打击乐成分：人声几乎全在谐波里
    harmonic, _ = librosa.effects.hpss(vocal.astype("float32"))
    vocal = harmonic

    # 带限：低频给贝斯/底鼓，极高频给镲片
    sos = butter(4, 100 / (work_sr / 2), btype="high", output="sos")
    vocal = sosfilt(sos, vocal)
    low = min(14000, work_sr * 0.48)
    sos = butter(4, low / (work_sr / 2), btype="low", output="sos")
    vocal = sosfilt(sos, vocal).astype("float32")

    if work_sr != sr:
        vocal = _resample(vocal[:, None], work_sr, sr)[:, 0]

    # resample_poly 的输出长度可能差 1~2 个采样，先对齐再相减
    n = data.shape[0]
    if vocal.shape[0] > n:
        vocal = vocal[:n]
    elif vocal.shape[0] < n:
        import numpy as _np
        vocal = _np.pad(vocal, (0, n - vocal.shape[0]))

    inst = (data - vocal[:, None]).astype("float32")
    return vocal, inst


def _demucs_split(src, out_dir):
    import numpy as np
    import soundfile as sf

    cmd = [sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
           "--out", str(out_dir), str(src)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError("demucs 分离失败：" + (r.stderr or r.stdout or "")[-500:])
    root = Path(out_dir) / "htdemucs" / Path(src).stem
    vocals = root / "vocals.wav"
    no_vocals = root / "no_vocals.wav"
    if not vocals.exists() or not no_vocals.exists():
        raise RuntimeError(f"demucs 没有产出预期文件：{root}")
    v, sr = sf.read(str(vocals), always_2d=True, dtype="float32")
    i, _ = sf.read(str(no_vocals), always_2d=True, dtype="float32")
    return _resample(v, sr), _resample(i, sr)


def _db(x):
    import numpy as np
    return float(20 * np.log10(np.max(np.abs(x)) + 1e-9)) if x.size else -120.0


def separate(src, out_dir=None, backend="auto"):
    """返回 {backend, vocals, instrumental, warnings, vocalPeakDb, presence}。"""
    import numpy as np
    import soundfile as sf

    src = Path(src)
    if not src.exists():
        raise ValueError(f"音频不存在: {src}")
    out_dir = ensure_dir(Path(out_dir) if out_dir else src.parent / "separated")

    warnings = []
    data, sr = load_stereo(src)
    mode = backend
    if mode in ("auto", "", None):
        mode = "demucs" if demucs_available() else "center"

    vocal = inst = None
    if mode == "demucs":
        try:
            vocal, inst = _demucs_split(src, out_dir)
        except Exception as e:
            warnings.append(f"demucs 不可用，回退中心声道分离：{e}")
            mode = "center"
    if mode == "center":
        if demucs_available():
            warnings.append("demucs 已安装但本次未使用；为保证速度使用中心声道分离")
        vocal, inst = _center_split(data, sr)
        warnings.append("中心声道分离是近似方案：鼓/贝斯的中置成分会残留，人声可能有伴奏残响")

    n = data.shape[0]
    if vocal.shape[0] > n:
        vocal = vocal[:n]
    elif vocal.shape[0] < n:
        vocal = np.pad(vocal, (0, n - vocal.shape[0]))
    if inst.shape[0] > n:
        inst = inst[:n]
    elif inst.shape[0] < n:
        inst = np.pad(inst, ((0, n - inst.shape[0]), (0, 0)))

    vpath = out_dir / "vocals.wav"
    ipath = out_dir / "instrumental.wav"
    sf.write(str(vpath), vocal, sr, subtype="FLOAT")
    sf.write(str(ipath), inst, sr, subtype="FLOAT")

    # 人声存在度：逐帧能量对比
    presence = None
    try:
        import librosa
        rv = librosa.feature.rms(y=vocal, frame_length=2048, hop_length=512)[0]
        rx = librosa.feature.rms(y=data.mean(axis=1), frame_length=2048, hop_length=512)[0]
        n = min(len(rv), len(rx))
        ratio = rv[:n] / (rx[:n] + 1e-9)
        presence = round(float(np.mean(ratio > 0.35)), 3)
        if presence < 0.15:
            warnings.append("几乎没有检测到人声（或人声不在中置）；Remix 结果可能缺少主唱")
    except Exception:
        pass

    return {
        "backend": mode, "vocals": str(vpath), "instrumental": str(ipath),
        "sampleRate": sr, "seconds": round(vocal.shape[0] / sr, 2),
        "vocalPeakDb": round(_db(vocal), 2), "instrumentalPeakDb": round(_db(inst), 2),
        "presence": presence, "warnings": warnings,
    }
