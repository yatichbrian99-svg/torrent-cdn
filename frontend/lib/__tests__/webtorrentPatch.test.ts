/**
 * Regression tests for the patch-package fix applied to `uint8-util`'s browser
 * entry (see patches/uint8-util+2.3.2.patch).
 *
 * Context: webtorrent's lib/torrent.js calls `arr2hex(parsedTorrent.infoHash)`
 * where `infoHash` is already a hex string. Upstream `arr2hex = (data) =>
 * data.toHex()` then throws `TypeError: e.toHex is not a function` (strings have
 * no `.toHex()`), which killed the WebTorrent P2P path on every embed. The patch
 * makes `arr2hex` string-tolerant and removes the `Uint8Array.fromHex` (Chrome
 * 149+) fast path in `hex2arr`, so the helpers work in every browser.
 *
 * We import the patched file directly (rather than `uint8-util`) so this test
 * fails loudly if the patch ever stops being applied (e.g. an unpatched
 * install or a version bump that breaks the patch).
 */
import { describe, expect, it } from 'vitest'
import {
  arr2hex as arr2hexTyped,
  hash as hashTyped,
  hex2arr as hex2arrTyped,
} from '../../node_modules/uint8-util/dist/src/browser.js'

// The upstream .d.ts only types ByteArray input; the patch intentionally also
// accepts already-hex strings (webtorrent's misuse), so widen the types here to
// cover both cases the tests exercise.
const arr2hex = arr2hexTyped as unknown as (input: string | Uint8Array) => string
const hex2arr = hex2arrTyped as unknown as (input: string) => Uint8Array
const hash = hashTyped as unknown as (data: string | Uint8Array, format?: 'hex') => Promise<string>

const SHA1_OF_ABC = 'a9993e364706816aba3e25717850c26c9cd0d89d'

describe('uint8-util browser patch (webtorrent toHex fix)', () => {
  it('arr2hex passes an already-hex string through unchanged', () => {
    // Exactly what webtorrent does: arr2hex(parsedTorrent.infoHash)
    const infoHash = '371d8c7bfc4d9c8a10aced20c599cc99b258deeb'
    expect(arr2hex(infoHash)).toBe(infoHash)
  })

  it('arr2hex hex-encodes a Uint8Array without native toHex', () => {
    expect(arr2hex(new Uint8Array([0xab, 0xcd, 0x00, 0xff]))).toBe('abcd00ff')
    expect(arr2hex(new Uint8Array([0x12, 0x34]))).toBe('1234')
  })

  it('arr2hex hex-encodes a 40-byte infohash buffer (parse-torrent path)', () => {
    const bytes = new Uint8Array(20)
    for (let i = 0; i < 20; i++) bytes[i] = i
    const hex = arr2hex(bytes)
    expect(hex).toHaveLength(40)
    expect(hex).toBe(
      '000102030405060708090a0b0c0d0e0f10111213',
    )
    // Round-trip through hex2arr
    expect(hex2arr(hex)).toEqual(bytes)
  })

  it('hex2arr decodes long (>64 char) hex strings without Uint8Array.fromHex', () => {
    const longHex =
      '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' // 80 chars
    const out = hex2arr(longHex)
    expect(out).toHaveLength(40)
    expect(arr2hex(out)).toBe(longHex)
  })

  it.skipIf(!globalThis.crypto?.subtle)('hash(..., "hex") round-trips and matches known SHA-1 (native toHex absent)', async () => {
    // skipped when jsdom lacks WebCrypto — nothing to assert then
    const hex = await hash('abc', 'hex')
    expect(hex).toBe(SHA1_OF_ABC)
    // hash of a Uint8Array input too
    const hex2 = await hash(new Uint8Array([0x61, 0x62, 0x63]), 'hex')
    expect(hex2).toBe(SHA1_OF_ABC)
  })
})
