declare module 'webtorrent' {
  interface TorrentFile {
    name: string;
    length: number;
    path: string;
    appendTo(
      root: HTMLElement | string,
      opts?: { autoplay?: boolean; controls?: boolean }
    ): void;
    renderTo(
      root: HTMLVideoElement | HTMLAudioElement | string,
      opts?: { autoplay?: boolean; controls?: boolean }
    ): void;
    getBlob(): Blob;
    getBlobURL(callback: (err: Error | null, url: string | null) => void): void;
  }

  interface Torrent {
    infoHash: string;
    magnetURI: string;
    files: TorrentFile[];
    numPeers: number;
    downloaded: number;
    uploaded: number;
    downloadSpeed: number;
    uploadSpeed: number;
    progress: number;
    timeRemaining: number;
    received: number;
    name: string;
    on(event: 'ready' | 'done', callback: () => void): this;
    on(event: 'download', callback: (bytes: number) => void): this;
    on(event: 'upload', callback: (bytes: number) => void): this;
    on(event: 'error', callback: (err: Error) => void): this;
    on(event: 'wire', callback: (wire: any, addr?: string) => void): this;
    on(event: 'noPeers', callback: (eventName: string) => void): this;
    on(event: string, callback: (...args: any[]) => void): this;
  }

  interface WebTorrentClient {
    add(
      magnetURI: string,
      ontorrent?: (torrent: Torrent) => void
    ): Torrent;
    add(
      magnetURI: string,
      opts: { announce?: string[] },
      ontorrent?: (torrent: Torrent) => void
    ): Torrent;
    seed(
      input: File | File[] | Blob | string,
      opts?: { name?: string; announce?: string[] },
      onseed?: (torrent: Torrent) => void
    ): Torrent;
    get(infoHash: string): Torrent | null;
    remove(infoHash: string, callback?: () => void): void;
    destroy(callback?: () => void): void;
    torrents: Torrent[];
  }

  interface WebTorrentConstructor {
    new (opts?: {
      tracker?: boolean;
      dht?: boolean;
      webSeeds?: boolean;
      maxConns?: number;
    }): WebTorrentClient;
  }

  const WebTorrent: WebTorrentConstructor;
  export default WebTorrent;
}
