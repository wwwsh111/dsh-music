# 编曲标准化流程（PIPELINE）

这份文件管三件事：**顺序**（先做什么、卡在哪）、**契约**（产物放哪、叫什么、谁是唯一真源）、
**纪律**（什么时候必须停下来问用户、什么时候说实话做不到、空间怎么不失控）。
「怎么做」的技法在 `SKILL.md`（曲风表、歌词规则、调教、混音、洛天依互通）；两份一起看。

工具一共 26 个，都以 `music_` 开头，全部走本机引擎（`music_doctor` 报活）。
**验收不靠引擎**：`tools/music_gate.py` 直接读 `project.json` / `midi/` / `report/*.json`，
引擎没起、端口没监听时照样能判一个工程过没过（见 §4）。

---

## 0. 速查表

| 阶段 | 干什么 | 主工具 | 产物 | 门禁（不过不许往下走） | 只查这一段 |
| --- | --- | --- | --- | --- | --- |
| **S0 体检** | 引擎/声音服务/磁盘 | `music_doctor` | — | engine 在线，磁盘 ≥ 3 GB | — |
| **S1 定方案** | 五问 + 曲风包 + 乐理 | `music_genres` `music_theory` | 口头方案 | 用户确认曲风/BPM/调性/结构/交付物 | — |
| **S2 建工程** | 工程骨架 | `music_project {op:"create"}` | `project.json` + 目录树 | 段落首尾相接；器乐轨 MIDI 都在；段落和声有差别 | `--stage S2` |
| **S3 写歌词** | 一字一音落谱面 | `music_lyrics` | `lyrics/lyrics.json` | 歌词可读、乐句 ≥ 4、`warnings` 清空 | `--stage S3` |
| **S4 编曲** | 伴奏 + 人声旋律 MIDI | `music_compose` → `music_render` | `midi/*.mid`、`audio/render-v1/*.wav` | **试听点 ①** + 分轨峰值/RMS/时长 | `--stage S4` |
| **S5 人声** | 分和声 → SVS 唱 → 收尾 → 调教 | `music_harmony` → `music_vocals` → `music_vocal_finish` → `music_tune` | `audio/vocal/vocal-tuned-*.wav`、`vocal-repair-*.wav`、`report/vocal-finish-*.json` | 音准 ≤ 12 cents；句句有气口；A/B 响度差 ≤ 1 LU；**修后哑音/重叠/缺口 = 0**（起音哑音只能改谱重唱）；和声已排 | `--stage S5` |
| **S6 混音** | 效果链 / 总线 / 自动化 / 粗混 | `music_fx` `music_bus` `music_automation` `music_mix` | `audio/mix/*.wav` + `report/mix-v1.json` | **试听点 ②** + 无削波、TP ≤ −1、crest ≥ 7、器乐轨全进 | `--stage S6` |
| **S7 母带** | 响度收口 | `music_master` | `audio/master/*.{wav,mp3}` | 目标 LUFS ±0.5、TP ≤ −1 dBTP、crest ≥ 7、mp3 在 | `--stage S7` |
| **S8 交付** | 试听 / DAW 包 / 方案页 | `music_preview` `music_export` `music_design` | `preview/`、`export/`、`design/` | **试听点 ③** + 用户点头 | `--stage S8` |

S4 之后还有四个「按需插槽」，不强制、不进主链：
`music_notes`（改音符）、`music_patterns`（乐句复制摆放）、`music_sampler`（自有采样当乐器）、
`music_tunefile`（洛天依工程往返）。改完立刻 `music_render`，不要攒着改。

---

## 1. 目录与命名契约

工程目录由 `music_project` 建，**不要手工加目录**。规范树（`engine/music_engine/project.py::_dirs`）：

