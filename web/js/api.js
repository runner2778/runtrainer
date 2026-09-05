/** window.pywebview.api 封装：统一信封处理 */
export async function call(name, ...args) {
  const res = await window.pywebview.api[name](...args);
  if (!res || !res.ok) throw new Error((res && res.error) || `调用 ${name} 失败`);
  return res.data;
}

/** 不抛异常的调用 */
export async function tryCall(name, ...args) {
  try {
    const data = await call(name, ...args);
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}
