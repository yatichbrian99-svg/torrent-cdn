declare module 'webtorrent' {
  interface TorrentFile {
    name: string
    length: number
    /** Removed in webtorrent 2.x — kept for type completeness of older refs. */
    renderTo?: (elem: HTMLMediaElement, opts?: { autoplay?: boolean }) => void
  }

  interface Torrent {
    infoHash: string
    name: string
    length: number
    pieceLength: number
    numPeers: number
    files: TorrentFile[]
    on: (event: string, cb: (...args: any[]) => void) => void
    destroy: () => void
  }

  interface SeedOptions {
    name?: string
    pieceLength?: number
    announce?: string[]
  }

  interface WebTorrentInstance {
    add: (magnet: string, opts?: any) => Torrent
    seed: (input: File | Blob | ArrayBuffer | Buffer | string, opts?: SeedOptions, cb?: (torrent: Torrent) => void) => Torrent
    destroy: () => void
  }

  const WebTorrent: new () => WebTorrentInstance
  export default WebTorrent
}
