# 混音练习：六轨流行小样

- 工程文件：`project.json`（schema 2）
- 速度 / 拍号：110 BPM，4/4
- 长度：16 小节 = **34.9 秒**
- 调性：A 小调
- 段落：intro 4 小节 → chorus 8 小节 → outro 4 小节
- 轨：`drums`、`bass`、`keys`、`gtr_rhythm_l`、`gtr_rhythm_r`、`pad`
- 人声：关闭

每轨已经写了 `gainDb` / `pan` / `send`，但**故意留了混音空间**：左右吉他各偏 0.55，Pad 推得很低，鼓和贝斯在中间。先 render，再按下面的顺序练。

## 打开、渲染、混音

```text
music_project op=open project="C:\Users\你\Music\dsh-music\mix-practice"
music_render project="C:\Users\你\Music\dsh-music\mix-practice"
music_mix project="C:\Users\你\Music\dsh-music\mix-practice" preset="pop"
music_master project="C:\Users\你\Music\dsh-music\mix-practice" preset="pop" targetLufs=-10
```

## 练习顺序（建议）

1. **先只听鼓和贝斯**：把 `keys` / 吉他 / Pad 的 `gainDb` 拉到 -60，确认低频不打架。
2. **加电钢**：`keys` 放在 -10 dB 左右，pan -0.18；听它和吉他的中频是否挤。
3. **加左右吉他**：两把都是 `amp_rhythm`，pan ±0.55；把其中一把的 `send.reverb` 从 0.12 调到 0.2，听宽度变化。
4. **最后加 Pad**：`pad` 在 -17 dB，混响 0.32；只补空气，不抢人声位置。
5. **总线**：用 `music_bus op=layout bus={genre:"pop"}` 一键接鼓/贝斯/键盘/吉他到 `mix`，再在 `mix` 上做整体压缩。

## 可以练什么

```text
music_bus project="C:\Users\你\Music\dsh-music\mix-practice" op=list
music_fx project="C:\Users\你\Music\dsh-music\mix-practice" track="gtr_rhythm_l" preset="guitar-bite"
music_automation project="C:\Users\你\Music\dsh-music\mix-practice" op=set lane={track:"pad", param:"gain", points:[{bar:1,value:-24},{bar:9,value:-14}]}
```

> 这是练习工程，不是成品母带。render 出来的分轨在 `audio/render-v1/`；mix 在 `audio/mix/mix-v1.wav`。改完参数重新 render 再 mix，不要拿旧分轨反复叠效果。
