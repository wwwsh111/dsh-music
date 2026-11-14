# dsh-music

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

DSH 音乐模式插件：**选曲风 → 写歌词 → 生成伴奏与人声 → 粗混 → 导出 DAW 工程包**。

本仓库是 **DSH Music 1.0.3 完整版源码**，MIT 许可，免费使用与再分发。
装在 DeepSeek Harness（DSH）里，全部计算在本机完成。

- 模型做制作人（曲风、结构、歌词、参数）；本地 Python 引擎做乐手与混音师。
- 人声接本地「肥鱼声音服务」（TTS / RVC / 按谱唱歌）：
  <https://github.com/Esmerubphire/astrbot_plugin_feiyu_voice>
- 默认离线；不接云端音乐 API。

## 依赖

| 组件 | 用途 |
| --- | --- |
| ffmpeg | 解码/编码/母带 |
| fluidsynth + SoundFont | MIDI → 音频 |
| Python 3.11/3.12 venv | 引擎运行时 |
| numpy/scipy/soundfile/librosa/pedalboard/pyloudnorm/pretty_midi/mido | 编曲/混音/分析 |
| pyworld | 调教用的 WORLD 声码器（F0/包络/非周期性重合成） |
| pypinyin | 歌词 → 带声调拼音（唱中文必需；服务不猜多音字） |
| 肥鱼声音服务（另装） | 唱歌、说话、换声 |

`music_doctor` 会逐项检查。

## 安装

### 一键安装（推荐，1.0+）

1. 用 `git clone` 拿到本仓库，或下载 zip **整个解压**到一个普通目录（不要在压缩包里双击）。
2. 双击 `一键安装.cmd`。脚本会自动找 DSH profile、安装 tgz、建 Python venv、装依赖、补 `dsh.profile.bundles`，最后跑一遍体检。
3. 完全退出 DSH 再打开，选「音乐模式」，对 agent 说：`跑 music_doctor`。

只想先看会装什么、不落盘：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -CheckOnly
```

装不上看 [`docs/安装指南-v1.0.md`](docs/安装指南-v1.0.md)；报错原文查 [`docs/错误手册.md`](docs/错误手册.md)；三个开箱工程在 [`examples/`](examples/README.md)。

### 手动安装（开发者 / 从源码）

#### 1. 引擎依赖

Linux/macOS：

```bash
bash scripts/bootstrap.sh
```

Windows（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

脚本会建 venv、装 Python 包、把 skill 复制到 `~/.dsh/skills/`。
系统包（ffmpeg/fluidsynth/SoundFont）需要包管理器安装；脚本会提示命令。

#### 2. 肥鱼声音服务（唱歌必需）

按仓库 `server/INSTALL_AGENT.md` 部署；Windows + WSL2 + CUDA 是官方验证路线。
模型（TTS 7.5 GB + RVC 90 MB）可以先用本插件的下载器拉到本地：

```bash
python scripts/download_voice_models.py --dest D:/feiyu/server/models
```

> SVS（按谱唱歌）的 SoulX-Singer 权重与对齐参考不在公开模型包里，需按仓库文档单独准备；
> 没装 SVS 时只能说话/换声，不能唱歌。

配置 `voiceApiKeyFile` 指向服务的 `.runtime/voice-service/api_key.txt`。

#### 3. 插件

```bash
cd /path/to/dsh-music
pnpm pack
```

然后在创造模式里：

```
plugin_manager.install_bundle { spec: "file:<绝对路径>/dsh-music-1.0.0.tgz", enabled: true }
```

没有 `plugin_manager` 时：把包加入 profile 的 `package.json` 依赖与 `dsh.profile.bundles`，
再 `pnpm install` 并重启 DSH。

#### 4. 音乐模式

bundle patch 里已包含 `preset-music`（显示名「音乐模式」）。重连后在模式选择器里选择。
skill 会被复制到 `~/.dsh/skills/music-production/`。

## 配置

```yaml
- id: dsh-music
  name: dsh-music
  config:
    enginePort: 17890   # 被别的程序占用时改成别的空闲端口，否则引擎起不来
    projectsRoot: "D:/Music/dsh-music"
    venv: "C:/Users/you/.local/share/dsh-music/venv"
    soundfont: "C:/soundfonts/FluidR3_GM.sf2"
    voiceBaseUrl: "http://127.0.0.1:9880"
    voiceApiKeyFile: "C:/feiyu/server/.runtime/voice-service/api_key.txt"
    defaultGenre: citypop
    quality: standard
    stems: auto
    daw: generic
    codeMode: native
    codeTimeoutMs: 120000
