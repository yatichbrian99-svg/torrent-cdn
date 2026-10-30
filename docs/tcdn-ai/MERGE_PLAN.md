# TCDN-AI — merge plan for the dark release

**State when this was written (2026-10-30):** `main` = `200109e`. The CDN fixes are on
`fix/product-security-hardening` (`7d3c8d2`, 5 commits, 33 files) and open as **draft PR #46**.
The TCDN-AI work is on `fix/tcdn-ai-redis-replay-nonce` (224 files more than the product
branch) and has **no PR**. Nothing is merged; this file is the plan, not a record of an action.

A **dark release** means: the code ships, the feature does not turn on. `ENABLE_TCDN_AI` is not
set in `render.yaml` and defaults off, so no `/api/ai/v1/*` route is mounted and the AI startup
DDL does not run. The CDN behaves exactly as it does today, with the fixes listed in PR #46.

---

## 1. Merge order

| Step | From | Into | Flag | Gate before merging |
|---|---|---|---|---|
| 1 | `fix/product-security-hardening` — **PR #46 (draft)** | `main` | no AI code involved | PR checks green (see §2), owner review, owner un-drafts |
| 2 | — | production | off | run §4 against production, watch 15 minutes of logs |
| 3 | `fix/tcdn-ai-redis-replay-nonce` — its own PR (not open yet) | `main` | **off** (dark) | the five P1 rows in §6 closed, §4 re-run |
| 4 | — | production | **on**, only when the owner decides | a separate decision with its own doc; never bundled with step 3 |

**Why this order.** The AI branch contains every product file *plus* the AI tree: 25 of PR
#46's 33 files are byte-identical there, and the other 8 differ only by AI-side additions. So
the AI branch is a strict superset, and merging the product fixes first keeps each production
diff attributable — if login breaks after step 1, it is one of five commits, not one of 229.
Merging the AI branch first would make PR #46 a no-op and put AI code in the CDN release.

**Never merge both in one push**, and never turn the flag on in the same deploy as a merge: a
regression then has two possible causes and no way to tell them apart.

## 2. What production auto-deploys when `main` moves

