# TCDN-AI console — frontend plan (v1)

**Branch:** `feat/tcdn-ai-console` (off `main` @ `a7ad768`) · **Status:** plan first, then
build in the stages in §9 · **Constraint that shapes everything:** the AI feature is
*off* in production and this work must not change that, must not change any existing
page, and must add no route, request or bundle weight when the flag is off.

---

## 1. What exists today, and what the dashboard must therefore add

The applied audit found the AI surface is **API/SDK-first**: `/api/ai/v1` has
18 paths, all API-key or signed-envelope authenticated, and `frontend/` contains
**zero** references to it. The console therefore needs two things, in this order:

1. **A small account-scoped API** — the existing AI API cannot serve a browser:
   `/balance`, `/usage` and `/models` demand an *API key* (which the dashboard is
   supposed to help create), and key create/revoke exists only under
   `/admin/api-keys` behind an admin JWT. There is also no "start a credit
   purchase" endpoint at all — only a provider webhook (`/billing/webhook`) that
   *credits* afterwards.
2. **The console itself** — routes under `/ai/*`, reusing the CDN's login,
   session handling and design tokens.

Everything new is behind `ENABLE_TCDN_AI` and adds no behaviour to existing pages.

## 2. Gating mechanism (the one decision to get right)

The backend already promises: *flag off ⇒ no AI route and no AI DDL*. That
promise is tested (checklist 1.9, `test_the_ai_schema_is_only_migrated_when_the_flag_is_on`).
So the frontend must **not** require an always-mounted `/api/ai/.../feature`
probe, which would hand the flag-off build a new AI route.

**Mechanism: the public status endpoint doubles as the feature probe.**

```
GET /api/ai/v1/status   (public, no auth, no secrets)
  200 with {enabled: true, ...}  → AI section is available
  404                            → feature is off: hide every AI link and route
  network error / 5xx            → treat as off (fail closed), show nothing
```

- One request, only on pages that already render a nav (so it must be cheap and
  cached): the result is memoised in a module-level promise for the tab's
  lifetime, and re-probed on `visibilitychange` at most once a minute.
- `NEXT_PUBLIC_*` env vars are **not** used for gating: a build-time flag would
  bake the section into a production bundle that never serves it, and the
  bundle-size requirement (§8) is measured, not asserted.
- With the flag off the request 404s, the nav link never renders, and every
  `/ai/*` URL renders a plain "AI is not available on this deployment" panel
  (no login wall, no redirect), so a stale bookmark does not dump the user into a
  half-built page.
- The probe lives in `lib/aiConsole.ts`, not in the shared `lib/api.ts`, so the
  landing/dashboard/upload bundles gain nothing.

## 3. API contract to add (`backend/app/ai/routes_console.py`)

