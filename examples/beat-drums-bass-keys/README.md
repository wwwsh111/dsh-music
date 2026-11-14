# Boom Bap Beat（鼓 / 贝斯 / 键盘）

- 工程文件：`project.json`（schema 2）
- 速度 / 拍号：100 BPM，4/4
- 长度：8 小节 = **19.2 秒**
- 调性：A 小调
- 段落：intro 2 小节 → hook 4 小节 → outro 2 小节
- 轨：`drums`、`bass`、`keys`（Rhodes 电钢）
- 人声：关闭

## 打开与渲染

```text
music_project op=open project="C:\Users\你\Music\dsh-music\beat-drums-bass-keys"
music_render project="C:\Users\你\Music\dsh-music\beat-drums-bass-keys"
```

## 可以练什么

1. 用 `music_notes track="drums"` 把底鼓从「正拍」挪到「反拍」，听 groove 变化。
2. 用 `music_patterns` 把 2 小节抓成 pattern，再摆到第 3–6 小节，练「先抓再排」。
3. 用 `music_fx track="keys" preset="keys-air"` 给电钢加空气感，再用 `music_bus` 把鼓和贝斯送到 `mix` 总线。

> 19.2 秒很短，适合反复改一个 2 小节 loop。需要说唱人声时，先启动肥鱼声音服务再 `music_vocals`。
