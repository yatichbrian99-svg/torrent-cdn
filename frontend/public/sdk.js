(function () {
  'use strict';

  var API_BASE = 'https://www.torrentcdn.store';

  // Zero-trust guards: both values arrive from the customer's page DOM (the
  // embed div attributes) and are therefore attacker-influenceable at the
  // boundary. Never let them shape request URLs or beacons beyond a UUID /
  // http(s) origin. (Mirrors lib/security.ts in the app bundle — keep the two
  // in sync when changing the rules; this file is vanilla JS by design.)
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function isValidFileId(id) {
    return UUID_RE.test(id || '');
  }

  function isValidApiUrl(url) {
    try {
      var u = new URL(url);
      // L1 FIX: Only accept https: — http: would send the Bearer token
      // (in the Authorization header) over an insecure connection.
      if (u.protocol !== 'https:') return false;
      // Only allow known production domains to prevent a compromised page
      // from redirecting API calls to a malicious server.
      var host = u.hostname;
      return host === 'www.torrentcdn.store' ||
             host === 'torrentcdn.store' ||
             host.endsWith('.r2.cloudflarestorage.com') ||
             host.endsWith('.r2.dev');
    } catch (e) {
      return false;
    }
  }

  function showError(el, message) {
    var errDiv = document.createElement('div');
    errDiv.textContent = message || 'Failed to load file. Please try again later.';
    errDiv.style.padding = '20px';
    errDiv.style.color = '#555';
    errDiv.style.textAlign = 'center';
    errDiv.style.border = '1px solid #222';
    el.appendChild(errDiv);
  }

  function getConfig(el) {
    var fileId = el.getAttribute('data-torrent-cdn');
    var apiUrl = el.getAttribute('data-api-url') || API_BASE;
    return {
      fileId: isValidFileId(fileId) ? fileId : '',
      apiUrl: isValidApiUrl(apiUrl) ? apiUrl : API_BASE,
    };
  }

  // Resolve the origin that served this SDK script so WebTorrent loads from
  // the same host (works for custom domains on the Business plan too).
  function getScriptOrigin() {
    var scripts = document.getElementsByTagName('script');
    for (var i = 0; i < scripts.length; i++) {
      var src = scripts[i].getAttribute('src') || '';
      if (/sdk\.js(\?|$)/.test(src)) {
        var a = document.createElement('a');
        a.href = src;
        if (a.host) return a.protocol + '//' + a.host;
      }
    }
    return API_BASE;
  }

  function createPlayer(el, fileData, apiUrl) {
    if (!fileData.magnet_link) {
      // No magnet link, fall back to direct URL immediately
      var directMedia = document.createElement('video');
      directMedia.controls = true;
      directMedia.style.width = '100%';
      directMedia.style.aspectRatio = '16 / 9';
      directMedia.style.backgroundColor = '#000';
      directMedia.src = fileData.minio_url;
      directMedia.play().catch(function () {});
      el.appendChild(directMedia);
      return;
    }

    var isVideo = /\.(mp4|webm|ogv|mov|mkv)$/i.test(fileData.filename);
    var media = document.createElement(isVideo ? 'video' : 'audio');
    media.controls = true;
    media.style.width = '100%';
    if (isVideo) media.style.aspectRatio = '16 / 9';
    media.style.backgroundColor = '#000';
    el.appendChild(media);

    // Fallback to direct URL after 5s
    var fallbackTimer = setTimeout(function () {
      if (!media.src) {
        media.src = fileData.minio_url;
        media.play().catch(function () {});
      }
    }, 5000);

    // Track peer_left on unload
    window.addEventListener('beforeunload', function () {
      navigator.sendBeacon(apiUrl + '/analytics/event', JSON.stringify({
        file_id: fileData.id, event_type: 'peer_left', bytes_p2p: 0, bytes_origin: 0
      }));
    });

    // Load WebTorrent from our own origin — pinned version with SRI integrity.
    // (Self-hosted instead of unpkg.com/@latest to eliminate supply-chain risk.)
    var script = document.createElement('script');
    script.src = getScriptOrigin() + '/vendor/webtorrent.min.js';
    script.integrity = 'sha384-6xyg5O6x7KTirI65LTuJedkVfpfk14DZF+mnj87irSN8ildVHD53YxWlBp3R+2+8';
    script.crossOrigin = 'anonymous';
    script.onerror = function () {
      // Integrity check failed or network error — fall straight back to direct URL
      clearTimeout(fallbackTimer);
      if (media && !media.src) {
        media.src = fileData.minio_url;
        media.play().catch(function () {});
      }
    };
    script.onload = function () {
      try {
        var client = new WebTorrent();
        var torrent = client.add(fileData.magnet_link);

        torrent.on('download', function () {
          clearTimeout(fallbackTimer);
        });

        torrent.on('ready', function () {
          var file = torrent.files.filter(function (f) {
            return f.name.match(/\.(mp4|webm|ogv|mov|mkv|mp3|ogg|wav)$/i);
          })[0] || torrent.files[0];

          if (!file) return;
          // webtorrent 2.x REMOVED File.renderTo (gone around 2.0), so calling
          // it throws 'file.renderTo is not a function' and the embed silently
          // falls back to direct origin streaming after the 5s timer — P2P
          // never engages. Stream via MediaSource instead (same approach as the
          // /embed page player): pieces arrive live from peers. If the browser
          // won't accept the codec in a SourceBuffer, fall back to the direct
          // URL when the whole file has downloaded.
          if (typeof MediaSource !== 'undefined' && file.type && MediaSource.isTypeSupported(file.type)) {
            var ms = new MediaSource();
            media.src = URL.createObjectURL(ms);
            ms.addEventListener('sourceopen', function onopen() {
              var sb;
              try { sb = ms.addSourceBuffer(file.type); } catch (e) { media.src = fileData.minio_url; return; }
              var reader = file.stream().getReader();
              function pump() {
                reader.read().then(function (res) {
                  if (res.done) {
                    if (ms.readyState === 'open') ms.endOfStream();
                    return;
                  }
                  if (res.value) {
                    function append() {
                      try {
                        sb.appendBuffer(res.value);
                      } catch (e) { /* ignore */ }
                    }
                    if (sb.updating) sb.addEventListener('updateend', append, { once: true });
                    else append();
                    if (media.paused) media.play().catch(function () {});
                  }
                  pump();
                }).catch(function () {});
              }
              pump();
            });
          } else {
            // Codec not accepted by MediaSource — play once downloaded.
            file.blob().then(function (blob) {
              media.src = URL.createObjectURL(blob);
              media.play().catch(function () {});
            }).catch(function () {});
          }
        });
      } catch (e) {
        // Fallback already handled
      }
    };
    document.head.appendChild(script);

    // Track peer_joined
    var txhr = new XMLHttpRequest();
    txhr.open('POST', apiUrl + '/analytics/event', true);
    txhr.setRequestHeader('Content-Type', 'application/json');
    txhr.send(JSON.stringify({
      file_id: fileData.id,
      event_type: 'peer_joined',
      bytes_p2p: 0,
      bytes_origin: 0,
    }));
  }

  document.addEventListener('DOMContentLoaded', function () {
    var elements = document.querySelectorAll('[data-torrent-cdn]');
    elements.forEach(function (el) {
      var config = getConfig(el);
      if (!config.fileId) {
        showError(el);
        return;
      }
      var xhr = new XMLHttpRequest();
      xhr.open('GET', config.apiUrl + '/files/public/' + config.fileId, true);
      xhr.onload = function () {
        if (xhr.status === 200) {
          var fileData = JSON.parse(xhr.responseText);
          createPlayer(el, fileData, config.apiUrl);
        } else {
          showError(el);
        }
      };
      xhr.onerror = function () {
        showError(el);
      };
      xhr.send();
    });
  });
})();
