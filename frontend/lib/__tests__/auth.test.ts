import { describe, it, expect, beforeEach } from 'vitest'
import { saveToken, getToken, removeToken, isLoggedIn } from '../auth'

describe('auth token store', () => {
  beforeEach(() => {
    sessionStorage.clear()
    removeToken()
  })

  it('returns null when no token has been saved', () => {
    expect(getToken()).toBeNull()
    expect(isLoggedIn()).toBe(false)
  })

  it('saves and retrieves a token (memory + sessionStorage)', () => {
    saveToken('abc123')
    expect(getToken()).toBe('abc123')
    expect(isLoggedIn()).toBe(true)
    expect(sessionStorage.getItem('torrent_cdn_token')).toBe('abc123')
  })

  it('removes the token from memory and sessionStorage', () => {
    saveToken('abc123')
    removeToken()
    expect(getToken()).toBeNull()
    expect(isLoggedIn()).toBe(false)
    expect(sessionStorage.getItem('torrent_cdn_token')).toBeNull()
  })

  it('overwrites an existing token', () => {
    saveToken('first')
    saveToken('second')
    expect(getToken()).toBe('second')
  })
})
