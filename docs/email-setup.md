# Email: what exists today, and how to run business mail on torrentcdn.store

Status as of 2026-10-20. Everything in "Verified current state" was read from the
repository or resolved from public DNS — nothing here is assumed.

## 1. Verified current state

### Code (all transactional, all outbound-only)

| Path | What it does |
|---|---|
| `backend/app/email_service.py` | Resend **SDK** path: verification mail, password reset, "MFA enabled" alert. Tokens are JWTs (`purpose=email_verify` / `reset_password`), 24h / 1h expiry. |
| `backend/app/services/email.py` | Welcome mail, sent via `httpx` directly to `https://api.resend.com/emails` (no SDK dependency). |
| `backend/app/routes/auth.py` | Calls verification (`/auth/register`), password reset (`/auth/forgot-password`), MFA notification (`/auth/mfa/*`), welcome (background task). |
| `backend/app/routes/verification.py` | `/auth/resend-verification` (60s cooldown per user). |
| `backend/app/config.py` | `resend_api_key`, and (new) `resend_from`. |
| `backend/requirements.txt` | `resend>=2.30.0,<3.0.0` — SDK is a real dependency, not optional. |

- **No SMTP anywhere.** No `smtplib`/`aiosmtplib`, no SMTP env vars. Every email
  goes through Resend's HTTPS API.
- **No inbound mail handling.** No webhook, no parser, no mailbox code.
- **No contact / support / partnership form** on the frontend (`frontend/app`
  has no such route; the only `<form>`s are auth, share-password, and MFA).
- **Notification emails** do not exist — `/notifications` is in-app only
  (`notifications` table + `routes/notifications.py`).
- Behavior when Resend is unconfigured: verification **auto-verifies** the
  account (dev-friendly), password reset returns "sent" but logs a warning in
  production rather than lying to the user.

### Live DNS for `torrentcdn.store` (resolved 2026-10-20)

| Record | Value | Meaning |
|---|---|---|
| NS | `dns1/dns2.registrar-servers.com` | DNS is managed at **Namecheap** |
| MX | `10 mx1.improvmx.com`, `20 mx2.improvmx.com` | **ImprovMX** handles inbound — forwarding only |
| TXT `@` | `v=spf1 include:spf.improvmx.com ~all` | Only ImprovMX may send as the apex (single SPF record, no conflict) |
| TXT `resend._domainkey` | `p=MIGfMA0GCSq…` | **Resend DKIM is published → the domain is verified in Resend** |
| TXT `send.torrentcdn.store` | `v=spf1 include:amazonses.com ~all` | Resend's bounce/return-path subdomain (SPF passes on the envelope domain) |
| MX `send.torrentcdn.store` | `10 feedback-smtp.eu-west-1.amazonses.com` | Resend sending region: **eu-west-1** |
| TXT `_dmarc` | `v=DMARC1; p=none;` | Monitor only — **no `rua=`**, so no reports are collected |
| A `@` / CNAME `www` | `216.198.79.1` / `…vercel-dns-017.com` | Vercel |

**Consequence:** transactional mail works and is DKIM-signed; **business mail
cannot be sent today** — there is no mailbox, and the apex SPF authorizes only
ImprovMX forwarding. Mail to `hello@`, `support@`, `dmca@`, `abuse@`,
`privacy@`, `legal@` reaches a mailbox only if an ImprovMX rule exists; those
addresses are **already published** in the legal pages, so a missing rule makes
a compliance contact dead.

## 2. Recommended provider

**Zoho Mail Lite — 1 user (≈ $1/user/month billed annually), aliases for the
rest of the addresses.**

Why this one:

- The **free** Zoho tier is web/mobile only — **no IMAP/POP/SMTP**, which fails
  the "proper SMTP" requirement. Lite is the cheapest paid tier that restores
  IMAP/SMTP + custom domain + DKIM.
- One mailbox (`yatich@`) covers the whole set via aliases — no per-address cost.
- Resend stays exactly as it is for system mail: a transactional API is not a
  mailbox and cannot receive business correspondence.

