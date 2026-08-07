/**
 * Unit tests for the browser auto-seeder (lib/seeder.ts).
 *
 * The critical guarantee these pin down: a browser can REPRODUCE the backend's
 * exact torrent infohash by seeding with the magnet's `dn` name + torrentool's
 * piece-length rule. The e2e/p2p-two-peer.mjs suite proves this end-to-end
 * against production; these unit tests lock in the helper logic (magnet
 * parsing + piece-length rule) that feeds the seeder.
 */
import { describe, expect, it } from 'vitest'
import { parseMagnet, torrentoolPieceLength } from '../seeder'

describe('parseMagnet', () => {
  it('extracts infohash from xt=urn:btih:', () => {
    const magnet = 'magnet:?xt=urn:btih:040dfc1fa956b96eb875babb156b7677f18059ca&dn=clip.mp4'
    expect(parseMagnet(magnet).infohash).toBe('040dfc1fa956b96eb875babb156b7677f18059ca')
  })

  it('extracts the dn name (the torrent name the backend used)', () => {
    // Backend magnet format: magnet:?xt=urn:btih:<hash>&dn=<safe_filename>&tr=...
    const magnet =
      'magnet:?xt=urn:btih:040dfc1fa956b96eb875babb156b7677f18059ca&dn=My_Video_Clip.mp4&tr=wss%3A%2F%2Ftracker.webtorrent.dev&tr=wss%3A%2F%2Ftracker.openwebtorrent.com'
    expect(parseMagnet(magnet).name).toBe('My_Video_Clip.mp4')
  })

  it('extracts all tr= trackers', () => {
    const magnet =
      'magnet:?xt=urn:btih:040dfc1fa956b96eb875babb156b7677f18059ca&dn=x.mp4&tr=wss%3A%2F%2Ftracker.webtorrent.dev&tr=wss%3A%2F%2Ftracker.openwebtorrent.com'
    expect(parseMagnet(magnet).trackers).toEqual([
      'wss://tracker.webtorrent.dev',
      'wss://tracker.openwebtorrent.com',
    ])
  })

  it('decodes URL-encoded + in names and values', () => {
    const magnet =
      'magnet:?xt=urn:btih:040dfc1fa956b96eb875babb156b7677f18059ca&dn=a%2Bb.mp4&tr=wss%3A%2F%2Ftracker.webtorrent.dev'
    expect(parseMagnet(magnet).name).toBe('a+b.mp4')
  })

  it('returns empty fields for a malformed magnet', () => {
    const parsed = parseMagnet('not-a-magnet')
    expect(parsed.infohash).toBe('')
    expect(parsed.name).toBe('')
    expect(parsed.trackers).toEqual([])
  })
})

describe('torrentoolPieceLength', () => {
  it('uses 262144 (256 KiB) for files > 32 KiB', () => {
    expect(torrentoolPieceLength(32769)).toBe(262144)
    expect(torrentoolPieceLength(1024 * 1024)).toBe(262144)
  })

  it('uses 32768 (32 KiB) for files <= 32 KiB', () => {
    expect(torrentoolPieceLength(32768)).toBe(32768)
    expect(torrentoolPieceLength(1000)).toBe(32768)
  })
})
