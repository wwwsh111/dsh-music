---
name: music-production
description: 音乐模式工作流：选曲风、写歌词、生成伴奏与人声、粗混、导出 DAW 工程包。
whenToUse: 用户要做歌、混音、翻唱、Remix，或进入音乐模式时。
---

# 音乐制作工作流

## 开场：最多五个问题

1. **曲风**：给 3 个推荐（按情绪/场景/参考曲），再给完整菜单入口。
2. **时长与结构**：30 秒 demo / 2 分钟完整 / 自定义段落。
3. **人声**：纯音乐 / 要唱 / 念白；歌词用户给还是你写。
4. **声线**：可爱 `cute` / 温柔 `gentle`（只影响说话；唱歌是肥鱼 RVC 声库）。
5. **交付**：试听版 / DAW 工程包 / 母带成品。

能推断的就推断，缺的项用曲风默认值并标注「默认值，可改」。

## 工具调用：先用 music_code 串起来

流程步骤多、每步只要一个结论时，优先写一段 `music_code` 而不是逐个调工具：
工具目录用 `music.tools('关键词')` 现查，中间结果留在程序里，只 return 需要的字段。
例如「建工程 → 写歌词 → 编曲 → 渲染」可以在同一次调用里完成。

```js
const p = await music.project({ op: 'create', name: 'demo', genre: 'citypop', bpm: 110, bars: 32 })
const warn = await music.lyrics({ project: 'demo', text: '...' })
await music.compose({ project: 'demo' })
return { name: p.name, warnings: warn.warnings }
```

- 同一工程上的写操作顺序 `await`，不要 `Promise.all`。
- 长任务（唱歌、渲染、母带）先 `{wait:false}` 拿 jobId，再用 `music.sleep` + `music.job` 轮询。
- 需要用户看的过程、试听点、错误解释，仍然照常直接说，不要只丢一段 JSON。
- 插件配成 `codeMode: compact` 时，普通工具不在列表里，别按名字硬调；先 `music.tools()`
  查目录，再 `music.<短名>()`。用户要求逐步确认或模型不适合写代码时，让用户把
  `codeMode` 改回 `native`，再退回普通工具逐个调用。

## 曲风菜单（15 类）

| id | 名称 | BPM | 一句话 |
| --- | --- | --- | --- |
| pop | 华语流行 | 92–120 | 钢琴/吉他，人声靠前 |
| citypop | 都市流行 | 96–118 | Rhodes + 清音吉他 + Funky Bass |
| lofi | Lo-fi Hip-hop | 70–90 | 脏钢琴、黑胶噪声 |
| rnb | R&B / Neo-soul | 70–100 | 七九和弦、转音 |
| hiphop | 嘻哈 Boom Bap | 85–95 | 采样切片、摇摆鼓 |
| trap | Trap | 130–160 | 808、三连音 Hi-hat |
| house | House | 118–128 | 四踩、Offbeat Bass |
| synthwave | Synthwave | 80–110 | 模拟合成器、门限鼓 |
| rock | 流行摇滚 | 100–140 | 失真吉他、实鼓 |
| vocalrock | 虚拟歌手摇滚 | 132–168 | 双轨失真吉他、真实箱体 IR、副歌长音 hook、人声和声轨（星尘/诗岸那一类） |
| folk | 民谣 | 70–100 | 木吉他、近讲人声 |
| guofeng | 国风 | 70–100 | 古筝/笛/二胡 + 弦乐 |
| acg | 二次元 / J-pop | 130–180 | 快速鼓、明亮合成器 |
| cinematic | 管弦 / 电影感 | 60–100 | 弦乐、铜管、渐进 |
| kids | 儿歌 / 童谣 | 90–120 | 简单和弦、可爱声线 |

想看细节调 `music_genres {op:"get", id}`。

## 歌词规则（中文按谱唱歌）

- **一字一音**；一个字对应一个音符。旋律由 `music_compose` 生成：先生成动机，再按段落做发展
  （重复/移调/模进/倒影/逆行/加花/拉伸），按段落密度取节奏（主歌疏、副歌密），句尾拖 1–2.5 拍。
  不要自己假设每个字都是一拍，也不要指望副歌是主歌的复制。