### 2a. Zero-cost path that works today (no new provider, no DNS change)

**Send through the Resend SMTP relay you already have; receive through the
ImprovMX forwarding you already have.** Cost: $0.

The domain is already verified in Resend (live DKIM at `resend._domainkey`,
return-path SPF at `send.torrentcdn.store`), and Resend exposes the same account
over SMTP (`smtp.resend.com`, user `resend`, password = API key; SMTPS 465).
That is enough for Gmail's *Send mail as* to send as `yatich@torrentcdn.store`:

1. Resend dashboard → API Keys → create a key named `gmail-send-as`.
2. Gmail → Settings → Accounts and Import → **Send mail as** → Add another
   address: name `Yatich Brian`, address `yatich@torrentcdn.store`, untick
   "Treat as an alias".
3. SMTP server `smtp.resend.com`, port `465`, username `resend`, password = the
   API key, TLS on.
4. Gmail sends a confirmation link to `yatich@` — it arrives through ImprovMX
   forwarding, which proves inbound works too.
5. Add `hello@`/`partnerships@` the same way if you want to pick the sender per
   message.

Why no SPF edit is needed: Resend sends with its own return path on
`send.torrentcdn.store` (already `include:amazonses.com`) and signs DKIM as
`d=torrentcdn.store`, so DMARC passes by DKIM alignment. The apex SPF record
stays untouched.

**What $0 does not buy:**
- Caps: Resend free is **100/day, 3,000/month**, shared with the app's own
  verification/reset mail.
- No mailbox: no inbox storage, no server-side folders, no shared team inbox;
  mail lives in Gmail, and only Gmail's "Sent" copy exists.
- Resend's terms target application/transactional mail. Genuine 1:1
  correspondence is fine; cold outreach at volume is not, and a complaint can
  cost the domain's sending reputation.
- Some clients show a small `via resend` note despite aligned DKIM. If that
  matters for a specific recipient, a real mailbox provider (Zoho Lite /
  Workspace) removes it.
- The API key sits in Gmail's SMTP settings: rotate it if it leaks, and prefer a
  dedicated key over the one the backend uses.

### 2b. Inbound: route the domain addresses into Gmail

Inbound already points at **ImprovMX** (live `MX 10 mx1.improvmx.com`,
`20 mx2.improvmx.com`), so no DNS change is needed to receive — only forwarding
rules inside the ImprovMX dashboard.

1. Log in at improvmx.com → **Add domain** (or open the existing
   `torrentcdn.store` entry).
2. Add a **destination**: your Gmail address. (Extra destinations are a plan
   question — the free tier's published limits cover domains, aliases and
   forwards, so add a second one and check what the dashboard allows rather
   than assuming.)
3. Add **aliases**, each pointing at that destination:
   `yatich@`, `hello@`, `partnerships@`, `support@`, `dmca@`, `abuse@`,
   `privacy@`, `legal@`, `dmarc@`.
4. Send a test from an unrelated mailbox to `hello@torrentcdn.store`; it must
   land in Gmail with the original `From:` intact (ImprovMX rewrites the
   envelope, not the header).

Free-plan limits (ImprovMX's own pricing page, checked 2026-10-20):
**1 domain, 25 aliases, 500 emails forwarded/day, 7-day log retention, 0 SMTP
sends** — enough for every address the site publishes. Sending is Resend's job
(§2a), so the missing SMTP on ImprovMX does not matter here. Forwarded mail is
filtered by Gmail like any other message, so on first receipt mark it
"Not spam" and add the sender to contacts. Note that forwarding breaks the
original sender's SPF (the message now comes from ImprovMX's servers) and
Gmail may therefore show a small "via" note in the message details; DKIM
usually survives forwarding, so DMARC still evaluates. That note is cosmetic
and appears only on mail you *receive*, never on mail you send as your domain.