| Surface | What happens | What to confirm before merging |
|---|---|---|
| **Render — `torrentcdn-backend`** | Docker build from `backend/Dockerfile`, health check `/health`. `render.yaml` sets no `branch:`, so the service tracks the repository's default branch (`main`). Render's blueprint default is auto-deploy on push. | Dashboard → the service → **Settings → Auto-Deploy**: is it on, and is the branch `main`? If it is off, the merge deploys nothing until someone presses **Manual Deploy**. |
| **Vercel — frontend** | Rebuild of `main` (the project's Production Branch). `frontend/package.json` + lockfile changed (`next` 15.5.25 → 15.5.28, `tailwindcss` moved to `devDependencies`); `frontend/Dockerfile` changed but Vercel does not use it. | Vercel → project → **Settings → Git → Production Branch** = `main`. |
| **Render — `torrentcdn-keepalive` cron** | Nothing. It is untouched and its pinger only calls `/health`. | — |
| **Render — `torrentcdn-redis` Key Value** | Nothing. No env change, no plan change. | — |
| **Env vars** | **No new variable has to be set by hand.** `RESEND_FROM` has a code default (`TorrentCDN <notifications@torrentcdn.store>`) and `render.yaml` carries the same value; a dashboard value still overrides it. | Whether the dashboard currently sets `RESEND_FROM` — if it does, the merge does **not** change the sender; if it does not, the sender changes to `notifications@`. |
| **Not deployed by this merge** | `render.staging.yaml` and the AI workflows are not on `main`; no new Render service; no new database; no new port; no new secret. | — |

The one production-visible change that does **not** wait for a deploy: nothing. Every product
change in PR #46 is code or config and takes effect on the Render deploy.

## 3. Migration impact

**PR #46 (the dark release): none.** `git diff main...fix/product-security-hardening --
backend/app/database.py` is empty, and no file on the branch issues DDL. `backend/app/models.py`
loses an unused pydantic class. There is nothing to run before the deploy and nothing to undo
after a rollback.

**The AI branch, when its turn comes** (recorded here so the plan is complete):

- Its startup DDL is **additive and idempotent** — `CREATE TABLE IF NOT EXISTS` and
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` against its own `ai_*` tables, never an `ALTER` of
  the CDN's existing tables.
- It runs **only when the flag is on** (audit finding F-4, fixed):
  `tests/test_ai_migrations.py::test_the_ai_schema_is_only_migrated_when_the_flag_is_on`
  records the statements issued in both flag positions and fails if the flag-off run creates
  anything.
- So a dark merge (flag off) creates **no** table at all; the first deploy with the flag on
  creates the 17 `ai_*` tables once; every later deploy is a no-op. Turning the flag back off
  leaves the tables in place and unread — no reverse migration, no data loss, no CDN impact.
- Verify on the deploy: the Render log for a flag-off boot contains no `ai_` DDL line.

## 4. Smoke list to run against production after the merge

Run in this order; each line is the check and the pass condition. `$P` = the production origin
(`https://www.torrentcdn.store`, i.e. through Vercel) for anything browser-facing, and
`https://torrent-cdn.onrender.com` for the direct backend path used by ops.

**Liveness**

1. `curl -s $P/health` → `{"status":"healthy"}`, HTTP 200.
2. `curl -s $P/api/health/live` → `redis: true`, `mediamtx_configured: true`,
   `mediamtx_reachable: true`, `last_error: null`.

**Auth — the branch's highest-risk area**

3. Register a new account from a real inbox → 200, and the verification mail arrives
   **from `TorrentCDN <notifications@torrentcdn.store>`** (check the header, not the body).
4. Click the verification link → "Email verified successfully"; login → token returned.
5. `GET /auth/me` with that token → 200; the session works.
6. **Log out, then replay the same token** → **401** `Session invalidated. Please log in
   again.` This is the fix; if it still returns 200, stop and roll back.
7. Password reset on that account → mail arrives from `notifications@`; the reset link works;
   the **old** session token now returns 401 (revocation by `iat`).
8. `curl -s -X POST $P/auth/login -H 'Content-Type: application/json' -d
   '{"email":"a\u0000b@example.com","password":"x"}'` → **400**, not 500 and not a stack trace
   in the Render log. Repeat for `/auth/register` and `/auth/forgot-password`.
9. An existing pre-merge account can still log in (a token minted before the deploy keeps
   working until it expires — the new rules apply to what is presented, and app-minted tokens
   already carry all three claims).

**Uploads and files**

10. Presign + upload a small file from the dashboard → appears in the list; download works.
11. Embed playback of an existing file (`/e/<id>` or the player page) → plays; no console
    errors.
12. Rename a file to a name containing a control character → **400** `Invalid filename`;
    rename normally → 200.

**Money (admin)**

13. Log in as admin in the browser; open the payouts admin view → recipients list loads
    (`require_admin`, unchanged).
14. With a `tcdn_` **API key** belonging to that admin, `POST /api/payouts/recipients` →
    **403** (session JWT required). With the session JWT → normal Paystack validation.
15. Start a plan checkout and abandon it → no charge, no 500.

**AI stays dark**

16. `curl -s -o /dev/null -w '%{http_code}' $P/api/ai/v1/status` → **404** (the AI routes are
    not mounted with the flag off). Anything else means the flag or the route wiring leaked.
17. `docker`/Render log for the boot: no `ai_` DDL, no AI keyring warning.

**Frontend**

18. Dashboard loads, navbar and demo page render, no baked-in backend origin in the bundle
    (the CI "API baseURL guard" covers this, but one manual page load is cheap).
19. One full signup→upload→play cycle from a phone on cellular data (covers the Vercel→Render
    proxy path with a different client IP).

## 5. Rollback — one step, no database work

- **Backend:** Render → `torrentcdn-backend` → **Deploys** → the last deploy that was live
  before the merge → **Rollback**. One click, a couple of minutes, old image and old code.
  There is **no DDL in this merge**, so no database step is involved and no data is lost.
- **Frontend:** Vercel → the project → **Deployments** → the previous production deployment →
  **Promote to Production** (or `vercel rollback` from the project directory).
- **Sender, if that is the only problem:** set `RESEND_FROM` in the Render dashboard to the old
  value (`TorrentCDN <noreply@torrentcdn.store>`, and `hello@` for welcome mail) and redeploy —
  a config change, no code revert. This is the one part of the change that is not
  all-or-nothing.
- **After rolling back, verify:** `/health` 200, one login, one verification mail from the old
  sender, one upload.

Because nothing in this merge touches the schema, the usual "the code rolled back but the
database did not" hazard does not exist here. That stops being true for the AI merge: rolling
that one back leaves the `ai_*` tables in place, which is harmless (nothing reads them with the
flag off) but should be said out loud.

## 6. What still blocks Level 1 (and therefore step 3)

Recounted 2026-10-30 from the tables in [`PUSH_CHECKLIST.md`](PUSH_CHECKLIST.md) — P1 is
**51 verified / 3 partial / 2 missing of 56**. Five P1 rows still have to change (row 12.5,
the temporary branch push trigger, closed on its own commit while this plan was being written):

| Row | Status | Why it blocks |
|---|---|---|
| **4.4** | PARTIAL | No Paystack sandbox session; M-Pesa is not enabled on the account. The billing state machine is tested, the provider is not exercised. |
| **5.5** | PARTIAL | **ZAP has not run** (see §7). Static, dependency, secret and image scanning are green. |
| **6.1** | PARTIAL | Redundant execution samples at **placement**, not after a commitment, and SDK-mode jobs cannot be re-executed (no plaintext). |
| **12.3** | MISSING | No draft PR for the AI branch. |
| **12.4** | MISSING | Post-push confirmation cannot exist before a push. |

Row **3.4** (P2, outbound worker mode for NAT/CGNAT workers) is `MISSING` and does not block
Level 1.

## 7. Limitations of this plan — stated, not hidden

- **There is no staging deployment.** `render.staging.yaml` has never been applied: the
  documented URL (`https://torrentcdn-staging-ai.onrender.com`) answers **404** on `/health`
  and `/api/ai/v1/status`. Everything verified for this plan was verified on a **local
  production-mode stack** (`APP_ENV=production`, `STAGING_DEPLOYMENT=true`, real Postgres,
  Redis, MinIO, MediaMTX, Paystack test keys), not on a hosted staging box.
- **ZAP has not run.** The `zaproxy/zap-stable` image could not be pulled in this environment
  (the registry handshake answered at ~4.5 kB/s and no layer ever started downloading across
  ~10 minutes; the `ghcr.io` mirror refused anonymous pulls). Row 5.5 stays `PARTIAL` for that
  reason and the checklist says so.
- The smoke list above is a **plan**. It has not been executed against production, because
  production is not what this pass may touch (owner's instruction: do not merge).
- "Auto-deploy on merge" is Render's blueprint default, not something this repository pins —
  hence the confirm-in-the-dashboard column. If auto-deploy is off, the merge needs a Manual
  Deploy and the rollback story is unchanged.