```

不写 `projectsRoot` 时：有 `/sdcard` 用 `/sdcard/dsh/music`，否则 `~/Music/dsh-music`。

## Code Mode（0.15.0，music_code）

28 个音乐工具一次性塞进上下文，光工具说明就要几千 token，模型还容易在
「先建工程还是先写歌词」这种顺序上反复试。0.15.0 加了 `music_code`：
写一段 JavaScript，在程序里串多个工具，中间结果不进上下文，只把结论带回来。

```js
const p = await music.project({ op: 'create', name: 'demo', genre: 'citypop', bpm: 110, bars: 32 })
await music.lyrics({ project: 'demo', text: '...' })
await music.compose({ project: 'demo' })
const r = await music.render({ project: 'demo' })
return { created: p.name, wav: r.wav }
```

调用方式：

- `music.tools()` / `music.tools('混音')` —— 目录与搜索，按需再 `music.describe('music_mix')` 看参数；
- `music.<短名>(参数)`，短名就是去掉 `music_` 前缀（`music.compose`、`music.render`）；
- `music.sleep(1000)` 等任务；长任务 `{wait:false}` 拿 jobId，再 `music.job({ jobId })` 轮询；
- 顶层支持 `await`，用 `return` 交回结果；只返回需要判断的字段。

输出带步骤轨迹，出错能落在具体一步：

```
✔ music_code · 3 步 · 12.4s
1. project(op:"create", name:"demo") ✓ 0.1s
2. compose(project:"demo") ✓ 2.3s
3. render(project:"demo") ✓ 10.0s
—— 结果 ——
{"wav":"..."}
```

三种模式（`codeMode`）：

| 值 | 注册的工具 | 适合 |
| --- | --- | --- |
| `native`（默认） | 全部普通工具 + music_code | 兼容现有工作流，模型可自己选 |
| `compact` | 只 music_doctor + music_code | 上下文紧、模型已会用 Code Mode |
| `off` | 只有普通工具 | 不想要写代码这一步 |

沙箱：vm 上下文里只有数据类内建对象与 music SDK，没有 `process` / `require` / `fs`；
同步死循环 15 秒拦下，整段程序默认 120 秒、上限 600 秒（`codeTimeoutMs` 可改默认值）。
vm 隔离的是程序结构，不是恶意代码，不要拿它跑不可信来源的脚本。

## 工具

| 工具 | 作用 |
| --- | --- |
| music_doctor | 引擎/声库/磁盘体检 |
| music_code | Code Mode：一段 JS 串联多个工具，中间结果不进上下文 |
| music_voicebanks | 声库识别：扫目录，认出磁盘上有什么声库/换声模型、什么格式、能不能用 |
| music_genres | 曲风菜单/详情 |
| music_theory | 乐理速查（和弦进行/配和弦/色彩/节奏/真实感/曲式/旋律/歌词/混音/曲风速查）；创作前先读 |
| music_project | 建/开/列工程 |
| music_lyrics | 歌词写入与校验 |
| music_compose | 伴奏 + 人声旋律 MIDI |
| music_render | MIDI → WAV |
| music_notes | 钢琴卷帘：按小节+拍改音符（get/add/set/remove/move/transpose/velocity/duration/quantize/humanize） |
| music_patterns | Pattern/Playlist：抓乐句 → 摆到 startBar → 展开成整曲 MIDI |
| music_sampler | 通道机架：注册自有采样当乐器（pitch）或鼓/音效（oneshot）；也能直接吃 `libraryId` |
| music_library | 音效库：扫描/拼音搜索/过滤语法/星标标签/波形/重复检查/批量导出/送进工程（移植自 SoundVault） |
| music_analyze | BPM/调性/段落/响度；`deep:true` 接 dsh-soundcheck 深分析（LUFS/真峰/结构/分轨/人声） |
| music_listen | 听系统**正在放**的声音：BPM/调性/响度，`grab:true` 留成 wav 当参考曲或采样（不挑播放器） |
| music_vocals | 唱歌（`part: lead` 主唱 / `harmony` 和声轨）/ 念白 / `mode:"import"` 导入外部干声 |
| music_harmony | 和声编排（分和声）：按段落/和弦给主旋律配和声，剪掉糊成一片的音；`show`/`clear` 管现有编排 |
| music_vocal_doctor | 人声体检：哑音/拖尾/重叠/缺口/走音；`fix` 直接出一份修好的干声，`suggest` 给只能重唱的谱面建议 |
| music_vocal_finish | S5 收尾：分和声 → 体检 → 修补 → 复扫 → 重唱建议（可选调教），写 `report/vocal-finish-*.json`，gate S5 读它 |
| music_tune | 调教：滑音/颤音/句尾/力度/气口 + WORLD 重合成 + 真实感混音链 |
| music_yutiao | 语调教：按汉语声调重写音高线，让唱歌的人声开口说话（时长不变；`dryRun` 先看声调表） |
| music_tunefile | 与洛天依的工程文件互通：导出 `.svp`/`.vsqx` 给她唱，或把改好的音符读回来 |
| music_fx | 每轨效果链（preset 一键套 / set 自写 EQ·压缩·混响·延迟） |
| music_bus | 混音台路由：分组总线、send、按角色自动套链（干这步之前先听） |
| music_automation | 自动化曲线（gain/pan/lowpass/highpass，按小节定位，master 是总线淡出） |
| music_mix | 自动混音（`reference` 可传参考曲，按频段/响度匹配） |
| music_master | 母带 |
| music_remix | 自动 Remix：分离 → 变速/变调 → 换伴奏 → 混音 → 母带 |
| music_design | 作品方案页：把工程做成歸藏设计风格的单文件 HTML（曲目档案/调教/混音/结构/发布计划） |
| music_export | DAW 工程包（Audacity 标签 / Reaper .rpp） |
| music_preview | 波形/段落/试听 |
| music_job | 长任务查询 |

## 系统音频律动（0.14.0，接 dsh-bgm 的路子）

灵感与做法来自 [skymecode/dsh-bgm](https://github.com/skymecode/dsh-bgm)（MIT）：
**不挑播放器**——采的是默认输出设备的回环，QQ 音乐 / 网易云 / 浏览器 / 游戏同一条链路，
不用给每个播放器写适配，也不需要「内录权限」。

- 采集：Windows 用 WASAPI Loopback（`PyAudioWPatch`），macOS 走 BlackHole，Linux 走 PulseAudio
  的 monitor 设备。PCM 只在本机内存里过一遍，**对外只有标量**（响度 / 低中高三段 / 起音 /
  BPM / 拍内相位 / 12 段频谱），不落盘、不上传；SSE 只绑 `127.0.0.1`。
- 面板：输入框上方的「系统输出」一排频谱柱跟着正在放的歌起伏，旁边显示测到的 BPM；
  **点 BPM 就能把它写进当前工程**（跟着 Spotify 里的歌起一首同速的曲子）。
  只改 `transform`/`opacity`，固定 12 个 DOM 节点，不重建；窄屏收起成 5 根小柱；
  `prefers-reduced-motion` 时降到 4 Hz 更新且不做插值；静默时 `playing:false`，柱子自己落回去。
- 工具：`music_listen` 抓一段（默认 8 秒）做 BPM/调性/响度分析，`grab:true` 留成 wav，
  可直接喂给 `music_mix reference`、`music_sampler` 或 `music_remix input`：

```bash
music_listen { seconds: 10, grab: true }        # 我刚听到的那首
music_mix    { project: "反乌托邦", reference: "<上一步 grabbed 的 wav>" }
```

- 和 dsh-bgm 的差别：dsh-bgm 服务会话界面（波面 / 判定 / Combo / 结算卡），
  这里服务制作——同样只传标量、同样静默即安静，但不做音游判定，也不往正文里加动效。
- 接口：`POST /v1/live/probe`（后端/设备自检）、`POST /v1/live/snapshot`（抓一段 + 分析），
  `GET /v1/panel/live/stream?fps=30`（SSE，落 `/v1/panel/` 前缀所以浏览器免 token，
  但仍要求回环 Origin）。`music_doctor` 里也会带一行 `live`。

## 音效库（0.14.0）

把硬盘上散落的音效、拟音、采样做成一个能秒搜、能试听、能直接进工程的库。
思路移植自 [xy040402/SoundVault（音匣）](https://github.com/xy040402/SoundVault)：
**不搬文件、不改文件名、不删素材**，只记路径和元数据。

- 索引在 `~/.dsh/music/library/index.db`（SQLite；删了下次扫描自动重建），
  库目录清单与导出设置在同目录 `store.json`；`MUSIC_LIBRARY` 可以把索引放到素材盘。
- 加库 → 扫描 → 搜索 → 用：

```bash
music_library op=roots action=add path="D:\SFX"
music_library op=scan              # 增量；有时间预算，没扫完再调一次
music_library op=search q="枪声 #fx star:>=4 dur:<3s"
music_library op=use project="<工程>" id="<12 位 id>"
```

搜索语法（和音匣一致）：

| 写法 | 含义 |
| --- | --- |
| `枪声` / `qs` / `qiangsheng` | 名字/路径，或拼音全拼/首字母 |
| `#拟音` | 带这个标签 |
| `star:>=4` / `star:>4` | 评分 ≥4 / >4（严格） |
| `dur:<2s` / `dur:>0.5s` | 时长小于 / 大于 |
| `sr:>48k` / `sr:44100` | 采样率 |
| `ch:2` / `type:wav` | 声道数 / 扩展名 |

