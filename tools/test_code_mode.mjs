// dsh-music · Code Mode 测试
//
//   node tools/test_code_mode.mjs
//
// 前半段用假工具验证目录/搜索/校验/超时/输出裁剪，后半段若本机引擎在跑，
// 再用真实引擎端点跑一次串联调用（工程列表 + 曲风列表）。

import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { buildCodeMode, schemaOf } from '../lib/code_mode.js'

const tests = []
function test(name, fn) { tests.push({ name, fn }) }

const fakeEntries = [
  { name: 'music_doctor', description: '体检：引擎/声库/磁盘', parameters: {}, fn: async () => ({ ok: true }) },
  { name: 'music_genres', description: '曲风菜单', parameters: { op: { type: 'string', enum: ['list', 'get'], default: 'list' } }, fn: async (args) => ({ op: args.op || 'list' }) },
  { name: 'music_project', description: '工程：建/开/列', parameters: { op: { type: 'string', enum: ['create', 'open', 'list'] }, name: { type: 'string' } }, fn: async ({ op = 'create', name }) => ({ op, name: name || null }) },
  { name: 'music_compose', description: '编曲：伴奏 + 人声旋律', parameters: { project: { type: 'string', required: true } }, fn: async ({ project }) => ({ project, tracks: ['drums', 'bass', 'keys'] }) },
  { name: 'music_mix', description: '自动混音', parameters: { project: { type: 'string', required: true }, targetLufs: { type: 'number' } }, fn: async ({ project }) => JSON.stringify({ project, lufs: -14.2 }) },
  { name: 'music_render', description: '渲染 MIDI → WAV', parameters: { project: { type: 'string', required: true } }, fn: async ({ project }) => ({ project, wav: 'out.wav' }) },
]

const code = buildCodeMode({ entries: fakeEntries, limits: { syncTimeoutMs: 400 } })

test('catalogue 列出全部工具与短调用名', () => {
  assert.equal(code.catalog.length, 6)
  const rows = code.run({ code: 'return music.tools()' })
  return rows.then((out) => {
    assert.match(out, /music\.compose/)
    assert.match(out, /music_project/)
    assert.match(out, /0 步/)
  })
})

test('搜索中文与英文命中', async () => {
  const out = await code.run({ code: 'return music.tools("混音").map(t => t.name)' })
  assert.match(out, /music_mix/)
  const out2 = await code.run({ code: 'return music.tools("render").map(t => t.name)' })
  assert.match(out2, /music_render/)
})

test('describe 返回 JSON Schema 与必填项', async () => {
  const out = await code.run({ code: 'return music.describe("compose")' })
  assert.match(out, /"required"/)
  assert.match(out, /"project"/)
})

test('短名直接调用 + 结果返回', async () => {
  const out = await code.run({ code: 'return await music.compose({ project: "demo" })' })
  assert.match(out, /"tracks"/)
  assert.match(out, /✔ music_code/)
})

test('call 全名与 JSON 字符串自动解析', async () => {
  const out = await code.run({ code: 'const r = await music.call("music_mix", { project: "demo" }); return r.lufs' })
  assert.match(out, /-14\.2/)
})

test('参数校验：缺必填 / 枚举', async () => {
  const missing = await code.run({ code: 'return await music.compose({})' })
  assert.match(missing, /✖/)
  assert.match(missing, /缺少必填参数 project/)
  const badEnum = await code.run({ code: 'return await music.genres({ op: "nope" })' })
  assert.match(badEnum, /参数 op 只能是 list \/ get/)
})

test('未知工具给出可执行提示', async () => {
  const out = await code.run({ code: 'return await music.nothing({})' })
  assert.match(out, /没有工具「nothing」/)
})

test('console 日志进入输出但不污染返回值', async () => {
  const out = await code.run({ code: 'console.log("hello", 42); return 1' })
  assert.match(out, /—— 日志 ——/)
  assert.match(out, /hello 42/)
})

test('整体超时', async () => {
  const out = await code.run({ code: 'await music.sleep(5000); return 1', timeoutMs: 700 })
  assert.match(out, /✖/)
  assert.match(out, /程序超时/)
})

test('同步死循环被 vm 超时拦下', async () => {
  const out = await code.run({ code: 'while (true) {}' })
  assert.match(out, /同步执行超过/)
})

