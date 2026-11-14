param(
  [switch]$Json,
  [string]$Venv = '',
  [string]$ProjectsRoot = '',
  [string]$VoiceBaseUrl = 'http://127.0.0.1:9880',
  [string]$SoundFont = '',
  [int]$MinDiskGB = 5
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $Venv) { $Venv = Join-Path $HOME '.local\share\dsh-music\venv' }
if (-not $ProjectsRoot) { $ProjectsRoot = Join-Path $HOME 'Music\dsh-music' }
$Venv = [System.IO.Path]::GetFullPath($Venv)
$ProjectsRoot = [System.IO.Path]::GetFullPath($ProjectsRoot)

$script:checks = New-Object System.Collections.ArrayList

function Add-Check {
  param(
    [string]$Id,
    [string]$Name,
    [string]$Status,   # pass / fail / warn / skip
    [string]$Detail,
    [string]$Fix
  )
  [void]$script:checks.Add([pscustomobject]@{
    id = $Id; name = $Name; status = $Status; detail = $Detail; fix = $Fix
  })
}

function Get-ToolInfo {
  param([string]$Name)
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if ($cmd) {
    return [pscustomobject]@{ Path = $cmd.Source; OnPath = $true }
  }
  foreach ($p in @(
      (Join-Path $HOME ".local\bin\$Name.exe"),
      (Join-Path $HOME ".local\bin\$Name\$Name.exe"))) {
    if (Test-Path $p) {
      return [pscustomobject]@{ Path = $p; OnPath = $false }
    }
  }
  return $null
}

function Write-CheckLine {
  param($Check)
  $icon = '·'
  $color = 'Gray'
  switch ($Check.status) {
    'pass' { $icon = '✅'; $color = 'Green' }
    'fail' { $icon = '❌'; $color = 'Red' }
    'warn' { $icon = '⚠'; $color = 'Yellow' }
    'skip' { $icon = '·'; $color = 'DarkGray' }
  }
  Write-Host ("{0} {1}：{2}" -f $icon, $Check.name, $Check.detail) -ForegroundColor $color
  if ($Check.fix) {
    Write-Host ("    修复：{0}" -f $Check.fix) -ForegroundColor DarkGray
  }
}

# ---- 1. node -------------------------------------------------------------
try {
  $node = Get-ToolInfo 'node'
  if (-not $node) {
    Add-Check 'node' 'Node.js' 'fail' '未找到 node' `
      'winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements'
  } else {
    $nv = (& $node.Path --version 2>$null)
    if ($LASTEXITCODE -eq 0 -and $nv) {
      $extra = ''
      if (-not $node.OnPath) { $extra = '（不在 PATH，DSH 可能找不到）' }
      Add-Check 'node' 'Node.js' 'pass' ("{0}{1}  {2}" -f $nv, $extra, $node.Path) ''
    } else {
      Add-Check 'node' 'Node.js' 'fail' ("node 存在但无法运行：{0}" -f $node.Path) `
        '重装 Node.js LTS：winget install --id OpenJS.NodeJS.LTS -e'
    }
  }
} catch {
  Add-Check 'node' 'Node.js' 'fail' ("检测异常：" + $_.Exception.Message) '重装 Node.js LTS'
}

