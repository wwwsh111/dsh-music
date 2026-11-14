// dsh-music —— 音乐模式插件：曲风、歌词、肥鱼人声、编曲/混音/Remix、DAW 导出。
//
// 设计：Node 侧只做「工具注册 + 引擎守护 + 体检」，重活全部交给本地 Python 引擎。
// 音频服务（肥鱼）是另一个进程，Node 只查 /health 与 /v1/capabilities。

import fs from 'node:fs'
import path from 'node:path'
import http from 'node:http'
import crypto from 'node:crypto'
import { spawn, execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { buildCodeMode } from './lib/code_mode.js'

export const name = 'dsh-music'
export const inject = ['tools']

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ENGINE_DIR = path.join(HERE, 'engine')
const GENRES_DIR = path.join(ENGINE_DIR, 'assets', 'genres')
const HOME = process.env.HOME || process.env.USERPROFILE || process.cwd()

const DEFAULTS = {
  enginePort: 17890,
  projectsRoot: '',
  venv: '',
  soundfont: '',
  voiceBaseUrl: 'http://127.0.0.1:9880',
  voiceApiKeyFile: '',
  defaultGenre: 'citypop',
  quality: 'standard',
  stems: 'auto',
  daw: 'generic',
  // Code Mode：off = 只注册普通工具；native = 普通工具 + music_code；compact = 只 music_doctor + music_code。
  codeMode: 'native',
  codeTimeoutMs: 120_000,
}

function exists(p) {
  try { fs.accessSync(p); return true } catch { return false }
}

function resolveConfig(raw = {}) {
  const cfg = { ...DEFAULTS, ...raw }
  cfg.projectsRoot = cfg.projectsRoot || (exists('/sdcard')
    ? '/sdcard/dsh/music'
    : path.join(HOME, 'Music', 'dsh-music'))
  cfg.venv = cfg.venv || path.join(HOME, '.local', 'share', 'dsh-music', 'venv')
  cfg.python = process.platform === 'win32'
    ? path.join(cfg.venv, 'Scripts', 'python.exe')
    : path.join(cfg.venv, 'bin', 'python')
  if (!cfg.soundfont) {
    const candidates = [
      '/usr/share/sounds/sf2/FluidR3_GM.sf2',
      '/usr/share/sounds/sf2/default-GM.sf2',
      path.join(HOME, 'soundfonts', 'FluidR3_GM.sf2'),
      path.join(HERE, 'engine', 'assets', 'soundfonts', 'FluidR3_GM.sf2'),
    ]
    cfg.soundfont = candidates.find(exists) || candidates[0]
  }
  if (!['off', 'native', 'compact'].includes(cfg.codeMode)) cfg.codeMode = 'native'
  cfg.codeTimeoutMs = Math.max(5_000, Math.min(Number(cfg.codeTimeoutMs) || 120_000, 600_000))
  cfg.tokenFile = path.join(HOME, '.dsh', 'music', 'token')
  cfg.logFile = path.join(HOME, '.dsh', 'music', 'engine.log')
  return cfg
}

let cfg = null
let token = ''

function ensureToken() {
  fs.mkdirSync(path.dirname(cfg.tokenFile), { recursive: true })
  if (!exists(cfg.tokenFile)) {
    fs.writeFileSync(cfg.tokenFile, crypto.randomBytes(24).toString('hex'), { mode: 0o600 })
  }
  token = fs.readFileSync(cfg.tokenFile, 'utf8').trim()
  return token
}

function httpJson(port, method, urlPath, body, opts = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : JSON.stringify(body)
    const req = http.request({
      host: '127.0.0.1',
      port,
      path: urlPath,
      method,
      headers: {
        ...(data ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) } : {}),
        ...(opts.auth === false ? {} : { authorization: `Bearer ${token}` }),
      },
      timeout: opts.timeoutMs || 15 * 60_000,
      signal: opts.signal,
    }, (res) => {
      let s = ''
      res.setEncoding('utf8')
      res.on('data', (d) => { s += d })
      res.on('end', () => {
        let parsed = null
        try { parsed = s ? JSON.parse(s) : null } catch { parsed = null }
        if (res.statusCode >= 400) {
          const msg = parsed?.error || parsed?.detail || s.slice(0, 500)
          reject(new Error(`engine HTTP ${res.statusCode}: ${msg}`))
        } else {
          resolve(parsed)
        }
      })
    })
    req.on('timeout', () => req.destroy(new Error('engine response timeout')))
    req.on('error', reject)
    if (data) req.write(data)
    req.end()
  })
}

async function pingEngine(timeoutMs = 1200) {
  try {
    const r = await httpJson(cfg.enginePort, 'GET', '/health', undefined, { auth: false, timeoutMs })
    return r?.status === 'ok'
  } catch {
    return false
  }
}

let starting = null

async function ensureEngine() {
  if (await pingEngine()) return
  if (starting) return starting
  starting = (async () => {
    if (!exists(cfg.python)) {
      throw new Error(`Python 解释器不存在：${cfg.python}。先运行 scripts/bootstrap.sh 安装引擎依赖。`)
    }
    fs.mkdirSync(path.dirname(cfg.logFile), { recursive: true })
    fs.mkdirSync(cfg.projectsRoot, { recursive: true })
    ensureToken()
    const fd = fs.openSync(cfg.logFile, 'a')
    const child = spawn(cfg.python, ['-m', 'music_engine.server', '--port', String(cfg.enginePort)], {
      cwd: ENGINE_DIR,
      detached: true,
      stdio: ['ignore', fd, fd],
      env: {
        ...process.env,
        MUSIC_TOKEN: token,
        MUSIC_ROOT: cfg.projectsRoot,
        MUSIC_SOUNDFONT: cfg.soundfont,
        MUSIC_CONFIG: JSON.stringify(cfg),
        PYTHONUNBUFFERED: '1',
      },
    })
    child.unref()
    fs.closeSync(fd)
    const deadline = Date.now() + 20_000
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 400))
      if (await pingEngine()) return
    }
    let tail = ''
    try { tail = fs.readFileSync(cfg.logFile, 'utf8').slice(-1500) } catch {}
    throw new Error(`音乐引擎启动失败（${cfg.python}）。日志尾部：\n${tail}`)
  })().finally(() => { starting = null })
  return starting
}

