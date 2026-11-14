param(
  [string]$Profile = '',
  [string]$Tgz = '',
  [switch]$CheckOnly,
  [switch]$Yes,
  [string]$ProfilesRoot = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $ProfilesRoot) { $ProfilesRoot = Join-Path $HOME '.dsh\profiles' }
$ProfilesRoot = [System.IO.Path]::GetFullPath($ProfilesRoot)
$PackageRoot = $PSScriptRoot
$Version = '1.0.2'
try {
  $pkg = Get-Content -Raw -Encoding UTF8 (Join-Path $PackageRoot 'package.json') | ConvertFrom-Json
  if ($pkg.version) { $Version = $pkg.version }
} catch {}

function Write-Head([string]$Text) {
  Write-Host ''
  Write-Host $Text -ForegroundColor Cyan
  Write-Host ('=' * [Math]::Min(60, [Math]::Max(10, $Text.Length))) -ForegroundColor Cyan
}

function Write-Step([string]$Text) {
  Write-Host ''
  Write-Host ('[{0}] {1}' -f $script:StepNo, $Text) -ForegroundColor Cyan
  $script:StepNo++
}

function Write-Ok([string]$Text) { Write-Host ('  ✅ ' + $Text) -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host ('  ⚠ ' + $Text) -ForegroundColor Yellow }
function Write-Err([string]$Text) { Write-Host ('  ❌ ' + $Text) -ForegroundColor Red }

function Fail([string]$Text) {
  Write-Err $Text
  Write-Host ''
  Write-Host '安装未完成。上面的错误就是原因；修好后重跑本脚本。' -ForegroundColor Red
  exit 1
}

function Get-ToolPath([string]$Name) {
  $c = Get-Command $Name -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @(
      (Join-Path $HOME ".local\bin\$Name.exe"),
      (Join-Path $HOME ".local\bin\$Name\$Name.exe"))) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

function Update-PathFromRegistry {
  try {
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($machine + ';' + $user)
  } catch {}
}

# ---- 发现 profile --------------------------------------------------------
$script:StepNo = 1
Write-Head ("DSH Music {0} 安装程序" -f $Version)
if ($CheckOnly) {
  Write-Host '模式：-CheckOnly（只检测并打印计划，不会安装、不会修改任何文件）' -ForegroundColor Yellow
}

if (-not (Test-Path $ProfilesRoot -PathType Container)) {
  Fail ("找不到 DSH profile 目录：{0}`n先启动一次 DSH，或确认 DSH 已正确安装。" -f $ProfilesRoot)
}

$profiles = @()
foreach ($d in (Get-ChildItem -Path $ProfilesRoot -Directory | Sort-Object Name)) {
  $pkgFile = Join-Path $d.FullName 'package.json'
  $valid = $false
  $bundles = @()
  $musicDep = ''
  $profileTitle = ''
  if (Test-Path $pkgFile) {
    try {
      $j = Get-Content -Raw -Encoding UTF8 $pkgFile | ConvertFrom-Json
      $valid = $true
      $profileTitle = [string]$j.name
      if ($j.dsh -and $j.dsh.profile -and $j.dsh.profile.bundles) {
        $bundles = @($j.dsh.profile.bundles)
      }
      if ($j.dependencies -and $j.dependencies.'dsh-music') {
        $musicDep = [string]$j.dependencies.'dsh-music'
      } elseif ($j.devDependencies -and $j.devDependencies.'dsh-music') {
        $musicDep = [string]$j.devDependencies.'dsh-music'
      }
    } catch {
      $valid = $false
    }
  }
  $profiles += [pscustomobject]@{
    Name = $d.Name
    Path = $d.FullName
    PackageJson = $pkgFile
    Valid = $valid
    Title = $profileTitle
    Bundles = $bundles
    HasMusic = ($bundles -contains 'dsh-music')
    MusicDep = $musicDep
  }
}

if ($profiles.Count -eq 0) {
  Fail ("{0} 下没有任何 profile 目录。先启动 DSH，让它在 profiles 下建一个 profile。" -f $ProfilesRoot)
}