多个词是「并且」。`favorites:true` 看收藏，`collection:"名字"` 看集合。

- 整理：`update` 打星/收藏/标签/集合；`duplicates` 找重复（默认按 大小+采样率+声道+时长，
  `deep:true` 再对首尾各 256KB 做哈希）；`export` 批量复制（`{n:00}_{name}` 模板、保留目录、
  附 CSV 清单，**只复制不移动**）。
- 交付：`use` 把素材挂成工程里的采样器轨（`oneshot` 播整段 / `pitch` 当乐器），
  之后 `music_notes` 写音符即可；`copy:true` 会把音频复制进工程 `samples/`。
- 面板：展开输入框上方的「音乐」面板，底部「音效库」一栏可以搜、试听、打星、贴标签、
  `＋目录`、`扫描`、`→ 工程`。
- 和音匣的差别：不做 WPF 界面、不做拖拽到 DAW（插件不是桌面窗口）、不做 LUFS 归一化试听；
  换来的是能直接进编曲/渲染链，并且能被模型调用。
- 自测：`<venv>/python tools/test_library.py`（27 项断言，覆盖扫描/拼音/过滤/星标标签/波形/
  重复/导出/use；用临时目录作素材与库，跑完自清理）。

## 标准流程、验收与空间（0.13.0）

- 顺序：`skills/music-production/PIPELINE.md`（S0 体检 → S1 方案 → S2 建工程 → S3 歌词 → S4 编曲 →
  S5 人声 → S6 混音 → S7 母带 → S8 交付；三个固定试听点卡在 S4/S6/S7 末尾）。
  技法与曲风包在 `skills/music-production/SKILL.md`。
- 验收：`tools/music_gate.py`（**离线**，不需要引擎）读 `project.json`/`midi/`/`report/*.json` 逐条判
  PASS/WARN/FAIL/`—`，`—` 不等于通过；`--stage S4,S7` 只查一段，`--all` 扫全部工程，`--json` 便于写报告。
  退出码 0 全过 / 1 有 FAIL。
- 空间：`tools/music_gc.py`（默认 dry-run）。真源（`project.json`/`midi/`/`lyrics/`/`source/`/`analysis/`/
  `report/`/`export/`/`design/`）永不自动删；音频可分三层回收。**先 dry-run 给数字，确认后才 `--apply`**
  （隔离到 `<root>\_trash\<时间戳>`，可 `--restore`）；隔离区与工程同盘时 `--apply` 不释放空间，
  要腾空间必须再 `--purge`。`report/*.json` 点名过的成品被钉住不删。

## 和声与演奏真实感（0.8.0）

这一版把「编曲」从模板循环改成有功能逻辑的生成，参考了 AI_MIDI 的 Library（Apache-2.0，正文重写）。

**和声**（`engine/music_engine/harmony.py`）

- 优先用曲风包自己的 `progressions`，按 `key.mode` 过滤：小调只用小调进行，没有就取内置小调表；
  大调过滤掉小调进行。`Imaj7` 在 Am 里会被拒绝（否则会弹成 A 大三，调性直接散掉）。
- 换和弦频率按 BPM：<62 每两小节；<90 主歌/intro 低能量时两小节；100–132 副歌高能量时半小节；
  >132 主歌低能量时两小节。不再是所有歌都一小节一换。
- 色彩：按曲风 `flavors` 给 V 加 7、I/IV 加 maj7、I 加 add9、小调三和弦加 m7；进行整体过 `colorize`。
- 功能：大调有 22% 概率做调式互换（IV→iv、vi→bVI、ii→bII）；8 小节以上段落有 38% 概率插副属
  （`V7/ii` 等，小调跳过 IV 目标，避免 `V7/iv` 变成 I7 的转调感）。
- 终止式四种（正格/变格/伪/半终止），按段落加权；末段 70% 正格、30% 变格。
- 4 小节短段落：verse/solo 旋转循环，让不同段落从 bVI/iv/bVII 起，且只保留结尾落在 I/V 的转位；
  intro 与末段仍走终止式。44 小节 vocalrock 实测各段起音已不同（i / bVI / bVII / iv）。

**声部连接**：`_cost` 在最近转位之外加了三项——顶声部移动、声部跨度、偏离中心，并给平行五/八度
每对 +2 的惩罚；共同音有 -1.2 的奖励。

**演奏层**（`engine/music_engine/performance.py`）

- 每轨一份档案 `(力度中心, 拱形幅度, 抖动 ms, 抢/拖 ms, 下限, 上限)`；目标力度 = 档案中心 +
  (段落 energy−0.6)×12 + 原力度偏差×0.28 + `sin(π·句内位置)^0.8` 拱形 + 重音表 + 长音 −2 +
  重复音递增 + 高斯噪声 N(0,2.2)，最后夹在上下限内。
- 微时值：每轨固定抢/拖（如 gtr_rhythm_l −4 ms、r 轨 +3 ms），再叠高斯抖动；8 分反拍按 swing 拖后。
- `gtr_lead`/`lead` 连奏补 5 ms；mono 轨（人声/贝斯/主音）按下一音起音削重叠留 12 ms（SVS 不接受重叠）。

**实测（vocalrock，F#m，155 BPM，44 小节，seed 12345；0.8.0 列为 ab-new2）**

| 指标 | 0.7.0 基线 | 0.8.0 |
| --- | --- | --- |
| 混音 LUFS / true peak | −15.43 / −2.91 | −15.9 / −3.03 |
| crest | 15.14 | 15.61 |
| 人声力度唯一值 | 9（100–108） | 15（95–109） |
| 贝斯力度唯一值 | 20 | 22 |
| 段落起音 | 每段都 i 起 | i / bVI / bVII / iv 起 |