```
<工程>/
  project.json          ← 唯一真源：速度/调性/结构/轨道/人声/渲染指针
  source/               用户原始素材（导入的音频，只读）
  analysis/             分析结果、语调教计划表
  midi/                 每轨 MIDI + vocal.mid（可重渲染的源头）
  lyrics/               歌词文本与 lyrics.json、pinyin.txt（咬字修正）
  preview/              波形图、段落图
  report/               render-*.json / mix-*.json / master-*.json / tune-*.json
  logs/
  audio/render-v1/      每轨分轨 WAV（可再生）
  audio/mix/            混音成品（历史版本在这里堆积）
  audio/master/         母带成品（同上）
  audio/vocal/          SVS 干声、调教成品、A/B 拼贴
  export/               DAW 工程包、.svp/.vsqx
  design/               作品方案页 HTML
```

**命名**：引擎一律写 `v1`（`render-v1`/`mix-v1`/`master-v1`/`vocal-<tag>-v1`）。
要留一版对比就自己另存 `<名字>-v2`，并且**在 `report/` 里同步留一份同名 json**。

**唯一真源**：`project.json` + `midi/` + `lyrics/`。音频全是派生物。
所以「重做」永远是从 MIDI 重渲，不是从 WAV 修。

**读 project.json 的四个坑（真实工程里都出现过）**：

- 顶层 `bars` **可能没有**。总长以 `sections[].bars` 求和为准，`startBar` 才是小节号的真凭据。
- 没有 `harmony` 键。段落和声在 `sections[].chords`（和弦名数组），可比的是「段与段之间和弦走向不同」。
- `tracks[].midi` 可以是 `null`（人声轨常这样，旋律走 `vocal.melody`）；`vocal-harmony` 这类轨
  可能根本没有 MIDI 文件——和声由 `vocal.harmony` 在渲染时生成。
- `renders` 里存的只是 id（`"master": "master-v1"`），**不是路径**。成品的真实位置在
  `report/master-v1.json` 的 `master` / `mp3` 字段（绝对路径）。

---

## 2. 阶段细则

### S0 体检（10 秒）
```
music_doctor
```
看四行：`engine`、`voice`（肥鱼声音服务）、`singing`（SVS+RVC）、`disk`。
`singing.available=false` 时**当场**告诉用户「能出纯音乐/念白，唱不了」，别等到 S5 才发现。
引擎没起时 `music_*` 全部不可用，但 `music_gate.py` 仍能审已有产物——先把能审的审了。

### S1 定方案（一轮对话）
先答五问（曲风 / 时长结构 / 人声与歌词 / 声线 / 交付形态），能推断的推断，缺的按曲风默认值补并标注「默认值」。
然后：
```
music_genres {op:"list"}                  → 给 3 个候选 + 菜单
music_genres {op:"get", id:"citypop"}     → 拿 BPM 区间、和弦库、鼓型、结构
music_theory {topic:"progressions"}       → 写和声前必读（也可 "melody"/"performance"/"arrangement"）
```
`music_theory` 是前置步骤，不是可选项。跳过它写出来的伴奏就是「四和弦循环」。

### S2 建工程
```
music_project {op:"create", name:"<slug>", genre, bpm, key, bars}
```
`bars` 省略 = 曲风结构默认总长。建完先 `{op:"open"}` 回读一遍，确认 `sections` 与预期段落一致。
验收：段落首尾相接（`startBar` 连续）、器乐轨 MIDI 全部落盘、段落和声至少 3 种走向
（lo-fi / hiphop / trap 天生就 2 套和弦，这时判 WARN，别硬加旋转）。
人声和声轨可以没有 MIDI，只要 `vocal.harmony` 打开。

### S3 写歌词
```
music_lyrics {op:"set", project, text}
```
中文一字一音；延音用 `_`；换行 = 乐句；标点不唱。改到 `warnings` 为空再进 S4。
英文/日文只能念白（`music_vocals mode:"speak"`），不要承诺演唱。

产物 `lyrics/lyrics.json` 是 dict：
`{language, text, sections:[{section, lines:[{text, syllables, suggestedBars}], syllables}], warnings, totalSyllables}`。
句数 = 各段 `lines` 之和；`warnings` 是「字数不是 4 的倍数」这类可执行的短句，要逐条改或明确接受。

