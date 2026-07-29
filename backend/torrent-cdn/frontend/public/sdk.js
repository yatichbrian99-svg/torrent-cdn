(function () {
  'use strict';

  var API_BASE = 'https://api.torrentcdn.com';

  function getConfig(el) {
    return {
      fileId: el.getAttribute('data-torrent-cdn'),
      apiUrl: el.getAttribute('data-api-url') || API_BASE,
    };
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

    // Load WebTorrent from unpkg
    var script = document.createElement('script');
    script.src = 'https://unpkg.com/webtorrent@latest/webtorrent.min.js';
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

          if (file) {
            file.renderTo(media, { autoplay: true });
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
      var xhr = new XMLHttpRequest();
      xhr.open('GET', config.apiUrl + '/files/public/' + config.fileId, true);
      xhr.onload = function () {
        if (xhr.status === 200) {
          var fileData = JSON.parse(xhr.responseText);
          createPlayer(el, fileData, config.apiUrl);
        } else {
          var errDiv = document.createElement('div');
          errDiv.textContent = 'Failed to load file. Please try again later.';
          errDiv.style.padding = '20px';
          errDiv.style.color = '#555';
          errDiv.style.textAlign = 'center';
          errDiv.style.border = '1px solid #222';
          el.appendChild(errDiv);
        }
      };
      xhr.onerror = function () {
        var errDiv = document.createElement('div');
        errDiv.textContent = 'Failed to load file. Please try again later.';
        errDiv.style.padding = '20px';
        errDiv.style.color = '#555';
        errDiv.style.textAlign = 'center';
        errDiv.style.border = '1px solid #222';
        el.appendChild(errDiv);
      };
      xhr.send();
    });
  });
})();
