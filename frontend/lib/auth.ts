const KEY = 'torrent_cdn_token'

export function saveToken(token: string) {
  localStorage.setItem(KEY, token)
}

export function getToken(): string | null {
  return localStorage.getItem(KEY)
}

export function removeToken() {
  localStorage.removeItem(KEY)
}

export function isLoggedIn(): boolean {
  return !!getToken()
}