async function engineCall(endpoint, args, exec) {
  await ensureEngine()
  const res = await httpJson(cfg.enginePort, 'POST', endpoint, args, { signal: exec?.signal })
  if (res && res.ok === false) throw new Error(res.error || 'engine error')
  return res
}

function voiceKey() {
  if (!cfg.voiceApiKeyFile || !exists(cfg.voiceApiKeyFile)) return ''
  try { return fs.readFileSync(cfg.voiceApiKeyFile, 'utf8').trim() } catch { return '' }
}

async function voiceGet(urlPath, needAuth = true) {
  const key = needAuth ? voiceKey() : ''
  if (needAuth && !key) return { available: false, error: 'voiceApiKeyFile 未配置或读不到' }
  return new Promise((resolve) => {
    const u = new URL(urlPath, cfg.voiceBaseUrl)
    const req = http.request({
      host: u.hostname, port: u.port || 80, path: u.pathname + u.search, method: 'GET',
      headers: key ? { authorization: `Bearer ${key}` } : {},
      timeout: 4000,
    }, (res) => {
      let s = ''
      res.on('data', (d) => { s += d })
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(s) }) }
        catch { resolve({ status: res.statusCode, body: null, raw: s.slice(0, 300) }) }
      })
    })
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.on('error', (e) => resolve({ available: false, error: e.message }))
    req.end()
  })
}

function firstLine(cmd, args) {
  try {
    const out = execFileSync(cmd, args, { timeout: 4000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] })
    return out.split('\n')[0].trim()
  } catch {
    return null
  }
}

function diskFreeGB(p) {
  try {
    const s = fs.statfsSync(p)
    return Math.round((s.bavail * s.bsize) / 1e9)
  } catch {
    return null
  }
}

async function doctor() {
  const out = {
    engine: { ok: false },
    python: cfg.python,
    ffmpeg: firstLine('ffmpeg', ['-version']),
    fluidsynth: firstLine('fluidsynth', ['--version']),
    soundfont: { path: cfg.soundfont, exists: exists(cfg.soundfont) },
    voice: { available: false, reason: 'not checked' },
    projectsRoot: cfg.projectsRoot,
    diskFreeGB: diskFreeGB(cfg.projectsRoot),
  }
  try {
    await ensureEngine()
    out.engine = await httpJson(cfg.enginePort, 'GET', '/health', undefined, { auth: false })
    out.engine.ok = out.engine?.status === 'ok'
    // 引擎侧体检：可选组件（soundcheck/pyworld/demucs）的真实状态
    try {
      const det = await httpJson(cfg.enginePort, 'POST', '/v1/doctor', {}, { timeoutMs: 20_000 })
      if (det?.soundcheck) out.soundcheck = det.soundcheck
      if (det?.modules) out.modules = det.modules
      if (det?.separateBackend) out.separateBackend = det.separateBackend
    } catch (e) {
      out.engineDetail = e.message
    }
    // 系统回环采集（律动条 / music_listen 用的那条链路）
    try {
      const probe = await httpJson(cfg.enginePort, 'POST', '/v1/live/probe', {}, { timeoutMs: 10_000 })
      out.live = { ok: !!probe.ok, backend: probe.backend, device: probe.device,
        sampleRate: probe.sampleRate, error: probe.error || undefined }
      if (!probe.ok && probe.hint) out.live.hint = probe.hint
    } catch (e) {
      out.live = { ok: false, error: e.message }
    }
  } catch (e) {
    out.engine = { ok: false, error: e.message }
  }
  const health = await voiceGet('/health', false)
  if (health.available === false) {
    out.voice = { available: false, health: 'unreachable', error: health.error }
  } else {
    const caps = await voiceGet('/v1/capabilities', true)
    out.voice = {
      available: health.status === 200,
      health: health.body?.status || health.status,
      capabilities: caps.body || caps.error,
    }
  }
  return out
}

// Code Mode 的目录来源：tool() 每构造一个工具就登记一份实现，
// 程序里 music.<短名>() 调到的永远是同一份 fn。
let registry = []

function tool(name, description, parameters, fn) {
  registry.push({ name, description, parameters, fn })
  return defineTool({
    name,
    description,
    parameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      const value = await fn(args, exec)
      return typeof value === 'string' ? value : JSON.stringify(value)
    },
  })
}

