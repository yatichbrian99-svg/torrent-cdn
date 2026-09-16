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
import api, { isTransientError, retryOnTransient } from '../api'

// The axios instance THIS import created. The production-routing describe below
// re-imports the module (vi.resetModules) to flip NODE_ENV, which reassigns
// mocks.instance to a fresh instance — the original api object keeps talking to
// this captured one, so tests that must stay on the original instance use it.
const devInstance = mocks.instance

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

  describe('transient-error helpers (page-load cold-start handling)', () => {
    it('isTransientError: true for proxy 502/503/504 and no-response failures', () => {
      expect(isTransientError({ response: { status: 502 } })).toBe(true)
      expect(isTransientError({ response: { status: 503 } })).toBe(true)
      expect(isTransientError({ response: { status: 504 } })).toBe(true)
      // A real axios timeout/network error carries request + code.
      expect(isTransientError({ code: 'ECONNABORTED', request: {} })).toBe(true)
      expect(isTransientError({ code: 'ERR_NETWORK', request: {} })).toBe(true)
    })

    it('isTransientError: false for 4xx, app-level 500, and locally-thrown errors', () => {
      // 401/403 are NOT transient — they mean dead session / not an admin and
      // must keep driving the re-login / access-denied UI paths.
      expect(isTransientError({ response: { status: 401 } })).toBe(false)
      expect(isTransientError({ response: { status: 403 } })).toBe(false)
      expect(isTransientError({ response: { status: 500 } })).toBe(false)
      expect(isTransientError({ response: { status: 429 } })).toBe(false)
      // A locally-thrown Error (e.g. a malformed-payload guard in a page) has
      // neither request nor code — retrying it can never succeed.
      expect(isTransientError(new Error('Malformed health payload'))).toBe(false)
      expect(isTransientError(new Error('timeout of 45000ms exceeded'))).toBe(false)
    })

    it('retryOnTransient: retries a cold-start 502 and succeeds on the next attempt', async () => {
      vi.useFakeTimers()
      let calls = 0
      const fn = vi.fn(async () => {
        calls++
        if (calls === 1) throw { response: { status: 502 } }
        return { plan: 'pro' }
      })

      const promise = retryOnTransient(fn)
      await vi.advanceTimersByTimeAsync(2000)
      const result = await promise

      expect(result).toEqual({ plan: 'pro' })
      expect(fn).toHaveBeenCalledTimes(2)
      vi.useRealTimers()
    })

    it('retryOnTransient: exhausts retries on repeated transients, then rejects', async () => {
      vi.useFakeTimers()
      const fn = vi.fn(async () => {
        throw { response: { status: 503 } }
      })

      const promise = retryOnTransient(fn)
      const assertion = expect(promise).rejects.toMatchObject({ response: { status: 503 } })
      // Escalating schedule 2+5+10+10+20+20 = 67s of retries after the first
      // failure — advance well past it.
      await vi.advanceTimersByTimeAsync(90000)
      await assertion
      // 1 initial + 6 scheduled retries
      expect(fn).toHaveBeenCalledTimes(7)
      vi.useRealTimers()
    })

    it('retryOnTransient: keeps retrying through a FULL Docker cold start (~50s) before success', async () => {
      vi.useFakeTimers()
      let calls = 0
      const fn = vi.fn(async () => {
        calls++
        // The backend answers 502 at the proxy until it finishes booting —
        // regression guard for the admin-dashboard 'failed to load while the
        // container was still starting' report.
        if (calls <= 5) throw { response: { status: 502 } }
        return { is_admin: true }
      })

      const promise = retryOnTransient(fn)
      await vi.advanceTimersByTimeAsync(50000)
      const result = await promise

      expect(result).toEqual({ is_admin: true })
      expect(fn).toHaveBeenCalledTimes(6)
      vi.useRealTimers()
    })

    it('retryOnTransient: never retries a 403 (admin denial) or 401 (dead session)', async () => {
      const fn403 = vi.fn(async () => { throw { response: { status: 403 } } })
      await expect(retryOnTransient(fn403)).rejects.toMatchObject({ response: { status: 403 } })
      expect(fn403).toHaveBeenCalledTimes(1)

      const fn401 = vi.fn(async () => { throw { response: { status: 401 } } })
      await expect(retryOnTransient(fn401)).rejects.toMatchObject({ response: { status: 401 } })
      expect(fn401).toHaveBeenCalledTimes(1)
    })
  })

  // Minimal fake XHR for the raw storage PUT — the presigned path deliberately
  // bypasses axios (the app JWT would break the SigV4 signature), so tests
  // drive the PUT through a stubbed global XMLHttpRequest.
  class FakeXHR {
    static instances: FakeXHR[] = []
    status = 200
    timeout = 0
    url = ''
    method = ''
    headers: Record<string, string> = {}
    upload = { onprogress: null as any }
    onload: any = null
    onerror: any = null
    ontimeout: any = null
    open(method: string, url: string) {
      this.method = method
      this.url = url
    }
    setRequestHeader(k: string, v: string) {
      this.headers[k] = v
    }
    send(_body: any) {
      FakeXHR.instances.push(this)
    }
    respond(status: number) {
      this.status = status
      this.onload?.()
    }
    fail() {
      this.onerror?.()
    }
  }

  describe('uploadFile — presigned direct-to-Supabase-Storage', () => {
    const file = () => new File(['fake mp4 bytes'], 'movie.mp4', { type: 'video/mp4' })
    const presignResponse = (overrides: any = {}) => ({
      upload_id: 'up-1',
      presigned_url: 'https://ref.supabase.co/storage/v1/s3/torrent-cdn-files/user/up-1/movie.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc',
      object_key: 'user/up-1/movie.mp4',
      content_type: 'video/mp4',
      expires_in: 900,
      result: null,
      ...overrides,
    })

    beforeEach(() => {
      mocks.instance.post.mockReset()
      FakeXHR.instances = []
      vi.stubGlobal('XMLHttpRequest', FakeXHR)
      vi.useFakeTimers()
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    it('presigns, PUTs straight to storage with the signed Content-Type, then finalizes', async () => {
      mocks.instance.post.mockImplementation(async (url: string) => {
        if (url === '/upload/presign') return { data: presignResponse() }
        if (url === '/upload/presign/finalize/up-1') return { data: { file_id: 'abc', filename: 'movie.mp4' } }
        throw new Error(`unexpected url ${url}`)
      })

      const promise = api.uploadFile(file())
      await vi.advanceTimersByTimeAsync(0)

      const xhr = FakeXHR.instances[0]
      expect(xhr).toBeTruthy()
      // The file bytes go straight to Supabase Storage — never through the
      // same-origin Vercel proxy or Render.
      expect(xhr.method).toBe('PUT')
      expect(xhr.url).toContain('supabase.co/storage/v1/s3')
      // ContentType is signed into the presigned URL — the PUT must echo it.
      expect(xhr.headers['Content-Type']).toBe('video/mp4')
      // The app JWT must NEVER leak into the storage PUT (axios interceptors
      // are bypassed on purpose — a Bearer header would break the signature).
      expect(xhr.headers.Authorization).toBeUndefined()

      xhr.respond(200)
      const result = await promise
      expect(result.file_id).toBe('abc')
      const urls: string[] = mocks.instance.post.mock.calls.map((c: any[]) => String(c[0]))
      expect(urls).toEqual(['/upload/presign', '/upload/presign/finalize/up-1'])
    })

    it('skips the PUT when the backend reports a completed upload (idempotency dedup)', async () => {
      mocks.instance.post.mockImplementation(async (url: string) => {
        if (url === '/upload/presign') {
          return {
            data: presignResponse({
              upload_id: null,
              presigned_url: '',
              result: { file_id: 'existing', filename: 'movie.mp4' },
            }),
          }
        }
        throw new Error(`unexpected url ${url}`)
      })

      const result = await api.uploadFile(file())
      expect(result.file_id).toBe('existing')
      expect(FakeXHR.instances).toHaveLength(0)
      const urls: string[] = mocks.instance.post.mock.calls.map((c: any[]) => String(c[0]))
      expect(urls).toEqual(['/upload/presign'])
    })

    it('retries the PUT once on a network failure, then succeeds', async () => {
      mocks.instance.post.mockImplementation(async (url: string) => {
        if (url === '/upload/presign') return { data: presignResponse() }
        if (url === '/upload/presign/finalize/up-1') return { data: { file_id: 'abc' } }
        throw new Error(`unexpected url ${url}`)
      })
      const onRetry = vi.fn()

      const promise = api.uploadFile(file(), { onRetry })
      await vi.advanceTimersByTimeAsync(0)
      FakeXHR.instances[0].fail()
      await vi.advanceTimersByTimeAsync(2000)
      await vi.advanceTimersByTimeAsync(0)

      expect(FakeXHR.instances).toHaveLength(2)
      FakeXHR.instances[1].respond(200)
      const result = await promise
      expect(result.file_id).toBe('abc')
      expect(onRetry).toHaveBeenCalledTimes(1)
    })

    it('falls back to chunked uploads through the proxy when the storage PUT keeps failing', async () => {
      mocks.instance.post.mockImplementation(async (url: string) => {
        if (url === '/upload/presign') return { data: presignResponse() }
        if (url === '/upload/session') return { data: { upload_id: 'sess-1', result: null } }
        if (url.startsWith('/upload/chunk/')) return { data: { received: 1, total: 2 } }
        if (url.startsWith('/upload/finalize/')) return { data: { file_id: 'abc', filename: 'movie.mp4' } }
        throw new Error(`unexpected url ${url}`)
      })
      const bigFile = new File([new Uint8Array(5 * 1024 * 1024)], 'movie.mp4', { type: 'video/mp4' })

      const promise = api.uploadFile(bigFile)
      await vi.advanceTimersByTimeAsync(0)
      FakeXHR.instances[0].fail()
      await vi.advanceTimersByTimeAsync(2000)
      await vi.advanceTimersByTimeAsync(0)
      FakeXHR.instances[1].fail() // PUT retry fails too → chunked fallback
      await vi.advanceTimersByTimeAsync(10000)

      const result = await promise
      expect(result.file_id).toBe('abc')
      const urls: string[] = mocks.instance.post.mock.calls.map((c: any[]) => String(c[0]))
      expect(urls).toContain('/upload/session')
      expect(urls.filter((u) => u.startsWith('/upload/chunk/'))).toHaveLength(2)
      expect(urls).toContain('/upload/finalize/sess-1')
    })

    it('falls back to chunked uploads when presign keeps failing transiently', async () => {
      mocks.instance.post.mockImplementation(async (url: string) => {
        if (url === '/upload/presign') throw new Error('Network Error')
        if (url === '/upload/session') return { data: { upload_id: 'sess-1', result: null } }
        if (url.startsWith('/upload/chunk/')) return { data: { received: 1, total: 2 } }
        if (url.startsWith('/upload/finalize/')) return { data: { file_id: 'abc' } }
        throw new Error(`unexpected url ${url}`)
      })
      const bigFile = new File([new Uint8Array(5 * 1024 * 1024)], 'movie.mp4', { type: 'video/mp4' })
      const onRetry = vi.fn()

      const promise = api.uploadFile(bigFile, { onRetry })
      await vi.advanceTimersByTimeAsync(10000)
      const result = await promise

      expect(result.file_id).toBe('abc')
      const urls: string[] = mocks.instance.post.mock.calls.map((c: any[]) => String(c[0]))
      // withTransientRetry: 2 extra attempts on /upload/presign, then chunked
      expect(urls.filter((u) => u === '/upload/presign')).toHaveLength(3)
      expect(urls).toContain('/upload/session')
      expect(onRetry).toHaveBeenCalled()
    })

    it('surfaces definitive 4xx rejections without falling back to chunked', async () => {
      mocks.instance.post.mockRejectedValueOnce({
        response: { status: 413, data: { detail: 'File too large. Max size is 1024MB' } },
      })

      await expect(api.uploadFile(file())).rejects.toMatchObject({ response: { status: 413 } })
      const urls: string[] = mocks.instance.post.mock.calls.map((c: any[]) => String(c[0]))
      expect(urls.filter((u) => u.startsWith('/upload/session'))).toHaveLength(0)
    })

    it('retries a chunk that fails transiently in the fallback path', async () => {
      let chunkCalls = 0
      mocks.instance.post.mockImplementation(async (url: string) => {
        if (url === '/upload/presign') throw new Error('Network Error')
        if (url === '/upload/session') return { data: { upload_id: 'sess-1', result: null } }
        if (url.startsWith('/upload/chunk/')) {
          chunkCalls++
          if (chunkCalls === 1) throw new Error('Network Error')
          return { data: { received: 1, total: 2 } }
        }
        if (url.startsWith('/upload/finalize/')) return { data: { file_id: 'abc' } }
        throw new Error(`unexpected url ${url}`)
      })
      const bigFile = new File([new Uint8Array(5 * 1024 * 1024)], 'movie.mp4', { type: 'video/mp4' })

      const promise = api.uploadFile(bigFile)
      await vi.advanceTimersByTimeAsync(10000)
      const result = await promise

      expect(result.file_id).toBe('abc')
      const chunkUrls: string[] = mocks.instance.post.mock.calls
        .map((c: any[]) => String(c[0]))
        .filter((u: string) => u.startsWith('/upload/chunk/'))
      // 2 distinct chunks + 1 retry of the first one
      expect(chunkUrls.length).toBe(3)
    })
  })

  describe('API keys (tcdn_)', () => {
    beforeEach(() => {
      mocks.instance.get.mockReset()
      mocks.instance.post.mockReset()
      mocks.instance.delete.mockReset()
    })

    it('getApiKey fetches key metadata (never the secret)', async () => {
      mocks.instance.get.mockResolvedValueOnce({
        data: { api_key: null, prefix: 'tcdn_abcd1234ef...', created_at: '2026-08-01T00:00:00Z', last_used_at: null },
      })
      const data = await api.getApiKey()
      expect(mocks.instance.get).toHaveBeenCalledWith('/auth/api-key')
      expect(data.api_key).toBeNull()
      expect(data.prefix).toBe('tcdn_abcd1234ef...')
    })

    it('createApiKey POSTs and returns the plaintext key once', async () => {
      mocks.instance.post.mockResolvedValueOnce({
        data: { api_key: 'tcdn_rotate_secret_xyz', prefix: 'tcdn_rotate_secret...', created_at: '2026-08-04T00:00:00Z' },
      })
      const data = await api.createApiKey()
      expect(mocks.instance.post).toHaveBeenCalledWith('/auth/api-key')
      expect(data.api_key).toBe('tcdn_rotate_secret_xyz')
    })

    it('revokeApiKey DELETEs the key', async () => {
      mocks.instance.delete.mockResolvedValueOnce({ data: { detail: 'API key revoked' } })
      const data = await api.revokeApiKey()
      expect(mocks.instance.delete).toHaveBeenCalledWith('/auth/api-key')
      expect(data.detail).toBe('API key revoked')
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

    it('retries once when the first attempt returns a bare 500, then succeeds', async () => {
      // A bare 500 (e.g. gunicorn timeout-kill or a proxy hiccup) has no JSON
      // detail the form could show — treat it like the other transient 5xxes
      // and retry once (login is idempotent).
      mocks.instance.post
        .mockRejectedValueOnce({ response: { status: 500, data: 'Internal Server Error' } })
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
      // Attach the rejection handler IMMEDIATELY — the retry rejects during
      // advanceTimersByTimeAsync below, and an unhandled rejection in that
      // window fails the run in CI even though the assertion later passes.
      const assertion = expect(promise).rejects.toMatchObject({ response: { status: 504 } })
      await vi.advanceTimersByTimeAsync(2000)
      await assertion
      expect(mocks.instance.post).toHaveBeenCalledTimes(2)
    })
  })
})

  describe('Paystack payouts (M-PESA transfers)', () => {
    beforeEach(() => {
      mocks.instance.get.mockReset()
      mocks.instance.post.mockReset()
      mocks.instance.delete.mockReset()
    })

    it('getPayoutBalance fetches the available balance', async () => {
      mocks.instance.get.mockResolvedValueOnce({
        data: { currency: 'KES', balance_cents: 150000, balance_kes: 1500.0 },
      })
      const data = await api.getPayoutBalance()
      expect(mocks.instance.get).toHaveBeenCalledWith('/billing/payouts/balance')
      expect(data.balance_kes).toBe(1500.0)
    })

    it('createPayoutRecipient POSTs the M-PESA details', async () => {
      mocks.instance.post.mockResolvedValueOnce({
        data: { recipient_code: 'RCP_abc', name: 'Jane M', active: true },
      })
      const data = await api.createPayoutRecipient({
        name: 'Jane M',
        account_number: '254712345678',
        description: 'Creator payout',
      })
      expect(mocks.instance.post).toHaveBeenCalledWith('/billing/payouts/recipients', {
        name: 'Jane M',
        account_number: '254712345678',
        description: 'Creator payout',
      })
      expect(data.recipient_code).toBe('RCP_abc')
    })

    it('initiatePayout POSTs the recipient + amount (whole KES)', async () => {
      mocks.instance.post.mockResolvedValueOnce({
        data: { transfer_code: 'TRF_123', status: 'pending', amount_kes: 500 },
      })
      const data = await api.initiatePayout({ recipient_code: 'RCP_abc', amount: 500, reason: 'test' })
      expect(mocks.instance.post).toHaveBeenCalledWith('/billing/payouts', {
        recipient_code: 'RCP_abc',
        amount: 500,
        reason: 'test',
      })
      expect(data.transfer_code).toBe('TRF_123')
    })

    it('initiatePayout forwards the idempotency key (Paystack reference)', async () => {
      mocks.instance.post.mockResolvedValueOnce({ data: { transfer_code: 'TRF_123' } })
      await api.initiatePayout({
        recipient_code: 'RCP_abc',
        amount: 500,
        idempotency_key: 'idem-1',
      })
      expect(mocks.instance.post).toHaveBeenCalledWith('/billing/payouts', {
        recipient_code: 'RCP_abc',
        amount: 500,
        idempotency_key: 'idem-1',
      })
    })

    it('deactivatePayoutRecipient DELETEs by recipient code', async () => {
      mocks.instance.delete.mockResolvedValueOnce({ data: { detail: 'Recipient deactivated' } })
      const data = await api.deactivatePayoutRecipient('RCP_abc')
      expect(mocks.instance.delete).toHaveBeenCalledWith('/billing/payouts/recipients/RCP_abc')
      expect(data.detail).toBe('Recipient deactivated')
    })

    it('fetchPayout GETs a transfer by code (status refresh)', async () => {
      mocks.instance.get.mockResolvedValueOnce({ data: { transfer_code: 'TRF_123', status: 'success' } })
      const data = await api.fetchPayout('TRF_123')
      expect(mocks.instance.get).toHaveBeenCalledWith('/billing/payouts/TRF_123')
      expect(data.status).toBe('success')
    })
  })

// The /auth/forgot-password and /auth/reset-password PAGES shadow the
// vercel.json /auth/* rewrite in production (afterFiles ordering), so the API
// calls must go through the /api/* rewrite instead. `isProduction` is baked at
// module load — the production case re-imports the module with NODE_ENV set,
// and asserts on the FRESH instance that re-import created.
describe('password reset API routing — page-shadowing fix', () => {
  it('routes forgot/reset through the /api rewrite in production', async () => {
    vi.resetModules()
    const prev = process.env.NODE_ENV
    ;(process.env as any).NODE_ENV = 'production'
    try {
      const prodApi = (await import('../api')).default
      const prodInstance = mocks.instance // the fresh instance from the re-import
      prodInstance.post.mockReset()
      prodInstance.post.mockResolvedValue({ data: { detail: 'ok' } })
      await prodApi.forgotPassword('user@example.com')
      await prodApi.resetPassword('reset-token-123', 'NewPass1')
      const urls: string[] = prodInstance.post.mock.calls.map((c: any[]) => String(c[0]))
      expect(urls).toEqual(['/api/auth/forgot-password', '/api/auth/reset-password'])
    } finally {
      ;(process.env as any).NODE_ENV = prev
    }
  })

  it('keeps plain /auth paths when calling the backend directly (dev/test)', async () => {
    devInstance.post.mockReset()
    devInstance.post.mockResolvedValue({ data: { detail: 'ok' } })
    await api.forgotPassword('user@example.com')
    await api.resetPassword('reset-token-123', 'NewPass1')
    const urls: string[] = devInstance.post.mock.calls.map((c: any[]) => String(c[0]))
    expect(urls).toEqual(['/auth/forgot-password', '/auth/reset-password'])
  })
})