### S4 编曲（第一个长阶段）
```
music_compose {project, brief:"<一句话风格>", seed}      # 伴奏 + 人声旋律
music_render  {project}                                  # MIDI → 每轨 WAV
music_preview {project}                                  # 波形/段落图
```
- `seed` 固定才能复现；换 seed = 换动机/鼓型/音色。
- `track:"drums"` 只重生成一轨，别整首重来。
- 耗时：纯 MIDI 秒级，渲染按轨数 × 时长，一般 10–60 秒。

验收（`report/render-v1.json` + 实测 WAV + MIDI 现算）：
- 每轨峰值 ≤ 0 dBFS（超了离削波只差一步，先降 gainDb 再渲）。
  **以落盘 WAV 为准**：报告里的 `peak` 是处理链上自报的，实测 lofi 工程报告写 drums +2.25 dB、
  文件却是 −1.00 dBFS。`music_gate.py` 会自己量 wav，量不到才回落到报告值并在结论里注明。
- 每轨 RMS 与 `rmsTarget` 差 ≤ 1.5 dB。
- 渲染时长与「总小节 × 4 拍 ÷ BPM」差 ≤ 2 秒（差得多说明中途停渲或段落没铺满）。

**唯一小节占比只当参考，不设门限**（0.99 与 0.10 都可能是对的）：
- ≈1.0 → 每小节内容都不一样（可能每小节重写，也可能缺动机重复）；引擎生成的工程常见。
- ≈0.1 → 整段在照抄（lo-fi / 极简编曲的正常写法，也可能是没发展）。
- 判断「像不像 loop」时用它，不要拿它当质量分，也不要写「低于 0.6 就 FAIL」。

**试听点 ①**：`music_preview` → `present`，问结构/和声/节奏。用户说改，回 S4 或 `music_notes`，不进 S5。

### S5 人声（最慢，必须报时）
```
music_harmony {project, op:"plan"}                               → 先分和声（副歌/桥段）
music_vocals {project, mode:"sing", part:"lead", wait:false}     → jobId
music_job {jobId, wait:true}                                     → 续查
music_vocals {project, mode:"sing", part:"harmony", wait:false}  → 和声轨（照编排唱）
music_vocal_finish {project}                                     → 收尾：复扫 + 修补 + 重唱建议
music_tune   {project, style:"natural", intensity:0.7, ab:true}  → 调教 + 真实感链（读修补后的干声）
```
- **先分和声，再让 SVS 唱**：`music_harmony` 只写 `midi/vocal-harmony.mid`；编排改一次就要重唱一次，
  所以谱面（`music_notes` 改音）定稿之后才排和声。和声素材不足会自动剪枝，`cut` 报告里能看剪了什么。
- **CPU 上 20 秒分段约 5–6 分钟**。整首歌先算总时长报给用户，再用 `wait:false` 拿 jobId 轮询，不要挂死调用。
- 和声轨用 `part:"harmony"`，唱的是 `music_harmony` 编排的那条，不再是写死的低三度。
- 先 `natural` + A/B 听一版，再按歌调 `style`/`intensity`/`formant`。
- **调教之前先跑 `music_vocal_finish`**：它逐音扫哑音（起音/音身）、拖尾、重叠、缺口、走音，
  能补的当场补（拉音高、切残响、剪重叠、补缺口），补完复扫一遍，把修前/修后写进
  `report/vocal-finish-<tag>.json`。顺序不能反：`music_tune` 会重新合成 F0，先调后修等于白修；
  `music_tune` 现在优先读 `audio/vocal/vocal-repair-<tag>.wav`，所以「先收尾、后调教」才连得起来。
  只想一步跑完时给 `music_vocal_finish` 传 `tune:"natural"`，它在修补之后自己调教。
