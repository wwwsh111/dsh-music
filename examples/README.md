# 示例工程

三个可以直接打开的工程，都是 **schema 2** 的真实工程（`project.json` + `midi/*.mid`），不是截图或占位文件。打开后就能 `music_render`；`mix-practice` 还能直接 `music_mix`。

| 目录 | 内容 | 速度/长度 | 轨数 |
| --- | --- | --- | --- |
| `instrumental-30s` | 纯器乐 30 秒 Demo，无人声 | 96 BPM / 12 小节 / 30.0 秒 | 4（鼓、贝斯、电钢、Pad） |
| `beat-drums-bass-keys` | Boom Bap beat，只有鼓/贝斯/键盘 | 100 BPM / 8 小节 / 19.2 秒 | 3 |
| `mix-practice` | 六轨流行小样，练推子/声像/混响 | 110 BPM / 16 小节 / 34.9 秒 | 6（鼓、贝斯、电钢、左右吉他、Pad） |

## 怎么打开

工程可以放在任何地方，`music_project op=open` 接受绝对路径。推荐先复制到 DSH 的工程根，列表里就能看到：

```powershell
$src = "C:\你的安装目录\examples"
$dst = "$env:USERPROFILE\Music\dsh-music"
Copy-Item "$src\instrumental-30s" $dst -Recurse
Copy-Item "$src\beat-drums-bass-keys" $dst -Recurse
Copy-Item "$src\mix-practice" $dst -Recurse
```

然后在音乐模式里：

```text
music_project op=open project="C:\Users\你\Music\dsh-music\instrumental-30s"
```

也可以不复制，直接指到安装目录里的绝对路径。

## 打开之后

```text
music_render project="C:\Users\你\Music\dsh-music\instrumental-30s"
```

- 分轨会写到工程目录的 `audio/render-v1/`。
- `mix-practice` 在 render 之后可以继续：

```text
music_mix project="C:\Users\你\Music\dsh-music\mix-practice" preset="pop"
music_master project="C:\Users\你\Music\dsh-music\mix-practice" preset="pop" targetLufs=-10
```

## 注意

- 三个示例的 `vocal.enabled` 都是 `false`，不需要肥鱼声音服务；要做人声，先按安装指南启动 9880。
- 渲染需要 `ffmpeg`、`fluidsynth` 和 `FluidR3_GM.sf2`；缺什么先跑 `scripts\doctor.ps1`。
- 示例里的 MIDI 是真实音符，可以在 `music_notes` 里改，也可以直接换配器。
- 如果安装目录在 `C:\Program Files` 之类不可写的位置，先把整个示例目录复制到 `%USERPROFILE%\Music\dsh-music` 再渲染。
