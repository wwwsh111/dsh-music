param(
  [string]$Venv = '',
  [switch]$SkipSkill
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not $Venv) { $Venv = Join-Path $HOME '.local\share\dsh-music\venv' }
$Venv = [System.IO.Path]::GetFullPath($Venv)

function Find-PythonLauncher {
  # 优先 py 启动器；py 不存在时退回真实 python.exe。
  # 注意：Windows 应用商店的 python.exe 别名会以非 0 退出，必须靠版本探测排除。
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @('-3.12', '-3.11', '-3.10')) {
      $code = & py $v -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $code -and ([int]$code -ge 310)) {
        return [pscustomobject]@{ Exe = 'py'; Prefix = @($v) }
      }
    }
  }
  foreach ($name in @('python', 'python3')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $code = & $cmd.Source -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $code -and ([int]$code -ge 310)) {
      return [pscustomobject]@{ Exe = $cmd.Source; Prefix = @() }
    }
  }
  return $null
}

function Test-Tool($name) {
  if (Get-Command $name -ErrorAction SilentlyContinue) { return $true }
  foreach ($p in @(
      (Join-Path $HOME ".local\bin\$name.exe"),
      (Join-Path $HOME ".local\bin\$name\$name.exe"))) {
    if (Test-Path $p) { return $true }
  }
  return $false
}

Write-Host '==> 检查系统工具' -ForegroundColor Cyan
if (-not (Test-Tool 'ffmpeg')) {
  Write-Host '缺少 ffmpeg：winget install --id Gyan.FFmpeg -e' -ForegroundColor Yellow
  Write-Host '  或把 ffmpeg.exe 放到 %USERPROFILE%\.local\bin 并加入 PATH。' -ForegroundColor Yellow
}
if (-not (Test-Tool 'fluidsynth')) {
  Write-Host '缺少 fluidsynth：winget install --id FluidSynth.FluidSynth -e' -ForegroundColor Yellow
  Write-Host '  或从 https://www.fluidsynth.org/ 安装并加入 PATH。' -ForegroundColor Yellow
}

$py = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
  if (Test-Path $Venv) {
    throw "venv 目录已存在但不完整：$Venv`n请先删除它再重跑：Remove-Item -Recurse -Force `"$Venv`""
  }
  Write-Host "==> 创建 venv: $Venv" -ForegroundColor Cyan
  $launcher = Find-PythonLauncher
  if (-not $launcher) {
    throw ("找不到 Python 3.10+。请先安装：winget install --id Python.Python.3.12 -e`n" +
      "如果 python 命令打开的是微软商店，请在「设置 → 应用 → 高级应用设置 → 应用执行别名」里关掉 python.exe / python3.exe。")
  }
  $venvArgs = @()
  $venvArgs += $launcher.Prefix
  $venvArgs += @('-m', 'venv', $Venv)
  & $launcher.Exe @venvArgs
  if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败（退出码 $LASTEXITCODE）：$Venv" }
  $py = Join-Path $Venv 'Scripts\python.exe'
  if (-not (Test-Path $py)) { throw "venv 创建后仍找不到 python：$py" }
} else {
  Write-Host "==> venv 已存在: $Venv" -ForegroundColor DarkGray
}

$version = & $py -c "import sys;print(sys.version.split()[0])"
Write-Host "==> Python $version" -ForegroundColor DarkGray

Write-Host '==> 安装 Python 依赖（首次可能几分钟）' -ForegroundColor Cyan
& $py -m pip install -U pip
if ($LASTEXITCODE -ne 0) { throw "pip 自更新失败（退出码 $LASTEXITCODE）" }

$deps = @('numpy', 'scipy', 'soundfile', 'librosa', 'pedalboard', 'pyloudnorm',
          'pretty_midi', 'mido', 'pypinyin')
& $py -m pip install @deps
if ($LASTEXITCODE -ne 0) { throw "安装核心 Python 依赖失败（退出码 $LASTEXITCODE）" }

# 可选：调教需要 pyworld；没有它只是 music_tune 不可用，不影响装插件
& $py -c "import pyworld" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host '可选：music_tune 需要 pyworld，安装：' -ForegroundColor Yellow
  Write-Host "  `"$py`" -m pip install pyworld" -ForegroundColor Yellow
}

if (-not $SkipSkill) {
  Write-Host '==> 复制 skill' -ForegroundColor Cyan
  $skillDst = Join-Path $HOME '.dsh\skills\music-production'
  New-Item -ItemType Directory -Force -Path (Split-Path $skillDst) | Out-Null
  if (Test-Path $skillDst) { Remove-Item -Recurse -Force $skillDst }
  Copy-Item -Recurse (Join-Path $root 'skills\music-production') $skillDst
}

Write-Host '==> 自检' -ForegroundColor Cyan
& $py -c "import numpy, scipy, soundfile, librosa, pedalboard, pyloudnorm, pretty_midi, mido, pypinyin; print('python deps ok')"
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖自检失败' }

Write-Host "完成。venv: $Venv" -ForegroundColor Green
