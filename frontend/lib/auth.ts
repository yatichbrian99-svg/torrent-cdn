const KEY = 'torrent_cdn_token'
let _token: string | null = null

export function saveToken(token: string) {
  _token = token
  if (typeof window !== 'undefined') {
    sessionStorage.setItem(KEY, token)
  }
}

export function getToken(): string | null {
  if (typeof window !== 'undefined') {
    return _token ?? sessionStorage.getItem(KEY)
  }
  return _token
}

export function removeToken() {
  _token = null
  if (typeof window !== 'undefined') {
    sessionStorage.removeItem(KEY)
  }
}

export function isLoggedIn(): boolean {
  return !!getToken()
}
