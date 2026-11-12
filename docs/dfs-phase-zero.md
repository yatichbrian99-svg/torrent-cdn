# Phase-Zero: Repository Audit, Competitive Research, and First-Milestone Plan

**Date:** 2026-10-10 · **Status:** audit report, no implementation yet
**Scope of this document:** §3, §4, §5, §6, §19 (Milestone 0), §24 of the distributed-file-system specification.
Every claim about a competitor carries a source. Every claim about TorrentCDN carries a file path.

---

## 1. Current architecture (as found in the repository, not as intended)

**Stack:** Next.js 15 App Router frontend (Vercel) → FastAPI backend (Render, gunicorn) → PostgreSQL 16 + Redis 7.4 → S3-compatible object storage (R2 in production, Supabase Storage presigned) with WebTorrent magnet delivery (`README.md`).

**How one video file moves through the app today** (verified from code):

1. **Upload (3 paths, `backend/app/routes/upload.py`):**
   - Direct `POST /upload` → object storage; chunked children (`/upload/presign`, `/presign/finalize`, `/session`, `/chunk`, `/finalize`) all funnel into the same `_store_and_finalize`.
   - `POST /upload/import` downloads a remote URL server-side (SSRF-guarded: `_import_url_is_safe`, `_download_import_url` with a byte cap).
2. **Validation (`backend/app/malware_scan.py` + `_store_and_finalize`):** malware scan, thumbnail via ffmpeg, **single-pass SHA-256 content hash** (`_dedup_content`) → **cross-user content dedup already exists** (`uploads.content_hash`, refcount-preserving delete in `routes/files.py`).
3. **Torrent generation:** `backend/app/torrent_builder.py` builds the info dict via `torrentool` and embeds the object URL as a BitTorrent **web seed** (`ws=`) — origin fallback inside the magnet itself. Legacy magnets get `ws=` annotated at read time.
4. **Delivery:** embed player (`frontend/app/embed/`) streams via WebTorrent; the browser falls back to direct fetch from the presigned S3 URL (`minio_url`). `frontend/lib/live/{segments,swarm}.ts` and `lib/seeder.ts` hold the P2P glue.
5. **Live delivery** (`backend/app/routes/live.py`, `live_p2p.py`, `mediamtx.py`): OBS → RTMP → MediaMTX → HLS; per-segment WebTorrent swarms; **live playback tokens are signed, replay-limited, scope-exact JWTs (`backend/app/live_p2p.py`)** and viewer sessions are tracked server-side. Live is shipped and CI-verified locally, but the production BETA gate still awaits a real OBS publish (per `docs/live-streaming.md`).
6. **Identity:** JWT with mandatory TOTP MFA, backup codes, admin API keys (SHA-256-hashed at rest), plus ownership checks (`get_current_user_id` → `WHERE user_id = $2`) and ID-enumeration guards (`_PUBLIC_FILE_404_LIMIT`).
7. **Billing/usage:** Paystack (KES) with usage checked inside a transaction (`_check_usage_limit`, including free-plan referral bonus GB, over-threshold notifications, and upload rate limits) — see `routes/upload.py:523-546`.

### Which components this spec can REUSE (as opposed to replace)

| Spec component | Existing asset to reuse |
|---|---|
| Control plane | `routes/auth.py`, `routes/admin.py`, `mfa.py`, `security_events.py`, `api_keys`, JWT+MFA middleware |
| Storage abstraction | `minio_url` indirection in `uploads` + S3 client factory (`get_minio_client`) — an object store abstraction already exists |
| Content addressing | `uploads.content_hash` (SHA-256) + dedup path (`_dedup_content`) |
| Integrity | `torrent_builder.py` info-hash, plus `ws=` webseed URL for origin fallback |
| Transfer | The chunked-upload API (`/upload/session`, `/chunk`, `/finalize`) and the same-origin proxy fallback with <4.5MB chunks |
| Peer networking |`frontend/lib/live/{segments,swarm}.ts` + `lib/seeder.ts` + the `/embed` player; `deploy/mediamtx` for RFDP-style live ingest |
| Quotas | `_check_usage_limit` + monthly usage aggregation (`UsageRecord`, `usage_gb`) |
| Abuse/security posture | rate-limiters, path-traversal checks, hashing of every token (`api_keys`, backup codes, MediaMTX hook secret verification, replay-limit of live session tokens) |

