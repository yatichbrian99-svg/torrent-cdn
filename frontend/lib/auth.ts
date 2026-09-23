const KEY = 'torrent_cdn_token'
// Mirror of the backend's _REMEMBER_ME_MINUTES (14 days). When a remembered
// token passes this age the client drops it locally — a belt-and-braces match
// with the server-side JWT exp, which is the actual authority.
const REMEMBER_TTL_MS = 14 * 24 * 60 * 60 * 1000
const REMEMBER_UNTIL_KEY = 'torrent_cdn_token_expires_at'

let _token: string | null = null

export function saveToken(token: string) {
  _token = token
  if (typeof window !== 'undefined') {
    sessionStorage.setItem(KEY, token)
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
    if (typeof window !== 'undefined') {
      localStorage.removeItem(KEY)
      localStorage.removeItem(REMEMBER_UNTIL_KEY)
    }
    return
  }
  _token = token
  if (typeof window !== 'undefined') {
    const until = String(Date.now() + REMEMBER_TTL_MS)
    localStorage.setItem(KEY, token)
    localStorage.setItem(REMEMBER_UNTIL_KEY, until)
    sessionStorage.setItem(KEY, token)
  }
}

function readRemembered(): string | null {
  if (typeof window === 'undefined') return null
  const token = localStorage.getItem(KEY)
  if (!token) return null
  const until = Number(localStorage.getItem(REMEMBER_UNTIL_KEY) || 0)
  if (!until || Date.now() > until) {
    // Expired (or missing marker — treat as expired rather than immortal).
    localStorage.removeItem(KEY)
    localStorage.removeItem(REMEMBER_UNTIL_KEY)
    return null
  }
  return token
}

export function getToken(): string | null {
  if (typeof window !== 'undefined') {
    return _token ?? sessionStorage.getItem(KEY) ?? readRemembered()
  }
  return _token
}

export function removeToken() {
  _token = null
  if (typeof window !== 'undefined') {
    sessionStorage.removeItem(KEY)
    localStorage.removeItem(KEY)
    localStorage.removeItem(REMEMBER_UNTIL_KEY)
  }
}

export function isLoggedIn(): boolean {
  return !!getToken()
}