All routes are mounted **only when the flag is on**, use the CDN's
`get_current_user_id` dependency (JWT or `tcdn_` key, email-verification gate,
logout blocklist — exactly as the rest of the app), and map
`account_id = str(user_id)` (the `ai_accounts.account_id` FK is `users.id`).
Prefix: `/api/ai/v1/console`.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/summary` | balance + usage totals + system status + checklist inputs | one round trip for the overview |
| GET | `/models` | catalogue: model, format, VRAM tier, standard prices, confidential prices flagged unavailable, allowlisted | mirrors `ModelInfo`, no manifest hashes |
| GET | `/keys` | list of the caller's keys: **prefix only**, id, scopes, status, created/last-used | never the hash, never the raw key |
| POST | `/keys` | create a key | raw key returned **once** in the response body, never logged, never persisted client-side |
| POST | `/keys/{id}/revoke` | revoke | scoped to the caller's account; idempotent |
| GET | `/usage?limit=&model=&status=` | jobs table: job id, model, status, tokens, cost, created | server-side filtering, capped `limit` |
| GET | `/jobs/{job_id}` | one job: status, region, receipt/verification state, tokens, cost | **never** prompts or outputs (they are not stored, so there is nothing to leak) |
| GET | `/billing/packs` | credit packs with USD credits and KES price | static, operator-editable |
| POST | `/billing/checkout` | start a Paystack **test-mode** transaction | refuses when `TCDN_AI_PAYMENTS_LIVE` is on; returns `authorization_url` + reference |
| GET | `/billing/checkout/{reference}` | pending / paid / failed | "paid" = the provider reference is present in the ledger (idempotency key) |
| POST | `/billing/checkout/{reference}/simulate` | **test seam only** | refuses unless `APP_ENV` ∈ {development, test} *and* `TCDN_AI_FAKE_CHECKOUT=1` *and* the flag is on; exists so the e2e journey can complete a purchase without a provider |

Store additions: `revoke_api_key(account_id, key_id)` (memory + Postgres), and a
`list_api_keys(account_id)`; both filter by account and never return `key_hash`.
`ai_api_keys` already has `status`, `revoked_at`, `prefix`, `scopes` — no DDL
change, so **migration surface is unchanged**.

Unchanged on purpose: `/status` (public shape), `/models` (API-key), `/balance`,
`/usage`, `/placements`, receipts, attestations, webhook — the console is
additive, so the 40018 acceptance, the ZAP path set and every existing contract
test keep their meaning.

## 4. Frontend structure

```
frontend/
  lib/aiConsole.ts                 # types + client + feature probe + formatting
  components/ai/AiGate.tsx         # probe → children | "not available" panel
  components/ai/AiShell.tsx        # section header + tab nav + footer note
  components/ai/ui.tsx             # StatCard, Badge, Table, EmptyState, ErrorState, Skeleton
  components/ai/CopyField.tsx      # clipboard + "shown once" affordance
  app/ai/layout.tsx                # AiGate + AiShell
  app/ai/page.tsx                  # 1. overview
  app/ai/billing/page.tsx          # 2. buy credits
  app/ai/keys/page.tsx             # 3. API keys
  app/ai/usage/page.tsx            # 4. usage + jobs (+ /ai/usage/[jobId])
  app/ai/models/page.tsx           # 5. models and prices
  app/ai/quickstart/page.tsx       # 6. snippets + "test my key"
  app/ai/status/page.tsx           # 7. public-ish status detail
