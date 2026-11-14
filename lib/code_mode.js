// dsh-music —— Code Mode 运行时。
//
// 一段 JS 程序在 vm 沙箱里跑，程序里直接用 music.<工具短名> 调本插件的工具：
//
//   const p = await music.project({ op: 'create', name: 'demo', genre: 'citypop' })
//   await music.compose({ project: p.name })
//   return await music.render({ project: p.name })
//
// 思路参照 MuWinds/dsh-codemode：工具目录不进模型上下文，程序内按需
// search / describe / call。区别在于这里不接 DSH 的 PTC 传输，而是复用插件
// 自己的工具实现（同一份 handler、同一个引擎），代码侧不会出现第二套行为。
//
// 沙箱只给数据类内建对象与 music SDK；没有 process / require / fs。
// vm 不是安全边界：这里用它隔离「程序结构」，不是隔离恶意代码。

import vm from 'node:vm'
import util from 'node:util'

const LIMITS = {
  timeoutMs: 120_000,
  maxTimeoutMs: 600_000,
  syncTimeoutMs: 15_000,
  maxOutputChars: 24_000,
  maxConsoleLines: 40,
  maxTraceRows: 60,
}

// 把插件里的 `{ project: { type: 'string', required: true } }` 这种属性表
// 转成标准 JSON Schema，供 describe 与参数校验用。
export function schemaOf(parameters = {}) {
  if (parameters && parameters.type === 'object' && parameters.properties) return parameters
  const properties = {}
  const required = []
  for (const [key, raw] of Object.entries(parameters || {})) {
    if (!raw || typeof raw !== 'object') continue
    const { required: isRequired, ...rest } = raw
    properties[key] = rest
    if (isRequired) required.push(key)
  }
  const schema = { type: 'object', properties }
  if (required.length) schema.required = required
  return schema
}

function clip(text, max) {
  const s = String(text ?? '')
  if (s.length <= max) return s
  const head = Math.floor(max * 0.7)
  const tail = max - head - 40
  return `${s.slice(0, head)}\n…（已截断 ${s.length - head - tail} 字）…\n${s.slice(-tail)}`
}

function oneLine(text, max = 110) {
  const s = String(text || '').replace(/\s+/g, ' ').trim()
  return s.length > max ? `${s.slice(0, max - 1)}…` : s
}

function shortOf(entry) {
  return entry.name.startsWith('music_') ? entry.name.slice('music_'.length) : entry.name
}

function briefArgs(args) {
  if (!args || typeof args !== 'object') return ''
  const parts = []
  for (const [key, value] of Object.entries(args)) {
    let shown
    if (typeof value === 'string') shown = JSON.stringify(value.length > 36 ? `${value.slice(0, 36)}…` : value)
    else if (Array.isArray(value)) shown = `[${value.length}]`
    else if (value && typeof value === 'object') shown = '{…}'
    else shown = String(value)
    parts.push(`${key}:${shown}`)
  }
  const joined = parts.join(', ')
  return joined.length > 160 ? `${joined.slice(0, 157)}…` : joined
}

function stringifyValue(value) {
  if (typeof value === 'string') return value
  if (value === undefined) return '（程序没有 return，返回值为 undefined）'
  const seen = new WeakSet()
  try {
    return JSON.stringify(value, (_key, val) => {
      if (typeof val === 'bigint') return val.toString()
      if (val && typeof val === 'object') {
        if (seen.has(val)) return '[循环引用]'
        seen.add(val)
      }
      if (typeof val === 'function') return '[function]'
      return val
    }, 2)
  } catch {
    return String(value)
  }
}

function typeOk(value, type) {
  switch (type) {
    case 'string': return typeof value === 'string'
    case 'number': return typeof value === 'number' && Number.isFinite(value)
    case 'integer': return Number.isInteger(value)
    case 'boolean': return typeof value === 'boolean'
    case 'array': return Array.isArray(value)
    case 'object': return value !== null && typeof value === 'object' && !Array.isArray(value)
    default: return true
  }
}

