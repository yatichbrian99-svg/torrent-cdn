#!/usr/bin/env python3
"""
Dependency audit report generator — runs pip-audit (backend) and npm audit
(frontend) against the repository's lockfiles and renders a single markdown
report.

Used by the `dependency-audit` CI job (see .github/workflows/dependency-audit.yml)
on a daily schedule, on manual dispatch, and on pull requests (where the report
is also posted as a PR comment).

Baseline mode (--baseline): the job must fail when NEW advisories appear, not
just criticals — but the known accepted risks (e.g. the `ip`-via-webtorrent
highs, tracked in SECURITY.md → Hardening Log) must stay green. A committed
baseline file captures the CURRENT accepted finding set; each run compares
against it and reports new findings (`new=N` in the summary, plus a "New since
baseline" section in the report). Regenerate the baseline with
--write-baseline whenever a risk is accepted or fixed:
    python dependency_audit_report.py --write-baseline backend/scripts/dependency-audit-baseline.json

The CI workflow gates on `new != 0` (a separate step, so the report/comment are
still posted showing WHAT is new) and on `critical != 0`.

Exit codes:
  0  audits ran and the report was written (findings may exist — the caller
     decides whether to gate on them via the printed summary)
  2  a required audit tool is missing/failed, or --baseline was given but the
     baseline file does not exist (broken environment / stale checkout)

Usage:
  python dependency_audit_report.py [--output report.md] [--repo-root /path]
                                    [--baseline baseline.json] [--write-baseline out.json]

Prints the report to stdout (or --output) and a single machine-readable line
to stdout:
  AUDIT_SUMMARY critical=0 high=4 moderate=0 low=0 info=0 pip=0 npm=4 new=0
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


def _pip_findings(data: dict) -> list[dict]:
    """Normalized backend findings: {ecosystem, package, id, severity}."""
    findings: list[dict] = []
    for dep in data.get("dependencies", []) or []:
        for vuln in dep.get("vulns", []) or []:
            findings.append({
                "ecosystem": "pip",
                "package": dep["name"],
                "id": str(vuln.get("id") or vuln.get("advisory") or "?"),
                "severity": str(vuln.get("severity") or "unknown").lower(),
            })
    return findings


def _npm_findings(data: dict) -> list[dict]:
    """Normalized frontend findings: {ecosystem, package, id, severity, direct, via, fix}."""
    findings: list[dict] = []
    for name, info in (data.get("vulnerabilities", {}) or {}).items():
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
        findings.append({
            "ecosystem": "npm",
            "package": name,
            "id": "; ".join(via_titles)[:160] or name,
            "severity": str(info.get("severity") or "unknown").lower(),
            "direct": bool(info.get("isDirect")),
            "via": "; ".join(via_titles)[:160] or "?",
            "fix": fix_summary,
        })
    return findings


def collect_findings(pip_data: dict | None, npm_data: dict | None) -> list[dict]:
    """All normalized findings across both ecosystems."""
    findings: list[dict] = []
    if pip_data is not None:
        findings.extend(_pip_findings(pip_data))
    if npm_data is not None:
        findings.extend(_npm_findings(npm_data))
    return findings


def _identity(f: dict) -> tuple:
    """Stable identity for a finding — used for baseline set comparison.

    pip: (ecosystem, package, advisory-id) — IDs are stable (PYSEC-…).
    npm: (ecosystem, package, severity) — one audit entry per package.
    """
    if f["ecosystem"] == "pip":
        return (f["ecosystem"], f["package"], f["id"])
    return (f["ecosystem"], f["package"], f["severity"])


def load_baseline(path: Path) -> tuple[set, dict[str, int]]:
    """Load a baseline file → (identity set, severity counts). Raises if missing/invalid."""
    data = json.loads(path.read_text())
    baseline_ids = {tuple(entry) for entry in data.get("findings", [])}
    counts = {k: int(data.get(k, 0)) for k in ("critical", "high", "moderate", "low", "info", "pip", "npm")}
    return baseline_ids, counts


def save_baseline(path: Path, findings: list[dict], counts: dict[str, int]) -> None:
    """Write the current finding set as a baseline file."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "critical": counts["critical"],
        "high": counts["high"],
        "moderate": counts["moderate"],
        "low": counts["low"],
        "info": counts["info"],
        "pip": counts["pip"],
        "npm": counts["npm"],
        "total": sum(counts[k] for k in ("critical", "high", "moderate", "low")),
        # findings: [identity-tuples...] — the set the job must not regress beyond.
        "findings": [list(_identity(f)) for f in findings],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def render_report(
    pip_data: dict | None,
    npm_data: dict | None,
    repo_root: Path,
    baseline: tuple[set, dict[str, int]] | None,
) -> tuple[str, dict[str, int], int]:
    """Build the markdown report, the severity-count summary, and the count of
    NEW findings vs the baseline (0 when no baseline is given)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0, "info": 0, "pip": 0, "npm": 0}

    lines = [
        "## Dependency Audit Report",
        "",
        f"_Generated: {now} — {repo_root.name} @ `main`_",
        "",
    ]

    # ── Backend (pip-audit) ──
    lines.append("### Backend — `pip-audit`")
    if pip_data is None:
        lines.append("\n[WARN] pip-audit could not run (missing tool or broken output).")
    else:
        rows = _pip_findings(pip_data)
        counts["pip"] = len(rows)
        for f in rows:
            sev = f["severity"] if f["severity"] in counts else "low"
            counts[sev] += 1
        if not rows:
            lines.append("\n[OK] **No known vulnerabilities** in the pinned backend dependencies.")
        else:
            lines.append(f"\n[WARN] {len(rows)} vulnerable package(s) found:\n")
            lines.append("| Package | Severity | Advisory |")
            lines.append("|---|---|---|")
            for f in sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["package"])):
                lines.append(f"| `{f['package']}` | **{f['severity'].upper()}** | `{f['id']}` |")
        lines.append("")

    # ── Frontend (npm audit) ──
    lines.append("### Frontend — `npm audit`")
    if npm_data is None:
        lines.append("\n[WARN] npm audit could not run (missing tool or broken output).")
    else:
        meta = (npm_data.get("metadata") or {}).get("vulnerabilities") or {}
        counts["npm"] = int(meta.get("total", 0))
        rows = _npm_findings(npm_data)
        for f in rows:
            sev = f["severity"] if f["severity"] in counts else "low"
            counts[sev] += 1
        if not rows:
            lines.append("\n[OK] **No known vulnerabilities** in the frontend dependency tree.")
        else:
            lines.append(f"\n[WARN] {len(rows)} vulnerable package(s) found:\n")
            lines.append("| Package | Severity | Direct? | Via / advisory | Fix |")
            lines.append("|---|---|---|---|---|")
            for f in sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["package"])):
                lines.append(
                    f"| `{f['package']}` | **{f['severity'].upper()}** | "
                    f"{'yes' if f['direct'] else 'no (transitive)'} | {f['via']} | {f['fix']} |"
                )
        lines.append("")

    # ── Baseline comparison ──
    findings = collect_findings(pip_data, npm_data)
    new_count = 0
    if baseline is not None:
        baseline_ids, _bl_counts = baseline
        new_findings = [f for f in findings if _identity(f) not in baseline_ids]
        new_count = len(new_findings)
        lines.append("### New since last baseline")
        if new_findings:
            lines.append(
                f"\n[ALERT] **{new_count} NEW finding(s)** not present in the committed baseline "
                "(`backend/scripts/dependency-audit-baseline.json`):\n"
            )
            lines.append("| Ecosystem | Package | Severity | Advisory |")
            lines.append("|---|---|---|---|")
            for f in sorted(new_findings, key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["ecosystem"], r["package"])):
                lines.append(
                    f"| {f['ecosystem']} | `{f['package']}` | **{f['severity'].upper()}** | `{f['id']}` |"
                )
            lines.append(
                "\nIf this is a newly accepted risk, regenerate the baseline:\n"
                "`python backend/scripts/dependency_audit_report.py --write-baseline backend/scripts/dependency-audit-baseline.json`"
            )
        else:
            lines.append("\n[OK] No new findings — the current advisory set matches the accepted baseline.")
        lines.append("")

    # ── Bottom line ──
    total = sum(counts[k] for k in ("critical", "high", "moderate", "low"))
    if new_count:
        lines.append(f"[ALERT] **{new_count} new advisory(ies) vs baseline.** This run is RED — investigate before merge.")
    elif counts["critical"]:
        lines.append(f"[ALERT] **{counts['critical']} CRITICAL advisory(ies).** This run is RED by design — investigate immediately.")
    elif total:
        lines.append(f"[WARN] {total} non-critical finding(s) — all within the accepted baseline (see SECURITY.md → Hardening Log).")
    else:
        lines.append("[OK] All clear — no known advisories in either tree.")

    return "\n".join(lines) + "\n", counts, new_count


def main() -> int:
    ap = argparse.ArgumentParser(description="pip-audit + npm audit report generator")
    ap.add_argument("--output", default=None, help="write the markdown report to this file (default: stdout)")
    ap.add_argument("--repo-root", default=str(REPO_ROOT), help="repository root (defaults to repo layout)")
    ap.add_argument(
        "--baseline", default=None,
        help="baseline JSON to compare against (fails the report with new=N when findings regress beyond it)",
    )
    ap.add_argument(
        "--write-baseline", default=None,
        help="write the CURRENT finding set as a baseline JSON and exit (use when accepting/fixing risks)",
    )
    args = ap.parse_args()

    if args.write_baseline and args.baseline:
        print("[dependency-audit] --write-baseline and --baseline are mutually exclusive", file=sys.stderr)
        return 2

    repo_root = Path(args.repo_root).resolve()
    pip_data = run_pip_audit(repo_root)
    npm_data = run_npm_audit(repo_root)

    baseline = None
    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"[dependency-audit] baseline file not found: {baseline_path}", file=sys.stderr)
            return 2
        baseline = load_baseline(baseline_path)

    if args.write_baseline:
        findings = collect_findings(pip_data, npm_data)
        counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0, "info": 0, "pip": 0, "npm": 0}
        for f in findings:
            sev = f["severity"] if f["severity"] in counts else "low"
            counts[sev] += 1
            counts[f["ecosystem"]] += 1
        save_baseline(Path(args.write_baseline), findings, counts)
        print(f"[dependency-audit] baseline written: {args.write_baseline}")
        print(f"AUDIT_SUMMARY critical={counts['critical']} high={counts['high']} moderate={counts['moderate']} "
              f"low={counts['low']} info={counts['info']} pip={counts['pip']} npm={counts['npm']} new=0")
        return 0

    report, counts, new_count = render_report(pip_data, npm_data, repo_root, baseline)

    summary = " ".join(f"{k}={counts[k]}" for k in ("critical", "high", "moderate", "low", "info", "pip", "npm"))
    summary += f" new={new_count}"
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
