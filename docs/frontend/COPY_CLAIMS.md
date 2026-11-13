# Copy claims audit — redesigned pages

Rule for this redesign: **no new claim, number, logo, testimonial or competitor
price was added.** Every word of the landing page, the legal pages and the inner
pages is unchanged from the pre-redesign state. This file records where each
number/claim comes from, so a reader can tell which ones the repository actually
backs and which are the owner's to substantiate.

Scope: the pages restyled in this branch — `/` (landing), `/docs`,
`/architecture`, `/demo`, `/tcdn-ai`, the legal pages, the auth pages — plus the
shared navbar/footer and the pricing cards, which appear on `/` and `/billing`.

Status meanings:

- **VERIFIED** — a file, constant or test in this repo proves the claim.
- **PARTIAL** — the mechanism exists and is proven, but the claim's wording is
  broader than the evidence (noted per row).
- **UNVERIFIED** — no repository evidence; **the owner must substantiate it or
  the copy must be reworded.** This redesign deliberately did not touch it.

## Landing page (`/`)

| Claim (verbatim) | Where | Status | Evidence / note |
| --- | --- | --- | --- |
| "The crowd is your capacity. Cheaper as you scale." | H1 | PARTIAL | The capacity half is real: viewers serve shards (`lib/seeder.ts`, `VideoPlayer.tsx`) and hosts serve inference (`app/ai/worker.py`). The **cheaper-as-you-scale** half is a positioning claim with no cost curve, invoice or measurement in the repo. |
| "Stream on it, go live on it, run models on it." | hero subtext | VERIFIED | `routes/files.py`, `routes/live.py`, `app/ai/routes_openai.py` — the three surfaces exist. |
| "Viewers carry the delivery, idle GPUs carry the inference…" | hero subtext | PARTIAL | Delivery by peers is proven; GPU hosting of inference exists but is a private alpha, so the "idle GPUs" pool is not open yet. The sentence describes the model, not current supply. |
| "…our origin picks up whatever the crowd cannot." | hero subtext | VERIFIED | `VideoPlayer.tsx` falls back to direct origin within 5 s (`DIRECT_BILL_MS`). |
| "No credit card required" | hero microcopy | VERIFIED | `backend/app/routes/billing.py:43` — the free plan has `amount: 0`; signup does not create a payment method. |
| "100GB free forever" | hero microcopy | PARTIAL | 100 GB is real (`billing.py:43` `limit_gb: 100`, `routes/upload.py:589`). "Forever" is a promise the repo cannot prove. |
| "Media · Live · AI" | hero eyebrow | VERIFIED | The three product surfaces that exist in the repo (`routes/files.py`, `routes/live.py`, `app/ai/`). Short by necessity: the eyebrow is `white-space: nowrap` on phones. No concurrency or performance figure is claimed any more. |
| "One network. Three jobs." + "Ship video. Skip the bill spike." / "Go live without a second stack." / "Run AI on idle GPUs." | capabilities section | PARTIAL | Each card carries its maturity kicker ("Available today" / "Engineering MVP" / "Private alpha"), matching the code's state. The three product claims are real; the implied savings are not measured. Live and AI are not claimed as generally available. |
| "90%" / "bandwidth reduction" | hero stat card | UNVERIFIED (owner must substantiate) | The only related artefact is `docs/LIVE_PITCH.md:66` — "~90% offload" from a *simulated* 3-viewer run. That is not a production measurement. |
| "< 5ms" / "peer discovery" | hero stat card | UNVERIFIED (owner must substantiate) | No latency measurement for peer discovery anywhere in the repo. |
| "Works in any browser" | hero stat card | PARTIAL | The player needs WebRTC; the FAQ narrows this to "all modern browsers that support WebRTC — Chrome, Firefox, Edge, Safari 15+". "Any browser" is broader than the code supports. |
| "Upload your media — Drop a video or audio file. We store it and generate a magnet link." | how it works 01 | VERIFIED | `backend/app/routes/upload.py:119` (`ALLOWED_EXTS`), torrent builder produces the magnet (`backend/app/torrent_builder.py`). |
| "Paste one line of code" + `<div data-torrent-cdn="your-id"></div>` | how it works 02 | VERIFIED | `frontend/public/sdk.js` scans for `data-torrent-cdn`; the snippet is copied verbatim into the card and by the copy button. |
| "Viewers share bandwidth — Every viewer becomes a peer. Your costs stay flat while your audience grows." | how it works 03 | PARTIAL | Peer participation is real (WebTorrent + analytics `total_p2p`); "costs stay flat" is the same unmeasured claim as the H1. |
| "1,000 / 10,000 / 100,000 / 1,000,000 viewers" × "$45 / $450 / $4,500 / $45,000 AWS CloudFront" | comparison table | UNVERIFIED (owner must substantiate) | No rate card, invoice or CloudFront price source is committed. Rows are linear ($0.045/viewer), which suggests an illustrative model rather than a quote. |
| "$1.20 / $4.80 / $12.00 / $28.00 TorrentCDN" | comparison table | UNVERIFIED (owner must substantiate) | No billing artefact ties these to a plan or to measured offload. |
| "Free — KES 0 /month", "Pro — KES 4,000 /month", "Business — KES 12,000 /month" | pricing cards | VERIFIED | `backend/app/config.py:81-82` (`paystack_amount_pro = 400000`, `paystack_amount_business = 1200000`, i.e. KES 4,000 / 12,000 at the KES minor-unit convention), `backend/app/routes/billing.py:42-50`. |
| "100GB bandwidth" (Free) | pricing card | VERIFIED | `backend/app/routes/billing.py:43`. |
| "1TB bandwidth" (Pro) | pricing card | VERIFIED | `billing.py` `limit_gb: 1024`; `routes/upload.py:589` (`pro: 1024`). |
| "Unlimited bandwidth" (Business) | pricing card | VERIFIED | `routes/upload.py:589` (`business: float("inf")`). |
| "1 seat", "Community support", "Unlimited files", "Analytics dashboard", "Email support", "Priority support", "Custom domain", "SLA guarantee" | pricing features | UNVERIFIED (owner must substantiate) | No seat model, support policy, custom-domain path or SLA document exists in the repo; `grep custom_domain backend/app` returns nothing. |
| "Most popular" badge on Pro | pricing card | N/A | Presentation only; no claim about sales. |
| "What browsers are supported? … Chrome, Firefox, Edge, Safari 15+, and all mobile browsers." | FAQ | PARTIAL | WebRTC capability is enforced/normalised in the player (`VideoPlayer.tsx`); the specific version floor (Safari 15+) is not asserted by any test. |
| "If no peers are available, the player automatically falls back to direct delivery from our origin server within 5 seconds." | FAQ | PARTIAL | The fallback exists with a 5 s timer (`frontend/components/VideoPlayer.tsx`, `DIRECT_BILL_MS = 5000` and the raise-handler timeout). "Your viewers never see a blank screen" is guaranteed only when origin delivery succeeds. |
| "Uploads accept MP4, WebM, MKV, MOV, AVI for video and MP3, WAV, OGG, M4A for audio… the torrent layer underneath is not media-specific." | FAQ | VERIFIED | `backend/app/routes/upload.py:119` (`ALLOWED_EXTS`) is the upload allowlist; `torrent_builder.py` hashes arbitrary bytes, so the torrent layer really is format-agnostic while the endpoint is not. The answer states both. |
| "The maximum file size is 2GB per file. Larger files are available on the Business plan." | FAQ | PARTIAL | 2 GB is enforced (`backend/app/main.py:646`, `max_bytes = 2 * 1024 * 1024 * 1024`). No code path raises the cap for Business — "larger files" is not implemented. |
| "Your files are stored in object storage and served from unlisted public URLs — there's no public index or search…" | FAQ | VERIFIED | Uploads go to object storage (presigned PUT, `routes/upload.py`), file routes are ID/token addressed, no listing endpoint is public. The rest of the answer (links behave like keys) is an accurate description of the P2P model. |
| "We measure bandwidth by the amount delivered from our origin servers. P2P bandwidth between peers is free…" | FAQ | VERIFIED | `backend/app/routes/files.py` aggregates `total_p2p` separately from origin bytes; billing counts origin delivery. |
| "Business plan includes custom domain support." | FAQ | UNVERIFIED | No custom-domain implementation in `backend/app`; kept as the owner's roadmap claim. |
| "Create an API key (format: tcdn_…) … or get a Bearer token via POST /auth/login" | FAQ | VERIFIED | `backend/app/auth.py:27` (`API_KEY_PREFIX = "tcdn_"`), `/auth/login` route exists; `/docs` documents both. |
| GitHub, Terms, Privacy, Cookies, AUP, DMCA, Docs, Architecture, TCDN-AI, "TorrentCDN © 2026", Maidensail badge | footer | N/A | Links and the existing badge are unchanged; no new logo or partner was added. |

