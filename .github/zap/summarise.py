#!/usr/bin/env python3
"""Read the ZAP JSON reports, print the findings, and fail on HIGH.

Exit codes:
  0  every report parsed and no HIGH alert
  1  at least one HIGH alert (the job fails; nothing is suppressed)
  2  a report is missing or unreadable (the scan did not produce evidence)

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

RISK = {"3": "HIGH", "2": "MEDIUM", "1": "LOW", "0": "INFO"}
ORDER = ("HIGH", "MEDIUM", "LOW", "INFO")
#: Every pass the workflow must produce a report for. A missing report is exit 2
#: ("the scan produced no evidence"), never a silent pass.
EXPECTED = ("unauth", "auth", "auth-ai")


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True, help="directory holding <mode>.json")
    parser.add_argument("--out", required=True, help="markdown summary to write")
    args = parser.parse_args()

    directory = pathlib.Path(args.reports)
    lines: list[str] = ["# ZAP scan summary", ""]
    totals = dict.fromkeys(ORDER, 0)
    high_rows: list[tuple[str, dict]] = []
    rows_written = 0

    for mode in EXPECTED:
        report = load(directory / f"{mode}.json")
        rows = alerts_of(report)
        counts = dict.fromkeys(ORDER, 0)
        for row in rows:
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
                lines.append(
                    f"| {row['risk']} | {row['confidence']} | {row['alert']} | "
                    f"{row['plugin']} | {row['count']} | `{example}` |"
                )
            lines.append("")
            if counts["HIGH"]:
                high_rows.extend((mode, row) for row in rows if row["risk"] == "HIGH")
        rows_written += len(rows)

    lines.append("## Totals")
    lines.append("")
    lines.append("| Risk | Alert types |")
    lines.append("|---|---|")
    for risk in ORDER:
        lines.append(f"| {risk} | {totals[risk]} |")
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
