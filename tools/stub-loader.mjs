// 测试用：把 @deepseek-ai/dsh-tools 指到本地 stub（该包由宿主在运行时注入）。
export async function resolve(specifier, context, next) {
  if (specifier === '@deepseek-ai/dsh-tools') {
    return { url: new URL('./dsh-tools-stub.mjs', import.meta.url).href, shortCircuit: true }
  }
  return next(specifier, context)
}