Together, §2a + §2b give the complete loop at **$0**: mail addressed to
`<anything>@torrentcdn.store` arrives in Gmail, and Gmail sends replies out as
that same address, DKIM-signed by Resend so recipients see only your domain.
The domain stores nothing — **Gmail is the mailbox**, and the only cost is
Resend's free sending cap. If you ever need mail stored at the domain itself
(audit trail, multiple devices without Gmail), that is the paid mailbox from §2.

Alternatives, if the trade-off differs:

| Option | Cost | Note |
|---|---|---|
| Google Workspace Business Starter | ~$7/user/mo | Best deliverability to corporate recipients; the upgrade path if outreach lands in spam. Aliases free. |
| ImprovMX Premium | ~$9/mo | Keeps the current MX/forwarding and adds outbound SMTP — no mailbox storage, but zero migration risk. |
| Brevo free / Mailjet free | $0 | 300/day (Brevo) or 200/day (Mailjet) SMTP relays; needs their DKIM record added, so more DNS work than Resend. |
| Migadu Micro | $19/yr | One real IMAP mailbox, unlimited aliases, ~20 outbound/day. |

## 3. Addresses

| Address | Type | Purpose / signature |
|---|---|---|
| `yatich@torrentcdn.store` | **Mailbox** (the only one) | Primary. `Yatich Brian` / `Founder & CEO` / `TorrentCDN` |
| `hello@torrentcdn.store` | Alias | General/business, website, inbound replies |
| `partnerships@torrentcdn.store` | Alias | Partnership + infrastructure outreach |
| `support@`, `dmca@`, `abuse@`, `privacy@`, `legal@` | Aliases | Already published on the legal pages — must exist |
| `dmarc@torrentcdn.store` | Alias | Report destination for `rua=` |
| `notifications@torrentcdn.store` | **Not a mailbox** | System/transactional sender only (`RESEND_FROM`) |

## 4. DNS records to create (Namecheap → Domain List → Advanced DNS)

> The MX **hostnames** below are Zoho's documented generic values. Zoho has
> regional data centres; **use the exact values shown in Zoho Admin Console →
> Domains → Tools & Configurations** if they differ (e.g. `mx.zoho.eu`).
> The DKIM value is **generated per account** — copy it from the console; it
> cannot be written from memory.

| # | Type | Host | Value | Priority | Action |
|---|---|---|---|---|---|
| 1 | MX | `@` | `mx.zoho.com` | 10 | add |
| 2 | MX | `@` | `mx2.zoho.com` | 20 | add |
| 3 | MX | `@` | `mx3.zoho.com` | 50 | add |
| — | MX | `@` | `mx1.improvmx.com`, `mx2.improvmx.com` | 10/20 | **remove** (a domain has one inbound provider) |
| 4 | TXT | `@` | `v=spf1 include:zoho.com ~all` | — | **replace** the existing SPF — never have two SPF records |
| 5 | TXT | `<selector>._domainkey` | `<public key from Zoho Admin Console>` | — | add (selector is chosen when you click "Add" for DKIM) |
| 6 | TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@torrentcdn.store; adkim=r; aspf=r; pct=100` | — | replace (adds reporting, stays in monitor mode) |
| 7 | TXT/CNAME | *(as shown in Zoho domain verification)* | *(from console)* | — | add, only if Zoho asks for it |

**Do not touch:** `resend._domainkey`, `send.torrentcdn.store` (TXT + MX),
`google-site-verification`, the Vercel A/CNAME records. Those are the working
transactional path and the live site.

Keep `p=none` until DKIM and SPF both pass for at least a week of real traffic,
then move to `p=quarantine` (and later `p=reject`). Going straight to
`p=reject` is how legitimate mail from the new mailbox gets bounced.

## 5. Code changes made (this repo)

1. `backend/app/config.py` — new `resend_from` setting, default
   `TorrentCDN <notifications@torrentcdn.store>`, overridable by `RESEND_FROM`.
2. `backend/app/email_service.py` — new `transactional_from_address()`; the
   three auth senders use it instead of a per-call `os.getenv` with a
   `noreply@` default.
3. `backend/app/services/email.py` — the welcome mail now uses the same helper
   (it previously hardcoded `hello@`, splitting the domain's sending identity).
4. `render.yaml`, `.env.example`, `docker-compose.yml` — `RESEND_FROM` is
   documented and defaulted to `notifications@`.
5. `backend/tests/test_email_sender.py` — pins the default, the dashboard
   override, and that both send paths use one helper.

No behavior was removed: same Resend account, same DKIM, same endpoints.

## 6. Render configuration

Render dashboard → `torrentcdn-backend` → Environment:

| Variable | Value | Notes |
|---|---|---|
| `RESEND_API_KEY` | `re_…` | Already set if verification mail works today; never commit it |
| `RESEND_FROM` | `TorrentCDN <notifications@torrentcdn.store>` | System mail only |
| `APP_URL` | `https://www.torrentcdn.store` | Drives verify/reset links — must stay the canonical host |

