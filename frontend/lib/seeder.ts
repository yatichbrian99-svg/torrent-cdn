/**
 * Browser-side auto-seeding for freshly uploaded files.
 *
 * After `api.uploadFile` succeeds, the uploader's tab seeds the file back into
 * the WebTorrent swarm, so the FIRST viewer (and anyone before a leecher has
 * finished downloading) can fetch pieces over WebRTC instead of waiting for a
 * manual seeder or falling back to origin bytes.
 *
 * CRITICAL: to be discoverable by embed players, the seeded torrent must have
 * the EXACT info dict the backend built (routes/upload.py `_torrent_and_thumbnail`):
 *   - name     == the magnet's `dn` param (torrentool names the torrent after
 *     the safe_filename it wrote, which is what the magnet carries — NOT the
 *     original File.name, which may contain characters the backend sanitized
 *     to `_`)
 *   - pieceLength == torrentool's rule: 262144 for files > 32 KiB, else 32768
 *     (torrentool/torrent.py: size_min=32768, size_default=262144)
 *   - announce == the magnet's `tr=` trackers (the backend bakes them in)
 *
 * This is the same recipe the E2E seeder (e2e/p2p-two-peer.mjs) uses, which is
 * verified to reproduce the backend's infohash byte-for-byte.
 *
 * The seed is best-effort: if webtorrent fails to load or the swarm never
 * forms, the upload itself is unaffected (direct origin streaming still works).
 * The seed lives only while the upload page is open — browsers can't seed
 * after the tab closes.
 */
export interface SeederHandle {
  /** Tear down the webtorrent client and stop announcing. */
  destroy: () => void
  /** Current number of connected peers (0 until the swarm forms). */
  peers: () => number
}

export interface MagnetInfo {
  infohash: string
  name: string
  trackers: string[]
}

/** Parse a magnet link into its infohash / dn name / tr trackers. */
export function parseMagnet(magnet: string): MagnetInfo {
  const params = new URLSearchParams((magnet.split('?')[1] || '').replace(/\+/g, '%2B'))
  const xt = params.get('xt') || ''
  return {
    infohash: xt.split(':').pop() || '',
    name: params.get('dn') || '',
    trackers: params.getAll('tr'),
  }
}

/** torrentool's piece-length rule (torrentool/torrent.py: size_min=32768, size_default=262144). */
export function torrentoolPieceLength(sizeBytes: number): number {
  return sizeBytes > 32768 ? 262144 : 32768
}

/**
 * Start seeding `file` under the identity described by `magnet`. Resolves with
 * a handle (or null on any failure — seeding is best-effort).
 */
export async function startSeeding(file: File, magnet: string): Promise<SeederHandle | null> {
  try {
    const { name, trackers } = parseMagnet(magnet)
    if (!name || !trackers.length) return null
    const WebTorrent = (await import('webtorrent')).default
    const client = new WebTorrent()
    const torrent = client.seed(file, {
      name,
      pieceLength: torrentoolPieceLength(file.size),
      announce: trackers,
    })
    return {
      destroy: () => {
        try {
          client.destroy()
        } catch {
          /* already torn down */
        }
      },
      peers: () => (torrent && typeof torrent.numPeers === 'number' ? torrent.numPeers : 0),
    }
  } catch {
    return null
  }
}
