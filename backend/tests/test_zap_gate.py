"""Tests for the ZAP gate (`.github/zap/summarise.py`).

The gate is the thing that decides whether a scanner finding fails the build, so
its acceptance conditions are tested here rather than trusted. Alert 40018 is
accepted only when **all three** hold for an instance: empty scanner `evidence`,
no 5xx on its path, and no database error text on its path. Every test below
breaks exactly one condition and requires the gate to fail.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GATE = REPO / ".github" / "zap" / "summarise.py"


def _alert(plugin: str, risk: int, instances: list[dict], alert: str = "SQL Injection") -> dict:
    return {
        "site": [
            {
                "alerts": [
                    {
                        "pluginid": plugin,
                        "alert": alert,
                        "riskcode": str(risk),
                        "confidence": "2",
                        "instances": [
                            {
                                "uri": i["uri"],
                                "param": i.get("param", "x"),
                                "attack": i.get("attack", "1'--"),
                                "evidence": i.get("evidence", ""),
                            }
                            for i in instances
                        ],
                    }
                ]
            }
        ]
    }


def _run(tmp_path: pathlib.Path, alerts_by_pass: dict[str, dict], proxy_lines: list[str]):
    reports = tmp_path / "reports"
    reports.mkdir()
    for mode in ("unauth", "auth", "auth-ai"):
        payload = alerts_by_pass.get(mode, {"site": []})
        (reports / f"{mode}.json").write_text(json.dumps(payload))
    log = tmp_path / "proxy.log"
    log.write_text("\n".join(proxy_lines) + "\n")
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--reports",
            str(reports),
            "--out",
            str(tmp_path / "summary.md"),
            "--proxy-log",
            str(log),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


BASE_LOG = [
    "auth-proxy POST /api/ai/v1/attestations -> 401",
    "auth-proxy POST /api/ai/v1/placements -> 429",
    "auth-proxy GET /api/ai/v1/models -> 200",
]


def test_40018_with_empty_evidence_and_a_clean_path_is_accepted(tmp_path):
    result = _run(
        tmp_path,
        {"auth-ai": _alert("40018", 3, [{"uri": "http://h/api/ai/v1/attestations"}])},
        BASE_LOG,
    )
    assert result.returncode == 0, result.stderr
    assert "HIGH (accepted)" in result.stdout
    assert "accepted for **1 instance(s)**" in result.stdout


def test_40018_with_scanner_evidence_fails(tmp_path):
    result = _run(
        tmp_path,
        {
            "auth-ai": _alert(
                "40018",
                3,
                [
                    {
                        "uri": "http://h/api/ai/v1/attestations",
                        "evidence": "syntax error at or near",
                    }
                ],
            )
        },
        BASE_LOG,
    )
    assert result.returncode == 1
    assert "scanner captured evidence" in result.stderr


def test_40018_with_a_5xx_on_its_path_fails(tmp_path):
    result = _run(
        tmp_path,
        {"auth-ai": _alert("40018", 3, [{"uri": "http://h/api/ai/v1/attestations"}])},
        BASE_LOG + ["auth-proxy POST /api/ai/v1/attestations -> 500"],
    )
    assert result.returncode == 1
    assert "5xx response(s) on this path" in result.stderr


def test_40018_with_database_error_text_on_its_path_fails(tmp_path):
    result = _run(
        tmp_path,
        {"auth-ai": _alert("40018", 3, [{"uri": "http://h/api/ai/v1/placements"}])},
        BASE_LOG + ["auth-proxy POST /api/ai/v1/placements -> 422 DBERR"],
    )
    assert result.returncode == 1
    assert "database error text" in result.stderr


def test_40018_on_a_path_the_pass_never_reached_fails(tmp_path):
    """No request on the path means no evidence; that is not a pass."""
    result = _run(
        tmp_path,
        {"auth-ai": _alert("40018", 3, [{"uri": "http://h/api/ai/v1/nodes/register"}])},
        BASE_LOG,
    )
    assert result.returncode == 1
    assert "unobserved, not clean" in result.stderr


def test_another_high_alert_is_never_accepted(tmp_path):
    result = _run(
        tmp_path,
        {
            "auth": _alert(
                "89", 3, [{"uri": "http://h/api/live/heartbeat"}], alert="Something Else"
            )
        },
        BASE_LOG,
    )
    assert result.returncode == 1
    assert "not covered by the documented exception" in result.stderr


def test_a_missing_proxy_log_is_not_a_pass(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    for mode in ("unauth", "auth", "auth-ai"):
        (reports / f"{mode}.json").write_text(json.dumps({"site": []}))
    result = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--reports",
            str(reports),
            "--out",
            str(tmp_path / "summary.md"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "cannot be\nchecked" in result.stderr or "cannot be" in result.stderr


@pytest.mark.parametrize("mode", ["unauth", "auth", "auth-ai"])
def test_a_missing_report_is_an_error_not_a_pass(tmp_path, mode):
    reports = tmp_path / "reports"
    reports.mkdir()
    for other in ("unauth", "auth", "auth-ai"):
        if other != mode:
            (reports / f"{other}.json").write_text(json.dumps({"site": []}))
    log = tmp_path / "proxy.log"
    log.write_text("\n".join(BASE_LOG) + "\n")
    result = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--reports",
            str(reports),
            "--out",
            str(tmp_path / "summary.md"),
            "--proxy-log",
            str(log),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