- 延音用 `_`：`微光悠_悠` 表示「悠」占两个音。
- 标点不唱；换行表示乐句。
- 4/4 常见：8 字 / 2 小节，16 字 / 4 小节。
- 英文/日文当前不能唱，只能 `music_vocals {mode:"speak"}`。
- 多音字先按常用读音，试听后改；插件会校验服务读音词表，缺的读音换最近的（个别字会读歪），
  要唱准就往工程里放 `lyrics/pinyin.txt`（空格分隔、`de5` 带调写法）。服务不支持 ü，`lv` 会唱成 `lu`。
- 每句尽量 4 的倍数；`music_lyrics` 会给出 warnings。

## 标准流程

> 这里是「怎么做」的技法。**顺序、门禁、产物契约、空间纪律**在 `PIPELINE.md`（同目录）：
> S0 体检 → S1 方案 → S2 建工程 → S3 歌词 → S4 编曲 → S5 人声 → S6 混音 → S7 母带 → S8 交付，
> 三个固定试听点卡在 S4/S6/S7 末尾。开长任务前先看它的「长任务纪律」小节。
> 验收跑 `tools/music_gate.py`（离线，`--stage S4,S7` 可只查一段）；删派生物用 `tools/music_gc.py`
> （默认 dry-run，先给用户看数字）。

0. `music_theory` —— 先读乐理速查，别凭感觉写。不传 `topic` 看目录；写和声读 `progressions`，
   写旋律读 `melody`，要「像人弹的」读 `performance`，排段落读 `arrangement`；再传 `section`
   只看其中一组（如 `{topic:"progressions", section:"终止式"}`）。这是创作前置步骤，不是可选项。
1. `music_doctor` —— 确认 engine / voice / singing 状态。
2. `music_genres` —— 选曲风。
3. `music_project {op:"create", genre, bpm, key, bars}`。
4. `music_lyrics {op:"set", text}` —— 看 warnings，改到每句字数合适。
5. `music_compose` —— 生成伴奏 + 人声旋律 MIDI。0.8.0 起和声会按曲风包 + 调式选进行，
   按 BPM 决定换和弦频率，并做副属/调式互换/终止式；各轨力度与微时值由 `performance.py`
   按角色和段落能量塑形（人声不再是一条直线）。
6. `music_render` —— MIDI → WAV（鼓走 FluidR3，旋律乐器走 GeneralUser，吉他最后过真实箱体 IR）。
7. `music_vocals {mode:"sing", part:"lead"}` —— 肥鱼按谱唱歌（需 SVS+RVC）；和声轨用 `part:"harmony"`。
   CPU 上约 5–6 分钟 / 20 秒分段，长歌先 `{wait:false}` 拿 `jobId`，之后 `{jobId:"…"}` 续查，别一直挂着调用。
8. `music_tune` —— 调教：加滑音/颤音/句尾/力度/气口，再走真实感混音链。先 `style:"natural"` 听 A/B，
   再按歌调整；`mix:false` 只看调教干声。
9. `music_automation {op:"set", lane:{track, param, shape, points:[{bar, value}]}}` —— 需要「让某个东西
   在歌里动起来」时先画曲线，再混音：`gain`/`pan`/`lowpass`/`highpass`，`track:"master"` 是总线淡出。
   它是 FL 的 automation clip 那一套，按小节定位，改 tempo 后自动跟着走。
10. `music_notes` —— 钢琴卷帘：按**小节 + 拍**改音符（`get`/`add`/`set`/`remove`/`move`/`transpose`/
    `velocity`/`duration`/`quantize`/`humanize`，用 `sel` 选一批）。改完直接 `music_render` 就听得见。
    坐标不是秒，改 tempo 不会错位。
11. `music_patterns` —— Pattern / Playlist：`capture` 抓几小节乐句 → `place` 摆到 `startBar`、`repeat` 次 →
    `apply` 展开成整曲 MIDI。改配器不用重写整曲。
12. `music_sampler` —— 通道机架：`set` 注册采样（`mode:"pitch"` 当乐器弹、`"oneshot"` 当鼓/音效），
    `addTrack` 建轨，再用 `music_notes` 往里写音符。这条轨渲染时放你的采样，不走 SoundFont。
    素材在硬盘上时先用 `music_library` 搜，再 `use`（也可给 `libraryId` 直接当采样）。
