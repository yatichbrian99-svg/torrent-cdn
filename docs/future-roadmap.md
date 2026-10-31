# TorrentCDN — the merged plan

**Date:** 2026-10-16 · **Status:** planning document, no implementation yet
**Read first:** [`vision.md`](vision.md) — the owner's north star, verbatim. This document exists to serve it.
**Companions:** [`dfs-phase-zero.md`](dfs-phase-zero.md) (repo audit + DFS M1), [`live-streaming.md`](live-streaming.md), `SECURITY.md`
**Rule:** every market claim carries a source (§10); every claim about TorrentCDN carries a file path. Anything unverified is marked as such, and **no number counts as a fact until the server measures it, not the browser** (§5).

> This supersedes the first draft of this document. The first draft assumed the product was "cheaper video delivery". The vision is much larger — a resource network. The research below says the larger thesis can work, but **not in the order the vision proposes, and not with bandwidth as the first paid resource.** Three assumptions failed checking (§2). They're fixable, and fixing them changes the order, not the goal.

---

## 0. Decisions taken (owner, 2026-10-16)

These are settled — the plan below implements them. Everything not listed here is still open (§9).

| # | Decision | Consequence |
|---|---|---|
| **D1** | **AI compute stays a gate, not a phase** (Stage 4). It advances only when every entry criterion in §4 Stage 4 is true. | The GPU ambition is preserved but cannot pull effort forward from Stages 0–3. First entry, if any, is inference on commercially provisioned GPUs. |
| **D2** | **The first paid resource class is storage/disk**, not bandwidth (Stage 3). | Stage 3 builds the node client around storage only. `file_manifests` + the verified availability meter become the core deliverables. |
| **D3** | *Implied by §2.1 and accepted:* **audience P2P stays free and voluntary**; we do not pay for commodity delivery bandwidth. | No provider earnings for bandwidth; offload is measured as a cost reduction against our own egress, not as revenue. |
| **D4** | *Implied by §2.1:* **no residential-proxy/scraping bandwidth resale**, ever. | Closes the only market that would pay $0.10+/GB for bandwidth (§2.1). |

---

## 1. Where the vision and the research already agree

