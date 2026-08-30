const BASE = import.meta.env.VITE_API_BASE || ''   // 默认同源：云端/Mac本地通吃
export const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'   // 默认真API；显式设true才用mock

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { Authorization: `Bearer ${localStorage.getItem('api_token') || ''}`, ...init?.headers },
    ...init,
  })
  if (!r.ok) throw new Error(`${r.status} ${path}`)
  return r.json()
}

export const api = {
  get: <T>(p: string) => req<T>(p),
  post: <T>(p: string, body?: unknown) =>
    req<T>(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  put: <T>(p: string, body?: unknown) =>
    req<T>(p, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  del: <T>(p: string) => req<T>(p, { method: 'DELETE' }),
}
export { BASE }
