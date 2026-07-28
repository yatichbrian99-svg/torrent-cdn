declare module 'webtorrent' {
  interface TorrentFile {
    name: string
    length: number
    renderTo: (elem: HTMLMediaElement, opts?: { autoplay?: boolean }) => void
  }

  interface Torrent {
    numPeers: number
    files: TorrentFile[]
    on: (event: string, cb: (...args: any[]) => void) => void
    destroy: () => void
  }

  interface WebTorrentInstance {
    add: (magnet: string, opts?: any) => Torrent
    destroy: () => void
  }

  const WebTorrent: new () => WebTorrentInstance
  export default WebTorrent
}
