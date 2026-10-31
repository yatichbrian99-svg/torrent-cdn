#!/usr/bin/env python3
"""Read the ZAP JSON reports, print the findings, and fail on HIGH.

Exit codes:
  0  every report parsed and no HIGH alert left after the documented exception
  1  at least one HIGH alert (the job fails; nothing is suppressed)
  2  a report is missing or unreadable (the scan did not produce evidence)

Exactly one exception is encoded here, and it is deliberately narrow: alert
40018 ("SQL Injection") on the four reviewed paths. It is **not** a rule-wide
suppression — the rule keeps running on those paths, the alerts stay in the
report, and every accepted instance is printed under "Accepted (documented)"
with its URL. The same alert on any other path, or any other HIGH alert, fails
the job.

Why the exception lives here and not in the scan plan: this image's automation
add-on rejects the `alertFilter` job's parameters ("Unrecognised parameter for
job alertFilter : alertFilters"), which made ZAP exit 2 and skip the remaining
passes — a silently broken scan, which is worse than a red one. The evidence
behind the exception, including what falsifies it, is
docs/tcdn-ai/SQLI_40018.md.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.parse

RISK = {"3": "HIGH", "2": "MEDIUM", "1": "LOW", "0": "INFO"}
ORDER = ("HIGH", "MEDIUM", "LOW", "INFO")
#: Every pass the workflow must produce a report for. A missing report is exit 2
#: ("the scan produced no evidence"), never a silent pass.
EXPECTED = ("unauth", "auth", "auth-ai")

#: Alert 40018 on these paths only. Each entry is matched against the URL *path*
#: (query string ignored), so a payload in a query parameter cannot widen it.
ACCEPTED_40018_PLUGIN = "40018"
ACCEPTED_40018_PATHS = (
    re.compile(r"^/api/ai/v1/placements/?$"),
    re.compile(r"^/api/ai/v1/nodes/register/?$"),
    re.compile(r"^/api/ai/v1/jobs/[^/]+/ack/?$"),
    re.compile(r"^/api/live/heartbeat/?$"),
)
ACCEPTED_40018_EVIDENCE = "docs/tcdn-ai/SQLI_40018.md"


def load(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        print(f"[FAIL] no report at {path} — the scan produced no evidence", file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(f"[FAIL] {path} is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)


def alerts_of(report: dict) -> list[dict]:
    rows: list[dict] = []
    for site in report.get("site") or []:
        for alert in site.get("alerts") or []:
            instances = alert.get("instances") or []
            rows.append(
                {
                    "risk": RISK.get(str(alert.get("riskcode", "0")), "INFO"),
                    "alert": alert.get("alert") or alert.get("name") or "(unnamed)",
                    "plugin": str(alert.get("pluginid", "")),
                    "confidence": str(alert.get("confidence", "")),
                    "urls": sorted({str(i.get("uri", "")) for i in instances}),
                    "count": len(instances),
                }
            )
    return rows


def _path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path or "/"


def accepted_40018(row: dict) -> tuple[bool, list[str]]:
    """Is this alert the documented exception — and if not, what escapes it?

    Returns ``(accepted, urls_outside)``. An alert is accepted only when *every*
    one of its instances sits on an allowlisted path: a single instance
    elsewhere keeps the whole alert in the HIGH bucket, so a widening cannot
    ride in on a matched sibling.
    """
    if row["plugin"] != ACCEPTED_40018_PLUGIN:
        return False, list(row["urls"])
    outside = [
        url
        for url in row["urls"]
        if not any(pattern.match(_path_of(url)) for pattern in ACCEPTED_40018_PATHS)
    ]
    return (bool(row["urls"]) and not outside), outside


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True, help="directory holding <mode>.json")
    parser.add_argument("--out", required=True, help="markdown summary to write")
    args = parser.parse_args()

    directory = pathlib.Path(args.reports)
    lines: list[str] = ["# ZAP scan summary", ""]
    totals = dict.fromkeys(ORDER, 0)
    accepted_total = 0
    high_rows: list[tuple[str, dict]] = []
    accepted_rows: list[tuple[str, dict]] = []
    rows_written = 0

    for mode in EXPECTED:
        report = load(directory / f"{mode}.json")
        rows = alerts_of(report)
        counts = dict.fromkeys(ORDER, 0)
        for row in rows:
            if row["risk"] == "HIGH":
                accepted, outside = accepted_40018(row)
                if accepted:
                    accepted_rows.append((mode, row))
                    accepted_total += row["count"]
                    continue
                if row["plugin"] == ACCEPTED_40018_PLUGIN:
                    print(
                        f"[FAIL] 40018 on a path outside the documented set: {outside[:3]}",
                        file=sys.stderr,
                    )
                high_rows.append((mode, row))
            counts[row["risk"]] += 1
            totals[row["risk"]] += 1
        generated = report.get("@generated") or report.get("generated") or "?"
        lines.append(f"## {mode} pass")
        lines.append("")
        lines.append(f"Report generated {generated} · {len(rows)} distinct alert types")
        lines.append("")
        if not rows:
            lines.append("No alerts at all. For a JSON API that is a *weak* result, not a pass:")
            lines.append("record it as such rather than as evidence of absence.")
            lines.append("")
        else:
            lines.append("| Risk | Confidence | Alert | Plugin | Instances | Example URL |")
            lines.append("|---|---|---|---|---|---|")
            for row in sorted(rows, key=lambda r: (ORDER.index(r["risk"]), -r["count"])):
                example = row["urls"][0] if row["urls"] else ""
                risk_label = row["risk"]
                if row["risk"] == "HIGH" and accepted_40018(row)[0]:
                    risk_label = "HIGH (accepted)"
                lines.append(
                    f"| {risk_label} | {row['confidence']} | {row['alert']} | "
                    f"{row['plugin']} | {row['count']} | `{example}` |"
                )
            lines.append("")
        rows_written += len(rows)

    if accepted_rows:
        lines.append("## Accepted (documented)")
        lines.append("")
        lines.append(
            f"Alert **40018** on the four reviewed paths only — evidence: "
            f"`{ACCEPTED_40018_EVIDENCE}`. The rule was not disabled and these "
            f"alerts are still in the report; {accepted_total} instance(s) accepted."
        )
        lines.append("")
        lines.append("| Pass | Instances | URLs |")
        lines.append("|---|---|---|")
        for mode, row in accepted_rows:
            urls = "<br>".join(f"`{u}`" for u in row["urls"][:4])
            lines.append(f"| {mode} | {row['count']} | {urls} |")
        lines.append("")

    lines.append("## Totals")
    lines.append("")
    lines.append("| Risk | Alert types |")
    lines.append("|---|---|")
    for risk in ORDER:
        lines.append(f"| {risk} | {totals[risk]} |")
    if accepted_total:
        lines.append(f"| HIGH accepted (40018, allowlisted paths) | {len(accepted_rows)} |")
    lines.append("")

    summary = "\n".join(lines)
    pathlib.Path(args.out).write_text(summary + "\n")
    print(summary)

    if totals["HIGH"]:
        print(f"[FAIL] {totals['HIGH']} HIGH alert type(s) — the job fails, nothing suppressed", file=sys.stderr)
        for mode, row in high_rows:
            print(f"  {mode}: {row['alert']} ({row['plugin']}) on {row['urls'][:3]}", file=sys.stderr)
        raise SystemExit(1)
    if rows_written == 0:
        print(
            "[WARN] every report is empty — the scan ran but returned nothing; "
            "treat this as 'not exercised', not as 'clean'",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
