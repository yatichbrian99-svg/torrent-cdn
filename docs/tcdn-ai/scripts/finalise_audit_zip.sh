#!/usr/bin/env bash
# Build the audit zip from a commit, verify nothing secret is inside it, and
# (unless --dry-run) rewrite the audit brief's commit row to match.
#
# Prepared for the post-smoke finalisation of the developer-console merge. It is
# deliberately a script and not a commit: the brief must only claim a zip once
# the zip exists and has been scanned, and the smoke has not run yet.
#
#   docs/tcdn-ai/scripts/finalise_audit_zip.sh                # origin/main
#   docs/tcdn-ai/scripts/finalise_audit_zip.sh <commit-ish>
#   docs/tcdn-ai/scripts/finalise_audit_zip.sh --dry-run      # checks only, writes nothing
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BRIEF="$ROOT/docs/tcdn-ai/AUDIT_BRIEF.md"
DIST="$ROOT/dist"
DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY=1
  shift
fi
COMMITISH="${1:-origin/main}"

cd "$ROOT"
SHA="$(git rev-parse --verify "${COMMITISH}^{commit}")"
SHORT="${SHA:0:7}"
ZIP_NAME="torrentcdn-audit-${SHORT}.zip"
ZIP="$DIST/$ZIP_NAME"

echo "commit   $SHA"
echo "zip      $ZIP"

if [[ $DRY -eq 0 && -n "$(git status --porcelain)" ]]; then
  echo "refusing: working tree is dirty; the zip must be reproducible from the commit" >&2
  exit 1
fi

# The one promise that must hold in the artefact: the AI surface is not enabled
# for production, and no live payment key is configured in the production IaC.
echo "== guard: ENABLE_TCDN_AI must not be set for production =="
if git show "$SHA:render.yaml" 2>/dev/null | grep -qiE '^\s*-?\s*key:\s*ENABLE_TCDN_AI\s*$'; then
  echo "found ENABLE_TCDN_AI in render.yaml — inspect before shipping an audit copy" >&2
  exit 1
fi
echo "[OK] render.yaml does not set ENABLE_TCDN_AI"

if [[ $DRY -eq 1 ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  git archive --format=zip --prefix="torrentcdn-audit-${SHORT}/" -o "$tmp/$ZIP_NAME" "$SHA"
  echo "[dry-run] archive built at $tmp/$ZIP_NAME ($(du -h "$tmp/$ZIP_NAME" | cut -f1)); nothing written to dist/"
  SCAN="$tmp/$ZIP_NAME"
else
  mkdir -p "$DIST"
  git archive --format=zip --prefix="torrentcdn-audit-${SHORT}/" -o "$ZIP" "$SHA"
  SCAN="$ZIP"
fi

echo
echo "== secret scan of the archive contents =="
UNPACK="$(mktemp -d)"
trap 'rm -rf "$UNPACK"' EXIT
unzip -q "$SCAN" -d "$UNPACK"
FINDINGS=0
report() { echo "  [$1] $2"; [[ "$1" == "HIT" ]] && FINDINGS=$((FINDINGS + 1)) || true; }

# Files that must never be in a release archive at all.
FORBIDDEN=0
while IFS= read -r f; do
  report HIT "forbidden file: ${f#"$UNPACK"/}"
  FORBIDDEN=$((FORBIDDEN + 1))
done < <(find "$UNPACK" \( -name '.env' -o -name '.env.*' -o -name '*.pem' -o -name '*.key' \
  -o -name 'id_rsa*' -o -name '*.p12' -o -name '*.pfx' \) \
  ! -name '*.env.example' ! -name '*.env.sample' ! -name '*.env.template' 2>/dev/null)
[[ $FORBIDDEN -eq 0 ]] && report OK "no .env / private-key files in the archive"

# Value-shaped secrets. Placeholders and the documented test strings are allowed
# by name, everything else is a finding.
scan() {
  local label="$1" pattern="$2"
  local hits
  hits="$(grep -rIlE "$pattern" "$UNPACK" 2>/dev/null | head -5 || true)"
  if [[ -n "$hits" ]]; then
    while IFS= read -r f; do report HIT "$label in ${f#"$UNPACK"/}"; done <<< "$hits"
  else
    report OK "$label: none"
  fi
}
scan "live Paystack key"        'sk_live_[A-Za-z0-9]{16,}'
scan "live Stripe key"          'sk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}'
scan "AWS access key id"        'AKIA[0-9A-Z]{16}'
scan "private key block"        'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'
scan "GitHub token"             'gh[pousr]_[A-Za-z0-9]{30,}'
scan "JWTs still live"          'eyJhbGciOiJIUzI1NiJ9\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'

if [[ $FINDINGS -gt 0 ]]; then
  echo "FAIL: $FINDINGS secret finding(s) — the archive is not shippable" >&2
  exit 1
fi
echo "[OK] no secrets found in the archive"

if [[ $DRY -eq 1 ]]; then
  echo
  echo "[dry-run] would now rewrite the commit row in docs/tcdn-ai/AUDIT_BRIEF.md and print the sha256."
  exit 0
fi

SUMS="$(sha256sum "$ZIP" | cut -d' ' -f1)"
SIZE="$(du -h "$ZIP" | cut -f1)"

echo
echo "== update the brief =="
SUBJECT="$(git show -s --format=%s "$SHA")"
COMMIT_DATE="$(git show -s --format=%cI "$SHA")"
python3 - "$BRIEF" "$SHA" "$SHORT" "$ZIP_NAME" "$SUMS" "$SUBJECT" "$COMMIT_DATE" <<'PY'
import re, sys
brief, sha, short, zip_name, sums, subject, when = sys.argv[1:]
text = open(brief).read()
row = (
    f"| Commit | header reviewed at the state merged into `main`; this archive is "
    f"**`{sha}`** ({when}, \"{subject}\"). `dist/{zip_name}` is `git archive` of exactly "
    f"that commit, `sha256 {sums}` |"
)
new, n = re.subn(r"^\| Commit \|.*$", row, text, count=1, flags=re.M)
if n != 1:
    sys.exit("could not find the | Commit | row in the brief")
scope = (
    "* The developer console: `backend/app/ai/routes_console.py`, `backend/app/ai/console.py`, "
    "`frontend/app/ai/**`, `frontend/components/ai/**`, `frontend/lib/aiConsole.ts` — flag-gated, "
    "flag-off proven, evidence in `docs/tcdn-ai/evidence/frontend/`. <!-- console-scope -->"
)
if "<!-- console-scope -->" not in new:
    new = new.replace("**Out of scope / deliberately not done**", scope + "\n\n**Out of scope / deliberately not done**", 1)
open(brief, "w").write(new)
print(f"[OK] brief updated: {brief}")
PY

echo
echo "sha256   $SUMS"
echo "size     $SIZE"
echo "path     $ZIP"
echo
echo "next: review the brief diff, then"
echo "  git add docs/tcdn-ai/AUDIT_BRIEF.md && git commit -m \"Record the console merge in the audit brief and rebuild the zip\""