和弦贴合（按 16 分吸附后）：keys 100%、pad 100%、bass 89%、vocal 74%——人声剩余部分是经过音，
不是错音。渲染后 stems 的 RMS 与基线几乎相同（渲染本身会归一化），差异主要体现在 MIDI 层与混音推子。

大调路径也做过端到端验证：citypop / C / 100 BPM / 24 小节，compose → render → mix 正常，
和弦为 `Imaj7–vi7–ii7–V7`（intro 里 IV 顶替 ii7），混音 −14.95 LUFS / TP −1.28 / crest 14.63，时长 57.6 s。

**乐理库**：`music_theory` 提供 11 个主题，内容重写自 AI_MIDI Library 的结构与要点（Apache-2.0），
不复制原文；它是给 agent 在 `music_compose` 之前读的，不改变生成结果，除非 agent 据此改参数。

## Remix（自动重做）

```
music_remix { input: "song.mp3", mode: "remix", targetGenre: "house", bpm: 124, key: "Am" }
```

| 模式 | 做什么 | 需要 |
| --- | --- | --- |
| `remix` | 新伴奏（目标曲风）+ 原唱（对齐到新速度/调性） | 分离 |
| `cover` | 新伴奏 + 肥鱼 RVC 换声 | 声音服务 |
| `rearrange` | 原伴奏 + 新鼓/贝斯（整体重新混音） | 分离 |

- 不给 `bpm` / `key` 就沿用原曲，避免人声被拉坏；给了就整体变速变调（±6 半音内取最近）。
- 分离后端：`backend` 可选 `auto`/`demucs`/`center`。装了 `torch` + `demucs` 时 auto 用 Demucs；否则用「中心声道相干掩码 + HPSS + 带限」的近似方案。
  近似方案会把中置的鼓/贝斯残留进人声轨，报告 `separation.warnings` 会写明。
- 输出：`audio/master/master-v1.{wav,mp3}`、`report/remix.json`（含 BPM/调性/分离/对齐/警告）。
- 版权：第三方歌曲 Remix/翻唱需自行确认授权。

PC（或愿意慢一点的本机）装 Demucs：

```bash
pip install "demucs==4.0.1"
```

装好后 `music_doctor` 的 `modules` 里能看到 `demucs`，`music_remix` 的 auto 会自动优先用它；手机 CPU 推理很慢，赶时间可显式 `backend:"center"`。

## 视频（可选）

`scripts/make_video.py` 把工程的母带做成 1080p MP4（渐变底 + 波形 + 标题/BPM/调性/LUFS），
方便直接发相册/社交平台：

```bash
python scripts/make_video.py <工程目录> out.mp4 --title "歌名"
```

需要 ffmpeg 与 DejaVu 字体；手机上没有 PIL 也能跑。

## 工程目录

```
projects/<id>/
├── project.json
├── source/ analysis/ midi/ lyrics/
├── audio/{render-v1,mix,master,vocal}/
├── preview/ report/ logs/ export/
```

## 虚拟歌手摇滚（vocalrock）

给「星尘 / 诗岸」那一类中文虚拟歌手摇滚用的曲风包与前处理链。它比通用 `rock` 多做四件事：

```bash
music_project { op: "create", name: "反乌托邦", genre: "vocalrock", bpm: 155, key: "F#m" }
music_lyrics  { project: "反乌托邦", text: "[verse1]\n...\n[chorus1]\n..." }
music_compose { project: "反乌托邦" }        # 伴奏 + 主唱旋律 + 和声轨
music_render  { project: "反乌托邦" }
music_vocals  { project: "反乌托邦", part: "lead" }
music_vocals  { project: "反乌托邦", part: "harmony" }
music_mix     { project: "反乌托邦" }        # 可加 reference: "参考曲.flac"
music_master  { project: "反乌托邦" }
```

| 层 | 做了什么 | 为什么 |
| --- | --- | --- |
| 段落 | intro → verse → prechorus → chorus → verse → prechorus → chorus → solo → chorus → outro，母带目标 -9.5 LUFS | 这类歌靠段落落差立住，「一路到底」的编曲听 30 秒就腻；-8.5 太挤（crest 只剩 5.6），-9.5 保住瞬态 |
| 鼓 | 每首从 3 套鼓里挑一套（`kit.VARIANTS`），按段落换谱（主歌 8 分踩镲 / 副歌 16 分 / solo 半拍），**小节级变异**（乐句后半抽踩镲、加 ghost 军鼓、挪底鼓），乐句末过门、段落出口开镲 | 律动来自重音、留白和过门；每小节都一样才是「机械鼓」 |
| 节奏吉他 | 左右两轨叠录（起音错 4–12 ms）、每首一套演奏档案（`styles.py`：anthem / drive / half 三种原型），每小节必动一处（拆 16 分、抽拍、抢先、附点），最后一轮副歌在 drive 原型下用 16 分墙 | 双轨错开才有宽度；小节级变化是「听起来像人弹」和「像 loop」的分界 |
| 主音吉他 | 副歌长音 hook、预副歌末两小节 16 分上行推、solo 走调内五声音阶、尾奏收尾 | 只有 solo 有主音吉他时，副歌会显得空 |
| 人声旋律 | 先造**动机**（音级偏移 + 轮廓），再按段落做发展（`literal / transpose / sequence / invert / retrograde / ornament / stretch`），节奏词库每首现生成，句尾拖 1–2.5 拍，强拍吸附和弦音，副歌第二句起自动生成低三度和声 | 以前每句都是「换一套节奏再念一遍」；有了动机和发展，段落之间才是同一首歌的不同部分 |
| 音色 | GM 音色只当 DI：吉他 HPF → 中频塑形 → 电子管前级（非对称 tanh）→ 失真 → **真实箱体 IR 卷积**（AK-SPKRS 4x12，48 kHz/4096 taps）；鼓总线瞬时整形 + 并行压缩 + 饱和；贝斯低架 + 压缩 + 轻饱和；人声压缩 + 去齿音 + 存在感 | GM 的失真吉他已经「烧死」；合成 EQ 做不出纸盆滚降，实测换真 IR 后吉他频谱重心从 610 → 1201 Hz |
| 双音源 | 鼓用 FluidR3_GM（31 套鼓组），旋律乐器用 GeneralUser GS（吉他采样 22 → 71），找不到时静默回退 | 每个音源各有所长，一个 SF2 打不满 |
| 混音 | 每轨先归一化到目标 RMS、pan 吉他 ±0.55、底鼓触发 sidechain、并联 reverb/delay send（**每首歌一套房间**：房型/预延迟/返回量，延迟跟随拍速的附点八分）、母线压缩、按段落 energy 的缓变推子（主歌 −1.25、副歌 +2 dB）、混音先推到「目标响度 −5 dB」 | 先把电平统一再谈音色；混音太轻会让母带加上 8 dB 以上，软限幅把瞬态全削平 |
| 母带 | 自己写软限幅 + 迭代增益（最多 8 轮，|ΔLUFS| ≤ 0.2 收手），每轮复测 4 倍过采样真峰值，24-bit WAV + 320k MP3 | pedalboard 的 Limiter 实测不认 threshold，输出峰值钉在 0 dBFS；ffmpeg 的 alimiter 也不是真峰值限制器，都会骗指标 |

