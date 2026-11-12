# TCDN-AI — the CI test account (`p2p-e2e`)

**Written:** 2026-11-12 · **Applies to:** `.github/workflows/ci-e2e.yml` job `p2p-e2e`

The end-to-end suites log in as one shared account. That account is infrastructure: it
must exist, it must be able to log in, and its credentials must be rotatable without
touching code. This file is the record of how it is made and what to do when the login
step goes red.

Secrets are referred to by **name only**. No value from this account ever belongs in this
repository, in a CI log, or in a ticket.

---

## 1. Why a separate account

- The suites upload, delete, seed and rename data. Those writes must not land on a person's
  account.
- A shared account can be locked, rate-limited, MFA-gated or rotated without anyone losing
  access to their own work.
- The login step can fail closed on a missing secret without breaking a real user's flow.

The account owns nothing anyone needs back. Treat everything it creates as disposable.

---

## 2. Secrets

Repository secrets, set under **Settings → Secrets and variables → Actions** (or with `gh`).

| Name | Required | What it is | Read by |
|---|---|---|---|
| `E2E_EMAIL` | always | The account's login email | login step |
| `E2E_PASSWORD` | always | The account's password | login step |
| `E2E_TOTP_SECRET` | only if the account has MFA enrolled | Base32 TOTP secret of the account's authenticator | login step |
| `E2E_ADMIN_TOKEN` | for `admin-noselect` | An admin API token used by the admin suite | suites |

`E2E_TOTP_SECRET` is **optional on purpose**. The job maps it into the environment, so an
unset secret arrives as an empty string rather than breaking the workflow. If the account
has no authenticator, the login step enrolls a throwaway one for the run and the cleanup
step at the end disables it again. That path needs no secret and leaves the account as it
found it — unless the run dies between enrollment and cleanup, in which case the account
is left MFA-enabled and the next run needs `E2E_TOTP_SECRET` (see §5).

---

## 3. Creating the account

New accounts are created on **staging**, not production (see §6). Steps:

1. Register through the normal signup form on the staging host. Use an address the team
   controls; do not reuse a personal mailbox.
2. Verify the email. An unverified account is refused at login with **403**, and the step
   will now say so instead of guessing.
3. Set a long random password and store it in the team password manager — not in a note,
   not in a chat message.
4. Leave MFA off. If policy requires it on, enroll once and store the base32 secret as
   `E2E_TOTP_SECRET` (§4). Never enroll by phone/SMS: the runner cannot read your SMS.
5. Confirm it can log in: `POST /auth/login` must return `access_token` (MFA off) or
   `{mfa_required, mfa_token, needs_setup: false}` (MFA on).
6. Set the three or four secrets from §2, then run the workflow once by hand
   (`workflow_dispatch`) and confirm the login step prints
   `shared login: access_token (no second factor required)`.

---

## 4. Setting and changing secrets

Names only in commands; values are typed into `gh`'s prompt or piped from the password
manager, so they never appear in shell history or in this repo:

```bash
gh secret set E2E_EMAIL        # paste the email
gh secret set E2E_PASSWORD     # paste the password
gh secret set E2E_TOTP_SECRET  # base32 secret, if the account has MFA
gh secret set E2E_ADMIN_TOKEN  # admin token, if the admin suite runs
gh secret list                 # check names + last-updated timestamps
```

`gh secret list` is the only supported way to see *when* a secret was last rotated. Secret
values cannot be read back, by anyone, including administrators — which is why the password
manager copy is the source of truth.

---

## 5. When the login step goes red

The step prints the HTTP status and a redacted `detail` from the server, plus one line
saying what that status means. It retries only `000`/`429`/`5xx`, three times, five seconds
apart; a `401`/`403` fails immediately.