| Vision | Research verdict | Status |
|---|---|---|
| Get off my PC, onto the Oracle VM, into the real world (§1) | Correct first move. We have zero production deployment and zero real users; everything else is unmeasurable until this exists. | **Adopt as Stage 0** |
| Prove P2P works before scaling (§2) | Correct — and it is *the* question, because the same finding says P2P only helps popular content, not the tail ([HN 30539791](https://news.ycombinator.com/item?id=30539791)). | **Adopt as Stage 1, with one repair (§2.2)** |
| Don't build everything at once (§11) | Matches the P2P post-mortem's biggest lesson: Spotify deleted P2P when its maintenance overhead outgrew the savings ([TechCrunch 2014](https://techcrunch.com/2014/04/17/spotify-removes-peer-to-peer-technology-from-its-desktop-client/)). | **Adopt** |
| Grow infrastructure only when a bottleneck forces it; usage pays for it (§12) | Sound, and it is the only way a solo operation survives the burn. Make it a hard budget rule (§6.3). | **Adopt as a gate** |
| Make joining ridiculously easy (§8) | Provider onboarding *is* the supply-side product. Nothing in our repo does this today. | **Adopt as Stage 3** |
| The network is the real product (§9) | Consistent with the platform argument; but reach it by shipping two resource classes, not by designing the abstraction first. | **Adopt as the long arc** |
| The rule: cheaper / faster / more reliable / more useful / easier to join (§The rule) | Adopt verbatim as the feature gate. Nothing enters a stage without naming which of the five it moves and by how much. | **Adopt** |

---

## 2. Where they conflict — and the evidence that resolves it

### 2.1 The paid-bandwidth arithmetic (this is the big one)

The vision's core loop is: *provider supplies bandwidth → customer pays → provider earns → we take a cut* (§The big idea, §6, §10). Here are the three numbers that decide whether that works:

| Number | Value | Source |
|---|---|---|
| What comparable networks **pay** a provider for bandwidth | ~**$0.10/GB** ($1 per 10 GB), minimum payout $20, real users earning **$5–20/month** | [Honeygain](https://www.honeygain.com/blog/passive-income-apps/), [maketecheasier](https://maketecheasier.com/what-is-honeygain/), [Medium $7.80/55 GB](https://medium.com/@theflyinggibbon/honeygain-it-took-me-5-months-and-1-day-to-earn-my-first-20-92eb65528525) |
| What **delivery** sells for | **$0.005/GB** (Bunny) · **$0.085/GB** (CloudFront) · **$0/GB egress** (R2) | [forasoft 2026-08-10](https://www.forasoft.com/blog/article/how-to-estimate-the-server-cost-for-a-video-platform-249), [Cloudflare Stream pricing](https://developers.cloudflare.com/stream/pricing/) |
| What the buyers who **do** pay $0.10+/GB are buying | residential **proxies**: **$1–$15/GB** advertised, $0.49–$4.90/GB at the cheap end | [dataimpulse 2026-06-28](https://dataimpulse.com/blog/residential-proxy-pricing-comparison/), [aimultiple 2026-10-02](https://aimultiple.com/proxy-pricing), [apiserpent 2026-08-14](https://apiserpent.com/blog/residential-proxy-pricing-effective-cost) |

Read those together: **if we pay a provider $0.10/GB to resell delivery, we are paying 20× the cheapest market price for the exact thing we're reselling.** Honeygain's model is viable only because its buyer is the residential-proxy market at $1–15/GB — 10–150× what it pays peers. We are not selling to that buyer, and we must not (see the non-goal below).

> **Resolution.** Two different products are hiding inside the vision, and they must not be merged:
> 1. **Audience P2P — free, voluntary, already built.** Viewers seed what they're watching. This is a *cost reduction* against our own egress bill, and at R2's $0/GB egress it competes against *zero*, not against Bunny. It is still worth having (it is the entire offload thesis, §5), but it is not revenue, and providers get credit/features rather than cash.
> 2. **Paid provider network — real money, but only for scarce resources.** Storage, GPU, specific geography, guaranteed latency. Sell bandwidth as *paid supply* only where the buyer's alternative is CloudFront-class pricing **and** the volume is worth the payout overhead. Never as the headline product.
>
> **Explicit non-goal:** we will not resell residential connectivity into the proxy/ad-verification/scraping market. It pays the most per GB precisely because the traffic is unwanted — it's the fastest route to being an abuse company, and the vision's own §7 ("abuse detection") plus our `/dmca` + `/aup` pages show we know it.

### 2.2 Their measurement list can't be measured yet (a repair, not a disagreement)

The vision's §2 proof list is right — viewers, bandwidth, P2P vs origin, **% offloaded**, startup, buffering, uptime, node failures, **cost per viewer**. The problem is what those numbers are *made of* today:

- `active_peers` is hardcoded `0` for VOD files (`backend/app/routes/files.py:204` and `:249`).
- `bytes_p2p` / `bytes_origin` are **self-reported by the embed client** (`backend/app/routes/analytics.py`), clamped but unverified. We cannot price, pledge or publish on them today.
- There is no server-side playback telemetry at all — no startup time, no rebuffer ratio, no error rate.

**Resolution:** verification ships *with* the measurement, in Stage 1 — not later. Otherwise Stage 1 produces numbers we'd have to walk back, which is worse than having none. Concretely: measure origin egress at the storage layer, derive offload = 1 − origin/delivered, and keep the client's claim only as a cross-check that can be flagged.

### 2.3 AI compute can't be Stage 4 as written (§4, §5)

The vision wants GPUs next after files. The evidence says the *supply* assumption is the weakest part of that plan:

| Finding | Number | Source |
|---|---|---|
| Registered GPU supply that survives verification | io.net: **5,350 verified of 327,000 registered** (~1.6%) after a cleanup audit | [zelcore 2026-06-29](https://zelcore.io/academy/news-explained/decentralized-compute-render-akash-io-net) (secondary; treat as directional, verify before quoting) |
| Active vs available GPU supply on a mature network | Akash Q1 2026: **84 active of 334 available** | same |
| Price floor to beat | A100 80GB **$1.32/hr spot, $1.43/hr on-demand**; Vast marketplace **$0.67/hr**; cheapest tracked listing **$0.020/GPU/hr** (GTX 1050 Ti, Salad) | [spheron 2026-10-01](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/), [getdeploying](https://getdeploying.com/gpus) |
| Decentralized GPU discount claim | "50–70% below AWS" | [buildmvpfast 2026-07-03](https://www.buildmvpfast.com/blog/depin-gpu-inference-io-net-akash-below-aws-2026) |

Two things follow. First, **GPU supply is fake until proven** — the entire product is attestation, isolation and reputation, not matchmaking. Second, at $0.02–$1.43/GPU/hr, a provider earning $1/day needs high sustained utilisation, and the buyer has a dozen verified alternatives. And §5 of the vision puts **customer prompts on a stranger's home PC** — that is a data-processing agreement, an isolation guarantee and an incident-response story, none of which we have.

> **Resolution:** keep the ambition, move it behind a gate (§4 Stage 4) with named entry criteria. When we do enter, start with **inference on commercially provisioned GPUs and business-line nodes we can hold accountable**, not residential GPUs, and never for customer data until isolation + agreements are proven.

### 2.4 Residential connections mostly can't legally be provider nodes

The vision's "someone has an internet connection → they provide it → they earn" (§The big idea) collides with the contracts those connections sit on:

- "Users may not resell, share, or otherwise distribute the Service or any portion thereof to any third party" — business fiber terms ([Optimum](https://www.optimum.com/terms-of-service/business/Fiber-internet)).
- AUPs reserve the right to "suspend or terminate Residential Broadband Service accounts where data consumption is not characteristic of a typical [residential customer]" ([example AUP](https://outerreachbroadband.com/acceptable-use-policy/)).
- "Many residential broadband contracts contain a clause that forbids running servers on the connection" ([SSD Nodes 2026-10-13](https://www.ssdnodes.com/learn/is-self-hosting-legal)); operators report enforcement is complaint-driven — i.e. it bites once you succeed ("telcos only enforce the server rule if you are… maxing out your bandwidth 24/7", [HN](https://news.ycombinator.com/item?id=18238827)).

Physical constraints stack on top: residential **upload** asymmetry (10:1 in the HN CDN post-mortem), CGNAT across Asia/Africa/LatAm, and US data caps ([HN 30539791](https://news.ycombinator.com/item?id=30539791)).

> **Resolution:** the **first paid provider cohort is commercially provisioned supply** — VPS, seedboxes, colo, business lines. They can legally serve, they have symmetric uplinks, and they run unattended. Residential contribution comes later, only for resource classes that don't resell connectivity (storage, idle GPU on the owner's own account), with the terms of service clearly the provider's own responsibility and never presented as "sell your ISP connection".

### 2.5 Deploying to the VM is agreed — but needs an exit criterion

Both plans say get it off the PC (§1 / Stage 0). Neither said what "done" looks like, so add one: **Stage 0 exits on numbers**, not on vibes (§4).

---

## 3. What each plan was missing

| Missing from the vision | Where it comes from | Why it matters |
|---|---|---|
| P2P consent + IP disclosure | My draft; [PeerTube privacy guide](https://docs.joinpeertube.org/admin/privacy-guide), [arXiv 1709.05395](https://arxiv.org/html/1709.05395v1) | Viewers' IPs go to peers; a paid product can't skip disclosure, and it's cheap to add |
| QoE telemetry split P2P on/off | [Mux](https://www.mux.com/blog/rebuffering-the-most-frustrating-and-frequent-problem-for-video-consumers) | Rebuffering is the #1 complaint; without this we can't prove P2P isn't hurting viewers |
| Server-side verification of offload | In-repo L5 (`routes/analytics.py`) | The whole thesis rests on this number |
| Captions/subtitles + accessibility | [W3C WCAG Level A](https://www.w3.org/WAI/media/av/captions/) | Business buyers ask; we have zero today |
| Abuse/DMCA posture for a *node network* | `/dmca`, `/aup` pages | A network of unknown nodes holding other people's content is the highest-risk surface in the plan |
| Payout rails, thresholds, KYC/tax for micro-amounts | Honeygain's $20 minimum payout | Paying 500 providers $3 each is an operations problem before it's a product |
| "We will not claim decentralization" discipline | `dfs-phase-zero.md` §8 | P2P is a delivery strategy, not a durability or governance story |

| Missing from my draft | Where it comes from | Why it matters |
|---|---|---|
| The node client + onboarding flow | vision §8 | This is the supply-side product; my draft only had a storage-node toggle |
| Node reputation + automatic workload migration | vision §7 | "If a node disappears, the workload should move" — that's a scheduler, not a feature |
| Provider dashboard (usage, jobs, earnings, uptime) | vision §8 | Providers need to see they're earning, or supply never compounds |
| The usage-funded-infrastructure budget rule | vision §12 | Keeps a solo operation solvent; becomes an explicit gate |
| The network-as-product abstraction | vision §9 | Correct long arc; I had been designing applications |
| GPU/compute as a first-class resource class | vision §4–6 | The largest ambition — now gated with evidence rather than deleted |

---

## 4. The merged plan — stages, and the gate that ends each one

Order is set by **dependency**: verification before claims, streaming before files, paid storage before paid GPU, marketplace after two working resource classes. Every stage names its exit gate; a stage that can't show its numbers does not advance.

### Stage 0 — Off the PC and into the real world (2–4 weeks)

**Build:** production deploy to the Oracle VM using the existing deployment notes (`docs/live-streaming.md`, `SECURITY.md`, `docker-compose.yml`, `Caddyfile`, `render.yaml` for the API shape — reconcile whichever is authoritative *before* touching anything), real domain + TLS, backups proven by an actual restore, uptime/error monitoring, secrets rotated per `SECURITY.md`, tracker/webseed config verified from the public internet, cost baseline written down.

**Exit gate:** 30 consecutive days of uptime · ≥3 real creators uploading and sharing without hand-holding · the live path verified by a real OBS publish (the open BETA gate in `docs/live-streaming.md`) · **the first bottleneck identified with a number attached** · VM cost covered by usage (vision §12) · no open CRITICAL/HIGH from the existing harnesses (`backend/scripts/pentest_local.py`, `pentest_idor.py`).

**Non-goals:** new features, autoscaling, multi-region, buying anything.

### Stage 1 — Prove the network (4–6 weeks)

This is vision §2, repaired. **Build:** server-side offload derivation (§2.2); real `active_peers` replacing the hardcoded `0` (`routes/files.py:204,249`); `playback_events` (startup_ms, rebuffer_ms, rebuffer_count, error_code, level_switches, p2p_enabled) with the clamp discipline already used in `routes/analytics.py`; P2P consent + IP disclosure + off switch (`frontend/public/sdk.js`, `frontend/app/embed/[file_id]/page.tsx`); a per-browser error taxonomy; auto-demotion to origin-only when a file's P2P rebuffer ratio exceeds its origin baseline.

**Exit gate — the first real proof, and it must be falsifiable:**
- **Verified offload %** on live streams and embeds, computed from origin egress, not from the client.
- **QoE with P2P on ≤ origin-only baseline** for startup and rebuffer ratio. If this fails, P2P is a liability and the cost story dies here — that is a *useful* answer, not a failure.
- **Cost per viewer** in money, from the invoice, not from estimates.
- Node/peer failure rate and recovery behaviour observed over weeks, not minutes.

**Non-goals:** paying providers, GPU, marketplace.

### Stage 2 — Files on the same network (6–8 weeks)

Vision §3. **Build:** peer-assisted downloads for regular files with the `ws=` webseed mandatory and byte-exact per-chunk verification (the DFS M4 shape already sketched in `dfs-phase-zero.md`); share links/paywall/tips on top of the shipped `share_links` plumbing; captions (WCAG Level A — `captions` table, `/files/{id}/captions`, `<track>` in `components/VideoPlayer.tsx`); discovery/SEO (video sitemap in `frontend/app/sitemap.ts`, JSON-LD, public pages, search via Postgres FTS); outbound webhooks + API keys in practice.

**Exit gate:** verified offload on file delivery · ≥1 paying customer whose need is files, not video · captions working end-to-end · upload completion rate and time-to-publish measured.

**Non-goals:** feed algorithms, comments, transcoding ladders beyond what playback requires.

### Stage 3 — The first *paid* supply: storage (8–12 weeks) · **ratified, D2**

This is where the vision's marketplace becomes real, and it starts with the resource whose price is genuinely scarce — **disk on machines we don't own** — not bandwidth (§2.1, D3). Also where the vision's §8 becomes a product.

**Build:** the **TorrentCDN Node client v1** (download → sign in → choose resources → start earning → dashboard with usage/jobs/earnings/uptime), storage-only resource class, `file_manifests` (exactly as specified in `dfs-phase-zero.md` §8/§9 — `uploads` stays the ownership source of record), verified availability meter ("12 of 12 chunks, last verified 6 min ago"), reputation v1, workload re-placement when a node vanishes, and payout operations with a **threshold** (batch payouts; Honeygain's $20 minimum exists for a reason) plus KYC/tax handling for anyone who clears it.

**First provider cohort: commercially provisioned** (VPS/seedbox/colo/business line), per §2.4.

**Exit gate:** contributed GB across ≥N independent providers · availability SLA met over 30 days · providers actually paid, with a published unit cost per GB-month stored and served · re-replication proven by killing a node · no unverified byte paid for.

**Non-goals:** residential bandwidth resale, proxy traffic, EC until replication invariants are measured (per `dfs-phase-zero.md` §5.3).

### Stage 4 — Decision gate: AI compute (go / no-go, not a phase) · **ratified as a gate, D1**

Vision §4 and §5 are neither approved nor rejected here — they get **entry criteria**, all of which must be true before we spend a week on it:

1. Stages 1–3 exit gates met, with ≥N paying customers and positive contribution margin on at least one resource class.
2. **Supply verification solved in principle** — an attestation design that would have caught the 1.6%-real problem (§2.3), tested on a handful of machines we control.
3. **Isolation proven** — per-workload sandboxing, resource limits, and a demonstrated escape test; the repo currently has *no* container/scheduler code, so this is greenfield.
4. **A named buyer segment and its price ceiling** — e.g. cents per 1M tokens for a specific model class, benchmarked against the $0.67–1.43/GPU-hr market, not against "AWS is expensive".
5. **Data agreements** — a DPA/retention story for customer prompts on third-party hardware, or a hard rule limiting us to customer-owned or non-sensitive workloads.
6. **Legal review** of provider terms and cross-border data movement.

If the gate passes: enter via **inference on commercially provisioned GPUs** we can hold accountable. If it fails on 2–5, the correct answer is to keep selling delivery + storage, which is a business.

### Stage 5 — The marketplace surface (10–14 weeks)

Vision §6 and §10. **Build:** explicit resource classes (storage, compute, GPU-inference, delivery, edge/geography), matching/placement, business plans with guaranteed capacity, SLAs, private networks, premium pricing for latency/geography, published provider and buyer dashboards.

**Exit gate:** one business customer paying monthly for guaranteed capacity · gross margin per resource class positive and published internally · a second independent provider cohort onboarded without founder involvement.

### Stage 6 — The network as the product

Vision §9. Public API, node SDK, published economics, provider/buyer reputation portable across resource classes. **Reached by having two working resource classes, not by designing the abstraction up front.**

---

## 5. The scoreboard (vision §13, made falsifiable)

| Metric | Source that counts | Why |
|---|---|---|
| Verified offload % | origin egress at the storage layer | The core thesis; client-reported bytes do not count (L5) |
| QoE: p75 startup, rebuffer ratio, fatal error rate — **split P2P on/off** | `playback_events` | Only way to know our feature isn't hurting viewers |
| Cost per viewer / per delivered GB | invoice ÷ delivered bytes | Vision §2 and §12 both need this in money |
| Node count and contributed capacity, by class | provider registry | Vision §13 |
| Node failure & recovery rate | scheduler records | Vision §2, §7 |
| Provider earnings paid | ledger | The supply-side promise |
| Revenue, cost, margin per resource class | finance + ledger | Tells us which class deserves the next quarter |
| Uptime | external monitor | Vision §2 |

**Rule:** a metric is not reportable unless the server produced it. Anything the browser tells us is a cross-check to be flagged, never a fact — that rule is what keeps §2.2's repair from un-happening.

---

## 6. Money, honestly

1. **Transaction fees (vision §10) apply to scarce classes** — storage, compute, placement — not to commodity delivery bandwidth, for the arithmetic in §2.1.
2. **Business/enterprise** (guaranteed capacity, SLAs, private networks, support) is the revenue that survives contact with Cloudflare/Bunny pricing.
3. **Usage-funded infrastructure (vision §12) becomes a budget rule:** no new spend until the previous spend is covered by usage, and every purchase must name the bottleneck metric it fixes.
4. **Provider payouts need operations, not just a ledger:** thresholds, batching, KYC/tax for anyone clearing them, and a published change policy — the platforms in §2.6 of the previous draft lost creator trust by changing rules unilaterally, and we'd be doing the same thing to our providers.

---

## 7. What we will not claim, and will not build

**Will not claim:** that P2P makes content durable (peers leave); that we are decentralized (auth, metadata, matching and manifests stay centralized — `dfs-phase-zero.md` §8); that offload numbers are verified before Stage 1 ships; that GPU supply is real before attestation; that any saving is proven before Stage 0's cost baseline exists.

**Will not build:** residential proxy/scraping bandwidth resale; a token, staking or yield of any kind; crypto payouts (excluded in `dfs-phase-zero.md` §2); customer prompts on residential GPUs before isolation + agreements; ads; comments; collaborative editing; distributed KV.

---

## 8. Risks

| # | Risk | Severity | First control |
|---|---|---|---|
| R1 | P2P measurably hurts QoE and we market it anyway | High | Stage 1 gate: QoE with P2P on must not exceed origin baseline |
| R2 | **Paid-bandwidth margin trap** — we pay $0.10/GB for something the market sells at $0.005/GB | High | §2.1: paid marketplace = scarce classes only; bandwidth stays voluntary |
| R3 | **Fake supply** — nodes claim resources they don't have (the 1.6% problem) | High | Attestation + verified-bytes-only payouts, before Stage 3 pays anyone |
| R4 | ISP terms / resale bans terminate provider accounts once we succeed | High | Commercial cohort first (§2.4); residential limited to non-connectivity classes |
| R5 | Abuse magnet — illegal content on unknown nodes | High | Never accept third-party ingests; `/dmca` + `/aup` enforcement wired to node eviction |
| R6 | Customer data on third-party hardware (privacy/DPA) | High | Stage 4 criterion 5; default to non-sensitive workloads |
| R7 | Payout compliance (KYC/tax/mass micro-payouts) | Medium | Thresholds + batched payouts before launch of any earnings page |
| R8 | Cost of verification/replication exceeds the savings | Medium | Publish unit economics per class at Stage 3 exit, before scaling |
| R9 | Consent UI suppresses offload, gutting the cost thesis | Medium | Measure it in Stage 1; if it kills offload, the pivot is "cost predictability + ownership", not peer savings |
| R10 | Scope creep across six stages with a small team | High | The vision's own rule + Stage gates as the only entry point |

---

## 9. Open questions (cheapest first)

1. **Which Oracle VM config, and is it already provisioned?** Stage 0 is blocked on this detail, not on code.
2. **5 creator interviews** (3 hours) — is the buyer cost-sensitive or QoE-sensitive? Decides Stage 1 vs Stage 2 emphasis.
3. **Our own invoice math** — real R2/render/Paystack numbers vs delivered bytes. Needed before any pricing claim or Stage 3 unit economics.
4. **Legal hour on resale + seeding + cross-border data** — cheapest *before* Stage 3 pays providers, not after.
5. **GPU price ceiling from a real buyer** — one conversation with someone actually paying for inference today (Stage 4 criterion 4).
6. **Confirm the io.net/Akash supply figures** from a primary source before they appear in any external document (currently secondary).

---

## 10. Sources (accessed 2026-10-16)

**P2P economics and its ceilings** — [HN 30539791 "Why don't many video hosting companies adopt PeerTube or P2P?"](https://news.ycombinator.com/item?id=30539791) (2022-03-03; commenters `freefaler`, `giantrobot`, `zinekeller`, `matheusmoreira`) · [Spotify dropping P2P](https://techcrunch.com/2014/04/17/spotify-removes-peer-to-peer-technology-from-its-desktop-client/) (2014-04-17)
**Quality of experience** — [Mux rebuffering survey](https://www.mux.com/blog/rebuffering-the-most-frustrating-and-frequent-problem-for-video-consumers) (2017-05-08) · [dev.to "Why I stopped self-hosting videos"](https://dev.to/masonwritescode/why-i-stopped-self-hosting-videos-and-moved-to-a-video-api-fn3) (2025-05-19) · [Swarmify, self-hosted player mistakes](https://swarmify.com/blog/self-hosted-video-player/) (2026-04-11) · [hls.js #7008](https://github.com/video-dev/hls.js/issues/7008) · [hls.js #522](https://github.com/video-dev/hls.js/issues/522) · [Android WebView freeze](https://stackoverflow.com/questions/73271339/)
**Privacy and consent** — [PeerTube privacy guide](https://docs.joinpeertube.org/admin/privacy-guide) · [arXiv 1709.05395 "One Leak Will Sink A Ship"](https://arxiv.org/html/1709.05395v1) (2017-09-15) · [security.org WebRTC leaks](https://www.security.org/vpn/webrtc-leak/) · [seeding-as-legal-risk thread](https://www.reddit.com/r/BiglyBT/comments/139zngj/use_case_for_webtorrent_http_seed_browser_to/) (2023)
**Delivery cost** — [forasoft 2026-08-10](https://www.forasoft.com/blog/article/how-to-estimate-the-server-cost-for-a-video-platform-249) · [BlazingCDN 2026-09-18](https://blog.blazingcdn.com/en-us/cloudflares-pricing-for-video-streaming-services) · [Cloudflare Stream pricing](https://developers.cloudflare.com/stream/pricing/)
**Upload reliability** — [AWS in Plain English 2025-11-09](https://aws.plainenglish.io/uploading-videos-to-amazon-s3-buckets-faster-using-multipart-upload-73753d6de396)
**Accessibility and discovery** — [W3C captions](https://www.w3.org/WAI/media/av/captions/) · [WCAG 1.2.2](https://www.w3.org/WAI/WCAG22/Understanding/captions-prerecorded.html) · [Section508.gov synchronized media](https://www.section508.gov/create/synchronized-media/) · [Level Access 2026-10-05](https://www.levelaccess.com/blog/closed-captioning/) · [r/selfhosted subtitles](https://www.reddit.com/r/selfhosted/comments/1uvrzq6/) · [video SEO thread](https://support.google.com/googlenews/thread/10007046/)
**Creator economics** — [blog.youtube 2026-08-10](https://blog.youtube/news-and-events/youtube-partner-program-updates-2027-new-opportunities-earn/) · [posteverywhere 2026-03-17](https://posteverywhere.ai/blog/how-much-do-facebook-creators-make) · [Meta: in-stream ads ended 2025-08-31](https://www.facebook.com/business/help/1049081556813520) · [Digiday 2026-08-20](https://digiday.com/media/the-winners-and-losers-of-youtubes-view-count-and-monetization-overhaul/) · [novicenolonger](https://novicenolonger.com/youtube-small-creator-demonetized/)
**Durability rationale** — [Storj on replication vs erasure coding](https://www.storj.io/blog/replication-is-bad-for-decentralized-storage-part-1-erasure-codes-for-fun-and-profit) (2018-11-05)
**In-repo** — `docs/dfs-phase-zero.md` (incl. its §10 sources on IPFS/Storj/Syncthing), `docs/live-streaming.md`, `docs/vision.md`, `SECURITY.md`, `README.md`.

**New in this revision:**
- Paid-bandwidth arithmetic — [Honeygain earnings](https://www.honeygain.com/blog/passive-income-apps/), [maketecheasier](https://maketecheasier.com/what-is-honeygain/), [user datapoint](https://medium.com/@theflyinggibbon/honeygain-it-took-me-5-months-and-1-day-to-earn-my-first-20-92eb65528525); proxy prices [dataimpulse 2026-06-28](https://dataimpulse.com/blog/residential-proxy-pricing-comparison/), [aimultiple 2026-10-02](https://aimultiple.com/proxy-pricing), [apiserpent 2026-08-14](https://apiserpent.com/blog/residential-proxy-pricing-effective-cost)
- GPU supply verification and prices — [zelcore 2026-06-29](https://zelcore.io/academy/news-explained/decentralized-compute-render-akash-io-net), [spheron 2026-10-01](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/), [getdeploying](https://getdeploying.com/gpus), [buildmvpfast 2026-07-03](https://www.buildmvpfast.com/blog/depin-gpu-inference-io-net-akash-below-aws-2026)
- ISP/resale constraints — [Optimum business fiber ToS](https://www.optimum.com/terms-of-service/business/Fiber-internet), [example AUP](https://outerreachbroadband.com/acceptable-use-policy/), [SSD Nodes 2026-10-13](https://www.ssdnodes.com/learn/is-self-hosting-legal), [HN 18238827](https://news.ycombinator.com/item?id=18238827)

## 11. Limitations of this document

- **No primary user research exists.** Every demand signal is second-hand; §9.2 is the cheapest fix.
- The io.net/Akash supply figures are from one secondary source (§9.6) — directionally decisive, not quotable externally yet.
- GPU price floors are list prices from comparison sites, not negotiated or measured by us.
- Honeygain's per-GB rate is its own published marketing figure; the user datapoint ($7.80/55 GB) corroborates the order of magnitude only.
- No cost model on our own invoices (§9.3), so every margin statement here is structural, not numeric.
- Stage durations are estimates, not team estimates; the GPU gate deliberately has no date.