**人声成本**：肥鱼 SVS 在 CPU 上约 5–6 分钟 / 20 秒分段。168 秒的歌 ≈ 3 段 ≈ 17 分钟；改一版歌词再唱一遍前，
先用短工程（1 verse + 1 chorus）验证旋律。`music_vocals { wait: false }` 只提交并返回 `jobId`，
之后用 `music_vocals { project, jobId }` 续查，不用一直挂着调用。

**中文读音**：插件用 pypinyin 把歌词转成带声调拼音，并校验服务的读音词表；
词表里没有的读音（例如某些 `shui2`）会换成语表中最近的读音，个别字会被「读歪」。
多音字想唱对，就在工程里放 `lyrics/pinyin.txt`（空格分隔、`de5` 这种带调写法），插件优先用它。
服务不支持 ü，`lv` 会唱成 `lu`。

## 虚拟歌姬调教（music_tune）

SVS 唱出来的是「准音源」：音高、时值、字都对，但每个音一条直线，句与句之间没有气口，
听起来像在念谱。真实歌姬的辨识度来自谱面之外的东西——起音滑音、长音颤音、句尾下落、
一句里的力度拱形、换气声。这些只在混音里补（EQ/压缩/混响）是补不出来的：
混响只会把「平」的人声弄得更糊。所以顺序必须是**先调教、再混音**。

```
music_vocals { project: "反乌托邦", part: "lead" }
music_tune   { project: "反乌托邦", style: "natural", intensity: 0.7 }
music_mix    { project: "反乌托邦" }
```

| 阶段 | 做了什么 | 为什么 |
| --- | --- | --- |
| 读谱 | 优先 `analysis/vocal-align.json`（带字/句/拍），没有就退回 `midi/vocal.mid` | 调教要按「音」下手，不是按时间盲扫 |
| 分析 | WORLD 声码器分解 F0 / 频谱包络 / 非周期性（10 ms 帧） | 改 F0 不改时长，颤音上不会出现相位声码器的「水声」 |
| 音高 | 目标曲线 = 音高网格 + 起音下滑 + 延迟进入的颤音（5–6 Hz，15–55 音分）+ 句尾下落 + 每音微游移；相邻音 20–90 ms 对数滑行 | 真人的音准是「朝目标走」，不是钉在格子上；大跳要滑，级进要连 |
| 力度 | 句内正弦拱形 + 重拍顶一下 + 字头 40 ms 小顶，平滑 60 ms 后乘到音频 | 谱面只有音高，力度是「一句一口气」的来源 |
| 气口 | 换行处优先补气口（间隔 ≥ 0.08 s 就补）；缝太小就先给上一句做一次 80–150 ms 的提前收声，再在腾出的位置铺带通噪声；句内则要 ≥ 0.22 s 的真静音。长度随气口宽度，电平跟前面 0.3 s 能量走 | 真人换行是「先收再吸」，不是无缝硬切；固定音量会像抽气 |
| 真实感链 | 5–9 kHz 分频去齿音 → HPF 88 Hz → 250 Hz 厚度 / 5.2 kHz / 8.8 kHz 空气 → 2A 类慢起音压缩 → 轻饱和 → EMT140 式板式混响发送（预延迟 22 ms，返回 −17 dB）→ 11/19 ms 左右加倍 | 对应参考教程的顺序：EQ 提亮高频、2A 压缩、板式混响、左右合唱；每一步都只做一点，混响量永远比「听着刚好」再小一档 |

风格预设：`natural`（默认）/ `ballad`（颤音、气口更多，共振峰略厚）/ `power`（颤音少、压缩多）
/ `cute`（薄而亮）。`intensity` 0.2–1.4 缩放整体「演」的程度；`formant` 以半音为单位改共振峰；
`mix:false` 只出调教干声；`strength` 控制贴目标曲线的程度（默认 0.92，保留一点原唱的微小起伏）。

产物：

```
audio/vocal/vocal-tuned-dry-<tag>.wav   # 调教干声（单声道）
audio/vocal/vocal-tuned-<tag>.wav       # 调教 + 真实感链（立体声，−19 LUFS）
audio/vocal/vocal-ab-<tag>.wav          # 同段落 A/B：原声 8 s → 调教 8 s
analysis/tune-plan-<tag>.json           # 本次参数/气口/音准统计
report/tune-<tag>.json                  # 前后音准、气口数、频谱重心、响度
```

音准统计用「音符范围内有声帧相对目标曲线的音分偏差」：调教前是原始 SVS 的偏差，
调教后是重合成曲线的偏差。这个数字只说明「唱准了没有」，不说明好不好听；
好听与否仍要人耳在小节层面判断（试听点见 skill）。

SVS 输出普遍比谱面晚 50–150 ms（起唱延迟）。`music_vocals` 会在重采样后按谱面量一次整体偏移，
置信度 ≥ 2 且偏移在 15–400 ms 之间时直接挪回去，把实际挪动量写进 `report/vocal-<tag>-v1.json`
的 `alignedMs`。不修这一步，调教和混音都拿不到「卡在拍上」的基准。

实测（本机 CPU，torch 2.8）：`feiyu-style01` 118 s 人声，调教 26 s，25 句、24 个气口，
音准中位偏 27.0 → 2.1 音分（p90 136.9 → 10.3），频谱重心 1148 → 1337 Hz；
`tune-test` 16 s 人声 2 句 1 气口，音准 29.5 → 2.2 音分，混音阶段人声相对乐谱偏移
由 +114.8 ms 修到 −3.5 ms，`music_mix` 自动选中 `vocal-tuned-lead.wav` 而不再套人声链。