# ---- 2. ffmpeg -----------------------------------------------------------
try {
  $ff = Get-ToolInfo 'ffmpeg'
  if (-not $ff) {
    Add-Check 'ffmpeg' 'ffmpeg' 'fail' '未找到 ffmpeg' `
      'winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements'
  } else {
    $line = (& $ff.Path -version 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $line) {
      if ($ff.OnPath) {
        Add-Check 'ffmpeg' 'ffmpeg' 'pass' ("{0}  {1}" -f $line, $ff.Path) ''
      } else {
        Add-Check 'ffmpeg' 'ffmpeg' 'fail' ("找到 {0}，但不在 PATH；引擎用 PATH 调用会失败" -f $ff.Path) `
          ("把 {0} 所在目录加入 PATH 后重开 DSH" -f (Split-Path $ff.Path -Parent))
      }
    } else {
      Add-Check 'ffmpeg' 'ffmpeg' 'fail' ("ffmpeg 无法运行：{0}" -f $ff.Path) `
        '重装：winget install --id Gyan.FFmpeg -e'
    }
  }
} catch {
  Add-Check 'ffmpeg' 'ffmpeg' 'fail' ("检测异常：" + $_.Exception.Message) '重装 ffmpeg'
}

# ---- 3. fluidsynth -------------------------------------------------------
try {
  $fs = Get-ToolInfo 'fluidsynth'
  if (-not $fs) {
    Add-Check 'fluidsynth' 'FluidSynth' 'fail' '未找到 fluidsynth' `
      'winget install --id FluidSynth.FluidSynth -e --accept-source-agreements --accept-package-agreements'
  } else {
    # 注意：`fluidsynth --version` 和 `-v` 在这个版本会进入交互模式，必须用 --help
    # 并关闭 stdin；否则进程会一直打印 ">" 挂住。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $fs.Path
    $psi.Arguments = '--help'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.StandardInput.Close()
    if (-not $proc.WaitForExit(8000)) {
      try { $proc.Kill() } catch { }
      throw 'fluidsynth --help 8 秒未返回（可能卡在交互模式）'
    }
    $fsOut = $proc.StandardOutput.ReadToEnd()
    $fsErr = $proc.StandardError.ReadToEnd()
    $line = (($fsOut + "`n" + $fsErr) -split "`r?`n" |
      Where-Object { $_ -match 'FluidSynth.*version' } | Select-Object -First 1)
    if ($proc.ExitCode -eq 0 -and $line) {
      if ($fs.OnPath) {
        Add-Check 'fluidsynth' 'FluidSynth' 'pass' ("{0}  {1}" -f $line.Trim(), $fs.Path) ''
      } else {
        Add-Check 'fluidsynth' 'FluidSynth' 'fail' ("找到 {0}，但不在 PATH；渲染会报「找不到 fluidsynth」" -f $fs.Path) `
          ("把 {0} 所在目录加入 PATH 后重开 DSH" -f (Split-Path $fs.Path -Parent))
      }
    } else {
      Add-Check 'fluidsynth' 'FluidSynth' 'fail' ("fluidsynth 无法运行（exit {0}）：{1}" -f $proc.ExitCode, $fs.Path) `
        '重装：winget install --id FluidSynth.FluidSynth -e'
    }
  }
} catch {
  Add-Check 'fluidsynth' 'FluidSynth' 'fail' ("检测异常：" + $_.Exception.Message) '重装 FluidSynth'
}

# ---- 4. SoundFont --------------------------------------------------------
try {
  $fontCands = @()
  if ($SoundFont) { $fontCands += $SoundFont }
  if ($env:MUSIC_SOUNDFONT) { $fontCands += $env:MUSIC_SOUNDFONT }
  $fontCands += @(
    (Join-Path $HOME 'soundfonts\FluidR3_GM.sf2'),
    (Join-Path $HOME '.local\share\dsh-music\FluidR3_GM.sf2'),
    (Join-Path $PSScriptRoot '..\engine\assets\soundfonts\FluidR3_GM.sf2'),
    'C:\usr\share\sounds\sf2\FluidR3_GM.sf2',
    'C:\Program Files\fluidsynth\soundfonts\FluidR3_GM.sf2'
  )
  $font = $fontCands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if (-not $font) {
    Add-Check 'soundfont' 'SoundFont（FluidR3_GM）' 'fail' '未找到 FluidR3_GM.sf2' `
      'New-Item -ItemType Directory -Force "$env:USERPROFILE\soundfonts"; Copy-Item "D:\你的\FluidR3_GM.sf2" "$env:USERPROFILE\soundfonts\FluidR3_GM.sf2"'
  } else {
    $sizeMB = [math]::Round((Get-Item $font).Length / 1MB, 1)
    if ($sizeMB -lt 1) {
      Add-Check 'soundfont' 'SoundFont（FluidR3_GM）' 'fail' ("文件过小，可能损坏：{0}（{1} MB）" -f $font, $sizeMB) `
        '重新下载 FluidR3_GM.sf2 覆盖该文件'
    } else {
      Add-Check 'soundfont' 'SoundFont（FluidR3_GM）' 'pass' ("{0}（{1} MB）" -f $font, $sizeMB) ''
    }
  }

  $melCands = @()
  if ($env:MUSIC_SOUNDFONT_MELODIC) { $melCands += $env:MUSIC_SOUNDFONT_MELODIC }
  $melCands += @(
    (Join-Path $PSScriptRoot '..\engine\assets\soundfonts\GeneralUser.sf2'),
    (Join-Path $HOME '.local\share\dsh-music\GeneralUser.sf2'),
    (Join-Path $HOME 'soundfonts\GeneralUser.sf2')
  )
  $mel = $melCands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if ($mel) {
    Add-Check 'soundfont-melodic' 'SoundFont（GeneralUser，旋律）' 'pass' $mel ''
  } else {
    Add-Check 'soundfont-melodic' 'SoundFont（GeneralUser，旋律）' 'warn' `
      '未找到 GeneralUser.sf2；渲染会回退到 FluidR3，音色会差一些' `
      '把 GeneralUser.sf2 放到 %USERPROFILE%\soundfonts\（可选）'
  }
} catch {
  Add-Check 'soundfont' 'SoundFont' 'fail' ("检测异常：" + $_.Exception.Message) '检查 SoundFont 路径与权限'
}

