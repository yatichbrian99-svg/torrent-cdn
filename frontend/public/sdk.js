/**
 * Torrent CDN SDK
 * Vanilla JS library for embedding P2P-powered video/audio content.
 * 
 * Usage:
 *   <script src="https://your-domain.com/sdk.js"></script>
 *   <div data-torrent-cdn="FILE_ID"></div>
 * 
 * Options via data attributes:
 *   data-torrent-cdn="FILE_ID"     - Required: file ID to load
 *   data-width="100%"              - Player width (default: 100%)
 *   data-height="auto"             - Player height (default: auto)
 *   data-theme="dark"              - Theme: 'dark' or 'light' (default: 'dark')
 *   data-autoplay="true"           - Autoplay (default: true)
 *   data-muted="false"             - Muted (default: false)
 */

(function () {
  'use strict';

  const API_BASE = window.TORRENT_CDN_API || 'http://localhost:8000';
  const FALLBACK_TIMEOUT = 5000; // 5 seconds before falling back to direct URL

  // Load WebTorrent from CDN
  function loadWebTorrent() {
    return new Promise(function (resolve, reject) {
      if (window.WebTorrent) {
        resolve(window.WebTorrent);
        return;
      }

      var script = document.createElement('script');
      script.src = 'https://cdn.jsdelivr.net/npm/webtorrent@latest/webtorrent.min.js';
      script.async = true;
      script.onload = function () {
        resolve(window.WebTorrent);
      };
      script.onerror = function () {
        reject(new Error('Failed to load WebTorrent'));
      };
      document.head.appendChild(script);
    });
  }

  // Fetch file details from API
  function fetchFileDetails(fileId) {
    return fetch(API_BASE + '/files/public/' + fileId)
      .then(function (res) {
        if (!res.ok) throw new Error('File not found');
        return res.json();
      });
  }

  // Track analytics event
  function trackEvent(fileId, eventType, bytesP2p, bytesOrigin) {
    bytesP2p = bytesP2p || 0;
    bytesOrigin = bytesOrigin || 0;

    try {
      var payload = JSON.stringify({
        file_id: fileId,
        event_type: eventType,
        bytes_p2p: bytesP2p,
        bytes_origin: bytesOrigin,
      });

      if (navigator.sendBeacon) {
        navigator.sendBeacon(API_BASE + '/analytics/event', payload);
      } else {
        fetch(API_BASE + '/analytics/event', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payload,
        }).catch(function () {});
      }
    } catch (e) {
      // Silently fail
    }
  }

  // Create player HTML
  function createPlayer(container, fileData, options) {
    var isAudio = fileData.filename.match(/\.(mp3|ogg|wav|m4a|aac|flac)$/i);
    var isDark = options.theme !== 'light';
    var bgColor = isDark ? '#0f172a' : '#ffffff';
    var textColor = isDark ? '#f8fafc' : '#0f172a';
    var mutedColor = isDark ? '#64748b' : '#94a3b8';

    container.style.position = 'relative';
    container.style.backgroundColor = bgColor;
    container.style.borderRadius = '8px';
    container.style.overflow = 'hidden';

    // Video element
    var video = document.createElement('video');
    video.style.width = '100%';
    video.style.display = 'block';
    if (isAudio) {
      video.style.height = '60px';
    } else {
      video.style.aspectRatio = '16 / 9';
    }
    video.controls = true;
    video.playsInline = true;
    video.crossOrigin = 'anonymous';
    if (options.autoplay) video.autoplay = true;
    if (options.muted) video.muted = true;
    container.appendChild(video);

    // Status overlay
    var overlay = document.createElement('div');
    overlay.style.cssText =
      'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;' +
      'background:rgba(0,0,0,0.5);border-radius:8px;';

    var statusText = document.createElement('div');
    statusText.style.textAlign = 'center';

    var spinner = document.createElement('div');
    spinner.style.cssText =
      'width:32px;height:32px;margin:0 auto 8px;border:3px solid rgba(255,255,255,0.1);' +
      'border-top-color:#3b82f6;border-radius:50%;animation:torrent-cdn-spin 0.8s linear infinite;';

    var statusLabel = document.createElement('div');
    statusLabel.textContent = 'Connecting to P2P network...';
    statusLabel.style.cssText = 'font-size:13px;color:' + textColor + ';';

    statusText.appendChild(spinner);
    statusText.appendChild(statusLabel);
    overlay.appendChild(statusText);
    container.appendChild(overlay);

    // Status bar
    var statusBar = document.createElement('div');
    statusBar.style.cssText =
      'display:flex;align-items:center;justify-content:space-between;' +
      'padding:6px 10px;background:' + (isDark ? '#1e293b' : '#f1f5f9') + ';' +
      'font-size:11px;';

    var sourceBadge = document.createElement('span');
    sourceBadge.textContent = 'Connecting...';
    sourceBadge.style.cssText = 'color:' + mutedColor + ';';
    statusBar.appendChild(sourceBadge);

    var peerCountEl = document.createElement('span');
    peerCountEl.style.cssText = 'color:' + mutedColor + ';';
    statusBar.appendChild(peerCountEl);

    container.appendChild(statusBar);

    // Add keyframes for spinner animation
    if (!document.getElementById('torrent-cdn-styles')) {
      var styleSheet = document.createElement('style');
      styleSheet.id = 'torrent-cdn-styles';
      styleSheet.textContent =
        '@keyframes torrent-cdn-spin {to{transform:rotate(360deg)}}';
      document.head.appendChild(styleSheet);
    }

    return {
      video: video,
      overlay: overlay,
      statusLabel: statusLabel,
      statusBar: statusBar,
      sourceBadge: sourceBadge,
      peerCountEl: peerCountEl,
    };
  }

  // Initialize player for a single container
  function initPlayer(container) {
    var fileId = container.getAttribute('data-torrent-cdn');
    if (!fileId) return;

    var options = {
      width: container.getAttribute('data-width') || '100%',
      height: container.getAttribute('data-height') || 'auto',
      theme: container.getAttribute('data-theme') || 'dark',
      autoplay: container.getAttribute('data-autoplay') !== 'false',
      muted: container.getAttribute('data-muted') === 'true',
    };

    var player, playerElements;

    fetchFileDetails(fileId)
      .then(function (fileData) {
        playerElements = createPlayer(container, fileData, options);
        player = {
          fileId: fileId,
          fileData: fileData,
          elements: playerElements,
          options: options,
          webtorrent: null,
          usingP2P: false,
          usingFallback: false,
          ready: false,
        };

        // Track peer joined
        trackEvent(fileId, 'peer_joined');

        // Try WebTorrent
        return initWebTorrent(player);
      })
      .catch(function (err) {
        if (playerElements) {
          playerElements.sourceBadge.textContent = 'Error: ' + err.message;
          playerElements.sourceBadge.style.color = '#ef4444';
          playerElements.overlay.style.display = 'none';
        }
      });
  }

  // Initialize WebTorrent for a player
  function initWebTorrent(player) {
    var fallbackTimer = setTimeout(function () {
      if (!player.ready) {
        fallbackToDirect(player);
      }
    }, FALLBACK_TIMEOUT);

    loadWebTorrent()
      .then(function (WebTorrent) {
        var client = new WebTorrent();
        player.webtorrent = client;

        var torrent = client.add(player.fileData.magnet_link);

        torrent.on('wire', function () {
          if (player.elements.peerCountEl) {
            player.elements.peerCountEl.textContent =
              torrent.numPeers > 0 ? torrent.numPeers + ' peers' : '';
          }
        });

        torrent.on('download', function (bytes) {
          player.usingP2P = true;
          clearTimeout(fallbackTimer);

          if (player.elements.sourceBadge) {
            player.elements.sourceBadge.textContent = 'P2P Streaming';
            player.elements.sourceBadge.style.color = '#22c55e';
          }
          if (player.elements.peerCountEl && torrent.numPeers > 0) {
            player.elements.peerCountEl.textContent = torrent.numPeers + ' peers';
          }

          // Track P2P bytes
          trackEvent(player.fileId, 'chunk_downloaded', bytes, 0);
        });

        torrent.on('ready', function () {
          var videoFile =
            torrent.files.find(function (f) {
              return f.name.match(/\.(mp4|webm|ogv|mov|mkv|mp3|ogg|wav)$/i);
            }) || torrent.files[0];

          if (videoFile && player.elements.video) {
            videoFile.renderTo(player.elements.video, {
              autoplay: player.options.autoplay,
            });
            player.ready = true;
            if (player.elements.overlay) {
              player.elements.overlay.style.display = 'none';
            }
            player.elements.sourceBadge.textContent = 'P2P';
            player.elements.sourceBadge.style.color = '#22c55e';
          }
        });
      })
      .catch(function () {
        clearTimeout(fallbackTimer);
        fallbackToDirect(player);
      });
  }

  // Fallback to direct MinIO URL
  function fallbackToDirect(player) {
    if (player.ready) return;

    player.usingFallback = true;

    if (player.elements.video && player.fileData.minio_url) {
      player.elements.video.src = player.fileData.minio_url;
      if (player.options.autoplay) {
        player.elements.video.play().catch(function () {});
      }
    }

    player.ready = true;
    if (player.elements.overlay) {
      player.elements.overlay.style.display = 'none';
    }
    player.elements.sourceBadge.textContent = 'Direct Stream';
    player.elements.sourceBadge.style.color = '#f59e0b';

    // Track origin bytes
    trackEvent(
      player.fileId,
      'chunk_downloaded',
      0,
      Math.round(player.fileData.file_size_mb * 1024 * 1024)
    );
  }

  // Initialize all players on the page
  function initAllPlayers() {
    var containers = document.querySelectorAll('[data-torrent-cdn]');
    containers.forEach(function (container) {
      initPlayer(container);
    });
  }

  // Run when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAllPlayers);
  } else {
    initAllPlayers();
  }

  // Expose API
  window.TorrentCDN = {
    init: initAllPlayers,
    loadFile: initPlayer,
    setApiBase: function (url) {
      window.TORRENT_CDN_API = url;
    },
  };
})();
