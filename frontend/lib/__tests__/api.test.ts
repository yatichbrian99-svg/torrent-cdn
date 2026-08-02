import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Hoisted so the axios/auth mocks can share state with the tests.
const mocks = vi.hoisted(() => ({
  requestHandlers: [] as ((config: any) => any)[],
  responseHandlers: [] as { ok: (r: any) => any; err: (e: any) => any }[],
  getToken: vi.fn(),
  removeToken: vi.fn(),
  // The mocked axios instance is captured here so tests can drive its post()
  // (e.g. for the login retry-on-5xx behavior).
  instance: null as any,
}))

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => {
      mocks.instance = {
        interceptors: {
          request: { use: (fn: any) => mocks.requestHandlers.push(fn) },
          response: { use: (ok: any, err: any) => mocks.responseHandlers.push({ ok, err }) },
        },
        get: vi.fn(),
        post: vi.fn(),
        delete: vi.fn(),
      }
      return mocks.instance
    }),
  },
}))

vi.mock('../auth', () => ({
  getToken: mocks.getToken,
  removeToken: mocks.removeToken,
}))

// Importing the module registers the interceptors via the mocked axios.create,
// and gives us the api client to test the login retry behavior.
import api from '../api'

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

  describe('login retry on transient 5xx (Render cold start)', () => {
    beforeEach(() => {
      mocks.instance.post.mockReset()
      vi.useFakeTimers()
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    it('retries once when the first attempt 504s, then succeeds', async () => {
      mocks.instance.post
        .mockRejectedValueOnce({ response: { status: 504 } })
        .mockResolvedValueOnce({ data: { access_token: 't1', token_type: 'bearer' } })

      const promise = api.login('user@example.com', 'securePass123')
      await vi.advanceTimersByTimeAsync(2000)
      const result = await promise

      expect(mocks.instance.post).toHaveBeenCalledTimes(2)
      expect(result.access_token).toBe('t1')
    })

    it('retries once when the first attempt times out (no response), then succeeds', async () => {
      // A Render cold start more often surfaces as a timeout (axios aborts with
      // no response) than as a fast 5xx — login is idempotent, so retry once.
      mocks.instance.post
        .mockRejectedValueOnce(new Error('timeout of 45000ms exceeded'))
        .mockResolvedValueOnce({ data: { access_token: 't1', token_type: 'bearer' } })

      const promise = api.login('user@example.com', 'securePass123')
      await vi.advanceTimersByTimeAsync(2000)
      const result = await promise

      expect(mocks.instance.post).toHaveBeenCalledTimes(2)
      expect(result.access_token).toBe('t1')
    })

    it('passes through a 401 (wrong password) without retrying', async () => {
      mocks.instance.post.mockRejectedValueOnce({
        response: { status: 401, data: { detail: 'Invalid email or password' } },
      })

      await expect(api.login('user@example.com', 'wrongPass1')).rejects.toMatchObject({
        response: { status: 401 },
      })
      expect(mocks.instance.post).toHaveBeenCalledTimes(1)
    })

    it('passes through a 429 (login lockout) without retrying', async () => {
      mocks.instance.post.mockRejectedValueOnce({
        response: { status: 429, data: { detail: 'Too many login attempts' } },
      })

      await expect(api.login('user@example.com', 'wrongPass1')).rejects.toMatchObject({
        response: { status: 429 },
      })
      expect(mocks.instance.post).toHaveBeenCalledTimes(1)
    })

    it('surfaces the error if the retry also 504s', async () => {
      mocks.instance.post.mockRejectedValue({ response: { status: 504 } })

      const promise = api.login('user@example.com', 'securePass123')
      await vi.advanceTimersByTimeAsync(2000)
      await expect(promise).rejects.toMatchObject({ response: { status: 504 } })
      expect(mocks.instance.post).toHaveBeenCalledTimes(2)
    })
  })
})
