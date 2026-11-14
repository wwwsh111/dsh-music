// dsh-music · apply() 集成测试（不需要宿主）：
//
//   node tools/test_apply.mjs
//
// 用 stub 顶替 @deepseek-ai/dsh-tools，真实跑 apply()，检查三种 Code Mode
// 下注册了哪些工具，并直接执行 music_code 串一次真引擎调用。

import assert from 'node:assert/strict'
import { register } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

register('./stub-loader.mjs', import.meta.url)
const here = path.dirname(fileURLToPath(import.meta.url))
const { apply } = await import(pathToFileURL(path.join(here, '..', 'index.js')).href)

function collect(config) {
  const registered = new Map()
  const ctx = { tools: { register: (t) => registered.set(t.name, t) } }
  apply(ctx, config)
  return registered
}

const native = collect({ codeMode: 'native', enginePort: 17890 })
assert.ok(native.has('music_code'), 'native 模式应该注册 music_code')
assert.ok(native.has('music_compose'), 'native 模式保留普通工具')
assert.ok(native.size >= 28, `native 模式工具数应 ≥ 28，实际 ${native.size}`)
console.log(`  ✓ native：${native.size} 个工具（含 music_code）`)

const code = native.get('music_code')
assert.ok(code.description.includes('music.tools()'), 'description 应带用法')
assert.ok(JSON.stringify(code.parameters).includes('"code"'), 'parameters 应含 code')

const compact = collect({ codeMode: 'compact' })
assert.deepEqual([...compact.keys()].sort(), ['music_code', 'music_doctor'])
console.log('  ✓ compact：只有 music_doctor + music_code')

const off = collect({ codeMode: 'off' })
assert.ok(!off.has('music_code') && off.has('music_compose'))
console.log(`  ✓ off：${off.size} 个普通工具，无 music_code`)

// 真引擎串联：先搜目录，再调工具，最后在程序里过滤结果。
const runId = await code.execute({
  code: `
    const hits = music.tools('曲风')
    const list = await music.genres({ op: 'list' })
    const projects = await music.project({ op: 'list' })
    return {
      matched: hits.map((t) => t.name),
      genres: Array.isArray(list) ? list.length : (list.genres?.length ?? Object.keys(list).length),
      projects: projects.projects?.length ?? projects.count ?? null,
    }
  `,
}, {})
assert.match(runId, /✔ music_code · 2 步/)
assert.match(runId, /genres/)
console.log('  ✓ 真引擎：music_code 串联 genres + project')

// 校验失败要带步骤号与可读原因。
const bad = await code.execute({ code: 'return await music.compose({})' })
assert.match(bad, /✖/)
assert.match(bad, /缺少必填参数 project/)
console.log('  ✓ 参数校验错误带工具名')

console.log('\napply() 集成测试通过')
process.exit(0)