// 引擎返回的可能是已解析对象，也可能是 JSON 字符串；程序里希望两者一致。
function normalizeValue(value) {
  if (typeof value !== 'string') return value
  const trimmed = value.trim()
  if (!trimmed || (trimmed[0] !== '{' && trimmed[0] !== '[')) return value
  try { return JSON.parse(trimmed) } catch { return value }
}

export function buildCodeMode({ entries = [], limits = {}, runToolName = 'music_code' } = {}) {
  const cap = { ...LIMITS, ...limits }
  const byName = new Map()
  const shortToFull = new Map()

  for (const entry of entries) {
    if (!entry || !entry.name || typeof entry.fn !== 'function') continue
    if (byName.has(entry.name)) throw new Error(`重复的工具名：${entry.name}`)
    const short = shortOf(entry)
    if (shortToFull.has(short)) throw new Error(`工具短名冲突：${short}`)
    byName.set(entry.name, entry)
    shortToFull.set(short, entry.name)
  }

  const rows = entries
    .filter((entry) => byName.has(entry.name))
    .map((entry) => ({
      name: entry.name,
      call: `music.${shortOf(entry)}`,
      desc: oneLine(entry.description),
    }))

  function resolve(name) {
    const key = String(name || '').trim()
    if (!key) throw new Error('工具名不能为空')
    if (byName.has(key)) return byName.get(key)
    const full = shortToFull.get(key) || shortToFull.get(key.replace(/^music_/, ''))
    if (full) return byName.get(full)
    throw new Error(`没有工具「${key}」。先 music.tools('关键词') 看目录，或 music.tools() 看全部。`)
  }

  function search(query, limit = 12) {
    const q = String(query || '').trim().toLowerCase()
    const max = Math.max(1, Math.min(Number(limit) || 12, rows.length))
    if (!q) return rows.slice(0, max)
    const terms = q.split(/\s+/).filter(Boolean)
    const scored = []
    for (const row of rows) {
      const haystack = `${row.name} ${row.desc}`.toLowerCase()
      let score = 0
      for (const term of terms) {
        if (row.name.toLowerCase().includes(term)) score += 6
        if (row.desc.toLowerCase().includes(term)) score += 3
        if (haystack.includes(term)) score += 1
      }
      if (score > 0) scored.push({ row, score })
    }
    scored.sort((a, b) => b.score - a.score)
    return scored.slice(0, max).map((item) => item.row)
  }

  function describe(name) {
    const entry = resolve(name)
    return {
      name: entry.name,
      call: `music.${shortOf(entry)}`,
      description: entry.description,
      parameters: schemaOf(entry.parameters),
    }
  }

  function validate(entry, args) {
    const schema = schemaOf(entry.parameters)
    for (const key of schema.required || []) {
      if (args[key] === undefined || args[key] === null) {
        throw new Error(`${entry.name}: 缺少必填参数 ${key}`)
      }
    }
    for (const [key, value] of Object.entries(args)) {
      const spec = schema.properties[key]
      if (!spec || value === undefined) continue
      if (spec.type && !typeOk(value, spec.type)) {
        throw new Error(`${entry.name}: 参数 ${key} 需要 ${spec.type}，收到 ${Array.isArray(value) ? 'array' : typeof value}`)
      }
      if (Array.isArray(spec.enum) && !spec.enum.includes(value)) {
        throw new Error(`${entry.name}: 参数 ${key} 只能是 ${spec.enum.join(' / ')}，收到 ${JSON.stringify(value)}`)
      }
    }
  }

  const description = [
    'Code Mode：写一段 JavaScript 程序，一次串联多个音乐工具，中间结果不进上下文。',
    '程序里用 music.<工具短名>(参数) 或 music.call("music_xxx", 参数)；顶层支持 await，用 return 把结果交回来。',
    '',
    '发现与调用：',
    '  music.tools()              返回全部工具目录（name / 短调用名 / 一句话）',
    '  music.tools("混音")        按名字或说明搜',
    '  music.describe("music_mix")  完整参数（JSON Schema）',
    '  music.sleep(1000)          等任务；长任务用 wait:false 拿 jobId，再 music.job({ jobId }) 轮询',
    '',
    '例：',
    "  const p = await music.project({ op: 'create', name: 'demo', genre: 'citypop', bpm: 110, bars: 32 })",
    "  await music.compose({ project: 'demo' })",
    "  return await music.render({ project: 'demo' })",
    '',
    `可用工具（${rows.length}）：${rows.map((row) => row.name.replace(/^music_/, '')).join(' ')}`,
    '',
    '规则：',
    '- 同一工程上的写操作按顺序 await，不要 Promise.all（引擎按工程串行写）。',
    '- 只 return 需要判断的字段；分析/列表结果先在程序里过滤或取长度，别整份倒回来。',
    `- 默认 ${Math.round(cap.timeoutMs / 1000)} 秒超时，可用 timeoutMs 放宽到 ${Math.round(cap.maxTimeoutMs / 1000)} 秒。`,
    '- 报错会带步骤号与工具名；按提示改参数重跑，不要盲重试。',
    '- 只有普通 JS：没有 import/require/fs；路径都从工具参数传。',
  ].join('\n')

  const parameters = {
    code: { type: 'string', required: true, description: '要执行的 JavaScript 程序；支持顶层 await，用 return 返回结果' },
    timeoutMs: { type: 'integer', description: `整段程序的超时（毫秒），默认 ${cap.timeoutMs}，上限 ${cap.maxTimeoutMs}` },
  }

  async function run({ code, timeoutMs } = {}, exec) {
    if (typeof code !== 'string' || !code.trim()) {
      return '✖ music_code：code 不能为空。写一段 JS，用 return 交回结果。'
    }
    const budget = Math.max(200, Math.min(Number(timeoutMs) || cap.timeoutMs, cap.maxTimeoutMs))
    const state = {
      startedAt: Date.now(),
      deadline: Date.now() + budget,
      aborted: false,
      trace: [],
      logs: [],
      controller: new AbortController(),
    }

    const log = (...args) => {
      if (state.logs.length >= cap.maxConsoleLines) return
      const line = args.map((arg) => (typeof arg === 'string' ? arg : util.inspect(arg, { depth: 2, breakLength: 120 }))).join(' ')
      state.logs.push(clip(line, 600))
    }

    const abort = () => {
      if (state.aborted) return
      state.aborted = true
      state.controller.abort()
    }

    const sleep = (ms) => new Promise((resolve, reject) => {
      if (state.aborted) return reject(new Error('程序已超时或被取消'))
      const wait = Math.min(Math.max(Number(ms) || 0, 0), 600_000)
      const timer = setTimeout(() => { cleanup(); resolve(null) }, wait)
      timer.unref?.()
      const onAbort = () => { cleanup(); reject(new Error('程序已超时或被取消')) }
      const cleanup = () => {
        clearTimeout(timer)
        state.controller.signal.removeEventListener('abort', onAbort)
      }
      state.controller.signal.addEventListener('abort', onAbort, { once: true })
    })

    async function invoke(name, args) {
      if (state.aborted) throw new Error('程序已超时或被取消')
      const entry = resolve(name)
      if (args !== undefined && (args === null || typeof args !== 'object' || Array.isArray(args))) {
        throw new Error(`${entry.name}: 参数必须是对象，例如 { project: 'demo' }`)
      }
      const payload = args || {}
      validate(entry, payload)
      const startedAt = Date.now()
      try {
        const value = await entry.fn(payload, { signal: state.controller.signal })
        state.trace.push({ short: shortOf(entry), name: entry.name, args: payload, ok: true, ms: Date.now() - startedAt })
        return normalizeValue(value)
      } catch (error) {
        state.trace.push({ short: shortOf(entry), name: entry.name, args: payload, ok: false, ms: Date.now() - startedAt, error: oneLine(error?.message || error, 160) })
        throw error
      }
    }

    const base = {
      tools: (query, limit) => search(query, limit),
      search: (query, limit) => search(query, limit),
      describe,
      call: (name, args) => invoke(name, args),
      sleep,
      log,
      // 元信息：程序里可以按需读，不占上下文
      meta: { runToolName, toolCount: rows.length, timeoutMs: budget },
    }

    const api = new Proxy(base, {
      get(target, prop) {
        if (prop in target) return Reflect.get(target, prop)
        if (typeof prop !== 'string') return undefined
        const full = byName.has(prop) ? prop : shortToFull.get(prop)
        if (!full) {
          const hint = typeof prop === 'string' ? prop.replace(/^music_/, '') : String(prop)
          return () => {
            throw new Error(`没有工具「${hint}」。先 music.tools('关键词') 看目录，或 music.tools() 看全部。`)
          }
        }
        return (args) => invoke(full, args)
      },
      has(target, prop) {
        return prop in target || (typeof prop === 'string' && (byName.has(prop) || shortToFull.has(prop)))
      },
    })

    const sandbox = {
      music: api,
      console: { log, info: log, debug: log, warn: log, error: log },
      sleep,
      JSON, Math, Date, Promise, Number, String, Boolean, Array, Object, Map, Set,
      RegExp, Error, TypeError, RangeError, Intl, URL, URLSearchParams,
      TextEncoder, TextDecoder, structuredClone, setTimeout, clearTimeout,
    }
    const context = vm.createContext(sandbox, { name: 'dsh-music-code' })

    const timer = setTimeout(abort, budget)
    const onOuterAbort = () => abort()
    exec?.signal?.addEventListener?.('abort', onOuterAbort, { once: true })

    let value
    let failure = null
    try {
      const script = new vm.Script(`(async () => {\n${code}\n})()`, { filename: 'music-code.js' })
      const pending = script.runInContext(context, { timeout: cap.syncTimeoutMs, displayErrors: true })
      value = await Promise.race([
        pending,
        new Promise((_resolve, reject) => {
          state.controller.signal.addEventListener('abort', () => {
            reject(new Error(`程序超时（${Math.round(budget / 1000)} 秒）。长任务请用 wait:false + music.job 轮询，或调大 timeoutMs（≤ ${Math.round(cap.maxTimeoutMs / 1000)} 秒）。`))
          }, { once: true })
        }),
      ])
    } catch (error) {
      failure = error
      if (error?.code === 'ERR_SCRIPT_EXECUTION_TIMEOUT') {
        failure = new Error(`同步执行超过 ${Math.round(cap.syncTimeoutMs / 1000)} 秒（可能死循环）。`)
      }
    } finally {
      clearTimeout(timer)
      exec?.signal?.removeEventListener?.('abort', onOuterAbort)
    }

    const total = ((Date.now() - state.startedAt) / 1000).toFixed(1)
    const lines = []
    lines.push(failure
      ? `✖ ${runToolName} · ${state.trace.length} 步 · ${total}s`
      : `✔ ${runToolName} · ${state.trace.length} 步 · ${total}s`)
    for (const [index, row] of state.trace.slice(0, cap.maxTraceRows).entries()) {
      const args = briefArgs(row.args)
      lines.push(`${index + 1}. ${row.short}(${args}) ${row.ok ? '✓' : '✗'} ${(row.ms / 1000).toFixed(1)}s${row.error ? ` · ${row.error}` : ''}`)
    }
    if (state.trace.length > cap.maxTraceRows) lines.push(`…还有 ${state.trace.length - cap.maxTraceRows} 步`)
    if (!state.trace.length) lines.push('（程序没有调用任何工具）')
    if (state.logs.length) lines.push('—— 日志 ——', ...state.logs)
    if (failure) lines.push('—— 错误 ——', oneLine(failure?.message || failure, 2_000))
    else lines.push('—— 结果 ——', stringifyValue(value))
    return clip(lines.join('\n'), cap.maxOutputChars)
  }

  return { name: runToolName, description, parameters, run, catalog: rows }
}
