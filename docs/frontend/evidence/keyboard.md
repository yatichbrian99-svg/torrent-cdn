# Keyboard walk-through

Recorded by `frontend/scripts/a11y_audit.mjs` in Chromium (1280×900, light theme).
Each line is one Tab stop, with the computed focus indicator.

| # | Element | Label | Outline | Visible indicator |
| --- | --- | --- | --- | --- |
| 1 | `<a>` | TorrentCDN home | solid 2px rgb(21, 78, 205) | yes |
| 2 | `<a>` | Docs | solid 2px rgb(21, 78, 205) | yes |
| 3 | `<a>` | Architecture | solid 2px rgb(21, 78, 205) | yes |
| 4 | `<a>` | Demo | solid 2px rgb(21, 78, 205) | yes |
| 5 | `<a>` | TCDN-AI | solid 2px rgb(21, 78, 205) | yes |
| 6 | `<button>` | Switch to dark mode | solid 2px rgb(21, 78, 205) | yes |
| 7 | `<button>` | Login | solid 2px rgb(21, 78, 205) | yes |
| 8 | `<button>` | Sign up | solid 2px rgb(21, 78, 205) | yes |
| 9 | `<a>` | Start for free→ | solid 2px rgb(21, 78, 205) | yes |
| 10 | `<a>` | Watch demo | solid 2px rgb(21, 78, 205) | yes |
| 11 | `<button>` | Copy | solid 2px rgb(21, 78, 205) | yes |
| 12 | `<button>` | Get started | solid 2px rgb(21, 78, 205) | yes |
| 13 | `<button>` | Get started | solid 2px rgb(21, 78, 205) | yes |
| 14 | `<button>` | Get started | solid 2px rgb(21, 78, 205) | yes |
| 15 | `<button>` | What browsers are supported? | solid 2px rgb(21, 78, 205) | yes |
| 16 | `<button>` | What happens if there are no peers? | solid 2px rgb(21, 78, 205) | yes |
| 17 | `<button>` | What file types are supported? | solid 2px rgb(21, 78, 205) | yes |
| 18 | `<button>` | What is the maximum file size? | solid 2px rgb(21, 78, 205) | yes |
| 19 | `<button>` | Is my content private? | solid 2px rgb(21, 78, 205) | yes |
| 20 | `<button>` | How does billing work? | solid 2px rgb(21, 78, 205) | yes |
| 21 | `<button>` | Can I use this on my own domain? | solid 2px rgb(21, 78, 205) | yes |
| 22 | `<button>` | Do you have an API? | solid 2px rgb(21, 78, 205) | yes |
| 23 | `<a>` | GitHub | solid 2px rgb(21, 78, 205) | yes |
| 24 | `<a>` | Terms | solid 2px rgb(21, 78, 205) | yes |

Focus never fell back to `<body>`: confirmed.

## Mobile menu (390 px)

- Enter on the burger opens the panel: aria-expanded=`true`, panel visible=`true`, focus moved inside=`true`, body scroll locked=`true`
- Escape closes it: aria-expanded=`false`, panel hidden=`true`, focus returned to the trigger=`true`, scroll lock released=`true`

Mobile menu contract: pass.