- 起音哑音（SVS 咬字慢）补不了，只能 `music_vocal_doctor {op:"suggest"}` 改谱后重唱。
- SVS 的 50–150 ms 起唱延迟由 `music_vocals` 自动挪回（报告里的 `alignedMs`）；
  自己导入外部干声时用 `align:true`，同样看 `alignedMs`。
- 对拍另外两个数在 `report/mix-v1.json` 的 `timing.vocal` 里（只在混了人声之后才有）：
  - `medianMs` —— 起音离最近 16 分线的中位距离，**这个数可信，≤ 40 ms 算贴**；`within30ms` 是命中率。
  - `offsetMs` + `confidence` —— 相对乐谱的整体偏移。**`confidence < 2` 时不要信**，它常锁到相邻音符：
    本机实测某工程 `offsetMs 66.9 / confidence 1.83`，但用 MIDI 起音与音频起音最近邻算中位只有 **13.2 ms**，
    照着 66.9 去 `vocal.nudgeMs` 反而把准的人声推早了。要交叉验证再用。
  - 真要用就写 `project.json` 的 `vocal.nudgeMs`（毫秒，负数=提前），`music_mix` 会按它 shift 人声，
    报告里 `appliedMs` 记实际挪了多少；挪完再看 `medianMs` 有没有变小，不要只看 `appliedMs`。

验收看 `report/tune-<tag>.json` 的 `stats`：
- `pitchAfter.medianAbsCents ≤ 12`（> 20 基本是跑调，回 `lyrics/pinyin.txt` 改咬字，不要用调教硬压）；
- `breaths` 与 `phrases` 同量级（≥ 80% 句数有气口，但不抢拍）；
- A/B 响度差 ≤ 1 LU（听 `audio/vocal/vocal-ab-*.wav`）；
- `centroidMixed` 比 `centroidDry` 高 100–400 Hz 属正常提亮。

### S6 混音（第二个长阶段）
先听再动：
```
music_fx   {project, op:"preset", track:"vocal", preset:"vocal-clean"}   # 有明确问题时
music_bus  {project, op:"layout", bus:{genre:"rock"}}                     # 需要分组时才建
music_automation {project, op:"set", lane:{track:"master", param:"gain", shape:"linear", points:[{bar:1,value:0},{bar:64,value:-6}]}}
music_mix  {project, reference:"<参考曲.flac>"}                            # reference 可选但强烈建议
```
- `music_fx` 挂在音色处理之后、推子之前；`music_bus` 是 FL 的 Send to。
- 分组总线不是每首歌都必须有——先听，再决定要不要「让鼓和贝斯咬在一起」。

验收看 `report/mix-v1.json`：`clippedSamples == 0`、`truePeak ≤ −1 dBTP`、`crest ≥ 7 dB`、
`silentRatio ≤ 5%`、器乐轨全部出现在 `stems` 里、主唱 `pan` 在 ±0.15 内。
**老报告可能没有 `crest` / `silentRatio` / `clippedSamples`**（只有 `clippingSamples`）：缺的项显示 `—`，
不要当成通过——要么重跑 `music_mix` 生成新报告，要么在交付说明里写明「这一版没测 crest」。

**试听点 ②**：`present` 混音文件，问平衡/人声位置/情绪。用户说「人声太靠后」→ 回 `music_fx`/`music_bus`，不是直接进母带。

### S7 母带
```
music_master {project, targetLufs:-9, truePeak:-1}
music_analyze {audio:"<工程>/audio/master/master-v1.wav", deep:true}
```
验收看 `report/master-v1.json`：`lufsOut` 与 `targetLufs` 差 ≤ 0.5 LU、`truePeakOut ≤ −1 dBTP`、
`crestOut ≥ 7 dB`、`overCompressed` 为否、`mp3Ok` 通过且 mp3 文件真在。
`crestOut` 低于 7 说明压平了，降 0.5–1 LU 重渲，不要靠限幅器硬顶。

成品位置以报告里登记的绝对路径为准；找不到时看 `audio/master/` 实际有什么（可能被另存成
`<曲名>_人声版_v3.wav` 这类名字）。音频没丢、只是名字不同，就说明链接断了，别当成渲染失败。