13. `music_fx` —— 每轨效果链：`preset` 一键套（vocal-clean / keys-air / drum-punch / guitar-bite / bass-tight），
    或 `set` 自己写 `[{type:"eq", freq:3200, gainDb:2}]`。位置在音色处理之后、推子之前。
14. `music_bus` —— 混音台路由：`set` 建分组总线（鼓/贝斯一条、键盘一条），`route` 把轨送进去，
    在 bus 上挂压缩/饱和，推子一动整组跟着动；`send` 给单轨或总线设混响/延迟送出量；
    `auto:true` 让没挂链的轨按角色自动套预设（人声不套，已经过 vocal_chain）。
    干这步之前先听一遍：分组总线是「让鼓和贝斯咬在一起」用的，不是每首歌都必须有。
15. `music_mix` —— 粗混 + 指标；有参考曲就传 `reference:"参考曲.flac"` 做频段/响度匹配。
    混完用 `music_analyze {audio:".../mix-v1.wav", deep:true}` 再核一遍 LUFS/真峰值/削波与结构
    （装了 dsh-soundcheck 才有；`deepStems:"4s"` 加人声/鼓/贝斯占比，`asr` 可听写歌词候选）。
16. `music_preview` + `present` —— 给用户试听。
17. `music_export` —— DAW 工程包。
18. 用户 DAW 混完，`music_master {mix:".../mixdown.wav"}`。
19. 要发给别人看的作品方案/发行页：`music_design {project, style:"swiss", theme:"ikb"}`，
    产物 `design/<slug>-<style>-<theme>-plan.html`；需要 PDF 就用 Edge headless `--print-to-pdf`。

## 音效库（music_library）

不是每首歌都从 SoundFont 开始：拟音、环境声、鼓采样、电影感 braam 这类素材在硬盘上时，
先用 `music_library` 建索引（`roots action=add` → `scan`），搜索用音匣语法
（`枪声 #fx star:>=4 dur:<3s sr:>48k`，中文支持全拼/首字母），然后 `use` 直接挂成采样器轨
（短音效默认 `oneshot`，长的默认 `pitch`）。库只记路径、不搬文件；工程要自带素材时
`use {copy:true}` 或 `export`。`duplicates`/`export` 用于清理和交付。

## 听「正在放的声音」（music_listen）

用户说「我刚听到的那首」「就照着现在这首歌的劲来」时，别让他先导出文件：
`music_listen {seconds:8}` 直接采默认输出设备的回环（不挑播放器，也不需要内录权限），
回 BPM / 调性 / 响度；`grab:true` 留成 wav，路径在 `grabbed`，可喂给
`music_mix {reference}`、`music_sampler` 或 `music_remix {input}`。
注意这是**系统混音**：同时有会议、通知音或第二首歌在放时 BPM/调性会被污染，
先问一句「现在只有这首歌在放吗」，结果与 `music_analyze` 明显打架时以后者为准。
面板上那排「系统输出」频谱是同一路数（只在本机算，不落盘不上传）。

## 调教（music_tune）

SVS 只保证「音高、时值、字」；虚拟歌姬听起来像真人的部分——起音滑音、长音颤音、句尾下落、
一句里的力度拱形、换气声——必须在音频层做。**不要用混音去补这些**：混响和 EQ 只会把平的人声弄糊。

- 风格：`natural` 默认；`ballad` 慢歌（颤音/气口多、共振峰略厚）；`power` 强声（颤音少、压缩多）；
  `cute` 薄亮。`intensity` 0.2–1.4 控制「演」的程度，先 0.7。
- 方言/口音改不了；改的是演唱表情，不是咬字。咬字问题回 `lyrics/pinyin.txt`。
- 长音少于 0.30 s 不加颤音，0.30–0.65 s 只加浅颤（按长度缩深度）；句内间隔 < 0.22 s 不插气口，
  但**歌词换行处**会补：缝太小时先给上一句 80–150 ms 提前收声再吸气，不会硬切。
- SVS 有 50–150 ms 起唱延迟，`music_vocals` 已按谱面自动挪回，挪了多少看报告里的 `alignedMs`；
  自己导入人声时用 `align:true`，同样看 `alignedMs`。
