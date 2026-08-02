import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Hoisted so the axios/auth mocks can share state with the tests.
const mocks = vi.hoisted(() => ({
  requestHandlers: [] as ((config: any) => any)[],
  responseHandlers: [] as { ok: (r: any) => any; err: (e: any) => any }[],
  getToken: vi.fn(),
  removeToken: vi.fn(),
}))

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => ({
      interceptors: {
        request: { use: (fn: any) => mocks.requestHandlers.push(fn) },
        response: { use: (ok: any, err: any) => mocks.responseHandlers.push({ ok, err }) },
      },
      get: vi.fn(),
      post: vi.fn(),
      delete: vi.fn(),
    })),
  },
}))

vi.mock('../auth', () => ({
  getToken: mocks.getToken,
  removeToken: mocks.removeToken,
}))

// Importing the module registers the interceptors via the mocked axios.create.
import '../api'

// Captured once — the arrays are populated at import time and must not be cleared.
const requestInterceptor = mocks.requestHandlers[0]
const responseInterceptor = mocks.responseHandlers[0]

describe('api client interceptors', () => {
  beforeEach(() => {
    mocks.getToken.mockReset()
    mocks.removeToken.mockReset()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  describe('request interceptor', () => {
    it('attaches the Bearer token when one is stored', () => {
      mocks.getToken.mockReturnValue('token-123')
      const config = { headers: {} }
      const result = requestInterceptor(config)
      expect(result.headers.Authorization).toBe('Bearer token-123')
    })

    it('leaves headers untouched when no token is stored', () => {
      mocks.getToken.mockReturnValue(null)
      const config = { headers: {} }
      const result = requestInterceptor(config)
      expect(result.headers.Authorization).toBeUndefined()
    })
  })

  describe('response interceptor', () => {
    it('passes through successful responses untouched', () => {
      const response = { data: { ok: true } }
      expect(responseInterceptor.ok(response)).toBe(response)
    })

    it('removes the token and redirects home on 401', async () => {
      // jsdom cannot navigate — stub location with a plain object.
      vi.stubGlobal('location', { href: '' })

      // The interceptor still rejects after doing its side effects — consume
      // the rejection so it isn't an unhandled rejection in the test run.
      await expect(responseInterceptor.err({ response: { status: 401 } })).rejects.toMatchObject({
        response: { status: 401 },
      })

      expect(mocks.removeToken).toHaveBeenCalledTimes(1)
      expect(window.location.href).toBe('/')
    })

    it('does not touch the token on non-401 errors', async () => {
      const error = { response: { status: 500 } }
      await expect(responseInterceptor.err(error)).rejects.toBe(error)
      expect(mocks.removeToken).not.toHaveBeenCalled()
    })

    it('does NOT redirect on 401 from /auth/login (wrong credentials, not expired session)', async () => {
      vi.stubGlobal('location', { href: '' })
      const error = { config: { url: '/auth/login' }, response: { status: 401 } }
      await expect(responseInterceptor.err(error)).rejects.toMatchObject(error)
      expect(mocks.removeToken).not.toHaveBeenCalled()
      expect(window.location.href).toBe('')
    })

    it('does NOT redirect on 401 from /auth/register', async () => {
      vi.stubGlobal('location', { href: '' })
      const error = { config: { url: '/auth/register' }, response: { status: 401 } }
      await expect(responseInterceptor.err(error)).rejects.toMatchObject(error)
      expect(mocks.removeToken).not.toHaveBeenCalled()
      expect(window.location.href).toBe('')
    })

    it('still redirects on 401 from authenticated endpoints (expired session)', async () => {
      vi.stubGlobal('location', { href: '' })
      const error = { config: { url: '/files' }, response: { status: 401 } }
      await expect(responseInterceptor.err(error)).rejects.toMatchObject(error)
      expect(mocks.removeToken).toHaveBeenCalledTimes(1)
      expect(window.location.href).toBe('/')
    })
  })
})