test('异常被格式化，保留步骤轨迹', async () => {
  const out = await code.run({ code: 'await music.compose({ project: "x" }); throw new Error("boom")' })
  assert.match(out, /错误 ——\nboom|boom/)
  assert.match(out, /1\. compose/)
})

test('长输出被裁剪', async () => {
  const long = await buildCodeMode({ entries: fakeEntries, limits: { maxOutputChars: 400 } })
    .run({ code: 'return "x".repeat(5000)' })
  assert.match(long, /已截断/)
  assert.ok(long.length < 1200, `输出仍过长：${long.length}`)
})

test('schemaOf 兼容完整 schema', () => {
  const full = { type: 'object', properties: { a: { type: 'string' } }, required: ['a'] }
  assert.deepEqual(schemaOf(full), full)
})

// —— 真实引擎（可选） ——
async function engineInfo() {
  const home = process.env.HOME || process.env.USERPROFILE || os.homedir()
  const stateFile = path.join(home, '.dsh', 'music', 'engine.json')
  const tokenFile = path.join(home, '.dsh', 'music', 'token')
  if (!fs.existsSync(tokenFile)) return null
  const token = fs.readFileSync(tokenFile, 'utf8').trim()
  const candidates = [17890]
  try { candidates.unshift(JSON.parse(fs.readFileSync(stateFile, 'utf8')).port) } catch {}
  for (const port of candidates.filter(Boolean)) {
    try {
      const health = await get(port, token, '/health')
      if (health?.status === 'ok') return { port, token, version: health.version }
    } catch {}
  }
  return null
}

function get(port, token, urlPath) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: urlPath, method: 'GET',
      headers: { authorization: `Bearer ${token}` }, timeout: 2500 }, (res) => {
      let s = ''
      res.on('data', (d) => { s += d })
      res.on('end', () => { try { resolve(JSON.parse(s)) } catch { reject(new Error(`HTTP ${res.statusCode}`)) } })
    })
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.on('error', reject)
    req.end()
  })
}

function post(port, token, urlPath, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body ?? {})
    const req = http.request({
      host: '127.0.0.1', port, path: urlPath, method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data), authorization: `Bearer ${token}` },
      timeout: 5000,
    }, (res) => {
      let s = ''
      res.on('data', (d) => { s += d })
      res.on('end', () => {
        let parsed = null
        try { parsed = s ? JSON.parse(s) : null } catch {}
        if (res.statusCode >= 400) reject(new Error(parsed?.error || `HTTP ${res.statusCode}`))
        else resolve(parsed)
      })
    })
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.on('error', reject)
    req.write(data)
    req.end()
  })
}

const info = await engineInfo()
if (info) {
  console.log(`（真实引擎测试：端口 ${info.port}，v${info.version}）`)
  const live = buildCodeMode({
    entries: [
      { name: 'music_theory', description: '乐理速查', parameters: { topic: { type: 'string' } },
        fn: (args) => post(info.port, info.token, '/v1/theory', args) },
      { name: 'music_project', description: '工程列表', parameters: { op: { type: 'string' } },
        fn: (args) => post(info.port, info.token, '/v1/projects', args) },
    ],
  })
  test('真实引擎：列曲风目录', async () => {
    const out = await live.run({ code: 'const r = await music.theory({}); return Array.isArray(r) ? r.length : Object.keys(r)' })
    assert.match(out, /✔ music_code/)
  })
  test('真实引擎：工程列表带步骤轨迹', async () => {
    const out = await live.run({ code: 'const r = await music.project({ op: "list" }); return { count: r.projects?.length ?? r.count ?? 0 }' })
    assert.match(out, /✔ music_code/)
  })
} else {
  console.log('（跳过真实引擎测试：没有 ~/.dsh/music/engine.json 或 token）')
}

let failed = 0
for (const { name, fn } of tests) {
  try {
    await fn()
    console.log(`  ✓ ${name}`)
  } catch (error) {
    failed += 1
    console.log(`  ✗ ${name}\n    ${error.message}`)
  }
}
console.log(failed ? `\n${failed}/${tests.length} 失败` : `\n${tests.length} 项全部通过`)
process.exit(failed ? 1 : 0)