The mailbox itself is **not** a Render resource: no SMTP credentials belong in
Render, and the Zoho password must never be stored there.

## 7. Manual steps (only you can do these)

1. Create the Zoho account for `torrentcdn.store`, choose **Mail Lite**, 1 user.
2. Create mailbox `yatich@torrentcdn.store` with the display name
   `Yatich Brian`.
3. Add aliases: `hello@`, `partnerships@`, `support@`, `dmca@`, `abuse@`,
   `privacy@`, `legal@`, `dmarc@` (check the alias allowance in Admin Console).
4. Zoho Admin Console → Domains → **verify** (add the TXT/CNAME it shows).
5. Admin Console → Email Configuration → **DKIM** → add selector, copy the TXT
   value, publish it at `<selector>._domainkey`.
6. In Namecheap: replace MX (3 Zoho records, remove ImprovMX), replace the SPF
   TXT, add DKIM, update `_dmarc` per §4.
7. Wait for DNS propagation (minutes at Namecheap), then click **Verify** for
   MX/SPF/DKIM in the Zoho console.
8. Add `RESEND_FROM` in Render if it is still `noreply@` (the code default now
   matches, so this is optional).
9. Optional but recommended: enable 2FA on both the Zoho account and the
   registrar, and set the Zoho catch-all to `yatich@` so a typo'd address still
   reaches you.

## 8. How to test

**Sending (mailbox)**
```bash
# From Zoho webmail or an SMTP client after the MX/SPF/DKIM records are live
# to: your own Gmail address
```
Then in Gmail: *Show original* → expect `SPF: PASS`, `DKIM: PASS` with
`d=torrentcdn.store`, `DMARC: PASS`.

**Sending (transactional)** — trigger a real reset against production:
```bash
curl -s -X POST https://torrent-cdn.onrender.com/auth/forgot-password \
  -H 'Content-Type: application/json' \
  -d '{"email":"<your account email>"}'
```
The message must arrive from `notifications@torrentcdn.store` with
`DKIM: PASS (torrentcdn.store)`.

**Receiving**
```bash
# after MX is live
swaks --to hello@torrentcdn.store --from you@example.com --server mx1.improvmx.com
```
(or simply send from Gmail). Expect delivery into the `yatich@` mailbox and a
reply that lands back in Gmail with SPF/DKIM/DMARC pass.

**SPF / DKIM / DMARC verification**
```bash
dig +short TXT torrentcdn.store              # exactly ONE v=spf1 record
dig +short TXT <selector>._domainkey.torrentcdn.store
dig +short TXT _dmarc.torrentcdn.store
# third-party view of the whole picture:
#   https://www.mail-tester.com  (send it a message → score + per-record detail)
```

**Corporate delivery**
Send a real message to a Gmail and an Outlook/Microsoft 365 address, then check
the receiving side's authentication results. Watch DMARC aggregate reports
(`rua=`) for two weeks before tightening `p=`. If Outlook filters the message,
that is the signal to move to Google Workspace rather than to add more DNS
records.