export function apply(ctx, rawConfig) {
  cfg = resolveConfig(rawConfig || {})
  ensureToken()
  registry = []

  const tools = [
    tool('music_doctor', '音乐环境体检：引擎、ffmpeg/fluidsynth、SoundFont、肥鱼声音服务、磁盘。'
      + '装完插件或唱歌不可用时先调它。', {}, doctor),

    tool('music_genres', '列出/查看曲风。op=list 返回菜单；op=get 返回完整曲风包（和弦、鼓型、音色、结构）。',
      {
        op: { type: 'string', enum: ['list', 'get'], default: 'list' },
        id: { type: 'string', description: '曲风 id，如 citypop / lofi / guofeng' },
      },
      async ({ op = 'list', id }) => {
        if (op === 'get') {
          if (!id) throw new Error('op=get 需要 id')
          const p = path.join(GENRES_DIR, `${id}.json`)
          if (!exists(p)) throw new Error(`没有曲风 ${id}`)
          return JSON.parse(fs.readFileSync(p, 'utf8'))
        }
        const files = fs.readdirSync(GENRES_DIR).filter((f) => f.endsWith('.json'))
        return files.map((f) => {
          const g = JSON.parse(fs.readFileSync(path.join(GENRES_DIR, f), 'utf8'))
          return { id: g.id, name: g.name, bpm: g.bpm, vocal: g.vocal?.range, lufs: g.lufs }
        })
      }),

    tool('music_theory', '创作前先读乐理速查：和弦进行、配和弦、色彩、节奏、演奏真实感、'
      + '曲式、旋律、歌词、混音、曲风速查。不传 topic 返回目录；传 topic 返回该条，'
      + '可再传 section 只看某一组（如「终止式」）。',
      {
        topic: { type: 'string', description: '条目 id：chord_basics / progressions / harmonize / color / rhythm / performance / arrangement / melody / lyrics / mixing / genre_cheatsheet' },
        section: { type: 'string', description: '可选：只看该条里的某一组，如「终止式」' },
      },
      async (args) => engineCall('/v1/theory', args)),

    tool('music_project', '工程：create 建工程 / open 读取 / list 列表。建工程时需要曲风、BPM、调性、小节数。',
      {
        op: { type: 'string', enum: ['create', 'open', 'list'], default: 'create' },
        name: { type: 'string', description: '工程名/目录名' },
        genre: { type: 'string', description: '曲风 id' },
        bpm: { type: 'number' },
        key: { type: 'string', description: '如 F#m / Cmaj / Am' },
        bars: { type: 'integer', description: '总小节数' },
        project: { type: 'string', description: 'open 时的工程目录或 project.json' },
      },
      async (args) => engineCall('/v1/projects', args)),

    tool('music_lyrics', '歌词：set 写入并校验（中文字数/句长），validate 只校验。',
      {
        op: { type: 'string', enum: ['set', 'validate'], default: 'set' },
        project: { type: 'string', required: true },
        text: { type: 'string', description: '按段落换行的歌词；空行分段' },
        section: { type: 'string', description: '可选：只设置某段' },
      },
      async (args) => engineCall('/v1/lyrics', args)),

    tool('music_compose', '编曲：按曲风与结构生成伴奏 MIDI + 人声旋律 MIDI（歌词单独交给声音服务）。',
      {
        project: { type: 'string', required: true },
        brief: { type: 'string', description: '一句话风格/情绪；会改变演奏原型（如「大合唱 wide」→anthem，「hard punk 冲」→16 分墙 drive，「emo 慢 半拍」→half）' },
        seed: { type: 'integer', description: '同一 seed 结果可复现；换 seed 等于换一首歌的动机、鼓型与音色' },
        track: { type: 'string', description: '只重生成某一轨（drums/bass/keys/gtr_lead/gtr_rhythm/vocal）' },
      },
      async (args) => engineCall('/v1/compose', args)),

    tool('music_render', '渲染：MIDI → 每轨 WAV（鼓走 FluidR3，旋律乐器走 GeneralUser，吉他再过真实箱体 IR）。',
      { project: { type: 'string', required: true }, quality: { type: 'string' } },
      async (args) => engineCall('/v1/render', args)),

    tool('music_analyze', '分析音频：BPM、调性、段落、响度；可选分轨（需 Demucs/MSST）。'
      + 'deep:true 时调用本机 dsh-soundcheck 引擎做整曲深分析：LUFS/真峰值/削波计数、'
      + '结构切分、分轨能量与人声段落、和弦根音、起音密度；asr 可额外听写歌词候选（可选组件）。',
      {
        audio: { type: 'string', required: true },
        stems: { type: 'string', enum: ['off', 'vocals', 'auto'] },
        targetBpm: { type: 'number' }, targetKey: { type: 'string' },
        deep: { type: 'boolean', default: false,
          description: '用 dsh-soundcheck 做深分析；未安装时自动降级并在 warnings 里说明' },
        deepStems: { type: 'string', enum: ['off', '4s', '6s'], default: 'off',
          description: '深分析分轨：4s=人声/鼓/贝斯/其他，6s 再加吉他/钢琴（首次 60–90 秒）' },
        asr: { type: 'string', enum: ['off', 'tiny', 'base', 'small'],
          description: '深分析时听写歌词候选；需要分轨与人声证据' },
        start: { type: 'string', description: '只深分析某一段的起点，如 "1:30" 或 90' },
        end: { type: 'string', description: '只深分析某一段的终点' },
      },
      async (args) => engineCall('/v1/analyze', args)),

    tool('music_vocals', '人声：sing 按谱唱歌（肥鱼 SVS+RVC），speak 生成念白（TTS），'
      + 'import 把外部软件渲染好的干声导进工程（洛天依/ACE/Synthesizer V 都走这条路）。'
      + '唱中文时插件会把歌词转成带声调拼音并校验读音词表；CPU 上 20 秒分段要 5-6 分钟，'
      + '长歌先 wait:false 拿 jobId，再用同一 jobId 续查。',
      {
        mode: { type: 'string', enum: ['sing', 'speak', 'import'], default: 'sing' },
        project: { type: 'string' },
        section: { type: 'string', description: '只重渲染某段，如 chorus' },
        part: { type: 'string', enum: ['lead', 'harmony'], default: 'lead',
          description: 'lead 唱主旋律，harmony 唱和声轨（照 music_harmony 的编排；没有编排就先跑 music_harmony）' },
        jobId: { type: 'string', description: '续查已经在跑的人声任务' },
        wait: { type: 'boolean', default: true, description: 'false 只提交并立刻返回 jobId' },
        text: { type: 'string', description: 'speak 时的文本' },
        style: { type: 'string', enum: ['cute', 'gentle'] },
        voice: { type: 'string', description: '音色名（声音服务里注册的名字）；省略则用工程 vocal.voice，默认 feiyu' },
        source: { type: 'string', description: 'import 时填：外部渲染的干声文件绝对路径（wav/flac/mp3）' },
        label: { type: 'string', description: 'import 时填：音色标签，如 洛天依' },
        align: { type: 'boolean', default: true, description: 'import 时是否按谱面把起唱延迟挪回去' },
      },
      async (args) => engineCall('/v1/vocals', args)),

    tool('music_harmony', '和声编排（照 AI 翻唱里的「分和声」）：给主旋律按段落配和声，'
      + '写到 midi/vocal-harmony.mid 与 analysis/vocal-harmony-align.json，之后 music_vocals part=harmony 直接唱。'
      + '策略按调内音阶数音级：third_below / third_above / sixth_below / fifth_above / octave_below / double。'
      + 'cut 控制剪枝：default 剪掉太短、贴太近、超出音域、音程过宽的音，strict 只留和弦音，off 全留。'
      + 'density full/half/phrase 决定每个音都跟、隔一个跟、还是一句只跟一遍。'
      + 'op=plan 生成，show 看现有编排，clear 删掉。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', enum: ['plan', 'show', 'clear'], default: 'plan' },
        sections: { type: 'array', items: { type: 'string' },
          description: '只给这几段编；省略则自动挑副歌/预副歌/桥段/尾奏' },
        strategy: { type: 'string',
          enum: ['third_below', 'third_above', 'sixth_below', 'fifth_above', 'octave_below', 'double'],
          description: '全局策略；省略时按段落角色走默认（副歌低三度、桥段低六度）' },
        cut: { type: 'string', enum: ['off', 'default', 'strict'], default: 'default' },
        density: { type: 'string', enum: ['full', 'half', 'phrase'], default: 'full',
          description: '每个音都跟 / 隔一个跟 / 一句只跟一遍' },
        sing: { type: 'string', description: '顺带让声音服务唱一遍和声（音色名），省略则只写 MIDI' },
      },
      async (args) => engineCall('/v1/harmony', args)),

    tool('music_vocal_doctor', '人声体检与修补（照 AI 翻唱的「去哑音、补音、剪重叠」）：'
      + '逐音量起音延迟、音身电平、拖尾、与前后音的重叠/缺口，以及音高偏差；'
      + 'op=scan 只出报告，op=fix 写一份修好的 audio/vocal/vocal-repair-*.wav（拉音高、切残响、剪重叠、补缺口），'
      + 'op=suggest 只给「起音哑音」这种只能重唱的谱面建议（可 apply 回对齐文件）。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', enum: ['scan', 'fix', 'suggest'], default: 'scan' },
        section: { type: 'string', description: '只查某一段' },
        vocal: { type: 'string', description: '直接指定干声文件（wav），省略则找 audio/vocal/vocal-*.wav' },
        actions: { type: 'array', items: { type: 'string' },
          description: 'fix 时只做这几类：pitch / tail / cut / debleed / fill' },
        pitchCents: { type: 'number', default: 45 },
        pitchStrength: { type: 'number', default: 0.9 },
        rangeMin: { type: 'number', default: 50 }, rangeMax: { type: 'number', default: 86 },
        dryRun: { type: 'boolean', default: false, description: 'fix 时只报计划不写文件' },
        apply: { type: 'boolean', default: false, description: 'suggest 时把谱面建议写回 vocal-align.json' },
      },
      async (args) => engineCall('/v1/vocaldoctor', args)),

    tool('music_vocal_finish', 'S5 人声收尾（标准化编曲的最后一个环节，一步跑完）：'
      + '分和声 → 体检主唱 → 就地修补 → 复扫 → 起音哑音的重唱建议（可选再调教）。'
      + '它把 music_harmony 与 music_vocal_doctor 串起来，写 report/vocal-finish-<tag>.json，'
      + 'gate --stage S5 直接读这份报告。'
      + 'verdict=ok 全部干净；needs-fix 还有能补的没补上；needs-resing 起音哑音只能重唱。'
      + '调教一定放在修补之后——tune 会重新合成 F0，先调后修等于白修。',
      {
        project: { type: 'string', required: true },
        section: { type: 'string', description: '只收尾某一段' },
        harmony: { type: 'string', default: 'auto',
          description: 'auto 按段落角色排；off 不排；也可填 third_below 之类的策略名' },
        tune: { type: 'string', enum: ['natural', 'ballad', 'power', 'cute'],
          description: '顺带跑一遍调教；省略则只抄已有 report/tune-*.json 的音准读数' },
        fix: { type: 'boolean', default: true, description: 'false 只体检不写修补文件' },
        cut: { type: 'string', enum: ['off', 'default', 'strict'], default: 'default' },
        density: { type: 'string', enum: ['full', 'half', 'phrase'], default: 'full' },
        dryRun: { type: 'boolean', default: false, description: '只报计划，不写和声/修补/报告' },
      },
      async (args) => engineCall('/v1/vocalfinish', args)),

    tool('music_voicebanks', '声库识别：扫描目录，认出磁盘上有什么声库/换声模型、是什么格式、'
      + '本引擎能不能直接加载。只读，不解密、不复制、不改动文件。'
      + 'feiyu-rvc-release-v1 包会被标成可用；Vocaloid 的 .vdb/.ddb 与 Synthesizer V 的 .svpk '
      + '能被识别出来，但会如实标成「加载不了」——那是厂商私有格式 + 授权校验，不是缺转接件。',
      {
        paths: { type: 'array', items: { type: 'string' },
          description: '要扫描的目录或文件；省略则扫 D:\\DSH\\voicebanks' },
        hash: { type: 'boolean', default: false,
          description: '同时算每个文件的 sha256（大文件很慢）' },
        serviceConfig: { type: 'string', description: '声音服务配置；省略则自动找' },
      },
      async (args) => engineCall('/v1/voicebanks', args)),

    tool('music_automation', '自动化轨道：照 FL 的 automation clip，按小节画断点，让某个参数在歌里动起来。'
      + '参数支持 gain(dB)、pan(-1..1)、lowpass(Hz)、highpass(Hz)；track 写 master 就是总线推子。'
      + 'shape：linear 直线 / step 保持 / smooth S 曲线。'
      + 'op=list 查已有曲线，set 写入（同轨同参数覆盖旧的），clear 清掉。'
      + '混音时轨道曲线挂在推子之后、进总线之前；总线曲线挂在段落曲线之后、参考匹配之前。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', enum: ['list', 'set', 'clear'], default: 'list',
          description: 'list 查看 / set 写入 / clear 清空' },
        lane: { type: 'object', additionalProperties: true,
          description: 'set 时填：{track, param, shape, points:[{bar, value}]}；'
            + 'clear 时可只给 {track} 或 {track, param} 或 {id}' },
      },
      async (args) => engineCall('/v1/automation', args)),

    tool('music_notes', '钢琴卷帘：直接查看/改写某条轨的音符（按小节+拍的坐标，不是秒）。'
      + 'op=get 列出音符；add/set/remove/clear 增删；move 平移、transpose 移调、'
      + 'velocity 改力度、duration 改时值、quantize 量化到网格、humanize 加人性化抖动。'
      + 'sel 用来选一批音符：{indices:[...]} 或 {bar:9,toBar:16} 或 {pitch:60} 或 {minVelocity:110}。',
      {
        project: { type: 'string', required: true },
        track: { type: 'string', required: true, description: '轨 id，如 keys / drums / bass' },
        op: { type: 'string', default: 'get',
          enum: ['get', 'add', 'set', 'remove', 'clear', 'move', 'transpose', 'velocity',
            'duration', 'quantize', 'humanize'] },
        notes: { type: 'array', description: 'add/set：{pitch, bar, beat, dur, velocity}，bar/beat 从 1 数' },
        sel: { type: 'object', additionalProperties: true, description: '选音符：{indices} / {bar,toBar} / {pitch} / {minVelocity}' },
        barDelta: { type: 'number', description: 'move：平移几小节' },
        beatDelta: { type: 'number', description: 'move：平移几拍' },
        pitchDelta: { type: 'number', description: 'move：同时改音高' },
        semitones: { type: 'number', description: 'transpose：移几个半音' },
        value: { type: 'number', description: 'velocity/duration：直接设为该值' },
        scale: { type: 'number', description: 'velocity/duration：按比例缩放（1.2 = 放大 20%）' },
        grid: { type: 'number', description: 'quantize 的网格（以拍计，0.5=八分音符，默认 0.5）' },
        strength: { type: 'number', description: 'quantize 的吸附强度 0–1，默认 1' },
        durations: { type: 'boolean', description: 'quantize 时顺带对齐时值' },
        timingMs: { type: 'number', description: 'humanize 的时间抖动（毫秒，默认 6）' },
        velJitter: { type: 'number', description: 'humanize 的力度抖动（默认 10）' },
        seed: { type: 'integer' },
      },
      async (args) => engineCall('/v1/notes', args)),

    tool('music_patterns', 'Pattern / Playlist：照 FL 的做法，把几个小节的乐句抓成 pattern，'
      + '再摆到 playlist 上重复排列，最后 apply 一键展开成整曲的 MIDI。'
      + 'capture 抓取 → place 摆放（startBar/repeat）→ apply 展开；改配器时不用重写整曲。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', default: 'list',
          enum: ['list', 'capture', 'place', 'unplace', 'delete', 'apply'] },
        name: { type: 'string', description: 'capture 的 pattern 名（也是 id）' },
        startBar: { type: 'number', description: 'capture 起点 / place 摆放位置，从 1 数' },
        bars: { type: 'number', description: 'capture 抓几小节' },
        tracks: { type: 'array', items: { type: 'string' }, description: '只抓/只摆这几条轨' },
        pattern: { type: 'string', description: 'place/unplace/delete 的目标 pattern' },
        repeat: { type: 'integer', default: 1, description: 'place 时连摆几次' },
        index: { type: 'integer', description: 'unplace 时按 playlist 序号删' },
      },
      async (args) => engineCall('/v1/patterns', args)),

    tool('music_sampler', '通道机架 / 采样器：把自己导入的音频当乐器弹。'
      + 'set 注册一个采样（file/rootNote/mode），addTrack 给它建一条轨，'
      + '之后用 music_notes 往这条轨写音符，render 时就会用你的采样发音——'
      + 'mode=pitch 按音高变调（当乐器），mode=oneshot 不管音高整段播（鼓、音效）。'
      + '音效库里的素材也可以直接当采样：sampler 里写 {id, libraryId:"<12位库 id>", mode}，'
      + '或者 file:"lib:<库 id>"；库里的文件被移走时，render 会按索引重新找到它。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', default: 'list', enum: ['list', 'set', 'remove', 'addTrack'] },
        sampler: { type: 'object', additionalProperties: true,
          description: 'set：{id, file 或 libraryId, rootNote:60, mode:"pitch"|"oneshot", gainDb, attackMs, releaseMs}；'
            + 'addTrack 时也可以直接给采样 id 字符串' },
        id: { type: 'string', description: 'remove 的采样 id' },
        track: { type: 'string', description: 'addTrack 的新轨 id' },
      },
      async (args) => engineCall('/v1/sampler', args)),

    tool('music_library', '音效库：把硬盘上散落的音效/采样/拟音建成一个能秒搜、能试听、能直接进工程的库。'
      + '移植自 xy040402/SoundVault（音匣）的思路：不搬文件，只建索引（SQLite 元数据 + 240 点波形）。'
      + '流程：roots action=add 加目录 → scan 扫描（增量、有时间预算，可反复调）→ search 搜 →'
      + ' get 看波形/标签 → update 打星标/收藏/贴标签 → use 直接变成工程里的采样器轨。'
      + '搜索语法沿用音匣：#标签；star:>=4（或 star:>4 严格）；dur:<2s；sr:>48k；ch:2；type:wav；'
      + '几个词要同时满足。中文文件名支持全拼和首字母（qiangsheng / qs 都能命中「枪声」）。'
      + '收藏=favorites:true；最近=recent:true；按集合=collection:"名字"。',
      {
        op: { type: 'string', default: 'search',
          enum: ['state', 'stats', 'roots', 'scan', 'search', 'get', 'update', 'tags',
            'collections', 'saved', 'folders', 'duplicates', 'export', 'use', 'import',
            'reveal', 'settings'] },
        action: { type: 'string',
          description: '子动作：roots=add/remove/rename/list；tags=list/rename/delete；'
            + 'collections=list/add/remove; saved=list/add/delete; settings=get' },
        q: { type: 'string',
          description: '搜索表达式，如 `枪声 #fx star:>=4 dur:<3s type:wav`；也可只给普通词' },
        id: { type: 'string', description: '库内素材 id（get/update/use/reveal 用）' },
        ids: { type: 'array', items: { type: 'string' }, description: 'export 的素材 id 列表' },
        path: { type: 'string', description: '素材绝对路径（id 的另一种写法；roots add 时也可用）' },
        root: { type: 'string', description: '库根目录 id 或路径（scan 限定范围 / roots remove）' },
        name: { type: 'string', description: '标签名 / 集合名 / 根目录别名 / 导出文件名前缀' },
        new: { type: 'string', description: 'tags action=rename 时的新名字' },
        collection: { type: 'string', description: '只看某个集合' },
        tags: { type: 'array', items: { type: 'string' }, description: '只看带这些标签的素材' },
        favorites: { type: 'boolean', description: '只看收藏' },
        recent: { type: 'boolean', description: '按最近使用/加入排序' },
        limit: { type: 'integer', default: 60 },
        offset: { type: 'integer', default: 0 },
        sort: { type: 'string', default: 'relevance',
          enum: ['relevance', 'name', 'dur', 'dur_desc', 'sr', 'rating', 'recent', 'used', 'random'] },
        minRating: { type: 'integer', description: '最低星级 0–5' },
        rating: { type: 'integer', description: 'update：打星 0–5' },
        favorite: { type: 'boolean', description: 'update：收藏/取消' },
        tagsAdd: { type: 'array', items: { type: 'string' } },
        tagsRemove: { type: 'array', items: { type: 'string' } },
        tagsSet: { type: 'array', items: { type: 'string' }, description: '整组替换标签' },
        collectionsAdd: { type: 'array', items: { type: 'string' } },
        collectionsRemove: { type: 'array', items: { type: 'string' } },
        markUsed: { type: 'boolean', description: 'update：记一次使用（影响 recent/used 排序）' },
        dest: { type: 'string', description: 'export 的目标目录' },
        template: { type: 'string', default: '{n:00}_{name}',
          description: '导出命名模板：{n}{n:00}{n:000}{name}{index}{dur}{sr}{ch}{lib}' },
        keepStructure: { type: 'boolean', default: true, description: '导出时保留原目录结构' },
        csvName: { type: 'string', description: '导出清单 CSV 文件名' },
        includeMissing: { type: 'boolean', description: 'export：连已丢失的文件也列进清单' },
        deep: { type: 'boolean', description: 'duplicates：对同尺寸候选做内容哈希确认' },
        by: { type: 'string', default: 'content', enum: ['content', 'name'], description: 'duplicates 依据' },
        maxSeconds: { type: 'number', default: 20, description: 'scan 的时间预算（秒）' },
        scanNow: { type: 'boolean', description: 'import：导入后立刻扫一遍' },
        project: { type: 'string', description: 'use：目标工程（名字或路径，缺省用最近改过的工程）' },
        track: { type: 'string', description: 'use：轨 id（默认用素材名）' },
        samplerId: { type: 'string', description: 'use：采样器 id（默认用素材名）' },
        mode: { type: 'string', enum: ['pitch', 'oneshot'],
          description: 'use：pitch 当乐器按音高弹，oneshot 按下即整段播（默认按时长自动选）' },
        rootNote: { type: 'integer', default: 60, description: 'use：pitch 模式的根音 MIDI 号' },
        gainDb: { type: 'number', description: 'use：采样增益 dB' },
        copy: { type: 'boolean', default: false,
          description: 'use：true 时把音频复制进工程的 samples/，默认只引用库里的原文件' },
      },
      async (args) => {
        const op = args.op || 'search'
        const payload = { ...args }
        delete payload.op
        return engineCall('/v1/library/' + op, payload)
      }),

    tool('music_fx', '每轨效果链：照 DAW 的插入槽，一条轨按顺序挂一串效果，'
      + '位置在「预设音色处理之后、推子与声像之前」。'
      + '可用类型：gain / highpass / lowpass / eq / comp / saturate / chorus / delay / reverb / limiter / widen。'
      + 'op=list 看现有链子；set 整条替换；add 末尾加一级；clear 清空；'
      + 'preset 一键套：vocal-clean / keys-air / drum-punch / guitar-bite / bass-tight。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', default: 'list', enum: ['list', 'set', 'add', 'clear', 'preset'] },
        track: { type: 'string', description: '轨 id' },
        chain: { type: 'array',
          description: '效果数组，如 [{type:"highpass",freq:90},{type:"eq",freq:3200,gainDb:2.5,q:1}]' },
        preset: { type: 'string', description: 'preset 名' },
      },
      async (args) => engineCall('/v1/fx', args)),

    tool('music_bus', '混音台路由：照 FL 的 Send to —— 把几条轨送进一条分组总线，'
      + '在 bus 上挂效果、动推子，整组一起变；另外给单条轨设混响/延迟送出量。'
      + 'op=set 建/改一条 bus（id/gainDb/pan/fxChain/send），route 把某轨送进 bus（bus 传空串回主输出），'
      + 'send 设送出量（0–1），auto=true 打开「按角色自动套预设」，'
      + 'layout 一键按曲风接好两层总线（鼓/贝斯/键盘/吉他 → mix → 主输出），clear 全部拆掉。',
      {
        project: { type: 'string', required: true },
        op: { type: 'string', default: 'list', enum: ['list', 'set', 'remove', 'route', 'send', 'auto', 'layout', 'clear'] },
        bus: { oneOf: [{ type: 'string' }, { type: 'object', additionalProperties: true }],
          description: 'set：{id, gainDb, pan, fxChain, send, bus(父总线)}；route/remove 时给 id 或 {id}；layout 时可给 {genre}' },
        track: { type: 'string', description: 'route/send 的目标轨 id' },
        sends: { type: 'object', additionalProperties: true, description: 'send：{reverb: 0.25, delay: 0.1}，0–1' },
        auto: { type: 'boolean', description: 'auto：是否按角色自动套效果链预设' },
      },
      async (args) => engineCall('/v1/bus', args)),

    tool('music_tune', '调教：把 SVS 唱出来的「准音源」做成有演唱设计的虚拟歌手，再走真实感混音链。'
      + '按谱面加滑音/颤音/句尾下落/力度起伏/气口，用 WORLD 声码器重合成（不改时长），'
      + '然后 EQ 提亮 → 2A 类光电压缩 → 板式混响发送 → 左右加倍。'
      + '先 music_vocals，再 tune；需要 dry 纯调教结果时 mix:false。',
      {
        project: { type: 'string', required: true },
        section: { type: 'string', description: '只调教某一段，如 chorus；省略用主唱人声' },
        style: { type: 'string', enum: ['natural', 'ballad', 'power', 'cute'], default: 'natural',
          description: 'natural 自然 / ballad 抒情（颤音与气口更多）/ power 有力 / cute 偏薄偏亮' },
        intensity: { type: 'number', description: '调教强度 0.2–1.4，默认 0.7；越大越「演」' },
        breath: { type: 'number', description: '气口比例 0–1.5，覆盖风格默认值' },
        formant: { type: 'number', description: '共振峰偏移（半音），正=更薄更亮，负=更厚' },
        mix: { type: 'boolean', default: true, description: 'false 只输出调教干声，不挂真实感混音链' },
        ab: { type: 'boolean', default: true, description: '生成同段落 A/B 对照（原声→调教）' },
        seed: { type: 'integer' },
      },
      async (args) => engineCall('/v1/tune', args)),

    tool('music_yutiao', '语调教：让唱歌的人声开口说话。按字的汉语声调重写音高线'
      + '（阴平平、阳平升、上声先降后升、去声降，句末疑问上扬、句内降调），'
      + '并压缩字尾、拉平字间响度，用 WORLD 重合成 —— 时长、节奏跟原唱完全一致。'
      + '做法参考洛叶的语调教教程（ACE 抽参路线）与 @幻夜jyw 的技术支持。'
      + 'dryRun:true 只出计划表不渲染，先看声调标注对不对再出音频。',
      {
        project: { type: 'string', required: true },
        section: { type: 'string', description: '只处理某段，如 chorus；省略用主唱人声' },
        intensity: { type: 'number', description: '语气强度 0.2–1.4，默认 0.8；越大声调起伏越明显' },
        strength: { type: 'number', description: '改音高的力度 0–1，默认 0.95；低于 1 保留一点原唱旋律感' },
        even: { type: 'boolean', default: true, description: '拉平字间响度（念白没有渐强渐弱，默认开）' },
        ab: { type: 'boolean', default: true, description: '生成原唱→语调的 A/B 对照' },
        dryRun: { type: 'boolean', default: false, description: 'true 只写 analysis/yutiao-plan-*.json，不渲染' },
        seed: { type: 'integer' },
      },
      async (args) => engineCall('/v1/yutiao', args)),

    tool('music_tunefile', '与洛天依的工程文件互通。洛天依不是开源声库，本插件不能替她发声，'
      + '但可以把两头接上：action=export 把主旋律和歌词写成 Synthesizer V 的 .svp 或 '
      + 'VOCALOID 4 的 .vsqx，在那边选洛天依开唱；action=import 把那边改好的 '
      + '.svp/.vsqx 音符与歌词读回工程，之后 music_yutiao / music_tune / '
      + 'music_vocals mode=import / mix / master 都能接着用。',
      {
        project: { type: 'string', required: true },
        action: { type: 'string', enum: ['export', 'import'], default: 'export',
          description: 'export 写工程文件给洛天依唱；import 把工程文件的音符读回来' },
        format: { type: 'string', enum: ['svp', 'vsqx'], default: 'svp',
          description: 'export 时的格式：svp=Synthesizer V（推荐，洛天依 AI 声库），vsqx=VOCALOID 4' },
        source: { type: 'string', description: 'action=import 时必填：.svp/.vsqx 的绝对路径' },
        part: { type: 'string', enum: ['lead', 'harmony'], default: 'lead', description: '写主唱还是和声轨' },
        out: { type: 'string', description: 'export 时可指定输出路径；省略写 <工程>/export/<名字>.<格式>' },
      },
      async (args) => engineCall('/v1/tunefile', args)),

    tool('music_design', '作品方案：把一个工程做成能发出去的设计页（单文件 HTML），'
      + '版式与配色沿用 歸藏设计（dsh-guizang-design）的设计系统：瑞士风或杂志风、'
      + '一套主题 token、中文排版规则。内容全部来自工程里的真实报告：曲目档案、'
      + '调教前后音准、气口/乐句、混音 LUFS/真峰值/人声偏移、段落能量，并附 A/B 试听。'
      + '打开生成的 HTML 即可看，也可打印成 PDF；图片卡片和 PPT 再用 guizang 的 skill 出。',
      {
        project: { type: 'string', required: true },
        kind: { type: 'string', enum: ['release'], default: 'release',
          description: 'release=作品发布方案（当前只有这一种）' },
        style: { type: 'string', enum: ['swiss', 'magazine'], default: 'swiss',
          description: 'swiss 无衬线/直角/单一锚点色；magazine 衬线/纸色/杂志版式' },
        theme: { type: 'string',
          description: '主题 id：swiss 的 ikb/lemon/lemon-green/safety-orange，magazine 的 ink-classic/indigo-porcelain/forest-ink/kraft-paper/dune/midnight-ink；省略用该风格默认' },
        deep: { type: 'boolean', default: false,
          description: 'true 时再用 dsh-soundcheck 跑一次深分析，把真峰值/削波/分轨占比/结构变化补进方案' },
      },
      async (args) => engineCall('/v1/design', args)),

    tool('music_mix', '自动混音：工程分轨或裸 stems → 粗混 + 指标 + MIX_NOTES。',
      {
        project: { type: 'string' },
        stems: { type: 'object', additionalProperties: true, description: '{drums,bass,keys,lead,vocal}' },
        preset: { type: 'string' },
        reference: { type: 'string', description: '参考曲音频，按它的 6 段频段/响度做匹配' },
        targetLufs: { type: 'number' },
      },
      async (args) => engineCall('/v1/mix', args)),

    tool('music_master', '母带：目标 LUFS/true peak；也接受 DAW 导出的 mixdown。',
      {
        project: { type: 'string' },
        mix: { type: 'string' },
        targetLufs: { type: 'number' },
        truePeak: { type: 'number' },
        preset: { type: 'string' },
      },
      async (args) => engineCall('/v1/master', args)),

    tool('music_remix', '自动 Remix：分离人声 → 变速/变调对齐 → 换新伴奏 → 混音 → 母带。'
      + 'mode=remix 保原唱换伴奏；cover 换声（需肥鱼 RVC）；rearrange 保留原伴奏叠新鼓/贝斯。'
      + '不给 bpm/key 时沿用原曲，给了就整体变速变调。',
      {
        input: { type: 'string', required: true, description: '要 remix 的音频（mp3/wav/m4a 均可）' },
        mode: { type: 'string', enum: ['remix', 'cover', 'rearrange'], default: 'remix' },
        brief: { type: 'string', description: '一句话风格，用来猜目标曲风，如「citypop 复古」' },
        targetGenre: { type: 'string', description: '目标曲风 id；省略则从 brief 猜，默认 citypop' },
        bpm: { type: 'number', description: '目标 BPM；省略沿用原曲速度（避免人声拖影）' },
        key: { type: 'string', description: '目标调性如 F#m；省略沿用原调' },
        bars: { type: 'integer', description: '目标小节数；省略按时长自动算' },
        keep: { type: 'string', enum: ['vocals', 'instrumental', 'both'], description: '保留哪部分（rearrange 默认 both）' },
        output: { type: 'string', description: '工程名/输出目录名' },
        seed: { type: 'integer' },
        backend: { type: 'string', enum: ['auto', 'demucs', 'center'],
          description: '分离后端：auto 有 Demucs 就用，center 是快速近似' },
      },
      async (args) => engineCall('/v1/remix', args)),

    tool('music_export', '导出 DAW 工程包：stems、MIDI、标记、歌词、MIX_NOTES/IMPORT；daw=reaper 时额外生成可直接打开的 session.rpp。',
      {
        project: { type: 'string', required: true },
        daw: { type: 'string', enum: ['generic', 'reaper'], default: 'generic' },
        sampleRate: { type: 'integer' }, bitDepth: { type: 'integer' },
      },
      async (args) => engineCall('/v1/export', args)),

    tool('music_preview', '生成波形/段落图，返回可交给 present 的音频路径。',
      { project: { type: 'string' }, audio: { type: 'string' } },
      async (args) => engineCall('/v1/preview', args)),

    tool('music_listen', '听「系统正在放的那首」：直接采默认输出设备的回环，不用先有文件。'
      + '这是从 dsh-bgm 借来的链路 —— QQ音乐 / 网易云 / 浏览器 / 游戏都是同一条路，'
      + '不挑播放器，也不需要内录权限。返回这段的 BPM、调性、响度与是否真的有声音；'
      + 'grab:true 会把这几秒留成 wav，可以拿去当参考曲（music_mix reference）、'
      + '采样（music_sampler）或 remix 的输入（music_remix input）。'
      + '面板里的律动条走的是同一个采集（SSE /v1/panel/live/stream）。',
      {
        seconds: { type: 'number', default: 8,
          description: '抓几秒，1–30；太短测不准 BPM，八秒一般够' },
        grab: { type: 'boolean', default: false,
          description: 'true 时落一份 wav 到 ~/Music/dsh-music/live/' },
        out: { type: 'string', description: 'grab 时的目标 wav 路径' },
        analyze: { type: 'boolean', default: true,
          description: 'false 只报采集状态，不做 BPM/调性分析（快很多）' },
      },
      async (args) => {
        const r = await engineCall('/v1/live/snapshot', args)
        if (r && r.ok === false && r.hint) throw new Error(`${r.error} — ${r.hint}`)
        return r
      }),

    tool('music_job', '查询/等待/取消长任务。',
      { jobId: { type: 'string', required: true }, wait: { type: 'boolean' } },
      async ({ jobId }) => {
        await ensureEngine()
        return httpJson(cfg.enginePort, 'GET', `/v1/jobs/${encodeURIComponent(jobId)}`)
      }),
  ]

  const nativeTools = cfg.codeMode === 'compact'
    ? tools.filter((t) => t.name === 'music_doctor')
    : tools
  for (const t of nativeTools) ctx.tools.register(t)

  if (cfg.codeMode !== 'off') {
    const entries = registry.slice()
    const code = buildCodeMode({ entries, limits: { timeoutMs: cfg.codeTimeoutMs } })
    ctx.tools.register(tool(code.name, code.description, code.parameters, (args, exec) => code.run(args, exec)))
  }
  void cfg
}