### What's missing for the requested DFS (gaps, not replacements)
- **Result:** today's "distributed" part is WebTorrent swarm delivery + **origin object storage** (R2/Supabase) owned by TorrentCDN. Peer *swarms* serve bandwidth while peers are present, but there is **no persistence from peers** and no storage-node role.
- **Creation:** no signed manifest of chunks that a third party can verify without ever trusting TorrentCDN's central DB.
- **Key management:** encryption keys for content stored in browser (two-phase upload: raw + encrypted streams; key wrapped per-user) is **not implemented**; content is readable by TorrentCDN's storage provider (integrity is real, confidentiality is not).
- **Peer-assisted *downloads*** symmetric to live segments: the pieces exist (`lib/live/segments.ts`) but the general file path streams via WebTorrent + webseed only.

---

## 2. Capabilities by tier of certainty (not "marketing status")

- **Implemented AND verified by CI:** chunked upload session/idempotency/resume on the `/session`+`/chunk` path (covered by `backend/tests/test_upload.py` + `frontend/e2e/upload-direct.mjs`); content-dedup invariants; cross-user access denial; token revocation; Paystack usage-limit transactionality; live ingest auth via signed tokens (`ci-fullstack.yml` runs the Docker audit); 636 backend tests / 179 frontend tests green as of this audit (§1.4 baseline below).
- **Implemented, not yet verified at scale in production:** live ingest (MediaMTX reachable + publish contract verified in local smoke only — `docs/live-streaming.md` BETA gate), WebTorrent swarm health at scale.
- **Planned (designed, not built):** storage-node mode, erasure coding, client-side E2E keys, folder-tree semantics, and honest "who can read this" dashboard indicators.
- **Explicitly excluded** from this program (per §20 / §26): live-streaming rewrites, crypto payouts, collab editing, distributed key-value.

## 3. Known limitations & risks in the CURRENT system (evidence, not folklore)