- 混了人声之后 `report/mix-v1.json` 的 `timing.vocal.medianMs` 是起音离网格的中位距离（≤ 40 ms 算贴）；
  `offsetMs` 要配 `confidence` 看，低于 2 多半是锁错音符，别照它去 `vocal.nudgeMs`。
- `formant` 正数更薄更亮、负数更厚；一次改 0.2–0.5 半音就够，别当变声器用。
- 产物里优先给用户听 `vocal-ab-<tag>.wav`（原声 → 调教），再给 `vocal-tuned-<tag>.wav`。
- 指标在 `report/tune-<tag>.json`：`pitchBefore/pitchAfter.medianAbsCents`（音准）、`breaths`（气口数）、
  `lufsMixed`、`centroidDry/centroidMixed`（频谱重心，提亮是否过头）。

**不要承诺**：调教不能修跑调的原唱（那是修音）、不能换声线（那是 RVC）、不能把英文唱成中文。


## 作品方案页（music_design）

调教和混音都跑完后，如果用户要「能发出去的一页介绍/发行方案」，用 `music_design`，
不要自己手写 HTML 糊数据：页面里的曲目档案、音准前后、LUFS/真峰值、段落能量全部从
`report/tune-*.json`、`report/mix-*.json` 和 `project.json` 读，缺的响度指标从混音 WAV 补测。

- `style`：`swiss`（无衬线、网格、单一锚点色，适合正式发行/数据页）或 `magazine`（衬线、纸色，适合文案向）。
- `theme`：swiss 用 `ikb`（默认）/`lemon`/`lemon-green`/`safety-orange`；magazine 用 `ink-classic`/`indigo-porcelain`/`forest-ink`/`kraft-paper`/`dune`/`midnight-ink`。
- `deep:true` 会再跑一次 dsh-soundcheck 深分析（有就加，没有也能出页）。
- 页内的数据必须能被报告对上；找不到的字段宁可空着，不要编。
- 交付时给 HTML（浏览器可开），需要纸质/邮件附件再导 PDF；登记进歸藏作品库用 `guizang_library_add`。


## Remix 流程（用户给一首现成的歌）

1. `music_analyze {audio}` —— 先报 BPM / 调性 / 时长；BPM 明显不对就先问用户。
2. 用一句话确认方向：目标曲风、要不要改速度/调性、是否保留原唱。
3. `music_remix {input, mode, targetGenre, bpm?, key?, backend?}`：
   - `mode:"remix"` 换新伴奏、保留原唱（默认）
   - `mode:"rearrange"` 保留原伴奏、叠新鼓/贝斯
   - `mode:"cover"` 换声（需声音服务，不在就明说不可用）
4. 报告 `separation.backend`：`center` 是近似分离，必须向用户说明人声可能带伴奏残留；有 Demucs 时 auto 会自动用，但手机 CPU 很慢，赶时间可传 `backend:"center"`。
5. `music_preview` → `present` 给试听；确认后 `music_export {daw:"reaper"}`。

**不要承诺**：没 Demucs 时做不到「干净扒人声」；没 RVC 时做不到「换那个歌手的声线」。

## 试听点（必须停下来问用户）

- 编曲后：和声、节奏、结构对不对。
- 粗混后：平衡、人声位置、情绪。
- 母带后：响度、质感、有没有削波。

## 验收指标

这些数字**不用自己一条条对着看**：跑 `tools/music_gate.py "<工程目录>"`（离线，不需要引擎），
它会读 `project.json` / `midi/` / `report/*.json` 逐条给出实测值、门限、结论和「该回哪一阶段」。
门限表在 `PIPELINE.md` §4，两边同源；`—` 表示没数据，**不等于通过**。
峰值一律以落盘 WAV 为准——引擎在 `report/render-*.json` 里自报的 `peak` 会偏高（实测差 3 dB 以上）。

- 母带：目标 LUFS ±0.5，TP ≤ -1 dBTP，crest ≥ 7 dB（低于 7 说明被压平了，降 0.5–1 LU 再渲）。
  用 `music_analyze {deep:true}` 复核时以它的 `loudness.truePeakDb`/`clipped` 为准（真峰值，采样间也算）。
