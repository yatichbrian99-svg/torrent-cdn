const KEY = 'torrent_cdn_token'
// Mirror of the backend's _REMEMBER_ME_MINUTES (14 days). When a remembered
// token passes this age the client drops it locally — a belt-and-braces match
// with the server-side JWT exp, which is the actual authority.
const REMEMBER_TTL_MS = 14 * 24 * 60 * 60 * 1000
const REMEMBER_UNTIL_KEY = 'torrent_cdn_token_expires_at'

let _token: string | null = null

/**
 * Storage access is always guarded.
 *
 * A browser can throw on `window.localStorage` itself (cookies/third-party
 * storage blocked, Safari private mode) — not just on setItem. An unguarded
 * read there rejects during hydration and takes the whole page down with the
 * error boundary, which is exactly the case the theme contract says must keep
 * working ("storage blocked → follow the system, toggle still works").
 */
function safeSession(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null
  } catch {
    return null
  }
}

function safeLocal(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

export function saveToken(token: string) {
  _token = token
  try {
    safeSession()?.setItem(KEY, token)
  } catch {
    // Storage unavailable — the in-memory token still carries this tab.
  }
}

/**
 * Persist the session across browser restarts ("Keep me logged in for 2
 * weeks"). The token is kept in localStorage with a stored expiry; sessionStorage
 * stays the default for normal logins so nothing outlives the tab unless the
 * user explicitly asked for it. saveRememberedToken(remember=false) is just
 * saveToken — see AuthModal.
 */
export function saveRememberedToken(token: string, remember: boolean) {
  if (!remember) {
    saveToken(token)
    try {
      const local = safeLocal()
      local?.removeItem(KEY)
      local?.removeItem(REMEMBER_UNTIL_KEY)
    } catch {
      // ignore
    }
    return
  }
  _token = token
  try {
    const until = String(Date.now() + REMEMBER_TTL_MS)
    safeLocal()?.setItem(KEY, token)
    safeLocal()?.setItem(REMEMBER_UNTIL_KEY, until)
    safeSession()?.setItem(KEY, token)
  } catch {
    // ignore — session-only fallback
  }
}

function readRemembered(): string | null {
  try {
    const local = safeLocal()
    if (!local) return null
    const token = local.getItem(KEY)
    if (!token) return null
    const until = Number(local.getItem(REMEMBER_UNTIL_KEY) || 0)
    if (!until || Date.now() > until) {
      // Expired (or missing marker — treat as expired rather than immortal).
      local.removeItem(KEY)
      local.removeItem(REMEMBER_UNTIL_KEY)
      return null
    }
    return token
  } catch {
    // Storage blocked — no remembered session exists.
    return null
  }
}

export function getToken(): string | null {
  if (typeof window !== 'undefined') {
    try {
      return _token ?? safeSession()?.getItem(KEY) ?? readRemembered()
    } catch {
      return _token
    }
  }
  return _token
}

export function removeToken() {
  _token = null
  try {
    safeSession()?.removeItem(KEY)
    safeLocal()?.removeItem(KEY)
    safeLocal()?.removeItem(REMEMBER_UNTIL_KEY)
  } catch {
    // ignore — nothing to clear
  }
}

export function isLoggedIn(): boolean {
  return !!getToken()
}