调教过的工程再跑 `music_mix` 时，混音会自动优先用 `vocal-tuned-<tag>.wav`，
并且**不再重复套人声链**（去齿音/压缩/提亮只做一次）；没有调教产物才退回原始 SVS。
A/B 拼贴（`vocal-ab-*`）和调教干声不会被当成主唱。

成本：2 分钟人声在 4 核 CPU 上约 25–40 s（WORLD 分析占七成），不需要 GPU。

## 人声后期：分和声、去哑音、补音（0.16.0 → 0.17.0）

这一节是按 B 站《进阶向 AI 翻唱教程》（手动分和声 / 去哑音 / 补音 / 改调）里的做法排的，
把「翻唱后期那些手工活」做成了两个工具。教程本身用的是 RVC + Audition 的手工流程，
这里换成可复现的检查 + 可回滚的修补。

**分和声（`music_harmony`）**

- 以前和声是写死的：副歌第 2 句起、低三度、超出 2–6 半音就丢。现在交给
  `engine/music_engine/harmony` 之外的 `harmonize.py` 统一编排，`music_compose` 编完歌会自动跑一遍。
- 策略按**调内音阶级数**走，不是固定半音：`third_below` / `third_above` / `sixth_below` /
  `fifth_above` / `octave_below` / `double`。段落默认：副歌低三度、预副歌低三度、桥段低六度、尾奏低三度。
- 和弦优先：段落里有 `chordPlan` 时，候选音会挪到最近的调内和弦音——但**只允许挪 1 个半音以内**。
  一开始允许挪一个音级，结果 C5 的低三度被拉到 G4（低五度），和声一下变空，所以收紧了。
- 剪枝（`cut`）：太短（<0.18 s）、和前面的音贴太近（<45 ms）、超出音域（E3–A5）、
  和主唱音程过宽（>9 半音）、同音、重复、密度超了，都会被剪掉，并在 `cut` 里计数。
  `strict` 只留和弦音，`off` 全留。
- 密度 `density`：`full` 每个音都跟；`half` 隔一个跟（省气、不容易糊）；`phrase` 一句只跟一遍。
- 产物：`midi/vocal-harmony.mid` + `analysis/vocal-harmony-align.json`（带 `strategy`/`interval`/`chord`）
  + `analysis/harmony-plan.json`；`music_vocals part=harmony` 直接照它唱，`show` 看编排，`clear` 删掉。
- 音色也能按段换：`music_vocals section=chorus3 voice=xxx part=harmony`，这就是教程里「适当切换聚类」那一步。

**人声体检（`music_vocal_doctor`）**

逐音扫 8 类问题，`scan` 只出报告不改文件：

| 问题 | 判据 | 能怎么修 |
| --- | --- | --- |
| `silent` 整音没声 | 音身电平 < -48 dB | 只能重唱 |
| `dead-onset` 起音哑音 | 起音比全曲中位晚 > 75 ms（用中位数当偏置，抵消检测伪影） | `suggest` 改谱（提前/拉长 40 ms）后重唱 |
| `dead-body` 音身哑音 | 音身中间塌陷 ≥ 15 dB 且持续 ≥ 50 ms（混响吃字） | 拉音高 / 重唱 |
| `tail` 拖尾 | 音尾 60–200 ms 比音身只低 14 dB 以内，且后面没别的音 | `fix` 淡出切掉 |
| `overlap` 重叠音 | 和下一个音重叠 > 50 ms | `fix` 从下一音前 35 ms 淡出 |
| `gap` 缺口 | 音内/音间 50–140 ms 塌到 -30 dB 以下 | `fix` 复制音符尾部循环补上 |
| `off-pitch` 走音 | 音身 F0 中位数偏离谱面 > 45 音分 | `fix` 用 WORLD 重合成拉回去（|偏差| > 7 半音视为检测错，跳过） |
| `out-of-range` 超音域 | 低于 E3 或高于 A5 | 改谱或换调 |

- `fix` 一次 WORLD 分析搞定所有音高修正，再按时间顺序做淡出/补洞，最后按修补前的 LUFS 恢复响度（±3 dB）。
  产物 `audio/vocal/vocal-repair-<tag>.wav` + `report/vocal-doctor-<tag>.json`，不动原始干声。
- `actions: ["pitch","tail","cut","fill"]` 只做其中几类；`dryRun:true` 只报计划。
- 实测（16 秒 / 20 音）：两个 -97 / -80 音分的音修到 -5 / -3 音分，响度前后都是 -25.6 LUFS。
- 起音哑音是谱面的锅，音频里补不出来——`suggest` 会写一份
  `analysis/vocal-fix-score-<tag>.json`，`apply:true` 时按字符+时间匹配回 `vocal-align.json`，再重唱。

**改调、换音色（教程后两节）**

- 改调：整段唱不上去时，先改谱不改音频——`music_notes track=vocal op=transpose semitones=-2 sel={bar:13,toBar:20}`
  或 `op=move pitchDelta=-2`，再 `music_vocals` 重唱这一段。已经是成品干声、只想挪几个音时走
  `music_vocal_doctor op=fix actions=["pitch"]`（按谱面把音高拉回来，±45 音分起修，|偏差| > 7 半音视为检测错）。
- 换音色：`music_vocals section=chorus3 voice=<另一个音色>` 给某一段换音色，就是教程里的「适当切换聚类」；
  和声轨也一样，可以主唱一个音色、和声另一个音色。

**推荐顺序**：`music_compose`（自动跑和声）→ `music_vocals part=lead` → `music_vocals part=harmony`
→ `music_tune` → `music_vocal_doctor op=fix` → `music_mix`。和声先唱、后修：修的时候是按
主唱谱面对齐的，两边一起动会让对不齐的地方更难查。

**S5 收尾一步到位（`music_vocal_finish`，0.17.0）**

标准化编曲的 S5 到 `music_tune` 就收工，剩下「和声排了没、哑音剪了没」靠人记。现在收尾交给一个工具：

```
music_vocal_finish { project: "summer-express", tune: "natural" }   // 一步跑完
```

它按固定顺序做五件事，顺序本身就是设计：

1. **分和声**（`harmony: "auto"` / `"off"` / 策略名）：和声必须按定稿的谱面编，改一次谱就得重排一次；
2. **体检主唱**：先查主唱，和声是另一条轨，主唱的问题不能被和声盖住；
3. **就地修补**：`pitch`/`tail`/`cut`/`fill`/`debleed` 能补的当场补，写 `audio/vocal/vocal-repair-<tag>.wav`，
   **补完复扫一遍**——后面判定用的是「修后」的数字，不是修前的；