- 重复度：每轨「唯一小节占比」**只当参考，不设门限**。≈1.0 是每小节都在变（可能缺动机重复），
  ≈0.1 是整段照抄（lo-fi/极简的正常写法）；它只回答「像不像 loop」，不是质量分。
- 混音：无 clipping，stems 可闻，人声居中。
- 人声：时长与伴奏匹配，无静音段，峰值正常。深分析的 `vocalVerdict` 只有在 `deepStems` 分轨后
  才可信（未分轨时整混音上的人声判定会报错，工具会写 warning，别当结论）。
- 调教：`pitchAfter.medianAbsCents` ≤ 12；`breaths` 与句数同量级（句句有气口但不抢拍）；
  A/B 两段的整体响度差 ≤ 1 LU；`centroidMixed` 比 `centroidDry` 高 100–400 Hz 属正常提亮；
  对拍看 `alignedMs` 与 `timing.vocal.medianMs`（≤ 40 ms），人声峰值留 ≥ 1 dB 余量。
- 工程：同 seed 重跑结果一致；每步可单独重跑。
- 和声：看 `project.json` 的 `sections[].chords`——段落之间和弦走向应有 3 种以上差别，段段完全相同
  说明旋转/终止式没生效（`project.json` 里**没有** `harmony` 这个键）。
  力度唯一值（`uniq`）过少（人声 < 10、贝斯 < 15）说明演奏层没跑。

## DAW 导入

- **Audacity**：拖入 `stems/*.wav`；不能直接吃 MIDI；标签用 `session/audacity_labels.txt`；混完导出 48k/24bit。
- **Reaper**：`music_export {daw:"reaper"}` 会生成 `session/session.rpp`，直接打开即可（BPM/轨道/增益/声像已写好）。
- **Ardour / LMMS**：导入 WAV/MIDI。
- **其他**：按 `MIX_NOTES.md` 与 `IMPORT.md`。

## 洛天依（工程文件互通 + 语调教）

洛天依不是开源声库，插件不能替她发声；能做的是把「谱子 → 洛天依 → 干声 → 后期」接成闭环。
详见 `docs/luotianyi-bridge.md`（含 VvTalk 解剖与优缺点）。

- `music_tunefile {action:"export", format:"svp"}` —— 把 `midi/vocal.mid` + 歌词写成
  `<工程>/export/<名字>.svp`，在 Synthesizer V 里选洛天依 AI 开唱；`format:"vsqx"` 给 VOCALOID 4
  （打开后按一次「歌词/发音 转换」）。有 `report/tune-lead.json` 时会把音高曲线写进 `pitchDelta`。
- `music_vocals {mode:"import", source:"导出.wav", label:"洛天依"}` —— 把她唱好的干声导回工程
  （48k 单声道、自动对拍），之后照常 tune / yutiao / mix / master。
- `music_tunefile {action:"import", source:"改过的.svp"}` —— 在宿主里改完音符再读回来。
- `music_yutiao {dryRun:true}` 先看每字的声调标注（拼音 + 调号 + centsRange），确认无误再出音频；
  它按四声重写 F0（阴平平、阳平升、上声先降后升、去声降，句内降调、疑问句末上扬），
  压字尾、拉平字间响度，**时长与节奏跟原唱一致**。
- 验收看 `pitchAfter.medianAbsCents`（≤ 12 合格，3 左右是本次实测水平），A/B 两段响度差 ≤ 1 LU。
- 引擎是常驻进程：改完 `engine/music_engine/*.py` 必须重启引擎/DSH 才生效。

## 诚实边界

- 你听不到声音，只报告指标与波形；不说「听起来很好」。
- 唱歌需要 GPU + SVS + RVC；不可用时直说，给纯音乐/念白/MIDI 降级。
- 英文歌词不承诺演唱；第三方歌曲翻唱/Remix 提醒版权。
- 对标 FL Studio 的是 **数据与参数层**（自动化、Pattern、采样器、钢琴卷帘、效果链），不是界面：
  没有实时音频、没有 VST 宿主、没有可点击的 GUI。用户说「像 FL 那样拖一下」时，把操作翻译成
  对应工具的调用，别假装有界面。
