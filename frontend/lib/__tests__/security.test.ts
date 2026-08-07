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
import { isUuid, isSafeHttpsUrl, sanitizeRefCode, isScannerUserAgent } from '../security'

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

describe('isScannerUserAgent', () => {
  it('blocks known attack scanners (case-insensitive, with version suffixes)', () => {
    expect(isScannerUserAgent('sqlmap/1.7')).toBe(true)
    expect(isScannerUserAgent('nikto/2.5')).toBe(true)
    expect(isScannerUserAgent('Nmap Scripting Engine')).toBe(true)
    expect(isScannerUserAgent('Mozilla/5.0 (compatible; Nmap Scripting Engine; https://nmap.org)')).toBe(true)
    expect(isScannerUserAgent('Mozilla/5.0 (compatible; Acunetix WVS Security Scanner)')).toBe(true)
    expect(isScannerUserAgent('wfuzz/2.4.7')).toBe(true)
    expect(isScannerUserAgent('Mozilla/5.0 (compatible; Nuclei/3.2; https://nuclei.projectdiscovery.io)')).toBe(true)
  })

  it('does not block real browsers', () => {
    expect(isScannerUserAgent('Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36')).toBe(false)
    expect(isScannerUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Gecko/20100101 Firefox/128.0')).toBe(false)
    expect(isScannerUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36 Edg/125.0')).toBe(false)
  })

  it('does not block generic HTTP clients used by the repo probes and health checks', () => {
    expect(isScannerUserAgent('curl/8.6.0')).toBe(false)
    expect(isScannerUserAgent('python-requests/2.31.0')).toBe(false)
    expect(isScannerUserAgent('Go-http-client/1.1')).toBe(false)
    expect(isScannerUserAgent('Wget/1.21.4')).toBe(false)
  })

  it('word boundaries: look-alike UAs never trip the block', () => {
    expect(isScannerUserAgent('zapier/1.0')).toBe(false)
    expect(isScannerUserAgent('Nmaple/1.0')).toBe(false)
    expect(isScannerUserAgent('Snapchat/12.0')).toBe(false)
    expect(isScannerUserAgent('')).toBe(false)
    expect(isScannerUserAgent(undefined as unknown as string)).toBe(false)
  })
})