**试听点 ③**：`present` 母带 + 波形图，问响度/质感/削波。

### S8 交付（按需，不强制全做）
```
music_preview {project}                          # 试听图
music_export  {project, daw:"reaper"}            # export/ + session/session.rpp
music_design  {project, style:"swiss", theme:"ikb"}   # design/<slug>-swiss-ikb-plan.html
```
- DAW 包给 Audacity/Reaper/Ardour/LMMS，见 `SKILL.md`「DAW 导入」。
- 方案页里的数据必须能被 `report/*.json` 对上；缺的字段宁可空着，不编。

---

## 3. 三个固定试听点（不可跳过）

1. **编曲后**（S4 末）——和声、节奏、结构。这里改最便宜。
2. **粗混后**（S6 末）——平衡、人声位置、情绪。
3. **母带后**（S7 末）——响度、质感、削波。

规矩：到点必须 `present` 文件并停下来问。你听不到声音，只能读指标；用户点头前不要往下跑。
一次做完整首再交付 = 返工从头来。

---

## 4. 验收：跑闸门，别靠印象

`tools/music_gate.py` 是离线闸门，只读工程产物，不需要引擎：

```powershell
$py   = "$HOME\.local\share\dsh-music\venv\Scripts\python.exe"
$gate = ".\tools\music_gate.py"   # 在仓库根目录下执行

& $py $gate "<工程目录>"                 # 逐条：实测值 → 门限 → 结论 + 下一步该回哪一阶段
& $py $gate "<工程目录>" --stage S4,S7   # 只查这两段
& $py $gate "<工程目录>" --json          # 机器可读（写进交付报告用这个）
& $py $gate --all                       # 全部工程一行一个摘要：S2✓/S3!/S4✗/…
```

退出码：`0` 全过（允许 WARN）/ `1` 有 FAIL / `2` 用法或数据错误。

判定只有四种：`PASS` 有依据通过、`WARN` 能往下但要跟用户说、`FAIL` 不许往下、`—` 没数据（**不是通过**）。

门限（改 `TH` 就是改这份 SOP，两边必须同步）：

| 阶段 | 检查项 | 门限 |
| --- | --- | --- |
| S2 | 段落总长 vs bars | 相等（没登记 `bars` 时按 `sections` 求和放行） |
| S2 | 段落首尾相接 | 0 处断档 |
| S2 | 器乐轨 MIDI 就位 | 全部；人声和声轨可缺（`vocal.harmony` 生成） |
| S2 | 段落和声有差别 | ≥ 3 种，且不全同（只有 2 种时对 lo-fi / hiphop / trap 判 WARN，不算错） |
| S3 | 歌词 / 乐句数 | 文件可读；乐句 ≥ 4 |
| S4 | 分轨峰值 | ≤ 0 dBFS（量落盘 WAV；量不到才用报告自报值并注明） |
| S4 | 分轨 RMS 贴合 | 与 `rmsTarget` 差 ≤ 1.5 dB |
| S4 | 渲染时长 | 与「bars × 4 拍 ÷ BPM」差 ≤ 2 s（> 8 s 才算 FAIL） |
| S4 | 唯一小节占比 | **参考值，不判** |
| S5 | 音准 | `medianAbsCents ≤ 12`（> 20 才 FAIL） |
| S5 | 哑音/重叠/缺口 | 修后 = 0（起音哑音 WARN，只能改谱重唱） |
| S5 | 和声编排 | 启用了和声就至少 1 个和声音 |
| S5 | 气口密度 | 气口 / 句 ≥ 80% |
| S5 | A/B 响度差 | ≤ 1 LU（近似） |
| S6 | 削波样本 | 0（字段缺失记 `—`） |
| S6 | 真峰值 | ≤ −1 dBTP |
| S6 | 波峰因数 | ≥ 7 dB |
| S6 | 静音占比 | ≤ 5% |
| S6 | 参与混音轨道 | 器乐轨全进 `stems` |
| S6 | 主唱位置 | \|pan\| ≤ 0.15 |
| S7 | 综合响度 | 目标 ± 0.5 LU |
| S7 | 真峰值 | ≤ −1 dBTP |
| S7 | 波峰因数 | ≥ 7 dB |
| S7 | 过压缩 | 否 |
| S7 | MP3 / 母带文件 | mp3 编码通过且文件在；报告指向的 wav+mp3 都在 |