| What it says | Cause | What to do |
|---|---|---|
| `401`, `Invalid email or password` | wrong password or unknown account | rotate `E2E_PASSWORD`; if that does not help, the account is gone or locked — recreate it (§3) |
| `403`, detail mentions verification | account created but never verified | verify the email, then re-run |
| `403`, other detail | account blocked from logging in | read the deployment's auth logs; unblock or recreate |
| `429` | login rate limiter, per IP, 15-minute window | wait; re-running in a loop extends the block |
| `5xx` | backend fault, not credentials | read the deployment logs for `/auth/login`; do not rotate secrets |
| `the password was accepted but a TOTP code is required, and E2E_TOTP_SECRET is not set` | the account has an authenticator enrolled (often left behind by an interrupted run) | disable MFA on the account, or enroll once and set `E2E_TOTP_SECRET` |
| `the second factor was rejected (HTTP 401)` | `E2E_TOTP_SECRET` does not match the enrolled authenticator | re-enroll (§7) and update the secret |

The step never prints a token, a password or a raw response body: it masks JWT-shaped and
long base64-ish blobs and clamps the detail to 240 characters. It does print the **names**
of the `E2E_*` variables that reached the job, which is how a missing mapping (a secret set
in the repo but not wired into `env:`) is told apart from a wrong value.

---

## 6. Stages: e2e runs against staging, never production

The suites write. Today they still run against **production** because the CI stack points
there; that is tracked as checklist row **10.6** (`MISSING`) and is the next piece of work.
Until then:

- Do not point an authenticated write suite at production data that anyone cares about.
- Do not add a new suite that deletes or renames anything without checking it is safe
  against the production backend.
- Any change that moves CI to staging must carry the account over (new email/password on the
  staging host, secrets updated, one green `workflow_dispatch` run) and update §3–§4 above.

Rationale: the same reason the account exists — test data must be disposable. A production
suite that silently mutates real events is worse than a red workflow.

---

## 7. Rotation

Password (any time; do it if the value may have leaked):

1. Change it on the site for the CI account.
2. `gh secret set E2E_PASSWORD` with the new value.
3. Dispatch `p2p-e2e` once. Green = done.

Authenticator secret (`E2E_TOTP_SECRET`):

1. Disable MFA on the account (needs a current code, so keep the old secret until this step
   succeeds).
2. Log in, enroll a fresh authenticator, and keep the base32 secret shown at setup.
3. `gh secret set E2E_TOTP_SECRET`.
4. Dispatch once. Green = done; only then delete the old secret from the password manager.

Admin token (`E2E_ADMIN_TOKEN`): revoke in the admin UI, mint a new one, `gh secret set`,
re-run the admin suite.

Rotation calendar: the password and the admin token at least every 90 days, and immediately
after anyone with access leaves. `gh secret list` shows the last update date; a secret that
has not moved in a year is a finding, not a convenience.

---

## 8. Standing alert: no green `p2p-e2e` in 7 days

**Status: idea, not implemented.** Recorded here so it is not lost.

The August failure ran for a month because a red job looked like every other red job. A
week of silence is the signal that matters, not any single run. Proposed check — a
scheduled workflow, daily at 06:00 UTC, that fails loudly:

```yaml
# idea only — not committed
name: e2e-freshness
on:
  schedule: [{cron: '0 6 * * *'}]
  workflow_dispatch:
permissions:
  actions: read
jobs:
  fresh:
    runs-on: ubuntu-latest
    steps:
      - env: {GH_TOKEN: '${{ github.token }}'}
        run: |
          last=$(gh run list --workflow ci-e2e.yml --status success --limit 1 \
                   --json createdAt --jq '.[0].createdAt')
          age=$(( ( $(date -u +%s) - $(date -u -d "$last" +%s) ) / 86400 ))
          echo "last green p2p-e2e: $last (${age}d ago)"
          if [ "$age" -ge 7 ]; then
            echo "::error::p2p-e2e has not passed in ${age} days — fix the workflow or delete it; do not leave it red."
            exit 1
          fi
```

A failed scheduled run emails the repo's default notification recipients, so the alert
arrives without anyone watching the Actions tab. If GitHub's notification settings prove
unreliable, the same command can post to a webhook instead — the decision to route it
somewhere else does not change the check.

---

## 9. Rules

- Secret values never enter this repo, a commit message, an issue, a CI log or a screenshot.
- The CI account is not a person. Nobody logs in to it by hand except to rotate (§7).
- A red login step is a bug in the environment, not a reason to skip the suite. Fix the
  cause; do not add a `continue-on-error`.
