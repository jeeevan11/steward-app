import { getTelegramInitData } from './telegram.js'

const SESSION_KEY = 'steward_jwt'

export async function authenticateWithTelegram(apiBase) {
  const initData = getTelegramInitData()
  if (!initData) return false
  try {
    const res = await fetch(`${apiBase}/miniapp/auth`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: initData }),
    })
    if (!res.ok) return false
    const { token } = await res.json()
    sessionStorage.setItem(SESSION_KEY, token)
    return true
  } catch {
    return false
  }
}

export function getStoredToken() {
  return sessionStorage.getItem(SESSION_KEY)
}

export function getAuthHeaders() {
  const token = getStoredToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}