Write-Host ''
Write-Host '检测到的 profile：' -ForegroundColor Cyan
Write-Host ('  {0,-18} {1,-10} {2,-12} {3}' -f '名称', '状态', 'dsh-music', '依赖') -ForegroundColor DarkGray
foreach ($p in $profiles) {
  $state = if ($p.Valid) { '可安装' } else { '未初始化' }
  $music = if ($p.HasMusic) { '已在 bundles' } else { '未加入' }
  $dep = if ($p.MusicDep) { $p.MusicDep } else { '无' }
  $color = if ($p.Valid) { 'Gray' } else { 'DarkGray' }
  Write-Host ('  {0,-18} {1,-10} {2,-12} {3}' -f $p.Name, $state, $music, $dep) -ForegroundColor $color
}

$validProfiles = @($profiles | Where-Object { $_.Valid })
if ($validProfiles.Count -eq 0) {
  Fail '所有 profile 都没有可读的 package.json；先启动一次 DSH，再重跑。'
}

$selected = $null
if ($Profile) {
  $wanted = $Profile
  if (Test-Path $Profile -PathType Container) { $wanted = Split-Path $Profile -Leaf }
  $selected = $profiles | Where-Object { $_.Name -eq $wanted } | Select-Object -First 1
  if (-not $selected) {
    Write-Err ("没有这个 profile：{0}" -f $Profile)
    Write-Host ('可用：' + (($profiles | ForEach-Object { $_.Name }) -join ', ')) -ForegroundColor Yellow
    exit 2
  }
  if (-not $selected.Valid) {
    Write-Err ("profile {0} 没有可读的 package.json，不能安装。" -f $selected.Name)
    exit 2
  }
} elseif ($validProfiles.Count -eq 1) {
  $selected = $validProfiles[0]
  Write-Host ''
  Write-Host ("只有一个可用 profile，自动选择：{0}" -f $selected.Name) -ForegroundColor Green
} elseif ($CheckOnly) {
  $selected = $validProfiles | Where-Object { $_.Name -eq 'desktop' } | Select-Object -First 1
  if (-not $selected) { $selected = $validProfiles[0] }
  Write-Host ''
  Write-Host ("检测到多个 profile；-CheckOnly 不交互，下面以 {0} 为例生成计划。" -f $selected.Name) -ForegroundColor Yellow
  Write-Host '正式安装时会列出编号并等待你输入；也可以用 -Profile <名字> 直接指定。' -ForegroundColor DarkGray
} else {
  Write-Host ''
  Write-Host '检测到多个 profile，请选择要安装到哪一个：' -ForegroundColor Yellow
  for ($i = 0; $i -lt $validProfiles.Count; $i++) {
    Write-Host ('  [{0}] {1}' -f ($i + 1), $validProfiles[$i].Name)
  }
  while ($true) {
    $inputText = Read-Host ('输入编号 1-{0}' -f $validProfiles.Count)
    $n = 0
    if ([int]::TryParse($inputText, [ref]$n) -and $n -ge 1 -and $n -le $validProfiles.Count) {
      $selected = $validProfiles[$n - 1]
      break
    }
    Write-Host '编号无效，请重输。' -ForegroundColor Red
  }
}

# ---- 找 tgz --------------------------------------------------------------
$tgzPath = $null
if ($Tgz) {
  if (-not (Test-Path $Tgz -PathType Leaf)) {
    Fail ("指定的 tgz 不存在：{0}" -f $Tgz)
  }
  $tgzPath = (Resolve-Path $Tgz).Path
} else {
  $found = Get-ChildItem -Path $PackageRoot -File -Filter 'dsh-music-*.tgz' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'dsh-music-panel-*' } |
    Sort-Object Name -Descending
  if ($found -and $found.Count -gt 0) {
    $tgzPath = $found[0].FullName
  }
}

