# 接洛天依：VvTalk 解剖 + dsh-music 的桥

这份文档记录两件事：

1. 对 **VvTalk 1.0.1**（用户给的那个"完全开源的洛天依软件"）的源码解剖与优缺点；
2. dsh-music 为此新增的接口：`music_tunefile`（工程文件互通）、`music_yutiao`（语调教）、
   `music_vocals mode=import`（外部干声导入）。

## 一、这是什么软件

| 项 | 值 |
| --- | --- |
| 名称 | **VvTalk**（旧名 TyTalk，汉语普通语语调教辅助工具） |
| 作者 | **GalaxieT**（GitHub `GalaxieT/VvTalk`；b 站空间 15114289） |
| 版本 | 1.0.1（`main.py` 里 `VERSION = '1.0.1'`） |
| 发布 | b 站专栏《VvTalk (TyTalk) 1.0.1 更新发布及开源说明》+ vsqx.top |
| 许可 | **CC BY-NC-SA 4.0**：署名、非商业、相同方式共享 |
| 依赖 | Python 3.8（conda）、lxml/jieba/pypinyin/pyworld/praat 相关包；**不需要 torch** |
| 体量 | 源码约 600 KB Python；另有 186 MB `misc_7z` 分卷（MFA/SPPAS/Praat/华为 SIS 与声学模型，**不是声库**） |

它不做"唱歌"，做的是 **语调教**：把一句话按普通话的声调与韵律，直接排成
**VOCALOID 的 .vsqx** 或 **Synthesizer V 的 .svp**，丢给洛天依（vsqx 里的歌手名
硬编码就是 `Luotianyi_CHN_Meng`），让她把这句话"说"出来。

## 二、架构解剖

```
main.py（tkinter GUI，multiprocessing.freeze_support，misc\temp\lock 防重复启动）
└── reliance.core.TalkCore          工程/设置 pickle 存档，子进程池 max_process=3
    └── ItemEditor                  append_item / run_pipeline / batch_imitate_pipeline / fast_auto
        ├── reliance.tyTalk.talker  ★ 规则韵律：文字 → 音高 / 时长 / 歌词
        │   ├── mark()      jieba（或百度 API）分词 + 词性 + pypinyin(TONE3) + 儿化标注
        │   └── vocalize()  四声调形、句内降调、疑问句末三字抬高、标点停顿、词内时长权重
        ├── reliance.imitation      ★ 真实录音 → 标注 → 音高/辅音长/力度（MFA、SPPAS、Praat、pyworld）
        └── reliance.output         ★ 写 .vsqx（X-SAMPA 音素）/ .svp（pitchDelta）/ SRT
```

三块职责分明，这是它设计上最值得学的地方：**规则生成**（tyTalk）与**真人模仿**
（imitation）解耦，两者都只产出"中间数据结构"（`note / pit / timing / 音素`），
再由 output 层写不同宿主格式。加一个新宿主，只需再写一个 writer。

关键参数（`talker.vocalize`）：四声核心调形
`1:[(0,.6),(1,.6)]  2:[(0,-.6),(1,.6)]  3:[(0,-.6),(.5,-1.5),(1,-.6)]  4:[(0,.8),(1,-1.5)]`，
`std_tone_range=3000`（cent 级），句内降调 0.15，疑问句末 3 字抬高（`goal_cover=3`），
基础字长 `230 ms / speed`，标点停顿 ×1/×2，轻声 ×0.6，儿化 ×1.3，句尾 ×1.05。

## 三、优点

1. **把"语调教"从手工活做成了流水线**：分词 → 声调调形 → 时长 → 停顿 → 写工程文件，
   一条命令出 vsqx/svp，比在 VOCALOID 里用音高铅笔一格一格画快几个数量级。
2. **完全可解释、可改参数**：全是专家规则（四声调形、降调、疑问上扬、词性权重），
   没有黑箱模型，不满意就改数字，这一点在"要控制力"的场景比 TTS 抽卡强。
3. **不依赖 GPU / 深度学习框架**：numpy + numba + pyworld + praat 就能跑，
   Win 笔记本/无显卡的机器照样用，装包量也远小于 GPT-SoVITS。
4. **双出口**：同时支持 VOCALOID 4（vsqx，含 X-SAMPA 音素表）与 Synthesizer V（svp，
   用 pitchDelta 参数曲线），前者是老洛天依用户的主场，后者是洛天依 AI 声库的入口。
5. **自带"模仿真实录音"的一条支线**：MFA/SPPAS 自动对齐 + Praat 取音高/强度 +
   `auto_get_consonant_len`，可以把真人或 TTS 的语调搬进工程，不只是纯规则生成。
6. **承认 bug**：`talker.py` 顶部注释直接写"vsqx 的时钟频率是 480 帧/拍，程序里认为每帧
   对应 1ms，是错误的"，这种自曝在学术/工程代码里很少见，接手成本因此低得多。

## 四、缺点（也是 dsh-music 没有照抄它的原因）

