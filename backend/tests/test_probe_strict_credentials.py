"""Regression guard: the production probes must never SILENTLY weaken.

Four live probes (upload / api-key / dedup / import) verify a throwaway account
by asking production to send a verification link. If the CI credential
(E2E_ADMIN_TOKEN) has been rotated or revoked, production rejects the call.

Two different events share those scripts:

* branch / PR / manual dispatch -> `PROBE_STRICT_CREDENTIALS=false`.
  A rejected credential is an ops fact, not a product regression, so the probe
  SKIPS (verify_account returns 2, the caller exits 0) instead of turning every
  push red for a reason nobody can fix from the diff.

* the SCHEDULED run on main -> `PROBE_STRICT_CREDENTIALS=true`.
  That run is the only automated proof the live product still works, so a
  rejected credential must FAIL the job (verify_account returns 1, which the
  caller maps to a non-zero exit) and post an ::error annotation.

These tests extract the real `verify_account` out of each shipped script (no
copy of the logic, so the scripts cannot drift from what is tested) and drive it
with a stubbed `curl` that returns production's rejected-credential response.
"""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PROBES = [
    "upload_audit_prod.sh",
    "api_key_audit_prod.sh",
    "dedup_audit_prod.sh",
    "import_audit_prod.sh",
]

# {"sub":"<uuid>"} — the probe decodes the JWT to learn the account id.
JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEifQ"
    ".sig"
)

HARNESS = r"""
set -uo pipefail
check() {{ printf '  check[%s]: %s\n' "$1" "$2"; }}
FE="https://production.invalid"
E2E_ADMIN_TOKEN="rotated-token"
PROBE_STRICT_CREDENTIALS="{strict}"
# Production's answer to a revoked key (see the probe's grep for this class).
curl() {{ printf '%s' '{{"detail":"Invalid or revoked API key"}}'; }}
{body}
verify_account "$JWT_ARG"
exit $?
"""


def _verify_account_body(script: str) -> str:
    src = (SCRIPTS / script).read_text()
    match = re.search(r"^verify_account\(\) \{.*?^\}\n", src, re.DOTALL | re.MULTILINE)
    assert match, f"verify_account() not found in {script}"
    return match.group(0)


@pytest.mark.parametrize("script", PROBES)
def test_scheduled_run_fails_when_credential_is_rejected(script):
    """strict=true: a rejected credential must FAIL, never skip."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            HARNESS.format(strict="true", body=_verify_account_body(script)),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "JWT_ARG": JWT, "GITHUB_ACTIONS": "true"},
    )
    assert proc.returncode == 1, (
        f"{script}: a rejected credential on a scheduled run must return 1 "
        f"(caller then exits non-zero), got {proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    # The failure must be legible in the Actions UI, not buried in the log.
    assert "::error" in proc.stdout, (
        f"{script}: expected an ::error annotation\n{proc.stdout}"
    )
    assert "SKIP" not in proc.stdout, (
        f"{script}: strict mode must not skip\n{proc.stdout}"
    )


@pytest.mark.parametrize("script", PROBES)
def test_branch_run_skips_when_credential_is_rejected(script):
    """strict=false: branch/PR runs skip (rc=2 -> caller exits 0)."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            HARNESS.format(strict="false", body=_verify_account_body(script)),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "JWT_ARG": JWT, "GITHUB_ACTIONS": "true"},
    )
    assert proc.returncode == 2, (
        f"{script}: non-strict mode must return 2 so the caller can skip, "
        f"got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    assert "::error" not in proc.stdout, (
        f"{script}: a skip must not annotate an error\n{proc.stdout}"
    )


@pytest.mark.parametrize("script", PROBES)
def test_callers_turn_strict_failure_into_a_nonzero_exit(script):
    """The caller must not swallow rc=1: strict failure has to fail the job."""
    src = (SCRIPTS / script).read_text()
    # Every verify_account call site must branch on the return code, skip on 2,
    # and exit non-zero on anything else.
    assert re.search(r'verify_account "\$JWT[A-Z_]*"; VERIFY_RC=\$\?', src), (
        f"{script}: verify_account must be called with its status captured"
    )
    assert re.search(r'if \[ "\$VERIFY_RC" = "2" \]; then', src), (
        f"{script}: the skip path must be the explicit rc=2 branch"
    )
    assert re.search(r'if \[ "\$VERIFY_RC" != "0" \]; then', src), (
        f"{script}: non-zero, non-2 return codes must fail"
    )
    # And the failure branch must actually exit non-zero.
    failure_branch = src.split('if [ "$VERIFY_RC" != "0" ]; then', 1)[1][:400]
    assert re.search(r"exit [1-9]", failure_branch), (
        f"{script}: the failure branch must exit non-zero\n{failure_branch}"
    )