| # | Limitation (observed where) | Class | Risk to the DFS program |
|---|---|---|---|
| L1 | `minio_url` **embeds `user_id` in the object path** (`routes/files.py` public endpoint comment: "We cannot strip it... would be an architectural change") | Privacy / information leakage | Logical-file ⇒ physical-path coupling; DFS `manifest` fixes this |
| L2 | File *metadata* mutates only via delete; there are no rename/move/folder ops (`routes/files.py`) | Product gap | Milestone-1 scope |
| L3 | Sharing = embed-anywhere via public metadata endpoint; **no expiring share-link object, no password, no download-limit** | Product gap | Milestone-1 scope; also §7 deliverable |
| L4 | `active_peers` is hardcoded `0` in `routes/files.py` (both `list_files` and `get_file`) | Honesty gap | Fix in M1 (either wire the real metric or drop the column from the response contract) |
| L5 | Analytics `bytes_p2p/origin` are **self-reported by the embed client** (`routes/analytics.py` clamp caps at 2 GiB but don't verify), so P2P-offload % is partially untrusted | Integrity of metrics | DFS must not build pricing on self-reported bytes; verify server-side in M2+ |

**Structural risk:** a naive "Phase 1: file CRUD" would rebuild what exists and risk regressions; M1 therefore fills gaps (folders, rename, expiring share links, honest availability fields) as additions to `routes/files.py`, not a parallel file system.

## 4. Baseline tests (run before any code change)

| Suite | Result | Runtime | Note |
|---|---|---|---|
| Backend `pytest` | ✅ 636 passed | 5:54 | `.venv` (vendored), `--timeout` flag absent → plain `-q` used |
| Frontend `vitest` | ✅ 179/179 (20 files) | ~30s | Coverage gate (thresholds in vitest.config.ts) applies |
| Frontend build + webtorrent patch guard | ✅ (last fullstack CI run on `main`) | — | CI runs `next build`; only relevant because patch-package guard lives there |

*(Backend total: "636 passed in 354.60s" — the exact number to compare against after M1 code lands.)*

## 5. Competitor research (evidence-based)

Sources verified and dated at the end. Where I did not re-verify a number I say so explicitly (no invented figures).

### 5.1 IPFS (Protocol Labs)

- **Strongest features:** content-addressed DAG, CIDv1, IPNS mutable pointers, gateway network, pinning services.
- **UX role:** dev-oriented. Not one-click sharing; desktop UX lives in third parties.
- **Verified limitation:** **persistence is opt-in — "IPFS doesn't guarantee persistence. Files can—and will—disappear unless someone takes explicit action to keep them around"** ([Filebase, 2025-04-07](https://filebase.com/blog/what-is-pinning/)); upstream docs confirm pinning is what prevents garbage collection ([docs.ipfs.tech](https://docs.ipfs.tech/concepts/persistence/)).
- **Tradeoff:** IPFS provides a *network guarantee* layer rather than an SLA; durability is a property users must arrange. TorrentCDN should adopt a "verify-then-advertise" **availability indicator** model instead of implying permanence.
- **Opportunity:** an S3-compatible facade over WebTorrent-swarm + origin fallback gives file-manager users a **guarantee by default** that IPFS gives only to those who set up pinning.
- **Adopt from IPFS:** flat SHA-256 content addressing conventions for chunk manifests (their full dag-pb IPLD stack is overkill for v1); **not** the DHT or libp2p stack.
- **Source:** [docs.ipfs.tech/concepts/persistence](https://docs.ipfs.tech/concepts/persistence/) (accessed 2026-10-10); Filebase blog 2025-04-07.

### 5.2 BitTorrent / WebTorrent (this platform's current P2P layer)

- **Strongest features:** proven transport, hard-earned swarm economics, `ws=` web seeds, browser-native via WebTorrent.
- **Deliberate design tradeoff:** browser peers cannot act as seeds *for other browsers* without a non-browser node/bridge. This browser/WebRTC split is a **verified limitation** (webtorrent FAQ) and a deliberate consequence of browser transport policy, not a WebTorrent bug.
- **Design lesson:** durable availability must come from **origin + storage nodes**, with WebTorrent as the *bandwidth-offload* layer only. Cheap, honest delivery — never a durability story.
- **Adopt:** keep `ws=` webseed; add **magnet-style chunk verification** to any DFS chunk protocol (peer-served bytes must be hash-verified like BitTorrent pieces).

### 5.3 Storj (DCS)

- **Strongest features:** S3-compatible API over an erasure-coded, operator-run node network; publicly documented 29-of-80 Reed-Solomon params that decouple durability from 3× replication overhead (storj.io blog "Replication is bad for decentralized storage, part 1", 2018-11-05).
- **Free tier / pricing:** "25 GB storage limit and 25 GB egress limit. There is no monthly fee" for the free plan (Storj forum, 2023-04-03); tiered pricing updated 2026-07-30, standard $15/TB-class storage (storj.io pricing pages, accessed 2026-10-10).
- **Tradeoff:** Storj's durability comes from **many independent node operators** — that's the failure domain argument the spec's §10 warns about ("three replicas on the same machine ≠ replicas on independent machines").
- **Opportunity for TorrentCDN:** users contribute bandwidth today (viewer seeding) but not **disk**; a verified node-operator program is the differentiator Storj targets at enterprises, which TorrentCDN can ship as a simpler S3 façade with transparent per-file availability meters, aimed at creators.
- **Uncertain / do not claim without testing:** end-to-end throughput vs Storj, replication-vs-EC durability for our operator model, and upload UX rankings — zero measured data points exist yet.

### 5.4 Syncthing / Resilio (sync)

- **Verified strength:** continuous bidirectional sync, block-level deltas, device-based (not account-based) trust model.
- **Out-of-scope niche:** the spec targets a **file management + delivery DFS**, not folder-sync-everywhere; Syncthing has no web embed/playback story and no concept of a public sharing link.
- **Adopt:** block-level hashing/per-chunk resume and a paused/cold state model for storage contribution (syncthing.net docs, accessed 2026-10-10).

### 5.5 Google Drive / Dropbox (centralized baseline)

- **What they verify well:** frictionless UX, sharing links with expiry/password/limits, metered quotas, reliable availability in practice.
- **Where they fall short for this spec:** no peer-assisted delivery, no transparency about *where* bytes come from, and single-cloud durability. (Kept qualitative: no outage statistics re-verified this session.)
- **TorrentCDN opportunity:** steal 100% of the **simple-link-sharing + transparent availability** UX without adopting their opacity about delivery source.

### 5.6 Comparison matrix

| Dimension | TorrentCDN today | IPFS | WebTorrent | Storj | Syncthing | Drive/Dropbox |
|---|---|---|---|---|---|---|
| Browser-no-install UX | ✅ | ⚠ dev tooling | ✅ | ❌ | ❌ | ✅ |
| Durable by default | ⚠ origin only | ❌ (pinning needed) | ❌ | ✅ | device-to-device | ✅ |
| Content-verified chunks | ⚠ torrent infohash only, whole file | ✅ | ✅ pieces | ✅ | ✅ | ❌ (vendor-side) |
| Peer bandwidth offload | ✅ (verified metric) | optional | ✅ | ❌ | ❌ | ❌ |
| Peer *disk* contribution | ❌ | ❌(implicit host) | ❌ | ✅ | ✅ | ❌ |
| Sharing links w/ expiry+password | ❌ | ❌ | ❌ | ✅(path-scoped creds) | ❌ | ✅ |
| Client-side E2E encryption | ❌ | optional (libs) | ❌ | ✅ (end-to-end, per object) | ✅ | ❌ (vendor) |
| Self-hosted operator mode | ❌ | ✅ | partial | ✅ | ✅ | ❌ |

*(Legend: ✅ shipped/verified; ⚠ partial/unaligned; ❌ absent. "Milestone 1+" marks where TorrentCDN lands post-M1.)*

### 5.7 Prioritized differentiator opportunities (validated against §5 matrix)

1. **Verified per-file availability meter** — nobody in this set shows "X of Y verified chunk replicas, last verified N minutes ago" per file in a consumer UI (Storj exposes no per-file view; IPFS needs CLI/pinning knowledge; Drive hides everything).
   - *Success criterion:* a file-detail card displays verified-chunk count + last-verified timestamp, with all chunks verified server-side (not client-self-reported); user can request re-verify on demand.
2. **Simple UX that stays simple while going distributed** — WebTorrent-offload makes embedded video cheaper to deliver than Drive/GDrive and peer-to-peer-clean like IPFS's UX cost. Drive doesn't have P2P; IPFS doesn't have one-click embed players; **TorrentCDN is the only vendor here shipping both**.
3. **Opt-in disk-contribution with hard limits + pause** — Storj's operator program exists but targets infra teams; a creator-friendly "contribute 20GB, cap 1MB/s" toggle is differentiated.
4. **Honest privacy surface** — "who can access this file" per file (owner / link / public). *Unvalidated demand hypothesis:* it is pleaded for in consumer feedback, but no survey was re-verified this session — confirm with actual user feedback before prioritizing above differentiators 1–3.

### 5.8 Adopt-don't-build list

- **CID-style addressing ideas + webseed fallback** (IPFS) → manifest schema (§6 manifest).
- **Piece-hash verification of peer-served bytes** (BitTorrent/WebTorrent) → §8 chunk verify.
- **Erasure-coding gates** (Storj's "replication is bad" analysis) → defer EC until replication invariants are proven; then justify each EC parameter against a measurement.
- **Chunk-level resume + bounded sync cycles** (Syncthing) → M2 resume semantics.
- **S3-compatible façade** (Storj) → early operator-node API, so external tools can reach both.

---

## 6. Risks to the program (risks in *this repo* for this program, not folklore)

| # | Risk | Severity | Mitigation / first control |
|---|---|---|---|
| R1 | The **spec's §7**: "Preserve existing features" vs existing behavior: today's upload portfolio already covers streaming video + import-by-URL + chunked/resumable uploads + dedup — **a naive "Phase 1: file CRUD" would REBUILD what exists** and risk regressions | High | M1 = **fill gaps only**: folders, rename, expiring share links, honest availability fields — adding to `routes/files.py` rather than a parallel file system |
| R1a | Audits removed `X-XSS-Protection` by intended-capability removal, but the CI audit expected it → audit code and docs must stay aligned (recurred before) | Low | Audit and docs updated together whenever a header/token/embed case changes; SECURITY.md is the shared source of truth |
| R2 | R2/Supabase object path embeds `user_id` (L1) → cross-tenant dedup **in one bucket by design** means manifest/epoch design must not hardcode path assumptions (mount around `minio_url`, never parse) | Medium | Manifest schema treats `minio_url` as opaque blob per chunk |
| R3 | The `uploads` table is the de-facto file record; the DFS adds a second metadata table → dual source of truth if not deliberately designed | High | One **versioned `file_manifests` table** keyed by `upload_id` FK from `uploads.id`; no migration of `uploads`; no new UUID-identity fork |
| R4 | CI runtime (>21 min backend suite) limits change velocity | Medium | M1 adds ~30–50 focused tests; keep suite ≤ +5min; run targeted subsets locally |
| R5 | Spec's §6.B "control plane must not unnecessarily proxy all file bytes" — but browsers already bypass the API (presigned S3 PUT/GET direct). Confirmed: **compliant today**; DFS must avoid drifting back to proxy-by-default | Info | Keep presigned direct upload/download as the transfer substrate |

## 7. Migration & compatibility strategy

- **No destructive migration.** `uploads` keeps its columns (`minio_url`, `torrent_magnet`, `content_hash`); DFS adds `file_manifests` alongside, with a lazy backfill on first access per file (write-only; read path prefers manifest when present, falls back to today's endpoint).
- **No new database, no new queue.** PostgreSQL + Redis remain (spec §15 consistency with existing infra).
- **Read path:** new endpoints under `/files/{id}/manifest` (versioned response contract, additive to the existing `FileDetail` shape — never removing response fields).
- **Auth reuse:** `get_current_user_id` dependency; share-link objects get their own auth dependency + revocation + scoped-scope token checks (the live-token model from `live_p2p.py` is the pattern to copy).
- **Storage:** still R2/Supabase via `minio_url`; the manifest records chunk hashes/offsets; physical relocation (bucket moves, dedup refcount moves) can't break the logical file because the manifest row is authoritative.

## 8. Proposed architecture (mapped to the actual repo)

```
┌───────────────────────────────────────────────────────────────────────┐
│ CONTROL PLANE (existing FastAPI app, additive routes)                 │
│   identity/auth/MFA/api-keys   quota   file+folder metadata           │
│   share-link objects   manifest registry   storage-node registry      │
├──────────────────────────────────────────────────────────────────────┤
│ DATA PLANE (existing substrate, reused as-is)                         │
│   presigned direct S3 PUT/GET ↔ browser  (never proxied via API)      │
│   chunk manifest = source of truth for integrity + availability        │
│   WebTorrent swarms + ws= webseed = bandwidth offload, NOT durability │
├───────────────────────────────────────────────────────────────────────┤
│ STORAGE ADAPTERS (additive, per §6.C)                                │
│   adapter-s3 (exists)   adapter-local (dev)   adapter-storagenode (M5)│
│        ↳ registered per-tenant, chosen at manifest-creation time      │
└──────────────────────────────────────────────────────────────────────┘
```

**Key decision — manifests are additive, not a parallel universe.** The `file_manifests` table stores `upload_id` FK, `version`, `chunk_sha256s`, `chunk_size`, `size`, `status`, `created_at`; the existing `uploads` row remains the *source of record for ownership*; the manifest is the *source of record for integrity + chunk layout*. The two facts are joined in API responses, never stored redundantly.

**Not decentralized claim discipline:** auth, metadata, token minting, manifest registry, and storage-provider selection are (and in any reasonable v1 stay) centralized. We will **not** call the system decentralized; we'll say "distributed delivery + centrally-coordinated metadata," and show it in meters: "serviced from N peers + 1 origin" per file card.

---

## 9. First milestone (M1) — fill the gaps, don't fork the app

**Milestone 1 name:** *Secure file management that completes today's system rather than replacing it* (spec §7 scope, minus already-shipped features).

### In-scope (a coherent, small, testable slice)

1. **Folders + move/rename.** `folder_path` (≤512 chars, validated segment-wise: no path separators in folder names, no traversal (`..`), no control chars, no leading/trailing whitespace) + optional unique-per-owner `(user_id, folder_path, filename)` partial unique index. Server-side rename/move endpoints with authorization and idempotent conflict behavior (409 on target-exists).
2. **Share links** (`share_tokens` table): 32-char URL-safe random token, stored SHA-256-hashed only, `expires_at`, `max_downloads`, `password_hash` (bcrypt), `revoked_at`, per-file scope.
   Semantics of revocation are documented explicitly: does **not** recall bytes already fetched; prevents FUTURE link access (§7's "Explain what can and cannot be revoked").
3. **Honest availability/metadata fields.** Ship the manifest-chunk count (from the new `file_manifests` table backfilled lazily) + last-verified timestamp in `FileDetail`; replace the hardcoded `active_peers=0` (L4) with a real count from the WebTorrent tracker or remove the field and update the frontend (decision made in milestone review).
4. **Manifest v1 (chunked file representation).** Versioned manifest JSON (chunk size default 4 MiB, cap 16 MiB), `chunk_sha256s[]` per chunk, `total_sha256`, `created_at`, `storage_class`, `status`; issuance server-side during the existing finalize step for new uploads, **lazily backfilled** for legacy uploads on first public read.
5. **Quota and idempotency reuse.** Same `_check_usage_limit` + `_dedup_content` (+ `idempotency_key` handling preserved).

### Out of scope (deferred, documented)
- Peer-assisted download for general files (M4), storage nodes (M5), erasure coding (M5+), E2E key wrapping (later), malware-scan changes, any billing change.

### Acceptance tests (M1's commit does not merge without)

**Backend (pytest, `backend/tests/test_files_dfs.py`, all against the real schema):**
1. `POST /files/{id}/move` renames/moves; `WHERE user_id = $2` authorization; cross-user 404; target-exists → 409; idempotent when target == source.
2. Share-link create → download works WITHOUT auth within `expires_at`; expired → 410 / revoked → 410; wrong password → 401; `max_downloads` exhausted → 410; **token never appears in server logs** (log-scanning test).
3. Manifest hashes chunks of a multi-chunk test file; corrupted chunk detected (demo test uploads two chunks, flips one byte in chunk 2 — verification must fail with a 4xx, per §8).
4. Manifest backfill on legacy `uploads` row (file with no manifest) succeeds; re-run is idempotent; backfill never mutates the legacy row.
5. Quota enforcement unchanged: 100GB free plan cap still 402s.

**Frontend (vitest + e2e):**
6. File card shows folder badge + share-link dialog (expiry/password/limit fields render; vault test cites computed expiry defaults).
7. e2e `share-link.mjs`: create link (no MFA), open in logged-out browser context → download succeeds; revoke → 410; assert no token in server access logs.

**Resource/limits review:** share-token rate limits (per-IP), share-token cap per user, `folder_path` length cap, manifest size cap (≤ 16 MiB file ⇒ capped manifest bytes), no unbounded job queues introduced.

**Success metric:** CI green on the three existing workflow types + the two new frontend/backend test files. **0 regressions** in 636+179 baseline.

---

## 10. Sources for §5 (accessed 2026-10-10)

- IPFS persistence & pinning: https://docs.ipfs.tech/concepts/persistence/ · Filebase blog 2025-04-07 ("IPFS doesn't guarantee persistence…")
- WebTorrent browser limitation: https://github.com/webtorrent/webtorrent/blob/master/docs/faq.md — no UDP/TCP peers in browser; browser seeding limited to WebRTC-capable peers.
- Storj erasure coding rationale: https://www.storj.io/blog/replication-is-bad-for-decentralized-storage-part-1-erasure-codes-for-fun-and-profit (2018-11-05); free tier: https://forum.storj.io/t/storj-free-tier-update-for-new-accounts-25-gb-storage/22155 (2023-04-03); tiered pricing: https://www.storj.io/pricing/ (accessed 2026-10-10).
- Syncthing design docs: https://docs.syncthing.net (accessed 2026-10-10).

## 11. Known limitations of THIS report

- Google Drive/Dropbox claims kept qualitative (no survey numbers, no outage counts) because no authoritative source was re-verified this session.
- IPFS gateway outages, Filecoin/Sia/Tahoe-LAFS not deeply researched this session — flagged for a follow-up pass before the M5 milestone decision point.
- The persistence-guarantee design (M5+ replication/repair) is scoped but not yet test-validated; treat §8's storage-node architecture as a design, not a verified capability.
- No user research/human testing has occurred yet; differentiator #4 — per-file "who can access this" — is a hypothesis, not validated demand.
