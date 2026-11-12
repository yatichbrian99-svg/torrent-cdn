import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The console client imports the shared axios instance; mocking it keeps these
// tests about behaviour (flag on/off, formatting, base URLs) rather than HTTP.
const get = vi.fn()
vi.mock('./api', () => ({
  default: { get: (...args: unknown[]) => get(...args) },
  API_BASE: '',
}))

import {
  AI_CONSOLE_BASE,
  AI_OPENAI_BASE,
  aiFeatureEnabled,
  bytes,
  kes,
  perMtok,
  relativeAge,
  resetAiFeatureProbe,
  timestamp,
  usd,
} from './aiConsole'

beforeEach(() => {
  resetAiFeatureProbe()
  get.mockReset()
})

afterEach(() => {
  resetAiFeatureProbe()
})

describe('aiFeatureEnabled', () => {
  it('is true when the status endpoint answers with the flag on', async () => {
    get.mockResolvedValue({ data: { feature_flag: { on: true } } })
    await expect(aiFeatureEnabled()).resolves.toBe(true)
    expect(get).toHaveBeenCalledWith(`${AI_CONSOLE_BASE}/status`, expect.any(Object))
  })

  it('is false on a 404 — the flag-off deployment does not mount the route', async () => {
    get.mockRejectedValue({ response: { status: 404 } })
    await expect(aiFeatureEnabled()).resolves.toBe(false)
  })

  it('fails closed on a server error or a network failure', async () => {
    get.mockRejectedValueOnce({ response: { status: 500 } })
    await expect(aiFeatureEnabled()).resolves.toBe(false)
    resetAiFeatureProbe()
    get.mockRejectedValueOnce(new Error('network down'))
    await expect(aiFeatureEnabled()).resolves.toBe(false)
  })

  it('memoises the probe so a page does not re-ask on every render', async () => {
    get.mockResolvedValue({ data: { feature_flag: { on: true } } })
    await aiFeatureEnabled()
    await aiFeatureEnabled()
    expect(get).toHaveBeenCalledTimes(1)
  })
})

describe('base URLs', () => {
  it('points the console at the same-origin API prefix', () => {
    expect(AI_CONSOLE_BASE).toBe('/api/ai/v1/console')
  })

  it('points an OpenAI client at /v1, not at the console prefix', () => {
    expect(AI_OPENAI_BASE).toBe('/v1')
  })
})

describe('formatting', () => {
  it('renders credits as dollars and the KES line as an approximation', () => {
    expect(usd(5_000_000, 2)).toBe('$5.00')
    expect(kes(5_000_000, 130)).toContain('≈ KES')
  })

  it('renders per-million-token prices', () => {
    expect(perMtok(3_000_000)).toBe('$3.00 / Mtok')
  })

  it('never throws on a timestamp that is not epoch seconds', () => {
    // The regression that took the job page down: a datetime string must not
    // become an invalid date that throws a RangeError.
    expect(timestamp(1_700_000_000)).toMatch(/2023-11-1[45]/)
    expect(timestamp(null)).toBe('—')
    expect(timestamp(undefined)).toBe('—')
    expect(timestamp('not-a-date')).toBe('—')
    expect(timestamp('1700000000')).toMatch(/2023/)
  })

  it('describes heartbeat ages in plain words', () => {
    expect(relativeAge(5)).toBe('5s ago')
    expect(relativeAge(120)).toBe('2m ago')
    expect(relativeAge(7200)).toBe('2h ago')
    expect(relativeAge(null)).toBe('never')
  })

  it('formats model sizes', () => {
    expect(bytes(16 * 1024 ** 3)).toBe('16.0 GB')
    expect(bytes(512)).toBe('512 B')
  })
})
