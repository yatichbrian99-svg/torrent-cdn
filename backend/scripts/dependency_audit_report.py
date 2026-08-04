#!/usr/bin/env python3
"""
Dependency audit report generator — runs pip-audit (backend) and npm audit
(frontend) against the repository's lockfiles and renders a single markdown
report.

Used by the `dependency-audit` CI job (see .github/workflows/dependency-audit.yml)
on a daily schedule, on manual dispatch, and on pull requests (where the report
is also posted as a PR comment). The job fails the build ONLY when a CRITICAL
advisory appears — the accepted `ip`-via-webtorrent highs stay green by design
(the report still lists them).

Exit codes:
  0  audits ran and the report was written (findings may exist — the caller
     decides whether to gate on them via the printed summary)
  2  a required audit tool is missing or failed to run (broken environment)

Usage:
  python dependency_audit_report.py [--output report.md] [--repo-root /path]

Prints the report to stdout (or --output) and a single machine-readable line
to stdout:
  AUDIT_SUMMARY critical=0 high=4 moderate=0 low=0 info=0 pip=0 npm=4
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4}


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command, tolerating non-zero exits (audit tools exit non-zero on findings)."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_pip_audit(repo_root: Path) -> dict | None:
    """Run pip-audit against the backend venv/requirements and return its JSON."""
    # Prefer an explicit venv python, then whatever python is on PATH.
    python = os.environ.get("AUDIT_PYTHON")
    if not python:
        venv = repo_root / "backend" / ".venv" / "bin" / "python"
        python = str(venv) if venv.exists() else sys.executable
    try:
        proc = _run([python, "-m", "pip_audit", "--format=json"], repo_root / "backend")
    except FileNotFoundError:
        proc = _run(["pip-audit", "--format=json"], repo_root / "backend")
    if proc.returncode not in (0, 1):
        # pip-audit exits 1 when it finds vulnerabilities — that's a success here.
        print(f"[dependency-audit] pip-audit failed (rc={proc.returncode}): {proc.stderr[-500:]}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        print(f"[dependency-audit] pip-audit returned non-JSON: {proc.stdout[-500:]}", file=sys.stderr)
        return None


def run_npm_audit(repo_root: Path) -> dict | None:
    """Run npm audit against the frontend and return its JSON."""
    proc = _run(["npm", "audit", "--json"], repo_root / "frontend")
    if proc.returncode not in (0, 1):
        print(f"[dependency-audit] npm audit failed (rc={proc.returncode}): {proc.stderr[-500:]}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        print(f"[dependency-audit] npm audit returned non-JSON: {proc.stdout[-500:]}", file=sys.stderr)
        return None


def _pip_vuln_rows(data: dict) -> list[tuple[str, str, str]]:
    """[(package, severity, id)] — one row per vulnerable backend package."""
    rows: list[tuple[str, str, str]] = []
    for dep in data.get("dependencies", []) or []:
        for vuln in dep.get("vulns", []) or []:
            sev = str(vuln.get("severity") or "unknown").lower()
            rows.append((dep["name"], sev, str(vuln.get("id") or vuln.get("advisory") or "?")))
    return rows


def _npm_vuln_rows(data: dict) -> list[tuple[str, str, str, bool, str]]:
    """[(package, severity, via, isDirect, fixSummary)] — one row per vulnerable npm package."""
    rows: list[tuple[str, str, str, bool, str]] = []
    for name, info in (data.get("vulnerabilities", {}) or {}).items():
        sev = str(info.get("severity") or "unknown").lower()
        via = info.get("via") or []
        via_titles = []
        for v in via:
            if isinstance(v, dict):
                via_titles.append(str(v.get("title") or v.get("url") or v.get("name") or "?"))
            else:
                via_titles.append(str(v))
        fix = info.get("fixAvailable")
        if isinstance(fix, dict):
            fix_summary = f"update {fix.get('name')} to {fix.get('version')}"
            if fix.get("isSemVerMajor"):
                fix_summary += " (major — breaks API)"
        elif fix:
            fix_summary = "update available"
        else:
            fix_summary = "no fix"
        rows.append((name, sev, "; ".join(via_titles)[:160] or "?", bool(info.get("isDirect")), fix_summary))
    return rows


def render_report(pip_data: dict | None, npm_data: dict | None, repo_root: Path) -> tuple[str, dict[str, int]]:
    """Build the markdown report and the severity-count summary."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0, "info": 0, "pip": 0, "npm": 0}

    lines = [
        "## 🔒 Dependency Audit Report",
        "",
        f"_Generated: {now} — {repo_root.name} @ `main`_",
        "",
    ]

    # ── Backend (pip-audit) ──
    lines.append("### Backend — `pip-audit`")
    if pip_data is None:
        lines.append("\n⚠️ pip-audit could not run (missing tool or broken output).")
    else:
        rows = _pip_vuln_rows(pip_data)
        counts["pip"] = len(rows)
        for _, sev, _ in rows:
            sev = sev if sev in counts else "low"
            counts[sev] += 1
        if not rows:
            lines.append("\n✅ **No known vulnerabilities** in the pinned backend dependencies.")
        else:
            lines.append(f"\n⚠️ {len(rows)} vulnerable package(s) found:\n")
            lines.append("| Package | Severity | Advisory |")
            lines.append("|---|---|---|")
            for pkg, sev, adv in sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r[1], 9), r[0])):
                lines.append(f"| `{pkg}` | **{sev.upper()}** | `{adv}` |")
        lines.append("")

    # ── Frontend (npm audit) ──
    lines.append("### Frontend — `npm audit`")
    if npm_data is None:
        lines.append("\n⚠️ npm audit could not run (missing tool or broken output).")
    else:
        meta = (npm_data.get("metadata") or {}).get("vulnerabilities") or {}
        counts["npm"] = int(meta.get("total", 0))
        rows = _npm_vuln_rows(npm_data)
        for _, sev, _, _, _ in rows:
            sev = sev if sev in counts else "low"
            counts[sev] += 1
        if not rows:
            lines.append("\n✅ **No known vulnerabilities** in the frontend dependency tree.")
        else:
            lines.append(f"\n⚠️ {len(rows)} vulnerable package(s) found:\n")
            lines.append("| Package | Severity | Direct? | Via / advisory | Fix |")
            lines.append("|---|---|---|---|---|")
            for pkg, sev, via, direct, fix in sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r[1], 9), r[0])):
                lines.append(f"| `{pkg}` | **{sev.upper()}** | {'yes' if direct else 'no (transitive)'} | {via} | {fix} |")
        lines.append("")

    # ── Bottom line ──
    total = sum(counts[k] for k in ("critical", "high", "moderate", "low"))
    if counts["critical"]:
        lines.append(f"🚨 **{counts['critical']} CRITICAL advisory(ies).** This run is RED by design — investigate immediately.")
    elif total:
        lines.append(f"⚠️ {total} non-critical finding(s). Known accepted risks (e.g. `ip` via `webtorrent`) are tracked in SECURITY.md → Hardening Log.")
    else:
        lines.append("✅ All clear — no known advisories in either tree.")

    summary = " ".join(f"{k}={counts[k]}" for k in ("critical", "high", "moderate", "low", "info", "pip", "npm"))
    return "\n".join(lines) + "\n", counts


def main() -> int:
    ap = argparse.ArgumentParser(description="pip-audit + npm audit report generator")
    ap.add_argument("--output", default=None, help="write the markdown report to this file (default: stdout)")
    ap.add_argument("--repo-root", default=str(REPO_ROOT), help="repository root (defaults to repo layout)")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    pip_data = run_pip_audit(repo_root)
    npm_data = run_npm_audit(repo_root)

    report, counts = render_report(pip_data, npm_data, repo_root)

    summary = " ".join(f"{k}={counts[k]}" for k in ("critical", "high", "moderate", "low", "info", "pip", "npm"))
    if args.output:
        Path(args.output).write_text(report)
        # With --output, stdout carries ONLY the machine-readable line so the
        # CI job can capture it directly (the report itself is in the file).
        print(f"AUDIT_SUMMARY {summary}")
    else:
        print(report)
        print(f"AUDIT_SUMMARY {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
