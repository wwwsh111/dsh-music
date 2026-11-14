param(
  [string]$Repo = (Join-Path $HOME 'feiyu'),
  [string]$Path = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Path) { $Path = Join-Path $Repo 'server' }
if (-not (Test-Path $Repo)) {
  git clone https://github.com/Esmerubphire/astrbot_plugin_feiyu_voice.git $Repo
}
$models = Join-Path $Path 'models'
python scripts\download_voice_models.py --dest $models
$svcVenv = Join-Path $Path '.venv'
if (-not (Test-Path $svcVenv)) { py -3.11 -m venv $svcVenv }
& (Join-Path $svcVenv 'Scripts\python.exe') -m pip install -U pip
& (Join-Path $svcVenv 'Scripts\pip.exe') install -r (Join-Path $Path 'requirements-service.txt')
Write-Host @"
下一步（需要 GPU）：
1. 按 $Path\INSTALL_AGENT.md 用 scripts/setup_runtime.py 安装 tts / rvc / svs 三套环境。
2. 配置 configs/voice_service.json 与 configs/runtime.*.local.json。
3. 启动：$svcVenv\Scripts\python.exe -m feiyu_voice_service --config configs\voice_service.json
4. 首次启动后 API Key 在 $Path\.runtime\voice-service\api_key.txt。
5. SVS 权重与对齐参考需按文档单独准备；没有 SVS 只能说话/换声。
"@