if (-not $tgzPath -and -not $CheckOnly) {
  Fail ("没有找到安装包（dsh-music-*.tgz）。`n把 tgz 放到本目录：{0}`n或用 -Tgz `"D:\path\dsh-music-{1}.tgz`" 指定。" -f $PackageRoot, $Version)
}

$tgzDisplay = if ($tgzPath) { $tgzPath } else { '<同目录下 dsh-music-*.tgz>' }

# 面板包（可选，但在就有）：和 dsh-music 一起装上，buyers 才有输入框上方那条
$panelTgz = $null
$panelDir = if ($tgzPath) { Split-Path $tgzPath -Parent } else { $PackageRoot }
$panelFound = Get-ChildItem -Path $panelDir -File -Filter 'dsh-music-panel-*.tgz' -ErrorAction SilentlyContinue |
  Sort-Object Name -Descending
if ($panelFound -and $panelFound.Count -gt 0) { $panelTgz = $panelFound[0].FullName }
$panelDisplay = if ($panelTgz) { $panelTgz } else { '（同目录没有面板包，跳过）' }

# 中文路径（用户名/解压目录）会被旧版 PowerShell 按 ANSI 传给 node，装完 dependencies
# 里会留下乱码路径。统一先复制到纯 ASCII 的暂存目录再装。
$stageDir = Join-Path $env:PUBLIC ('dsh-music-stage-' + $PID)
$stageMusic = $null
$stagePanel = $null
if ($tgzPath) {
  $stageMusic = Join-Path $stageDir (Split-Path $tgzPath -Leaf)
  $tgzSpec = 'file:' + ($stageMusic -replace '\\', '/')
} else {
  $tgzSpec = 'file:<tgz 绝对路径>'
}
if ($panelTgz) {
  $stagePanel = Join-Path $stageDir (Split-Path $panelTgz -Leaf)
  $panelSpec = 'file:' + ($stagePanel -replace '\\', '/')
} else {
  $panelSpec = $null
}

# ---- 工具检测 ------------------------------------------------------------
$nodePath = Get-ToolPath 'node'
$dshPath = Get-ToolPath 'dsh.cmd'
if (-not $dshPath) { $dshPath = Get-ToolPath 'dsh' }
$tarPath = Get-ToolPath 'tar'
$ffmpegPath = Get-ToolPath 'ffmpeg'
$fluidPath = Get-ToolPath 'fluidsynth'
$wingetPath = Get-ToolPath 'winget'

if (-not $nodePath) { Fail '找不到 node。先装 Node.js LTS：winget install --id OpenJS.NodeJS.LTS -e' }
if (-not $dshPath) { Fail '找不到 dsh 命令。确认 DSH 已安装，并且 dsh 在 PATH 里。' }
if ($CheckOnly -and -not $tarPath) {
  Write-Warn '找不到 tar.exe；正式安装解压 tgz 会失败（Windows 10 1803+ 自带 tar）。'
}

# ---- CheckOnly：打印计划 -------------------------------------------------
if ($CheckOnly) {
  Write-Head '安装前检查结果'

  Write-Host ('node      : {0}' -f (& $nodePath --version 2>$null)) -ForegroundColor Gray
  Write-Host ('dsh       : {0}' -f $dshPath) -ForegroundColor Gray
  if ($tarPath) { Write-Host ('tar       : {0}' -f $tarPath) -ForegroundColor Gray }
  else { Write-Host 'tar       : 未找到' -ForegroundColor Red }

  if ($ffmpegPath) { Write-Host ('ffmpeg    : {0}' -f $ffmpegPath) -ForegroundColor Gray }
  else { Write-Host 'ffmpeg    : 未找到' -ForegroundColor Yellow }
  if ($fluidPath) { Write-Host ('fluidsynth: {0}' -f $fluidPath) -ForegroundColor Gray }
  else { Write-Host 'fluidsynth: 未找到' -ForegroundColor Yellow }
  if ($tgzPath) { Write-Host ('安装包    : {0}' -f $tgzPath) -ForegroundColor Gray }
  else { Write-Host '安装包    : 未找到（验收模式允许；正式安装会报错）' -ForegroundColor Yellow }
  if ($panelTgz) { Write-Host ('面板包    : {0}' -f $panelTgz) -ForegroundColor Gray }
  else { Write-Host '面板包    : 未找到（只装插件，输入框上方不会出现音乐条）' -ForegroundColor Yellow }

  Write-Host ''
  Write-Host ("计划（profile: {0}）" -f $selected.Name) -ForegroundColor Cyan
  Write-Host ("  1. 安装插件：dsh plugin --profile {0} add `"{1}`"" -f $selected.Name, $tgzSpec)
  if ($panelSpec) {
    Write-Host ("     再装面板：dsh plugin --profile {0} add `"{1}`"" -f $selected.Name, $panelSpec)
  }
  Write-Host '     （tgz 先复制到 C:\Users\Public\dsh-music-stage-<pid>，避开中文路径传参乱码）' -ForegroundColor DarkGray
  if ($selected.MusicDep) {
    Write-Host ("     当前已安装：{0}；执行后会改为上面的 file: 依赖。" -f $selected.MusicDep) -ForegroundColor DarkGray
  } else {
    Write-Host '     当前未安装 dsh-music。' -ForegroundColor DarkGray
  }
  $bundlesNow = if ($selected.HasMusic) { '已包含' } else { '未包含，会补上' }
  Write-Host ("  2. 检查 profile package.json 的 dsh.profile.bundles：{0}" -f $bundlesNow)
  Write-Host '     用 node 内联脚本写入，保留原缩进，临时文件 + rename 原子替换，并留 .bak-install-<时间戳>。'
  Write-Host ("  3. 解压 {0} 到 %TEMP%\dsh-music-install-<pid>，" -f $tgzDisplay)
  Write-Host '     运行解压出来的 scripts\bootstrap.ps1（建 venv、pip 依赖、复制 skill）。'
  if ($ffmpegPath -and $fluidPath) {
    Write-Host '  4. ffmpeg / fluidsynth 都已找到，跳过系统依赖安装。'
  } else {
    Write-Host '  4. ffmpeg / fluidsynth 有缺失：给出 winget 命令并询问是否现在装（-Yes 直接装），装完复检。'
  }
  Write-Host '  5. 运行解压目录里的 scripts\doctor.ps1，打印逐项体检结果。'
  Write-Host '  6. 打印「重启 DSH → 选音乐模式 → 对 agent 说：跑 music_doctor」。'

  Write-Host ''
  if (-not $tgzPath) {
    Write-Warn ("没有 tgz：{0} 下没有 dsh-music-*.tgz。验收模式继续；正式安装前把 tgz 放进来。" -f $PackageRoot)
  }
  if (-not $ffmpegPath) { Write-Warn 'ffmpeg 未找到；正式安装会尝试 winget install --id Gyan.FFmpeg -e' }
  if (-not $fluidPath) { Write-Warn 'fluidsynth 未找到；正式安装会尝试 winget install --id FluidSynth.FluidSynth -e' }
  if (-not $wingetPath -and (-not $ffmpegPath -or -not $fluidPath)) {
    Write-Warn 'winget 也不可用：正式安装会给出下载地址，让你手动装。'
  }
  Write-Host ''
  Write-Host '检查完成：没有安装、没有修改任何文件。' -ForegroundColor Green
  exit 0
}

# ---- 正式安装 ------------------------------------------------------------
if (-not $tarPath) {
  Fail '找不到 tar.exe，无法解压 tgz。Windows 10 1803+ 自带 tar；或在「设置 → 应用 → 可选功能」里装 bsdtar。'
}

$script:StepNo = 1
Write-Step ("安装插件到 profile：{0}" -f $selected.Name)
if (-not (Test-Path $stageDir)) { New-Item -ItemType Directory -Force -Path $stageDir | Out-Null }
Copy-Item -LiteralPath $tgzPath -Destination $stageMusic -Force
if ($panelTgz) { Copy-Item -LiteralPath $panelTgz -Destination $stagePanel -Force }
$addArgs = @('plugin', '--profile', $selected.Name, 'add', $tgzSpec)
Write-Host ('  运行：dsh ' + ($addArgs -join ' ')) -ForegroundColor DarkGray
try {
  if ($dshPath -like '*.ps1') {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $dshPath @addArgs
  } else {
    & $dshPath @addArgs
  }
  if ($LASTEXITCODE -ne 0) {
    Fail ("步骤 1 失败：dsh plugin add 退出码 {0}。看上面的 pnpm/DSH 输出。" -f $LASTEXITCODE)
  }
  Write-Ok '插件已加入 profile。'

  if ($panelSpec) {
    $panelArgs = @('plugin', '--profile', $selected.Name, 'add', $panelSpec)
    Write-Host ('  面板：dsh ' + ($panelArgs -join ' ')) -ForegroundColor DarkGray
    if ($dshPath -like '*.ps1') {
      & powershell -NoProfile -ExecutionPolicy Bypass -File $dshPath @panelArgs
    } else {
      & $dshPath @panelArgs
    }
    if ($LASTEXITCODE -ne 0) {
      Write-Warn ("面板没装上（退出码 {0}）。插件本体可用，输入框上方不会有音乐条；重跑本脚本或手动装面板包即可。" -f $LASTEXITCODE)
    } else {
      Write-Ok '面板已加入 profile。'
    }
  }
} finally {
  if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir -ErrorAction SilentlyContinue }
}

Write-Step '确保 dsh.profile.bundles 含 dsh-music'
if (-not (Test-Path $selected.PackageJson)) {
  Fail ("步骤 2 失败：安装后仍找不到 {0}" -f $selected.PackageJson)
}
$bundlesJs = @'
const fs = require("fs");
const file = process.argv[2];
const raw = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
const obj = JSON.parse(raw);
if (obj.dsh === undefined) obj.dsh = {};
if (obj.dsh === null || typeof obj.dsh !== "object" || Array.isArray(obj.dsh)) {
  throw new Error("package.json 的 dsh 字段不是对象，拒绝改写");
}
if (obj.dsh.profile === undefined) obj.dsh.profile = {};
if (obj.dsh.profile === null || typeof obj.dsh.profile !== "object" || Array.isArray(obj.dsh.profile)) {
  throw new Error("package.json 的 dsh.profile 字段不是对象，拒绝改写");
}
let bundles = obj.dsh.profile.bundles;
if (bundles === undefined || bundles === null) bundles = [];
if (!Array.isArray(bundles)) bundles = [bundles];
const names = process.argv.slice(3).filter(Boolean);
const missing = names.filter((n) => !bundles.includes(n));
if (missing.length) {
  for (const n of missing) bundles.push(n);
  obj.dsh.profile.bundles = bundles;
  const m = raw.match(/\n([ \t]+)"/);
  const indent = m ? m[1] : "  ";
  const trailing = raw.endsWith("\n") ? "\n" : "";
  const out = JSON.stringify(obj, null, indent) + trailing;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const bak = file + ".bak-install-" + stamp;
  fs.copyFileSync(file, bak);
  const tmp = file + ".tmp-" + process.pid;
  fs.writeFileSync(tmp, out, "utf8");
  fs.renameSync(tmp, file);
  console.log("已加入 " + missing.join(" / ") + "；备份：" + bak);
} else {
  console.log("bundles 已包含所需插件，未改动。");
}
'@
$tmpJs = Join-Path $env:TEMP ("dsh-music-bundles-" + $PID + ".cjs")
$bundleNames = @('dsh-music')
if ($panelSpec) { $bundleNames += 'dsh-music-panel' }
try {
  [System.IO.File]::WriteAllText($tmpJs, $bundlesJs, (New-Object System.Text.UTF8Encoding($false)))
  & $nodePath $tmpJs $selected.PackageJson @bundleNames
  if ($LASTEXITCODE -ne 0) { Fail ("步骤 2 失败：改写 bundles 退出码 {0}" -f $LASTEXITCODE) }
} finally {
  if (Test-Path $tmpJs) { Remove-Item $tmpJs -Force -ErrorAction SilentlyContinue }
}
Write-Ok 'bundles 已确认。'

Write-Step '解压 tgz 并运行 bootstrap.ps1（venv + pip + skill）'
$tmpDir = Join-Path $env:TEMP ("dsh-music-install-" + $PID)
if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir }
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
Write-Host ("  tar -xzf `"{0}`" -C `"{1}`"" -f $tgzPath, $tmpDir) -ForegroundColor DarkGray
& $tarPath -xzf $tgzPath -C $tmpDir
if ($LASTEXITCODE -ne 0) {
  Fail ("步骤 3 失败：解压 tgz 退出码 {0}。安装包可能损坏。" -f $LASTEXITCODE)
}
$boot = Get-ChildItem -Path $tmpDir -Recurse -File -Filter 'bootstrap.ps1' -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -match '\\scripts\\' } | Select-Object -First 1
if (-not $boot) {
  Fail ("步骤 3 失败：tgz 里没有 scripts\bootstrap.ps1。临时目录：{0}" -f $tmpDir)
}
Write-Host ('  运行：' + $boot.FullName) -ForegroundColor DarkGray
& powershell -NoProfile -ExecutionPolicy Bypass -File $boot.FullName
if ($LASTEXITCODE -ne 0) {
  Fail ("步骤 3 失败：bootstrap.ps1 退出码 {0}" -f $LASTEXITCODE)
}
Write-Ok 'Python 引擎依赖与 skill 已就绪。'

$script:StepNo = 4
Write-Step '检查 ffmpeg / fluidsynth'
Update-PathFromRegistry
$ffmpegPath = Get-ToolPath 'ffmpeg'
if (-not $ffmpegPath) {
  $wingetCmd = 'winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements'
  Write-Warn ("缺少 ffmpeg。安装命令：{0}" -f $wingetCmd)
  $doInstall = $false
  if ($Yes) {
    $doInstall = $true
  } else {
    $ans = Read-Host '现在安装 ffmpeg？[Y/n]'
    if ($ans -eq '' -or $ans -match '^[Yy]') { $doInstall = $true }
  }
  if ($doInstall) {
    if (-not $wingetPath) {
      Write-Err 'winget 不可用，请手动安装 ffmpeg，然后重跑本脚本。'
      Fail '缺少 ffmpeg，安装中止。'
    }
    & $wingetPath install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    Update-PathFromRegistry
    $ffmpegPath = Get-ToolPath 'ffmpeg'
  }
}
if ($ffmpegPath) { Write-Ok ("ffmpeg：{0}" -f $ffmpegPath) }
else { Write-Warn 'ffmpeg 仍未安装；doctor.ps1 会继续报 ❌。' }

$fluidPath = Get-ToolPath 'fluidsynth'
if (-not $fluidPath) {
  $wingetCmd = 'winget install --id FluidSynth.FluidSynth -e --accept-source-agreements --accept-package-agreements'
  Write-Warn ("缺少 fluidsynth。安装命令：{0}" -f $wingetCmd)
  $doInstall = $false
  if ($Yes) {
    $doInstall = $true
  } else {
    $ans = Read-Host '现在安装 fluidsynth？[Y/n]'
    if ($ans -eq '' -or $ans -match '^[Yy]') { $doInstall = $true }
  }
  if ($doInstall) {
    if (-not $wingetPath) {
      Write-Err 'winget 不可用，请手动安装 FluidSynth，然后重跑本脚本。'
      Fail '缺少 fluidsynth，安装中止。'
    }
    & $wingetPath install --id FluidSynth.FluidSynth -e --accept-source-agreements --accept-package-agreements
    Update-PathFromRegistry
    $fluidPath = Get-ToolPath 'fluidsynth'
  }
}
if ($fluidPath) { Write-Ok ("fluidsynth：{0}" -f $fluidPath) }
else { Write-Warn 'fluidsynth 仍未安装；doctor.ps1 会继续报 ❌。' }

$script:StepNo = 5
Write-Step '运行依赖体检 doctor.ps1'
$doctor = Join-Path (Split-Path (Split-Path $boot.FullName -Parent) -Parent) 'scripts\doctor.ps1'
if (-not (Test-Path $doctor)) {
  Fail ("步骤 5 失败：解压目录里没有 doctor.ps1：{0}" -f $doctor)
}
& powershell -NoProfile -ExecutionPolicy Bypass -File $doctor
$doctorExit = $LASTEXITCODE

Write-Head '安装结束'
if ($doctorExit -eq 0) {
  Write-Host '✅ 安装完成，体检全部必需项通过。' -ForegroundColor Green
  Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
} else {
  Write-Host '⚠ 安装步骤完成，但体检有未通过项（见上）。按修复命令处理后重跑 doctor.ps1。' -ForegroundColor Yellow
  Write-Host ("  临时目录保留：{0}" -f $tmpDir) -ForegroundColor DarkGray
}
Write-Host ''
Write-Host '下一步：' -ForegroundColor Cyan
Write-Host '  1. 完全退出 DSH，再重新启动（不是只关窗口）。'
Write-Host '  2. 新建会话时选「音乐模式」。'
Write-Host '  3. 对 agent 说：跑 music_doctor'
Write-Host ''
if ($doctorExit -ne 0) { exit 1 }
exit 0
