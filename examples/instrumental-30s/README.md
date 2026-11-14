# 纯器乐 30 秒 Demo

- 工程文件：`project.json`（schema 2）
- 速度 / 拍号：96 BPM，4/4
- 长度：12 小节 = **30.0 秒**
- 调性：A 小调
- 段落：intro 4 小节 → verse 4 小节 → outro 4 小节
- 轨：`drums`（FluidR3 鼓组）、`bass`、`keys`（电钢）、`pad`（弦乐 Pad）
- 人声：关闭

## 打开与渲染

```text
music_project op=open project="C:\Users\你\Music\dsh-music\instrumental-30s"
music_render project="C:\Users\你\Music\dsh-music\instrumental-30s"
```

## 可以练什么

1. 在 `music_notes track="keys"` 里改和弦排列，听 30 秒内和声密度怎么变。
2. 用 `music_fx` 给 `keys` 加 `chorus`，给 `pad` 加 `lowpass`，对比前后的空间感。
3. 用 `music_automation` 给 `pad` 画一条从第 1 小节到第 12 小节的 `gain` 渐强，再渲染。

> 这是纯器乐工程，不需要 9880 声音服务。渲染前确认 `scripts\doctor.ps1` 里 ffmpeg / fluidsynth / SoundFont 是 ✅。