1. **纯规则，没有学习成分**：作者自己在路线图里写"方法：专家规则，之后考虑机器学习"，
   "焦点预测与正式度系统""语义定韵律边界"都还是空的。句重音只能靠词性 + 拍点猜，
   长句容易平。
2. **计时模型有已知错位**：上面那条 480 dots/拍 被当成 1 ms 的 bug 会整体缩放音高曲线
   的时间轴；好在它同时把 `ms_per_dot` 用在换算里，实际输出"能听"，但时间精度不干净。
3. **依赖重、可复现性差**：conda py3.8 + setup.py + 186 MB 模型分卷，还要外挂 MFA/SPPAS/
   Praat/华为 SIS；Windows 打包版能开箱用，从源码重建则相当劝退。
4. **接口层薄**：`get_pits_by_timing()` 之类的 API 是给内部用的，没有稳定的 CLI/HTTP；
   想把它的能力嵌进别的工具，基本只能 import 它的模块（于是被 CC BY-NC-SA 传染）。
5. **工程卫生一般**：`global_vars.py` 一堆模块级常量、1008 行的 `humanConverter.py`、
   拼音→X-SAMPA 词表硬编码在 writer 里；没有测试，靠人工点 GUI 验证。
6. **许可限制**：CC BY-NC-SA 4.0——不能商用，二次分发要同样许可。这也是这里
   **只按公开文件格式独立实现、不搬它的代码与词表**的原因。

## 五、dsh-music 这边的接法

洛天依不是开源声库，插件不能替她发声；能做的是把"谱子 → 洛天依 → 干声 → 后期"接成闭环：

```
music_compose / music_lyrics          写好旋律与歌词
        ↓
music_tunefile action=export          → 工程/xxx.svp（或 .vsqx）
        ↓                             在 Synthesizer V / VOCALOID 里选洛天依开唱
        ↓                             （想改语调就在宿主里改，或回来用 yutiao 的规则）
music_vocals mode=import              把她唱好的 wav 导回工程（48 kHz 单声道，自动对拍）
        ↓
music_yutiao / music_tune             语调教、演唱设计（WORLD 重合成，时长不变）
        ↓
music_tunefile action=import          若在宿主里改了音符，读回 .svp/.vsqx 继续
        ↓
music_mix / music_master / music_design / music_export
```

新增/改动的代码：

- `engine/music_engine/tunefile.py`（新）：`.svp` / `.vsqx` 读与写。音符取
  `analysis/vocal-align.json`（优先）或 `midi/vocal.mid`；svp 用真实音高写 note，
  有 `report/tune-lead.json` 时把音高曲线写进 `pitchDelta`（单位 cent）。
- `engine/music_engine/yutiao.py`（新）：语调教。按字的声调重写 F0 曲线
  （阴平平、阳平升、上声先降后升、去声降，句内降调、疑问句末上扬），
  再压字尾、拉平字间响度，WORLD 重合成，**时长与节奏跟原唱一致**。
- `engine/music_engine/vocals.py`：新增 `import_vocal()`（外部干声 → 工程），
  `_duration_of()` 改为先读 wav 头、再退回 ffmpeg stderr（本机没有 ffprobe）。
- `engine/music_engine/server.py`：新增 `/v1/yutiao`、`/v1/tunefile`，`/v1/vocals` 改成分派。
- `index.js`：新增 `music_tunefile` 工具；`music_vocals` 增加 `mode=import` 与
  `source` / `label` / `align` 参数。

## 六、实测（本机，2026-11）

- `music_yutiao` 跑 tune-test（20 字、2 句、16.0 s）：
  音高偏差 `medianAbsCents 67.0 → 3.2`，`p90 204 → 9.0`（对目标声调曲线），
  输出 `audio/vocal/vocal-yutiao-lead.wav`，LUFS -25.9，时长不变。
- `.svp` / `.vsqx` 导出后回读：20 个音符的 `start/dur/midi/歌词` 全部一致（svp 精确到
  blick，vsqx 有 0.4 ms 量化误差）；`import` 写回 midi + 歌词 + `vocal-align.json` 后再跑
  `music_yutiao` 通过，音高 `42.1 → 2.0` cent。
- `music_vocals mode=import` 导入 16.0 s 干声，报告 `engine: external`、`duration: 16.0`。

## 七、限制与注意事项

- **引擎是常驻进程**：改完 `engine/music_engine/*.py` 要重启引擎（或重启 DSH）才生效，
  否则跑的还是旧代码。
- 插件里没有洛天依声库，也不分发任何声库/模型；请用你合法持有的 VOCALOID 洛天依
  或 Synthesizer V 洛天依 AI 声库。
- `.vsqx` 导出把音素字段留空，VOCALOID 打开后按一次「歌词/发音 转换」即可；
  如果歌手名对不上，在界面里手动选洛天依。
- 引用的第三方资料：VvTalk（GalaxieT，CC BY-NC-SA 4.0）、洛叶《语调教教程》
  （luo-ling-yi.github.io/yutiao-tutorial）、@幻夜jyw 的语调教技术支持、
  洛天依 GPT-SoVITS v2pro 模型（只想干饭的麦克斯韦妖）。