4. **重唱建议**：起音哑音补不出来，只给改谱方案（`analysis/vocal-fix-score-<tag>.json`）；
5. **可选调教**：`tune:"natural"` 就跑一遍；不跑则抄已有 `report/tune-*.json` 的音准读数。
   调教一定排在修补之后——它会重新合成 F0，先调后修等于白修。

判定 `verdict`：`ok` 全干净 / `needs-fix` 还有能补的 / `needs-resing` 只剩起音哑音。
产物 `report/vocal-finish-<tag>.json` + `project.json` 的 `vocal.finish`，`tools/music_gate.py --stage S5`
直接读这份报告，新增两行验收：「哑音/重叠/缺口 修后 = 0」「和声编排 ≥ 1 音」。
`PIPELINE.md` 的 S5 也改成了 `music_harmony → music_vocals → music_vocal_finish → music_tune`（先修后调；
`tune` 会优先读 `vocal-repair-*.wav`）。

## 作品方案页（music_design，接 dsh-guizang-design）

把工程里已经量出来的东西排成一页能发出去的作品方案：曲目档案、调教前后、混音验收、
段落结构、发布计划。版式与配色沿用本机安装的 `dsh-guizang-design` 插件（歸藏设计系统，
AGPL-3.0）的 Design Token；没装插件时退回内嵌的一套同值 token，页面仍然能生成。

```
music_design { project: "反乌托邦", style: "swiss", theme: "ikb" }
music_design { project: "反乌托邦", style: "magazine", theme: "ink-classic", deep: true }
```

产物：

```
design/<slug>-<style>-<theme>-plan.html   # 单文件，可直接发；打印样式已去掉音频播放器
design/<slug>-<style>-<theme>-plan.json   # 本次用到的报告来源、主题、字节数
```

- `style`: `swiss`（无衬线/网格/单一锚点色）或 `magazine`（衬线/纸色）。
- `theme`: swiss 的 `ikb` / `lemon` / `lemon-green` / `safety-orange`；magazine 的 `ink-classic` /
  `indigo-porcelain` / `forest-ink` / `kraft-paper` / `dune` / `midnight-ink`。
- `deep: true`: 额外调 dsh-soundcheck 对混音跑一次深分析，把真峰值、削波、分轨占比写进第 03 节。
- 页面里的数字优先取 `report/tune-*.json` 和 `report/mix-*.json`；混音报告缺 LUFS/峰值/重心时
  直接从混音 WAV 补测（不猜）。选报告时优先挑带 `lufs` 的那份，避免挑到手工混音记录。
- 想导出 PDF：用 Edge/Chrome headless `--print-to-pdf`；想登记进歸藏作品库，用
  `guizang_library_add { path: "<plan.html>", skill: "guizang-page-skill", title: "…" }`。
- 依赖是可选的：缺 `dsh-guizang-design` 时只影响主题来源与作品库登记，不影响页面生成。

## 深分析（可选，接 dsh-soundcheck）

本插件自己的分析只回答「多快、什么调、粗段落」；混音验收还要知道 LUFS/真峰值、
削波计数、结构变化点、分轨占比、人声在哪几段、和弦根音、起音密度。
这些交给另一个本机插件 **dsh-soundcheck**（MIT，本地 Python 引擎，音频不出本机）。

```
music_analyze { audio: "mix-v1.wav", deep: true }
music_analyze { audio: "song.flac", deep: true, start: "1:30", end: "2:10" }
music_analyze { audio: "song.flac", deep: true, deepStems: "4s" }   # 人声/鼓/贝斯/其他
music_analyze { audio: "song.flac", deep: true, deepStems: "6s", asr: "base" }  # 加吉他/钢琴 + 歌词候选
```

| 深分析多给的 | 用途 |
| --- | --- |
| `loudness.lufs` / `truePeakDb` / `clipped` / `overScaleSamples` | 母带验收；我们自己只算 sample peak，这里是真峰值 |
| `bpm.primary/secondary/maybeDouble` | 两层拍速，避免把半速当结论 |
| `structureBoundaries` / `sections` | 声学状态变化点（不是「副歌」标签，段落角色仍要人听） |
| `chords.roots` / `unresolvedRatio` | 根音级候选，用于核对和声走向 |
| `vocal.verdict/segments` | 有没有人唱、哪几段在唱（**需要 `deepStems` 分轨**才有意义） |
| `stems.ratios` | 人声/鼓/贝斯/其他的平均能量占比；分轨后的人声判定以此为准 |
| `instruments`、`activity` | 乐器区域线索与起音密度；模型分低，只当方向 |

- 没装 dsh-soundcheck 时 `deep:true` 不会失败：基础结果照常返回，`warnings` 里写「深分析不可用」。
- 带了 `start/end` 时，顶层 `loudness` 仍是整首的统计，深分析那一段的 LUFS/真峰值在 `deep.loudness` 里，
  两个范围别混着看。
- 不传 `deepStems` 时 `deep.vocal`（整混音分类）会把有人声的混音报成「无」，只能参考；
  传了 `deepStems` 会改成 `vocal.source = "stems"`，并给出 `present/ratio`。
- 分轨是可选组件：首次跑要加载 Demucs 模型（16 秒素材实测约 15 秒，4 核 CPU，按素材长度线性增加），
  之后按文件缓存；长歌建议 `deepStems` 配合 `start/end` 只分需要验收的段落，别整首跑。
- 引擎位置自动找：同 `node_modules` 下的 `dsh-soundcheck/engine`，或各 profile，或环境变量
  `MUSIC_SOUNDCHECK_ENGINE` 指向它的 `engine` 目录。`music_doctor` 会报告 `soundcheck.available` 与模型齐不齐
  （doctor 的 Node 侧代码改动要等插件重新加载后生效；引擎的 `/v1/doctor` 立刻返回同样的状态）。
- 分轨与 ASR 是它的可选组件：缺组件时只有对应字段缺失，不影响其余深分析；
  按它 README 的 `/audio-install` 或 `audio_provision` 补装。

## 人声卡点诊断（timing）

混音时会给每条人声量一次「唱在哪条线上」，结果写进 `report/mix-v1.json` 的 `timing`：

