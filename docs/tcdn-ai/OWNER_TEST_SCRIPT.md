# Owner click-through — the console you can and cannot see

Two scripts, one per deployment. Part A is the one that matters today: it proves
production, where `ENABLE_TCDN_AI` is **not** set, behaves exactly as it did
before this work. Part B is the optional staging run that proves the console
itself.

Run Part A on **production** and Part B on the **staging** stack only. Nothing
here needs a live key: Part A never reaches the AI surface (that is the point),
and Part B uses the simulated checkout.

Write down the step number and what you saw for anything that does not match —
that is what makes a failure fixable. At the end of each part there is a short
"send this back" list.

---

## Part A — production, flag off (the regression check)

**Before you start:** use a normal browser window, not a private one (some
steps need a session that survives a reload), and have the deploy SHA from
`/api/health/live` handy. Production UI is at https://www.torrentcdn.store; the
backend answers at https://torrent-cdn.onrender.com.

| # | Do this | Expected result |
|---|---|---|
| A1 | Open https://www.torrentcdn.store/ | Landing page renders fully, no console errors, no horizontal scrollbar. |
| A2 | Signed out, look at the top navigation on the landing page. | No **AI** link anywhere in the navbar or footer. |
| A3 | Open https://www.torrentcdn.store/ai directly | The "AI console unavailable" panel only — a short explanation, no tabs, no data, no spinner that never ends. |
| A4 | Visit /demo, /docs, /architecture, /privacy, /terms, /cookies, /dmca (same as before) | Each renders as before. `/pricing` still answers 404 — that was already true and is unchanged. |
| A5 | Open https://torrent-cdn.onrender.com/health | `{"status":"healthy"}` with HTTP 200. |
| A6 | Open https://torrent-cdn.onrender.com/api/health/live | `healthy`, with Redis and the media pipeline reporting reachable. |
| A7 | Open https://torrent-cdn.onrender.com/api/ai/v1/status | HTTP 404. |
| A8 | Open https://torrent-cdn.onrender.com/api/ai/v1/never-existed | HTTP 404 (control — proves the 404s above are real, not a proxy quirk). |
| A9 | Through the app origin: https://www.torrentcdn.store/api/ai/v1/status | HTTP 404, and **not** an HTML error page. |
| A10 | Through the app origin: https://www.torrentcdn.store/v1/chat/completions (GET) | HTTP 404 (or 405) — never 200, never a 500. The OpenAI surface does not exist with the flag off. |
| A11 | Sign in with your own account (this is a normal login, nothing AI-related). | You land signed in; the navbar has Dashboard, Live, Billing — and still **no AI link**. |
| A12 | Reload the dashboard. | Still no AI link; dashboard data loads as before. |
| A13 | Upload a small file from the dashboard and let it finish. | Upload completes exactly as before; no AI mention in the UI. |
| A14 | Open Billing. | Existing billing page renders as before; no AI credits section, no AI wording. |
| A15 | Sign out, then sign in again. | Sign-out returns you to the landing page; sign-in works; no AI link appears at any point. |
| A16 | Trigger one password-reset email from /forgot-password with your own address. | "If that account exists, we sent a link" style confirmation, email arrives, link works — unchanged. |
| A17 | Open DevTools → Console and Network on the landing page, /ai, the dashboard and billing. | No new console errors. `/ai` makes at most one request to the AI surface (the availability probe, which 404s); no other page makes any. |

### Send this back if a step fails

For the failing step number: the URL you were on, the HTTP status from the
Network tab, the first line of any console error, and a screenshot. If A9/A10
returned **200**, stop and say "flag-off leak" — that is a rollback trigger, and
the rollback is in Part C.

---

## Part B — staging, flag on (the console itself, optional)

**Before you start:** the staging backend must have `ENABLE_TCDN_AI=true`,
`APP_ENV=staging`, `TCDN_AI_FAKE_CHECKOUT=1`, **no** Paystack live key and
`TCDN_AI_PAYMENTS_LIVE` unset. Use a test account on staging — the credits are
simulated. Full deploy notes: `FRONTEND_PLAN.md` §12.

| # | Do this | Expected result |
|---|---|---|
| B1 | Sign in to staging | An **AI** entry appears in the navbar; signed out it does not. |
| B2 | Open `/ai` | Balance in USD with an approximate KES figure, a usage summary, service status, and a getting-started checklist that ticks off as you complete it. |
| B3 | Open `/ai/billing` and buy the smallest pack | A visible **test mode** marker, a receipt/confirmation, and the balance going up by exactly that pack. No real charge. |
| B4 | Open `/ai/keys`, create a key with the inference scope | The key is shown **once**, with a copy button and a warning that it will not be shown again. |
| B5 | Reload `/ai/keys` | Only the prefix is listed; the full key is gone. The key is not in localStorage, sessionStorage or cookies (check Application → Storage). |
| B6 | `/ai/quickstart` → "Test my key" | A successful round trip and a job id. |
| B7 | `/ai/usage` | That job is listed with tokens and cost; open it | Region, model, tokens, cost and verification state — and no prompt or output text anywhere. |
| B8 | `/ai/models` | Standard tier is selectable; the confidential tier reads "coming soon" and cannot be selected; no "secure" or "production ready" claims. |
| B9 | `/ai/status` | Per-region worker counts with heartbeat ages. |
| B10 | Revoke the key from `/ai/keys`, then retry the quickstart call | The call is refused (401) and the UI says so plainly. |
| B11 | Set the staging backend's `ENABLE_TCDN_AI` off, reload `/ai` | The unavailable panel, no AI link, no console errors — the same as A2–A3. |

### Send this back if a step fails

Step number, the request URL and status, the exact error text shown, and a
screenshot. For B4/B5 also say whether the key appeared anywhere in DevTools →
Application. **Never paste a full key into a ticket or chat** — a prefix is
enough.

---

## Part C — rollback, if Part A fails after the deploy

Fastest and safest, in this order:

1. **Revert the merge on main** (no force-push, no history rewrite):

   ```
   git revert -m 1 <the merge commit of PR #50> --no-edit
   git push origin main
   ```

   Vercel and Render redeploy the reverted tree. `/ai` returns 404, the nav
   link is gone with it, and the proxy rules revert with the file.
2. If the frontend is still serving the bad build, redeploy the last known-good
   deployment from the Vercel dashboard (Deployments → the previous production
   deployment → Promote).
3. If the backend is unhealthy, redeploy the previous Render deployment (Events
   → the deploy before this one → Redeploy).
4. Confirm with A5, A6, A7 and A9 that the site is back to its pre-merge
   behaviour, then send back the step that failed and the rollback time.

Nothing in this work writes production data, so a rollback needs no data
restore. The only production side effects possible from the console are credit
purchases, and those are refused unless `TCDN_AI_PAYMENTS_LIVE` is set — which
it is not, and must not be.
