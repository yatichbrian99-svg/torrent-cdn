/**
 * Unit tests for the zero-trust client guards (lib/security.ts).
 *
 * These pin down the boundary validation added in the frontend audit:
 *  - file_ids arriving from embed URLs must be UUIDs before they become
 *    request path segments (blocks URL-normalization path confusion)
 *  - server-provided redirect targets must be absolute https:// URLs
 *    (blocks javascript:/data:/http: scheme abuse in window.location)
 *  - ?ref= referral codes are reduced to the backend's accepted alphabet
 */
import { describe, expect, it } from 'vitest'
import { isUuid, isSafeHttpsUrl, sanitizeRefCode } from '../security'

describe('isUuid', () => {
  it('accepts a well-formed UUID v4', () => {
    expect(isUuid('3fa85f64-5717-4562-b3fc-2c963f66afa6')).toBe(true)
  })

  it('accepts uppercase hex UUIDs', () => {
    expect(isUuid('3FA85F64-5717-4562-B3FC-2C963F66AFA6')).toBe(true)
  })

  it('rejects empty and missing values', () => {
    expect(isUuid('')).toBe(false)
    expect(isUuid(undefined as unknown as string)).toBe(false)
  })

  it('rejects path-confusion payloads that URL normalization could redirect', () => {
    expect(isUuid('x/../../admin/stats')).toBe(false)
    expect(isUuid('..%2F..%2Fadmin')).toBe(false)
    expect(isUuid('3fa85f64-5717-4562-b3fc-2c963f66afa6/../../admin')).toBe(false)
  })

  it('rejects look-alike non-UUIDs', () => {
    expect(isUuid('demo')).toBe(false)
    expect(isUuid('3fa85f6457174562b3fc2c963f66afa6')).toBe(false) // no dashes
    expect(isUuid('3fa85f64-5717-4562-b3fc-2c963f66afa6-extra')).toBe(false)
    expect(isUuid('3fa85f64_5717_4562_b3fc_2c963f66afa6')).toBe(false)
  })
})

describe('isSafeHttpsUrl', () => {
  it('accepts absolute https URLs', () => {
    expect(isSafeHttpsUrl('https://checkout.paystack.com/c/pay/xyz')).toBe(true)
  })

  it('rejects dangerous or non-https schemes', () => {
    expect(isSafeHttpsUrl('javascript:alert(1)')).toBe(false)
    expect(isSafeHttpsUrl('data:text/html,<script>alert(1)</script>')).toBe(false)
    expect(isSafeHttpsUrl('http://checkout.paystack.com/xyz')).toBe(false)
    expect(isSafeHttpsUrl('mailto:support@torrentcdn.com')).toBe(false)
  })

  it('rejects relative and malformed values', () => {
    expect(isSafeHttpsUrl('/dashboard')).toBe(false)
    expect(isSafeHttpsUrl('checkout.paystack.com/xyz')).toBe(false)
    expect(isSafeHttpsUrl('')).toBe(false)
    expect(isSafeHttpsUrl('not a url')).toBe(false)
  })
})

describe('sanitizeRefCode', () => {
  it('keeps alphanumeric referral codes intact', () => {
    expect(sanitizeRefCode('ABC123DEF')).toBe('ABC123DEF')
  })

  it('strips everything that is not alphanumeric', () => {
    expect(sanitizeRefCode('AB<C>12/3 D E$F')).toBe('ABC123DEF')
    expect(sanitizeRefCode('javascript:alert(1)')).toBe('javascriptalert1')
  })

  it('caps at the backend 20-char column', () => {
    expect(sanitizeRefCode('A'.repeat(50))).toHaveLength(20)
  })

  it('returns empty for empty/absent values', () => {
    expect(sanitizeRefCode('')).toBe('')
    expect(sanitizeRefCode('!!!')).toBe('')
  })
})