闸门能证明的只有「指标在范围内」。**结构好不好、人声靠不靠前、副歌起没起来，闸门判不了**——
那是三个试听点要问用户的事。机器质检不等于验收。

**本机基线**（引擎 0.13.0，2026-11-07 实测）：新建 lofi 工程 48 小节 → compose → render → mix → master，
闸门一次跑过 S2/S4/S6/S7（S2 因 lo-fi 只有 2 套和弦给 WARN、S6 因这一版没人声给 WARN），
母带 −12.15 LUFS / −1.05 dBTP / crest 9.08 / mp3 在。说明闸门既不全拦、也不全放。
反向对照：旧工程 `brave-tomorrow-v3` 的 S4 与 S7 母带文件是真 FAIL（drums 报告超限、报告指向的
`master-v1.*` 已被改名成 `不必害怕明天的风_人声版_v3.*`）。`--all` 当前 43 个工程、40 个有 FAIL——
大部分是 `chk-*`/`style-*`/`fl-four-test*` 这类调试残留，不用返工。

---

## 5. 空间纪律

实测：一个完整工程 100–700 MB，分轨 WAV 是大头（48 kHz/24 bit 每轨每分钟约 8 MB）。
本机工程库清理后 **43 个工程 / 2.96 GB**（清理前 17.3 GB / 14092 文件）。

**三层生命周期**

| 层 | 内容 | 策略 |
| --- | --- | --- |
| 真源（永不自动删） | `project.json`、`midi/`、`lyrics/`、`source/`、`analysis/`、`report/`、`export/`、`design/` | 只靠用户决定 |
| 当前产物（保留最新） | 最新 `audio/master/*`、`audio/mix/mix-v1`、`audio/vocal/vocal-tuned-*`，**以及 `report/*.json` 点名过的每一个文件** | 保留 |
| 派生物（可回收） | `audio/render-*/`、旧 `mix-*`/`master-*`、`preview/`、`frames/`、`__pycache__` | 定期清理 |

**清理器**：`tools/music_gc.py`（默认 dry-run，只读）

```powershell
$gc = ".\tools\music_gc.py"   # 在仓库根目录下执行

& $py $gc                     # 全量盘点：每工程 keep / regenerable / history / junk
& $py $gc --summary           # 只看合计
& $py $gc --projects brave-tomorrow,math-pi-89   # 只看这几个工程
& $py $gc --apply --only regenerable             # 隔离移动到 <root>\_trash\<时间戳>\
& $py $gc --restore "<root>\_trash\<时间戳>\manifest.json"   # 反悔
& $py $gc --purge --older-than 14                 # 真删 14 天前的隔离区
& $py $gc --dupes             # >4 MB 完全重复文件
```

铁律：
- **先 dry-run，把数字给用户看，确认后才 `--apply`。**
- `--apply` 是搬进 `_trash`（保留相对路径 + `manifest.json`），不是删；确认没人再要才 `--purge`。
- **隔离区与工程在同一块盘时，`--apply` 不释放空间**（只是改目录项）；空间是 `--purge` 之后才腾出来的。
  想「当场腾空又留反悔余地」只有一条路：`--trash-dir` 指到**另一块盘**（跨盘是复制后删除）。
  工程根如果是指向另一块盘的 junction，隔离区默认跟工程同盘，顺序必须是
  `dry-run → --apply → 检查 → --purge`。