# ---- 5. Python venv ------------------------------------------------------
$py = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
  Add-Check 'venv' 'Python venv' 'fail' ("找不到 {0}" -f $py) `
    ('powershell -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'bootstrap.ps1'))
} else {
  try {
    $pv = (& $py -c "import sys;print(sys.version.split()[0])" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $pv) {
      Add-Check 'venv' 'Python venv' 'pass' ("Python {0}  {1}" -f $pv, $py) ''
    } else {
      Add-Check 'venv' 'Python venv' 'fail' ("venv 里的 python 无法运行：{0}" -f $py) `
        ('删除后重跑 bootstrap：Remove-Item -Recurse -Force "{0}"; powershell -ExecutionPolicy Bypass -File "{1}"' -f $Venv, (Join-Path $PSScriptRoot 'bootstrap.ps1'))
    }
  } catch {
    Add-Check 'venv' 'Python venv' 'fail' ("检测异常：" + $_.Exception.Message) '重跑 bootstrap.ps1'
  }
}

# ---- 6. 关键 Python 模块 -------------------------------------------------
if (Test-Path $py) {
  try {
    $probe = @'
import importlib
mods = ['numpy', 'scipy', 'soundfile', 'librosa', 'pedalboard',
        'pyloudnorm', 'pretty_midi', 'mido', 'pypinyin']
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        missing.append(m + '(' + type(e).__name__ + ')')
print('OK' if not missing else 'MISSING:' + ','.join(missing))
'@
    $res = (& $py -c $probe 2>&1 | Out-String).Trim()
    if ($res -eq 'OK') {
      Add-Check 'py-modules' '关键 Python 模块' 'pass' 'numpy/scipy/soundfile/librosa/pedalboard/pyloudnorm/pretty_midi/mido/pypinyin 均可导入' ''
    } else {
      $miss = $res -replace '^MISSING:', ''
      Add-Check 'py-modules' '关键 Python 模块' 'fail' ("缺少或损坏：{0}" -f $miss) `
        ('"{0}" -m pip install numpy scipy soundfile librosa pedalboard pyloudnorm pretty_midi mido pypinyin' -f $py)
    }

    $optional = @{
      'pyworld' = 'music_tune 调教'
      'demucs'  = 'music_remix 的 Demucs 分离'
      'torch'   = 'Demucs 推理后端'
    }
    $missingOpt = @()
    foreach ($m in $optional.Keys) {
      & $py -c "import $m" 2>$null
      if ($LASTEXITCODE -ne 0) { $missingOpt += $m }
    }
    if ($missingOpt.Count -eq 0) {
      Add-Check 'py-optional' '可选 Python 模块' 'pass' 'pyworld / demucs / torch 均已安装' ''
    } else {
      $tips = @()
      foreach ($m in $missingOpt) { $tips += ("{0}（{1}）" -f $m, $optional[$m]) }
      Add-Check 'py-optional' '可选 Python 模块' 'warn' ("未安装：" + ($tips -join '、')) `
        ('按需安装，例如："' + $py + '" -m pip install pyworld')
    }
  } catch {
    Add-Check 'py-modules' '关键 Python 模块' 'fail' ("检测异常：" + $_.Exception.Message) '重跑 bootstrap.ps1'
  }
} else {
  Add-Check 'py-modules' '关键 Python 模块' 'skip' 'venv 不存在，跳过模块检查' '先修 venv'
}

# ---- 7. 肥鱼声音服务 9880 ------------------------------------------------
try {
  $uri = [System.Uri]$VoiceBaseUrl
  $port = $uri.Port
  if ($port -le 0) { $port = 9880 }
  $client = New-Object System.Net.Sockets.TcpClient
  $connected = $false
  try {
    $ar = $client.BeginConnect($uri.Host, $port, $null, $null)
    $connected = $ar.AsyncWaitHandle.WaitOne(1500, $false)
    if ($connected) { $client.EndConnect($ar) }
  } catch {
    $connected = $false
  } finally {
    $client.Close()
  }
  if (-not $connected) {
    Add-Check 'voice' '肥鱼声音服务（9880）' 'fail' ("{0}:{1} 无法连接" -f $uri.Host, $port) `
      ('powershell -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'setup_voice_service.ps1'))
  } else {
    $healthUrl = $VoiceBaseUrl.TrimEnd('/') + '/health'
    try {
      $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3 -ErrorAction Stop
      Add-Check 'voice' '肥鱼声音服务（9880）' 'pass' ("HTTP 可用：{0}" -f $healthUrl) ''
    } catch {
      Add-Check 'voice' '肥鱼声音服务（9880）' 'fail' ("端口已监听，但 {0} 不可用：{1}" -f $healthUrl, $_.Exception.Message) `
        '查看声音服务日志；确认服务已加载模型并完成启动'
    }
  }
} catch {
  Add-Check 'voice' '肥鱼声音服务（9880）' 'fail' ("检测异常：" + $_.Exception.Message) '按 README 第 2 节部署并启动服务'
}

