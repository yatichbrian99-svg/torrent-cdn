"""TCDN-AI — decentralized inference coordinator, worker and weight transport.

Module map (see docs/tcdn-ai/ARCHITECTURE.md):

* ``config``       env-driven settings + fail-closed production validation
* ``crypto``       Ed25519/X25519/HKDF/AES-256-GCM wrappers, domain separation
* ``keyring``      key registry, rotation with overlap, revocation, secret stores
* ``tickets``      signed placement tickets + replay protection
* ``pricing``      integer micro-credit pricing with effective dates
* ``ledger``       double-entry ledger, holds, exact splits
* ``scheduler``    worker filtering/scoring; no confidential downgrade
* ``receipts``     signed execution receipts + reconciliation
* ``verification`` deterministic sampling and majority tiebreak
* ``verification2`` transcript commitment, challenge derivation, tolerance verifier, verdict shapes (penalties/probes not built)
* ``verifier_sign`` Part 3 voucher signing (real Ed25519 over a closed domain set)
* ``transcripts``    worker-side per-token capture + encrypted TTL record store
* ``shards``       signed manifests, Merkle proofs, swarm fetch, weight sinks
* ``attestation``  verifier interface, policy, simulated + fail-closed adapters
* ``payments``     credit purchases (Paystack + dev fake)
* ``payouts``      seeder transfer receipts, fraud controls, exact distribution
* ``burn``         disabled, dry-run-only burn module
* ``store``        ai_* DDL, Postgres store, in-memory test store
* ``service``      coordinator logic used by routes
* ``worker``       worker agent + vLLM/Ollama/dev adapters
* ``routes``       FastAPI surface under /api/ai/v1
"""