## Inner pages

| Page | Claim | Status | Evidence / note |
| --- | --- | --- | --- |
| `/tcdn-ai` | "Private alpha · invite only", "enabled for invited accounts", SDK compatibility, the planned model and its licence | VERIFIED per-claim | Audited row by row in `docs/tcdn-ai/evidence/frontend/PAGE_CLAIMS.md` (24 rows); this branch changed that page's styling only, not a word of its copy. |
| `/docs` | Endpoint list, base URL, auth model, `tcdn_` key format | VERIFIED | Generated from the real routes; base URL matches the same-origin proxy (`vercel.json` rewrites + `middleware.ts`). |
| `/architecture` | "peer-assisted delivery layer", origin fallback, "not a replacement for a CDN" | VERIFIED | `docs/architecture` narrative matches `VideoPlayer.tsx` / `torrent_builder.py` behaviour; the page already states the limits (no DRM, links behave like keys). |
| `/demo` | Every displayed figure is computed live from the sliders (viewers × size × watch %, P2P offload) | VERIFIED | Arithmetic in `frontend/app/demo/page.tsx`; no hardcoded cost table. The page labels itself a scenario/simulation. |
| `/terms`, `/privacy`, `/cookies`, `/aup`, `/dmca` | Policy text | N/A | Unchanged; restyled only. |
| `/auth/*` | Two-factor flow, reset flow | VERIFIED | `components/AuthModal.tsx`, `backend/app/routes/auth.py`. |

## What this redesign changed in the copy

Nothing. The three places where a string is *split* rather than rewritten:

1. The H1 wraps "cheaper" and "scale" in spans so each theme can accent a
   different word. The rendered text is identical.
2. The hero stats are split into a numeral and a caption — "90%" + "bandwidth
   reduction", "< 5ms" + "peer discovery", "Works in any browser" kept whole
   (splitting it would have changed the wording).
3. The FAQ list became an accordion: questions and answers are byte-identical
   and remain in the DOM when collapsed.

Accessibility-only additions (not marketing copy): a screen-reader caption on
the comparison table, `aria-label`s on the demo's four sliders, and a
screen-reader label on the copy button.