# ---- 8. 工程目录可写 -----------------------------------------------------
try {
  if (-not (Test-Path $ProjectsRoot)) {
    New-Item -ItemType Directory -Force -Path $ProjectsRoot | Out-Null
  }
  if (-not (Test-Path $ProjectsRoot -PathType Container)) {
    Add-Check 'projects-root' '工程目录可写' 'fail' ("不是目录：{0}" -f $ProjectsRoot) `
      ('换一个目录：New-Item -ItemType Directory -Force "D:\Music\dsh-music"，并把配置里 projectsRoot 指过去')
  } else {
    $probeFile = Join-Path $ProjectsRoot ('.doctor-write-' + $PID + '.tmp')
    try {
      Set-Content -Path $probeFile -Value 'ok' -Encoding ASCII -ErrorAction Stop
      Remove-Item $probeFile -Force -ErrorAction Stop
      Add-Check 'projects-root' '工程目录可写' 'pass' $ProjectsRoot ''
    } catch {
      Add-Check 'projects-root' '工程目录可写' 'fail' ("写入失败：{0}" -f $ProjectsRoot) `
        ('检查目录权限，或换到可写目录；测试：New-Item -ItemType File "{0}\write-test.txt"' -f $ProjectsRoot)
    }
  }
} catch {
  Add-Check 'projects-root' '工程目录可写' 'fail' ("检测异常：" + $_.Exception.Message) `
    ('手动创建：New-Item -ItemType Directory -Force "{0}"' -f $ProjectsRoot)
}

# ---- 9. 磁盘空间 ---------------------------------------------------------
try {
  $qualifier = [System.IO.Path]::GetPathRoot($ProjectsRoot)
  $letter = $qualifier.Substring(0, 1)
  $drive = Get-PSDrive -Name $letter -ErrorAction Stop
  $freeGB = [math]::Round($drive.Free / 1GB, 1)
  if ($freeGB -ge $MinDiskGB) {
    Add-Check 'disk' '磁盘空间' 'pass' ("{0}: 剩余 {1} GB（要求 ≥ {2} GB）" -f $letter, $freeGB, $MinDiskGB) ''
  } else {
    Add-Check 'disk' '磁盘空间' 'fail' ("{0}: 剩余 {1} GB，低于 {2} GB；长工程和 Demucs 模型会占空间" -f $letter, $freeGB, $MinDiskGB) `
      'cleanmgr  /  删除旧工程的 audio/render-v1 与 audio/master'
  }
} catch {
  Add-Check 'disk' '磁盘空间' 'fail' ("检测异常：" + $_.Exception.Message) 'Get-PSDrive 查看剩余空间'
}

# ---- 汇总 ---------------------------------------------------------------
$passed = @($script:checks | Where-Object { $_.status -eq 'pass' }).Count
$failed = @($script:checks | Where-Object { $_.status -eq 'fail' }).Count
$warned = @($script:checks | Where-Object { $_.status -eq 'warn' }).Count
$skipped = @($script:checks | Where-Object { $_.status -eq 'skip' }).Count
$ok = ($failed -eq 0)

if ($Json) {
  [pscustomobject]@{
    ok = $ok
    checks = @($script:checks)
    summary = [pscustomobject]@{
      passed = $passed
      failed = $failed
      warned = $warned
      skipped = $skipped
    }
  } | ConvertTo-Json -Depth 8
  if ($ok) { exit 0 } else { exit 1 }
}

Write-Host ''
Write-Host 'DSH Music 依赖体检' -ForegroundColor Cyan
Write-Host '==================' -ForegroundColor Cyan
foreach ($c in $script:checks) { Write-CheckLine $c }
Write-Host ''
Write-Host ("通过 {0} 项，未通过 {1} 项，提醒 {2} 项，跳过 {3} 项。" -f $passed, $failed, $warned, $skipped) -ForegroundColor Cyan
if ($ok) {
  Write-Host '结论：✅ 必需项全部通过。' -ForegroundColor Green
  exit 0
} else {
  Write-Host '结论：❌ 有未通过项，按上面的修复命令处理后重跑本脚本。' -ForegroundColor Red
  exit 1
}
