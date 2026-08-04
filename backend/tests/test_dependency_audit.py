"""
Tests for backend/scripts/dependency_audit_report.py — the baseline-file
comparison logic behind the `dependency-audit` CI job.

The script is a standalone CLI module (no package), so it is loaded by file
path via importlib. All tested functions are pure — no audit tools or network
are invoked — keeping the suite fast and hermetic.

Covered:
  1. Finding normalization (pip-audit JSON → rows, npm audit JSON → rows)
  2. Baseline identity rules (pip keys on advisory-id; npm keys on severity)
  3. save_baseline / load_baseline round-trip
  4. render_report: no-baseline, matching-baseline (new=0), NEW finding
     (new=1 + report section), npm severity escalation (new=1)
  5. main(): missing baseline file → exit 2; mutually-exclusive flags → exit 2
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dependency_audit_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("dependency_audit_report", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_module()

# ── Sample audit-tool JSON (shapes copied from pip-audit / npm audit) ──

PIP_DATA = {
    "dependencies": [
        {
            "name": "badlib",
            "version": "1.0.0",
            "vulns": [{"id": "PYSEC-2026-0001", "severity": "HIGH"}],
        }
    ]
}

NPM_DATA = {
    "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 1, "critical": 0, "total": 1}},
    "vulnerabilities": {
        "ip": {
            "name": "ip",
            "severity": "high",
            "isDirect": False,
            "via": [{"title": "ip SSRF improper categorization in isPublic"}],
            "fixAvailable": {"name": "webtorrent", "version": "0.7.3", "isSemVerMajor": True},
        }
    },
}


# ── Finding normalization ──

class TestFindingNormalization:
    def test_pip_findings_normalized(self):
        rows = audit._pip_findings(PIP_DATA)
        assert rows == [{
            "ecosystem": "pip",
            "package": "badlib",
            "id": "PYSEC-2026-0001",
            "severity": "high",
        }]

    def test_npm_findings_normalized(self):
        rows = audit._npm_findings(NPM_DATA)
        assert len(rows) == 1
        row = rows[0]
        assert row["ecosystem"] == "npm"
        assert row["package"] == "ip"
        assert row["severity"] == "high"
        assert row["direct"] is False
        assert "ip SSRF" in row["via"]
        assert "webtorrent" in row["fix"]
        assert "major" in row["fix"]

    def test_pip_missing_advisory_id_falls_back(self):
        data = {"dependencies": [{"name": "x", "version": "1", "vulns": [{}]}]}
        rows = audit._pip_findings(data)
        assert rows[0]["id"] == "?"

    def test_collect_findings_combines_ecosystems(self):
        findings = audit.collect_findings(PIP_DATA, NPM_DATA)
        assert {f["ecosystem"] for f in findings} == {"pip", "npm"}
        assert len(findings) == 2

    def test_collect_findings_handles_none(self):
        assert audit.collect_findings(None, None) == []
        assert len(audit.collect_findings(PIP_DATA, None)) == 1


# ── Baseline identity rules ──

class TestIdentity:
    def test_pip_identity_uses_advisory_id(self):
        f = {"ecosystem": "pip", "package": "badlib", "id": "PYSEC-2026-0001", "severity": "high"}
        assert audit._identity(f) == ("pip", "badlib", "PYSEC-2026-0001")

    def test_npm_identity_uses_severity(self):
        f = {"ecosystem": "npm", "package": "ip", "severity": "high", "id": "anything"}
        assert audit._identity(f) == ("npm", "ip", "high")


# ── save_baseline / load_baseline round-trip ──

class TestBaselineRoundTrip:
    def _findings(self):
        return audit.collect_findings(PIP_DATA, NPM_DATA)

    def _counts(self, findings):
        counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0, "info": 0, "pip": 0, "npm": 0}
        for f in findings:
            sev = f["severity"] if f["severity"] in counts else "low"
            counts[sev] += 1
            counts[f["ecosystem"]] += 1
        return counts

    def test_save_then_load_round_trip(self, tmp_path):
        baseline_path = tmp_path / "baseline.json"
        findings = self._findings()
        audit.save_baseline(baseline_path, findings, self._counts(findings))

        ids, counts = audit.load_baseline(baseline_path)
        assert ids == {
            ("pip", "badlib", "PYSEC-2026-0001"),
            ("npm", "ip", "high"),
        }
        assert counts["high"] == 2
        assert counts["pip"] == 1
        assert counts["npm"] == 1

    def test_baseline_file_is_json_with_counts(self, tmp_path):
        baseline_path = tmp_path / "baseline.json"
        findings = self._findings()
        audit.save_baseline(baseline_path, findings, self._counts(findings))
        raw = json.loads(baseline_path.read_text())
        assert raw["total"] == 2
        assert raw["generated_at"]


# ── render_report baseline comparison ──

class TestBaselineComparison:
    @staticmethod
    def _render(pip_data, npm_data, baseline_ids=None):
        baseline = (baseline_ids, {}) if baseline_ids is not None else None
        return audit.render_report(pip_data, npm_data, Path("repo"), baseline)

    def test_no_baseline_new_is_zero(self):
        report, counts, new = self._render(PIP_DATA, NPM_DATA)
        assert new == 0
        assert counts["high"] == 2
        assert "New since last baseline" not in report

    def test_matching_baseline_new_is_zero(self):
        ids = {("pip", "badlib", "PYSEC-2026-0001"), ("npm", "ip", "high")}
        report, _counts, new = self._render(PIP_DATA, NPM_DATA, ids)
        assert new == 0
        assert "No new findings" in report

    def test_new_npm_finding_detected(self):
        # Baseline accepted `ip`; the tree now ALSO has a new vulnerable package.
        npm = json.loads(json.dumps(NPM_DATA))
        npm["vulnerabilities"]["newpkg"] = {
            "name": "newpkg", "severity": "high", "isDirect": True,
            "via": ["newpkg vuln"], "fixAvailable": False,
        }
        ids = {("npm", "ip", "high")}
        report, _counts, new = self._render(None, npm, ids)
        assert new == 1
        assert "newpkg" in report
        assert "New since last baseline" in report
        assert "1 new advisory(ies) vs baseline" in report

    def test_new_pip_finding_detected(self):
        # Same package, but a SECOND advisory id → new finding.
        pip = json.loads(json.dumps(PIP_DATA))
        pip["dependencies"][0]["vulns"].append({"id": "PYSEC-2026-0002", "severity": "MODERATE"})
        ids = {("pip", "badlib", "PYSEC-2026-0001")}
        report, _counts, new = self._render(pip, None, ids)
        assert new == 1
        assert "PYSEC-2026-0002" in report

    def test_npm_severity_escalation_is_new(self):
        # npm identity includes severity: the SAME package escalating
        # high → critical must be reported as new, not masked by the baseline.
        npm = json.loads(json.dumps(NPM_DATA))
        npm["vulnerabilities"]["ip"]["severity"] = "critical"
        ids = {("npm", "ip", "high")}
        report, _counts, new = self._render(None, npm, ids)
        assert new == 1
        assert "CRITICAL" in report.upper()

    def test_fixed_finding_is_not_new(self):
        # Baseline accepted `ip` + `badlib`; the tree now only has `badlib`
        # (a fix landed) → nothing is new.
        ids = {("npm", "ip", "high"), ("pip", "badlib", "PYSEC-2026-0001")}
        report, _counts, new = self._render(PIP_DATA, None, ids)
        assert new == 0
        assert "No new findings" in report


# ── main() exit-code / arg handling ──

class TestMain:
    def _run_main(self, argv, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", ["dependency_audit_report.py"] + argv)
        # Keep main() from invoking real audit tools for the exit-code paths
        # that run them (missing-baseline returns 2 BEFORE the tools run).
        return audit.main()

    def test_missing_baseline_file_exits_2(self, monkeypatch, tmp_path):
        assert self._run_main(["--baseline", str(tmp_path / "nope.json")], monkeypatch, tmp_path) == 2

    def test_write_and_compare_flags_mutually_exclusive(self, monkeypatch, tmp_path):
        rc = self._run_main(
            ["--baseline", "a.json", "--write-baseline", "b.json"], monkeypatch, tmp_path
        )
        assert rc == 2
