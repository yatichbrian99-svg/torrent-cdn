#!/usr/bin/env bash
# =============================================================================
# secret_scan.sh — fail CI if a GitHub token or other high-signal secret is
# introduced to the repo.
#
# Complements the Gitleaks full-history scan (the static-audit job): this gate
# is diff-scoped, so a secret that lands in the current changeset is caught
# here in seconds with a readable message. Gitleaks remains the backstop for
# secrets already buried in history.
#
# Usage:
#   bash backend/scripts/secret_scan.sh            # diff origin/main...HEAD (fallback HEAD^)
#   bash backend/scripts/secret_scan.sh <base>     # diff <base>...HEAD
#   bash backend/scripts/secret_scan.sh --full     # whole working tree (tracked + untracked, non-ignored)
#   bash backend/scripts/secret_scan.sh --help
#
# Exit codes: 0 = clean, 1 = secrets found, 2 = usage error
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# name|flags|extended-regexp    (flags: i = case-insensitive)
# High-signal patterns only — realistic token shapes/lengths, so placeholder
# values like sk_test_xxx / whsec_xxx / changeme / minioadmin never trip.
# (JWT patterns are deliberately absent: JWT-shaped placeholders in docs are a
# documented false-positive source; gitleaks covers real JWTs.)
PATTERNS=(
  "GitHub PAT (classic)|i|ghp_[A-Za-z0-9]{36}"
  "GitHub PAT (fine-grained)|i|github_pat_[A-Za-z0-9_]{22,}"
  "GitHub OAuth / server token|i|gh[ousr]_[A-Za-z0-9]{36}"
  "AWS access key ID||AKIA[0-9A-Z]{16}"
  "AWS secret access key|i|aws_secret_access_key[[:space:]]*[=:][[:space:]]*['\"]?[A-Za-z0-9/+=]{40}"
  "Stripe live secret key||sk_live_[0-9a-zA-Z]{16,}"
  "Stripe restricted key||rk_live_[0-9a-zA-Z]{16,}"
  "Stripe webhook secret||whsec_[0-9a-zA-Z]{16,}"
  "SendGrid API key||SG\\.[A-Za-z0-9_-]{16,}"
  "Slack token||xox[baprs]-[0-9A-Za-z-]{10,}"
  "Slack webhook||https://hooks\\.slack\\.com/services/[A-Za-z0-9/]{40,}"
  "npm access token||npm_[A-Za-z0-9]{36}"
  "PyPI upload token||pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{30,}"
  "Google API key||AIza[0-9A-Za-z_-]{35}"
  "Twilio API key||SK[0-9a-fA-F]{32}"
  "Resend API key||re_[a-z0-9]{23,}"  # Resend's documented format: 're_' + 23 lowercase alnum (tightened to avoid matching generated 're_' identifiers)
  "Discord webhook||https://discord(app)?\\.com/api/webhooks/[0-9]{17,19}/[A-Za-z0-9_-]{60,}"
  "Private key (RSA/EC/OpenSSH/DSA/PGP)||-----BEGIN (RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----"
)

usage() { sed -n '2,15p' "$0"; }

MODE="diff"
BASE=""
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --full)    MODE="full" ;;
  "")        BASE="origin/main" ;;
  --*)       echo "Unknown argument: $1 (try --full, --help, or a base ref)" >&2; exit 2 ;;
  *)         BASE="$1" ;;
esac

# --- Collect the file list ----------------------------------------------------
# Note: both modes operate on git-tracked files (diff mode) or tracked +
# untracked-non-ignored files (--full, via --exclude-standard). Gitignored
# files (.env, node_modules) are intentionally skipped — they cannot be
# committed, so CI only needs to see what git would actually track.
if [ "$MODE" = "full" ]; then
  FILES="$(cd "$REPO_ROOT" && git ls-files --cached --others --exclude-standard)"
else
  if ! (cd "$REPO_ROOT" && git rev-parse --verify -q "$BASE" >/dev/null 2>&1); then
    if (cd "$REPO_ROOT" && git rev-parse --verify -q "HEAD^" >/dev/null 2>&1); then
      BASE="HEAD^"
    else
      echo "Cannot resolve base ref '$BASE' (and no HEAD^ available). Use --full." >&2
      exit 2
    fi
  fi
  FILES="$(cd "$REPO_ROOT" && git diff --name-only --diff-filter=ACMRTUXB "$BASE"...HEAD)"
fi

# --- Scan ---------------------------------------------------------------------
FOUND=0
SCANNED=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  FULL="$REPO_ROOT/$f"
  [ -f "$FULL" ] || continue  # deleted / renamed-away in this diff
  SCANNED=$((SCANNED + 1))
  for entry in "${PATTERNS[@]}"; do
    name="${entry%%|*}"
    rest="${entry#*|}"
    flags="${rest%%|*}"
    re="${rest#*|}"
    if [ "$flags" = "i" ]; then
      GREP_ARGS=(-nIiE)
    else
      GREP_ARGS=(-nIE)
    fi
    while IFS= read -r line; do
      lineno="${line%%:*}"
      content="${line#*:}"
      # Redact every matched token in the echoed line (literal replace — no
      # regex/delimiter pitfalls), so the secret never lands in CI logs.
      while :; do
        m="$(printf '%s\n' "$content" | grep -oE -- "$re" | head -1 || true)"
        [ -n "$m" ] || break
        content="${content//"$m"/***REDACTED***}"
      done
      echo "  ❌ ${name} → ${f}:${lineno}: ${content}"
      FOUND=$((FOUND + 1))
    done < <(grep "${GREP_ARGS[@]}" -- "$re" "$FULL" || true)
  done
done <<< "$FILES"

# --- Report -------------------------------------------------------------------
if [ "$FOUND" -gt 0 ]; then
  echo ""
  echo "✋  $FOUND potential secret(s) across $SCANNED scanned file(s)."
  echo "     Remove or rotate them before merging — treat any leaked token as compromised."
  exit 1
fi

if [ "$MODE" = "full" ]; then
  echo "✅  Secret scan clean — no high-signal secrets in the working tree ($SCANNED files)."
else
  echo "✅  Secret scan clean — no high-signal secrets in diff ${BASE}...HEAD ($SCANNED files)."
fi
exit 0