```

Nav: `Navbar.tsx` gains one conditional link ("AI"), rendered only after the
probe returns enabled; it is a plain `<a href="/ai">` like the other links.

**Lazy loading:** `/ai/*` is a separate route segment with no import path into
the landing page, `dashboard`, `billing` or `upload`. The heavy pieces (charts,
snippet tabs) are `next/dynamic` with `ssr: false` so they are not in any shared
chunk. Bundle analysis (§8) is the proof, not the claim.

**Design system:** existing CSS variables only — `--bg-primary`, `--bg-card`,
`--border`, `--border-strong`, `--text-primary/muted/dim/soft`, `--ok`, `--err`,
`--inverse-bg/fg`; `font-mono` labels with `uppercase tracking-widest` for
section ears, 1px borders, square corners, no shadows or gradients, same as
`/dashboard` and `/billing`. Light/dark both come free from those tokens and are
checked in the screenshot pass.

## 5. Page-by-page behaviour

1. **Overview** — balance in USD (`credits` / `available_micro`), an approximate
   local-currency line (`KES`, from the pack rate, labelled "≈"), 7-day job and
   spend summary, system status pill (from `/status`: flag, build commit,
   Postgres/Redis probes, worker count), and a getting-started checklist
   (verified account → create key → first request → add credits) where a step is
   *derived from data*, never faked.
2. **Buy credits** — pack presets (USD credits, KES price, both shown), channel
   note from what the backend reports, then Paystack hosted checkout in test
   mode. States: idle, redirecting, `?pending=<ref>` polled, paid (receipt with
   reference, amount, credits added), failed/cancelled. No card data touches this
   app; no key, public or secret, appears in the frontend.
3. **API keys** — create (name optional, scopes from what the API supports:
   `inference`, `worker`), then the raw key shown **once** in a `<code>` block
   with a copy button and a "you will not see this again" warning; list shows
   prefix + `…`, status, created, last used; revoke needs a confirm step.
   No key is ever written to `localStorage`, `sessionStorage`, cookies, the URL
   or a log.
4. **Usage and jobs** — table: job, model, status, input/output tokens, cost,
   time; filters (model, status) and a page cap; row → job detail with status,
   region, token counts, cost breakdown, receipt/verification state
   (verified / sampling / pending / not sampled). Explicit "we never store
   prompts or outputs" note; nothing in the UI tries to show them.
5. **Models and prices** — table from `/console/models`: model, format, VRAM
   tier (from `required_vram_gb`), standard input/output price per Mtok,
   availability. Privacy: **Standard** selectable; **Confidential** shown with a
   "coming soon" badge and no price-as-available framing. Any model the backend
   marks non-allowlisted is dimmed and labelled. No "secure", "private",
   "production ready" claims anywhere.
6. **Quickstart** — base URL (`${origin}/api/ai/v1`), the model ids the API
   actually returned, and tabs: curl, OpenAI Python, OpenAI JS, Cursor, Cline,
   Continue, OpenCode. Each snippet contains a `YOUR_API_KEY` placeholder, never
   a real key. "Test my key" makes exactly one minimal
   `POST /chat/completions` (`max_tokens` small) with a *pasted* key held in
   component state only, and renders the raw status + body so a 401, 402, 429 or
   502 is diagnosable.
7. **Status** — from the public `/status`: overall state, per-region worker
   counts and heartbeat age, dependency probes, build commit, flag state. No
   node ids, endpoints or key ids (the endpoint does not return them).
8. **States** — every page has loading (skeleton), empty ("no jobs yet" with the
   quickstart link), error (message + retry), and unavailable ("AI service
   unavailable" when `/status` is 5xx/network error) variants. Empty states are
   never a blank box.

## 6. Safety rules applied to every component

- **Auth** — the existing `lib/auth.ts` token (session or remembered) is read by
  the existing axios instance; no new storage, no new cookie, no token in a URL.
- **XSS** — no `dangerouslySetInnerHTML` anywhere; every API string is a React
  text child; job ids and model names are validated/encoded before use in a link
  path, and unknown values render as plain text.
- **CSRF** — the API is bearer-token only (no cookies), so there is no ambient
  credential to forge; state-changing calls are POST/DELETE with `Authorization`.
- **CSP** — no inline `<style>`/`<script>`, no `unsafe-inline`, no new origin:
  the console calls the same-origin `/api` proxy, which is already in
  `connect-src` via `'self'`. Clipboard access is a user-gesture API, not CSP.
- **Secrets** — the one-time key lives in React state for the lifetime of the
  page and is cleared on navigate/refresh; the "test my key" input likewise and
  is masked by default.
- **Input validation** — key name ≤ 64 chars, scopes from a fixed set, job id
  `^[A-Za-z0-9_-]{1,64}$`, pack id from the returned list, limit clamped
  1–200. Client checks are for UX; the server validates the same rules.
- **Headers** — unchanged; the console adds no header and needs no new
  directive. ZAP must stay green (§8).

## 7. Accessibility and responsiveness

- Semantic landmarks (`main`, `nav`, `table` with `<caption>`/`<th scope>`),
  one `h1` per page, labelled controls, `aria-live="polite"` for async states
  (key created, purchase pending), visible focus rings using the existing
  tokens, no colour-only status (text + shape), contrast checked against
  `--text-muted` on `--bg-card` (the weakest pair already in use).
- Keyboard: tabs reachable in order, copy button focusable, dialogs (revoke
  confirm, one-time key) trap focus and close on Escape.
- axe-core on every page in the e2e run, zero serious/critical violations.
- 375 / 768 / 1280 screenshots of every page; tables scroll inside their own
  container rather than the page (`overflow-x-auto`), and the page itself must
  not scroll horizontally.

## 8. Evidence to produce (`docs/tcdn-ai/evidence/frontend/`)

| Area | Command / artefact |
|---|---|
| Typecheck | `npx tsc --noEmit` |
| Lint | `npx eslint` if configured, else the repo's existing checks (`next build` + tsc) |
| Unit | `npx vitest run` (new component + lib tests) |
| Flag ON e2e | `node e2e/ai-console.mjs` — login → buy test credits → create key → call API → see usage, every page visited, axe per page |
| Flag OFF proof | `node e2e/ai-flag-off.mjs` — no AI link, `/ai` shows the unavailable panel, **zero** AI network requests, existing pages byte-identical in behaviour (nav items, headings, no console errors) |
| Bundle | `node scripts/ai_bundle_report.mjs` — build with the flag off and on, compare `.next` chunk sizes, prove `/` and `/dashboard` do not reference the AI chunks |
| Lighthouse | `npx lighthouse` against the local flag-on stack for each page, mobile preset, report JSON kept |
| Accessibility | axe results per page from the e2e run |
| Responsive | `frontend/e2e/screenshots/ai-*.png` at 375/768/1280, reviewed and iterated |
| Security | dependency audit diff, CSP/ZAP note, and a component-by-component XSS/CSRF review table |

Local flag-on stack for the e2e run: disposable Postgres 16.4 + Redis 7.4.2,
backend `ENABLE_TCDN_AI=true TCDN_AI_FAKE_CHECKOUT=1` (test seam, §3), frontend
`next dev` with `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000`. The flag-off run is
the same stack with `ENABLE_TCDN_AI` unset.

## 9. Delivery stages (each pushed with a short report)

| Stage | Contents |
|---|---|
| 0 | this plan |
| 1 | backend console API + store additions + tests; frontend shell, gate, overview, keys, models, quickstart |
| 2 | buy credits (packs, checkout, pending/paid/failed), usage/jobs + detail, status |
| 3 | polish: empty/error/loading everywhere, axe, responsive screenshots + iteration, Lighthouse, bundle report, flag-off e2e, docs and checklist row 9.2 |

## 10. Checklist and claims

- Only **row 9.2** is in scope, and only for the parts actually exercised: key
  management, models, quickstart snippets, status. It stays **PARTIAL** unless
  every listed console (developer/host/admin) is exercised — host and admin
  consoles are out of scope for v1.
- Nothing in the UI claims "secure", "private" or "production ready"; the
  confidential tier is labelled "coming soon"; simulated adapters are labelled
  where shown.
- No merge to `main`, no production calls, no live keys, `ENABLE_TCDN_AI` stays
  unset in production, and existing pages keep their current tests passing.

## 11. Known limits of this v1

- Credit purchases are **test-mode only** by construction (the endpoint refuses
  when `TCDN_AI_PAYMENTS_LIVE` is on); flipping that is a separate, audited
  owner action.
- Local-currency display is an approximation from the pack rate, labelled "≈";
  it is not an FX quote.
- No host or admin console, no team accounts, no billing history export.
- The e2e purchase completes through a test-only simulation endpoint that
  refuses to exist outside development/test.

---

## 12. Delivery record and preview instructions

**Status: built and exercised on `feat/tcdn-ai-console`** (draft PR, not merged).
Stages 0–3 are in the branch history; the measured results are in
`evidence/frontend/README.md`, and checklist row 9.2 is now PARTIAL for exactly
the developer-console scope below.

### What shipped

`app/ai/{layout,page,billing,keys,models,quickstart,status,usage,usage/[jobId]}`,
`components/ai/{AiGate,AiShell,ui}`, `lib/aiConsole.ts`, one conditional nav link,
the console API in `backend/app/ai/{console.py,routes_console.py}` plus
`list_api_keys` / `revoke_api_key` / `ledger_amount` in the stores, and prefix-
preserving proxy rules for `/api/ai/*` and `/v1/*` (mirroring `/api/live/*`).

Two defects were found and fixed by building this: `settled_at` leaked a
`timestamptz` through `JobRecord` (the job page crashed on it), and `/v1/*` was
not proxied at all, so the documented `base_url` could not work through the app
origin.

### Deploying a Vercel preview against staging

1. Deploy the staging backend with the AI flag on and the console reachable:
   `ENABLE_TCDN_AI=true`, `TCDN_AI_PAYMENTS_LIVE` **unset**, and
   `TCDN_AI_FAKE_CHECKOUT` **unset** unless you want the simulated checkout
   (it only exists outside production). Set `APP_ENV=staging` so the simulated
   seam stays available for a dry run.
2. In Vercel, create a preview deployment from `feat/tcdn-ai-console`.
   Environment variables to set on that preview only (names, no values here):
   - `NEXT_PUBLIC_API_URL` → `https://<staging-backend-host>` (the preview is a
     different origin, so `lib/api.ts` uses this in non-production builds),
   - `BACKEND_ORIGIN` → the same staging backend host,
   - `EDGE_PROXY_AUTH` → only if the staging backend sets it; otherwise leave unset.
3. Open the preview, sign in with a staging account, and check, in order:
   `/ai` (balance + checklist), `/ai/billing` (packs, test-mode banner),
   `/ai/keys` (create → key shown once → revoke), `/ai/quickstart`
   (copy a snippet, "Test my key"), `/ai/usage` (the job from that test),
   `/ai/models`, `/ai/status`.
4. To prove the flag-off path on the same preview, unset `ENABLE_TCDN_AI` on the
   staging backend and reload: the AI nav entry disappears and `/ai` shows the
   unavailable panel.

### Known limits of this delivery

- Credit purchase is test-mode only by construction; a live purchase still needs
  the owner action behind `TCDN_AI_PAYMENTS_LIVE`.
- No host or admin console. No team accounts.
- The shared navbar's colour contrast remains below AA on every page (pre-existing).
