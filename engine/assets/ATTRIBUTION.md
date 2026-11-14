# 第三方素材与许可（engine/assets）

引擎自带的合成音色与处理链是自写的，但有两类素材来自第三方，许可见下。
插件本体（代码）是 MIT；下面这些素材各自的许可优先于 MIT。

## 1. 音源 `soundfonts/GeneralUser.sf2`

- 名称：GeneralUser GS 1.471
- 作者：S. Christian Collins
- 来源：<https://schristiancollins.com/generaluser.php>（本仓库通过
  `https://cdn.jsdelivr.net/npm/generaluser@1.47.1/GeneralUser.sf2` 取得）
- 许可：GeneralUser GS License v2.0（许可全文内嵌在 SF2 文件的 INFO/ICMT 块里，可用
  `fluidsynth` 或任意 SF2 工具读出）。要点：可用于私人或商业音乐作品、可修改、可随
  软件分发；再分发时不要直链作者官网的下载页。
- 用途：旋律乐器（吉他、贝斯、键盘、铺底）默认用它渲染——吉他采样 71 个，
  比 FluidR3_GM 的 22 个细得多。
- 鼓仍然用系统里的 FluidR3_GM.sf2（31 套鼓组、30 个 kick 采样），不随仓库分发。

## 2. 箱体脉冲响应 `ir/cab_*.wav`

- 名称：AK-SPKRS（AK Speaker Impulse Responses）
- 作者：Adventure Kid（<https://www.adventurekid.se/akrt/audio-impulses/>）
- 许可：**CC BY 4.0**（署名必须保留，见上）
- 原始文件：`ModernUk/AK-SPKRS_ModUk_002.wav`、`AK-SPKRS_006.wav`、`AK-SPKRS_007.wav`
- 处理：44100 Hz 立体声 → 48000 Hz 单声道、峰值对齐、截到 4096 taps、512 点尾窗、
  单位能量归一化。改造成这种形式是为了让 `numpy`/`scipy` 直接做 FFT 卷积
  （`oaconvolve`），不需要加载 VST。
- 用途：`dsp.guitar_amp` 的最后一级箱体。没有 IR 时才回退到合成的共振 + 低通近似。

## 3. 其它

- 肥鱼（feiyu）唱歌服务、声库与模型：见肥鱼声音服务仓库的条款，本仓库不包含任何声库权重。
- 用户自己导入的参考曲、人声、采样：版权归用户/原作者，插件只处理本地文件。
