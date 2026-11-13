# First-load JS budget

Parsed from the two `next build` logs by `frontend/scripts/bundle_report.mjs`.
Budget enforced here: **no existing page may grow by more than 30 kB**.

Shared chunk: before 103 kB → after 103 kB.

| Route | Before (kB) | After (kB) | Δ (kB) | Within budget |
| --- | --- | --- | --- | --- |
| `/_not-found` | 104 | 104 | 0.0 | yes |
| `/.well-known/security.txt` | 103 | 103 | 0.0 | yes |
| `/admin` | 136 | 136 | 0.0 | yes |
| `/admin/health` | 130 | 130 | 0.0 | yes |
| `/admin/payouts` | 135 | 135 | 0.0 | yes |
| `/ai` | 133 | 133 | 0.0 | yes |
| `/ai/billing` | 130 | 130 | 0.0 | yes |
| `/ai/keys` | 130 | 130 | 0.0 | yes |
| `/ai/models` | 129 | 129 | 0.0 | yes |
| `/ai/quickstart` | 130 | 130 | 0.0 | yes |
| `/ai/status` | 129 | 129 | 0.0 | yes |
| `/ai/usage` | 133 | 133 | 0.0 | yes |
| `/ai/usage/[jobId]` | 133 | 133 | 0.0 | yes |
| `/architecture` | 103 | 103 | 0.0 | yes |
| `/aup` | 103 | 103 | 0.0 | yes |
| `/auth/forgot-password` | 130 | 130 | 0.0 | yes |
| `/auth/reset-password` | 131 | 131 | 0.0 | yes |
| `/billing` | 128 | 128 | 0.0 | yes |
| `/cookies` | 103 | 103 | 0.0 | yes |
| `/dashboard` | 250 | 250 | 0.0 | yes |
| `/dashboard/upload` | 129 | 129 | 0.0 | yes |
| `/demo` | 243 | 243 | 0.0 | yes |
| `/dmca` | 103 | 103 | 0.0 | yes |
| `/docs` | 109 | 109 | 0.0 | yes |
| `/embed/[file_id]` | 128 | 129 | +1.0 | yes |
| `/live` | 132 | 132 | 0.0 | yes |
| `/live/channels` | 103 | 103 | 0.0 | yes |
| `/live/channels/[id]` | 134 | 135 | +1.0 | yes |
| `/live/channels/[id]/analytics` | 132 | 132 | 0.0 | yes |
| `/live/watch/[slug]` | 136 | 136 | 0.0 | yes |
| `/privacy` | 103 | 103 | 0.0 | yes |
| `/robots.txt` | 103 | 103 | 0.0 | yes |
| `/share/[token]` | 127 | 127 | 0.0 | yes |
| `/sitemap.xml` | 103 | 103 | 0.0 | yes |
| `/tcdn-ai` | 105 | 105 | 0.0 | yes |
| `/terms` | 103 | 103 | 0.0 | yes |
| `/verify-email` | 126 | 126 | 0.0 | yes |

Worst existing-page change: +1.0 kB. All within budget.
