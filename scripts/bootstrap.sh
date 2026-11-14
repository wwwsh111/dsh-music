#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${VENV:-$HOME/.local/share/dsh-music/venv}"
PY="${PYTHON:-python3}"

echo "==> 检查系统工具"
for c in ffmpeg fluidsynth; do
  command -v "$c" >/dev/null 2>&1 || {
    echo "缺少 $c。Ubuntu/Debian: sudo apt install ffmpeg fluidsynth fluid-soundfont-gm"
    echo "macOS: brew install ffmpeg fluid-synth"
    exit 1
  }
done

echo "==> 创建 venv: $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -U pip
"$VENV/bin/pip" install numpy scipy soundfile librosa pedalboard pyloudnorm pretty_midi mido pypinyin
if ! "$VENV/bin/python" -c "import pyworld" >/dev/null 2>&1; then
  echo "可选：music_tune 需要 pyworld，安装：$VENV/bin/pip install pyworld"
fi

echo "==> 复制 skill"
mkdir -p "$HOME/.dsh/skills"
rm -rf "$HOME/.dsh/skills/music-production"
cp -r skills/music-production "$HOME/.dsh/skills/"

echo "==> 自检"
"$VENV/bin/python" - <<'PY'
import numpy, scipy, soundfile, librosa, pedalboard, pyloudnorm, pretty_midi, mido, pypinyin
print("python deps ok")
PY
echo "完成。把 venv 路径写进 profile 的 dsh-music 配置（默认就是 $VENV）。"