| 字段 | 含义 |
| --- | --- |
| `medianMs` | 起音离最近 16 分网格的中位距离（越小越齐；量的是散布） |
| `within30ms` | 落在 ±30 ms 内的比例 |
| `offsetMs` | 相对乐谱（`analysis/vocal-align.json`）的整体偏移，负=抢拍，正=拖拍 |
| `confidence` | 这个偏移有多可信（< 2 基本是测不准，别照着挪） |
| `suggestedMs` | 建议位移（置信度够才给） |
| `appliedMs` | 这次实际挪了多少 |

**默认不自动挪**：自动测出的偏移偶尔会锁到相邻音符（差一个音的周期），挪错比不挪更难听。
SVS 的起唱延迟已经在 `music_vocals` 阶段修掉了（见上文 `alignedMs`），所以正常情况下
`offsetMs` 应该接近 0；这里的 nudge 是给「手动换过素材 / 用户自己导入人声」留的。
要手动挪就在 `project.json` 里写：

```json
{ "vocal": { "nudgeMs": -15 } }
```

再跑 `music_mix`。实测 vr-demo2：人声相对鼓的中位 +5.3 ms、p25 −26.7 ms、p75 +34.7 ms——
不是整体拖/抢，而是逐音 ±30 ms 的散布；这种散布靠整体位移修不掉，要先看 `medianMs` 有没有系统偏差。
DAW 里对应的做法是逐 clip 拖波形（slip edit），本插件做的是「诊断 + 手动 nudge」。



在 Windows + 本地肥鱼服务上实测过完整链路（`music_doctor` … `music_master`）：

- 编曲：`vocalrock` 短工程（155 BPM / F#m / 7 段 / 68 s，带主唱与和声）生成
  drums 843、bass 389、节奏吉他 2×1384、主音吉他 104、keys 168、pad 180、人声 97、和声 38 个音符
- 段落落差：intro −23.9 / verse −20.7 / prechorus −19.4 / chorus −17.0 dB RMS（段间最大差 6.8 dB）
- 混音：−15.5 LUFS / TP −3.0 dBTP / crest 16.3；母带 −8.7 LUFS / −1.05 dBTP / crest 8.5
- 混音频段（30–120 / 120–300 / 300–800 / 800–2k / 2k–5k / 5k–12k）：−26.3 / −27.5 / −26.4 / −28.1 / −27.9 / −30.1 dBFS
- 真峰值：母带软限幅 + 迭代校正，实测 −1.05 dBTP（≤ −1 dBTP）；目标 −8.5 LUFS 差 0.19 LU
- SoundFont：Windows 上自动在 `MUSIC_SOUNDFONT`、`C:\usr\share`、DSH bash 包的 `usr/share` 等候选里找，找不到会明确报错
- `music_export`（stems / MIDI / Audacity 标签 / Reaper `.rpp` / MIX_NOTES）
- `music_preview`（ffmpeg 波形图）
- `music_mix { reference }`：按参考曲 6 段去均值做形状匹配并回报 `appliedDb`；实测把 300–800 Hz 补 +1.6 dB、800–2k 补 +3.2 dB 后，
  各段与参考形状差多在 1 dB 内（2–5k 会过冲约 1 dB，因为 0.8 Q 的峰形滤波器做不到精确塑形）
- 肥鱼唱歌桥：上传 → 拼音校验 → 提交 → 轮询（2400 s，可续查）→ 下载 → 重采样，全链路跑通

已知限制：

- 谐波失真来自 tanh 前级，和真电子管/箱体模拟还有差距；GM SoundFont 的镲片本身偏轻，靠总线提亮补
- SVS 在 CPU 上很慢（见上），长歌要等；`cover` 换声依赖 RVC 包
- 分离后端默认 `center`（近似）；装 `torch + demucs` 后 auto 自动切 Demucs
- 没有客户端播放器，听感验收要自己导出听
- 0.8.0 的演奏层只写在 MIDI 上：力度/微时值/连奏能改善「像人弹」，但改不了 GM 音源本身的音色；
  人声重叠会被削到 12 ms 留白（SVS 不接受重叠），所以人声听感上的连句仍要靠 `music_tune` 的气口与滑音
- 母带是软限幅 + 真峰值换算：报告里的 `truePeakOut` 可能比采样峰值高几 dB（过采样插值），
  这是正常的；以 `truePeakOut` 守门，不看采样峰值

## 洛天依与语调教（0.12.0）

洛天依不是开源声库，插件不会替她发声；这一版把「谱子 → 洛天依 → 干声 → 后期」接成了闭环：

```bash
music_tunefile  {project, action:"export", format:"svp"}   # 或 vsqx
# 在 Synthesizer V（洛天依 AI）/ VOCALOID 4 里打开、选洛天依、唱、导出 wav
music_vocals    {project, mode:"import", source:"她唱的.wav", label:"洛天依"}
music_yutiao    {project, intensity:0.8}                   # 语调教：按四声重写音高线
music_tune / music_mix / music_master / music_design
```

- `tunefile.py`：`.svp`（SynthV，blick 计时）与 `.vsqx`（VOCALOID，480 dots/拍）的独立读写实现，
  不依赖宿主 SDK；有 `report/tune-lead.json` 时把音高曲线写进 `pitchDelta`。
- `yutiao.py`：把每字的普通话声调（阴平/阳平/上声/去声/轻声）摊成语调曲线，叠句内降调与
  疑问句末上扬，再压字尾、拉平字间响度，用 WORLD 重合成（时长与节奏不变）。
  本机实测：音高偏差 `medianAbsCents 67 → 3.2`。
- `vocals.import_vocal()`：外部渲染的干声（洛天依/ACE/SynthV/GPT-SoVITS）统一走这里进工程。
- 详细解剖与优缺点见 [docs/luotianyi-bridge.md](docs/luotianyi-bridge.md)。

致谢：用户提供的 VvTalk 1.0.1（作者 **GalaxieT**，CC BY-NC-SA 4.0，b 站空间 15114289）；
语调教思路来自 **洛叶**《语调教教程》（luo-ling-yi.github.io/yutiao-tutorial）；
**@幻夜jyw** 提供了语调教技术支持；洛天依 GPT-SoVITS v2pro 模型来自「只想干饭的麦克斯韦妖」。
这几项都是参考资料，代码为按公开文件格式的独立实现。

## 许可

MIT。肥鱼声库与模型遵循其仓库条款；第三方歌曲翻唱/Remix 需自行确认授权。

`engine/music_engine/theory.py` 的组织方式参考 [abab996/AI_MIDI](https://github.com/abab996/AI_MIDI) 的 Library
（Apache-2.0）：先读乐理再创作、按主题分组。正文按本插件参数重写，不是原文复制；
和声/演奏层（`harmony.py`、`performance.py`）为本项目实现。
