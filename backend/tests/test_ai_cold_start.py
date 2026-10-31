"""Row 7.2: a cold start from an empty cache, over real TLS.

``scripts/cold_start_run.py`` is the run itself (it writes the evidence file);
this test keeps it from rotting. It is sync on purpose: the script owns its own
event loop and starts a real TLS server, so running it inside pytest-asyncio's
loop would nest loops for no benefit.

What must hold, in the order the row states it:

* an **empty** cache is filled until every shard matches its manifest hash;
* a **corrupt** shard is never written and its sender is banned and penalised;
* shards no peer can serve come from the **origin over HTTPS**, with a
  certificate validated against a real CA and the connect pinned to the address
  SSRF validation checked;
* a **warm** second run resumes all shards without the network;
* a **corrupt origin** is refused just like a corrupt peer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from scripts.cold_start_run import PINNED_PUBLIC_IP, run


def test_cold_start_from_an_empty_cache(tmp_path: Path):
    result = asyncio.run(run(tmp_path, shard_count=6, shard_bytes=32 * 1024))

    assert result["ok"], result
    assert result["verified_shards"] == 6
    assert result["cache_files"] == [
        f"shard-{index:05d}.safetensors" for index in range(6)
    ]

    cold = result["cold_run"]
    assert cold["failed_shards"] == []
    assert cold["shards_resumed"] == 0, "the cache started empty"
    assert cold["corrupt_peers"] == ["corrupt-peer"]
    assert cold["penalties"] == [["corrupt-peer", "corrupt shard 0"]]
    assert cold["origin_shards"] == 3, "the peers could not serve the last half"
    assert cold["peers_used"] == ["honest-peer"]

    # The origin really was TLS, and the socket really went to the address
    # validate_url checked rather than a fresh DNS answer.
    assert result["origin"]["tls"] is True
    assert result["origin"]["connect_targets"] == [PINNED_PUBLIC_IP]

    warm = result["warm_run"]
    assert warm["shards_resumed"] == 6
    assert warm["shards_written"] == 0
    assert warm["origin_shards"] == 0
    assert result["corrupt_origin_refused"] is True