- 用户没点头，不许跑 `--apply`，永远不许对真源目录跑。
- `report/*.json` 里点名过的音频文件被钉住，永不自动删（脚本里叫 `protected`）；
  「报告还在、文件没了」= 交付链接断掉，比多占几 GB 严重。
- 手搭的工作区（没有 `project.json` 的目录）只报告，不碰——里面常混着用户自己的脚本和半成品。
  只清它的环境垃圾（`cpu_torch`/`dl`/`__pycache__`/`.whl`），用 `--apply --orphans-junk`，
  `.py/.wav/.mid/.lrc/.md` 一律不动。

**单工程瘦身**：交付后跑一次 `--projects <名字> --apply --only regenerable`，
再 `music_render` 随时能还原分轨。历史版本只留「最后一版 mix + 最后一版 master」
（`--keep-mix 1 --keep-master 1` 就是这个意思）。

---

## 6. 失败与降级

| 现象 | 判断 | 动作 |
| --- | --- | --- |
| `music_doctor` engine 不可达 | 引擎没起/端口变了 | 报端口与 `~/.dsh/music/engine.json`，让用户重启 DSH；不要自己改代码 |
| 只有 `music_gate.py` 能用 | 引擎离线 | 先把已有工程审一遍，把要补的清单给用户，等引擎起来再跑 S4–S7 |
| `singing.available=false` | 缺 SVS/RVC 或 GPU | 降级：纯音乐 / 念白 / 只出 MIDI 让用户去 ACE 或 Synthesizer V 唱 |
| Demucs 缺失（Remix） | `separation.backend="center"` | 明说人声带伴奏残留；要干净就装 Demucs 或换 `backend:"center"` 赶时间 |
| dsh-soundcheck 缺失 | `deep:true` 自动降级并写 warning | 用基础指标验收，报告里注明「未做深分析」 |
| 报告里的 `peak` 跟文件对不上 | 引擎自报值 vs 落盘 WAV | 以 WAV 为准（闸门会自己量）；文件已被清理就重渲一次再判 |
| 报告字段缺失（老格式） | 闸门显示 `—` | 不当通过；重跑对应阶段或注明「未测」 |
| 磁盘 < 3 GB | 渲染/母带会中途失败 | 先 `--purge` 清空隔离区（同盘时这是唯一真正腾空间的一步），或 `--apply --only regenerable` |
| 人声时长对不上伴奏 | `alignedMs` 偏大 / 手工导入没对齐 | 回 S5 重渲；外部干声要 `align:true` |
| 人声听着「拖」或「抢」 | `timing.vocal.medianMs` > 40 ms | 先交叉验证再用 `vocal.nudgeMs` 微调；`offsetMs` 低置信时别照抄 |
| 渲染中途失败 | — | 报**阶段名 + 首条错误**，最多重试 2 次；第三次停下说明卡在哪 |
| 改了 `engine/music_engine/*.py` 不生效 | 引擎是常驻进程 | 必须重启引擎/DSH，别怀疑代码 |

---

## 7. 长任务纪律

- 提交前先报预计耗时（人声 CPU 上 5–6 分钟 / 20 秒；深分析 60–90 秒；整曲母带 1–2 分钟）。
- 用 `wait:false` 拿 `jobId`，再用 `music_job {jobId}` 轮询；不要挂着一个调用等十分钟。
- 同一 jobId 续查，不要重复提交（会重复烧 CPU 并生成新版本）。
- 失败只报事实：哪个阶段、什么错、已经产出了什么。

---

## 8. 一句话总结

`体检 → 五问 → 乐理 → 建工程 → 歌词 → 编曲（停）→ 唱 → 调教 → 混音（停）→ 母带（停）→ 交付`，
每步产物落在契约目录里，真源永远是 `project.json + midi/ + lyrics/`；
到点跑 `music_gate.py`，`—` 不等于通过，三个试听点必须让用户点头，
派生物想删就删、随删随渲，删之前先 dry-run、并在同盘时用 `--apply` 后再 `--purge`。
