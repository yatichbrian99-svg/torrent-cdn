#!/usr/bin/env python3
"""Read the ZAP JSON reports, print the findings, and fail on HIGH.

Exit codes:
  0  every report parsed and no HIGH alert left after the documented exception
  1  at least one HIGH alert (the job fails; nothing is suppressed)
  2  a report is missing or unreadable (the scan did not produce evidence)

## The one exception, and its conditions

Alert **40018 ("SQL Injection")** is accepted at *rule* level — not for a fixed
list of URLs — because the rule matches a response *shape*, not a query, and it
moves between endpoints from run to run while its `evidence` stays empty.
Accepting it by path meant the gate went red whenever the scanner happened to
pick a different route.

An instance is accepted **only if all of these hold**; any failure keeps it in
the HIGH bucket and the job fails:

1. **Empty `evidence`.** `evidence` is where ZAP puts the response fragment
   that matched a database-error signature. A non-empty value means the scanner
   did see something, and nothing here excuses that.
2. **No 5xx on that path** in this run, measured at the auth proxy (the only
   place with a per-request status for both authenticated passes). A 500 next
   to the alert is a real defect, whatever the alert says.
3. **No database error text in any response body on that path** in this run.
   The proxy marks such responses `DBERR`; the scanner's JSON keeps no bodies,
   so this is the only place the check can be made.
4. **The path was actually exercised in this run** (at least one request in the
   proxy tally). "No 5xx observed" must not be mistaken for "not looked at": a
   path the authenticated passes never sent a request to has no evidence, so its
   instances are refused rather than accepted.

A non-40018 HIGH alert is never accepted. Nothing is removed from the report:
accepted instances are printed with their URLs and left in place, and the count
appears in the totals.

The *mechanism* behind the exception is proven separately and independently of
this gate:

* `backend/scripts/sql_injection_audit.py` (AST, whole `backend/app`, blocking
  in the `static-audit` job) rejects SQL built with an f-string, `.format()`,
  `%` or concatenation, and rejects any query argument that is not a literal
  constant it can resolve;
* `backend/tests/test_ai_sqli_routes.py` drives the payload families through
  every `/api/ai/v1` input route with a valid credential and asserts never-5xx,
  no database error text, no state change for a refused request, and no gross
  timing difference;
* `backend/tests/test_ai_sqli_binding.py` shows the payloads arrive as bound
  arguments for the store calls those routes reach.

Timing at gate time is not measured (a scan has no stable baseline); it is
asserted in `test_ai_sqli_routes.py` instead. The write-up, including what would
falsify the exception, is `docs/tcdn-ai/LEVEL1_EXCEPTIONS.md` §40018.

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

#: The only HIGH alert this gate can accept, and only under the three conditions
#: documented above.
ACCEPTED_PLUGIN = "40018"
ACCEPTED_EVIDENCE_DOC = "docs/tcdn-ai/LEVEL1_EXCEPTIONS.md"


def load(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        print(f"[FAIL] no report at {path} — the scan produced no evidence", file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(f"[FAIL] {path} is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _instances(report: dict) -> list[dict]:
    rows: list[dict] = []
    for site in report.get("site") or []:
        for alert in site.get("alerts") or []:
            for instance in alert.get("instances") or []:
                rows.append(
                    {
                        "risk": RISK.get(str(alert.get("riskcode", "0")), "INFO"),
                        "alert": alert.get("alert") or alert.get("name") or "(unnamed)",
                        "plugin": str(alert.get("pluginid", "")),
                        "confidence": str(alert.get("confidence", "")),
                        "url": str(instance.get("uri", "")),
                        "param": str(instance.get("param", "")),
                        "attack": str(instance.get("attack", "")),
                        "evidence": str(instance.get("evidence") or ""),
                    }
                )
    return rows


def _path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path or "/"


def parse_proxy_logs(paths: list[pathlib.Path]) -> dict[str, dict[str, int]]:
    """Per-path `{status: count}` plus `db-error` counts, from the proxy lines.

    Line shape: ``auth-proxy <METHOD> <path> -> <status>[ DBERR]``.
    """
    tally: dict[str, dict[str, int]] = {}
    pattern = re.compile(r"^auth-proxy (\w+) (\S+) -> (\d{3})( DBERR)?$")
    for path in paths:
        if not path.exists():
            print(
                f"[FAIL] {path} does not exist — that authenticated pass did not run",
                file=sys.stderr,
            )
            raise SystemExit(2)
        for line in path.read_text(errors="replace").splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            route = urllib.parse.urlsplit(match.group(2)).path or "/"
            row = tally.setdefault(route, {})
            row[match.group(3)] = row.get(match.group(3), 0) + 1
            if match.group(4):
                row["db-error"] = row.get("db-error", 0) + 1
    return tally


def _fives_on(path: str, tally: dict[str, dict[str, int]]) -> int:
    return sum(count for status, count in tally.get(path, {}).items() if status.startswith("5"))


def _db_errors_on(path: str, tally: dict[str, dict[str, int]]) -> int:
    return tally.get(path, {}).get("db-error", 0)


def _requests_on(path: str, tally: dict[str, dict[str, int]]) -> int:
    return sum(
        count for status, count in tally.get(path, {}).items() if status.isdigit()
    )


def acceptance_reasons(
    instance: dict, tally: dict[str, dict[str, int]]
) -> list[str]:
    """Empty list means accepted; otherwise the reasons it is NOT accepted."""
    reasons: list[str] = []
    if instance["plugin"] != ACCEPTED_PLUGIN:
        reasons.append("not the documented rule")
        return reasons
    if instance["evidence"]:
        reasons.append(f"scanner captured evidence: {instance['evidence'][:80]!r}")
    path = _path_of(instance["url"])
    fives = _fives_on(path, tally)
    if fives:
        reasons.append(f"{fives} 5xx response(s) on this path in the same run")
    db_errors = _db_errors_on(path, tally)
    if db_errors:
        reasons.append(f"{db_errors} response(s) on this path carried database error text")
    if not _requests_on(path, tally):
        reasons.append("no request on this path in the proxy tally — unobserved, not clean")
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True, help="directory holding <mode>.json")
    parser.add_argument("--out", required=True, help="markdown summary to write")
    parser.add_argument(
        "--proxy-log",
        action="append",
        default=[],
        help="auth proxy log(s) for the 5xx / DB-error conditions (repeatable)",
    )
    args = parser.parse_args()

    proxy_paths = [pathlib.Path(p) for p in args.proxy_log]
    if not proxy_paths:
        print(
            "[FAIL] no --proxy-log given: the 5xx and DB-error conditions cannot be "
            "checked, so no HIGH alert may be accepted",
            file=sys.stderr,
        )
        raise SystemExit(2)
    tally = parse_proxy_logs(proxy_paths)

    directory = pathlib.Path(args.reports)
    lines: list[str] = ["# ZAP scan summary", ""]
    totals = dict.fromkeys(ORDER, 0)
    accepted_instances = 0
    rejected_reasons: list[str] = []
    high_rows: list[tuple[str, dict]] = []
    accepted_rows: list[tuple[str, dict]] = []
    alerts_written = 0

    for mode in EXPECTED:
        report = load(directory / f"{mode}.json")
        instances = _instances(report)
        counts = dict.fromkeys(ORDER, 0)

        # Group by (alert, plugin, risk) so the table stays readable, but decide
        # acceptance per instance: one instance with evidence must not be hidden
        # behind accepted siblings.
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for instance in instances:
            groups.setdefault(
                (instance["alert"], instance["plugin"], instance["risk"]), []
            ).append(instance)

        lines.append(f"## {mode} pass")
        lines.append("")
        generated = report.get("@generated") or report.get("generated") or "?"
        lines.append(
            f"Report generated {generated} · {len(instances)} instance(s), "
            f"{len(groups)} alert type(s)"
        )
        lines.append("")
        if not groups:
            lines.append("No alerts at all. For a JSON API that is a *weak* result, not a pass:")
            lines.append("record it as such rather than as evidence of absence.")
            lines.append("")
        else:
            lines.append("| Risk | Confidence | Alert | Plugin | Instances | Example URL |")
            lines.append("|---|---|---|---|---|---|")
            for (alert, plugin, risk), group in sorted(
                groups.items(), key=lambda kv: (ORDER.index(kv[0][2]), -len(kv[1]))
            ):
                accepted = [i for i in group if not acceptance_reasons(i, tally)]
                if risk == "HIGH" and accepted:
                    accepted_rows.append((mode, {**group[0], "accepted": accepted, "total": len(group)}))
                    accepted_instances += len(accepted)
                    remaining = len(group) - len(accepted)
                    counts["HIGH"] += 1 if remaining else 0
                    totals["HIGH"] += 1 if remaining else 0
                    label = "HIGH (accepted)" if not remaining else "HIGH (partly accepted)"
                    if remaining:
                        rejected_reasons.extend(
                            f"{mode} {i['url']}: {', '.join(acceptance_reasons(i, tally))}"
                            for i in group
                            if acceptance_reasons(i, tally)
                        )
                        high_rows.append((mode, {**group[0], "urls": [i["url"] for i in group if acceptance_reasons(i, tally)]}))
                else:
                    counts[risk] += 1
                    totals[risk] += 1
                    label = risk
                    if risk == "HIGH":
                        high_rows.append(
                            (mode, {**group[0], "urls": [i["url"] for i in group]})
                        )
                        rejected_reasons.extend(
                            f"{mode} {i['url']}: {', '.join(acceptance_reasons(i, tally)) or 'rule not accepted'}"
                            for i in group
                        )
                lines.append(
                    f"| {label} | {group[0]['confidence']} | {alert} | {plugin} | "
                    f"{len(group)} | `{group[0]['url']}` |"
                )
            lines.append("")
        alerts_written += len(groups)

    if accepted_rows:
        lines.append("## Accepted (documented)")
        lines.append("")
        lines.append(
            f"Alert **{ACCEPTED_PLUGIN}** accepted for **{accepted_instances} instance(s)** "
            f"because each satisfies all four conditions: empty scanner `evidence`, no 5xx "
            f"on its path, no database error text on its path, and the path was actually "
            f"exercised in this run. "
            f"The rule was not disabled, the methodology is "
            f"`{ACCEPTED_EVIDENCE_DOC}`, and the alerts remain in the report."
        )
        lines.append("")
        lines.append("| Pass | Accepted | Instance URL | Path 5xx | Path DB-error | Requests |")
        lines.append("|---|---|---|---|---|---|")
        for mode, group in accepted_rows:
            for instance in group["accepted"]:
                path = _path_of(instance["url"])
                lines.append(
                    f"| {mode} | {len(group['accepted'])}/{group['total']} | "
                    f"`{instance['url']}` | {_fives_on(path, tally)} | "
                    f"{_db_errors_on(path, tally)} | {_requests_on(path, tally)} |"
                )
        lines.append("")

    lines.append("## Totals")
    lines.append("")
    lines.append("| Risk | Alert types |")
    lines.append("|---|---|")
    for risk in ORDER:
        lines.append(f"| {risk} | {totals[risk]} |")
    if accepted_instances:
        lines.append(f"| HIGH accepted ({ACCEPTED_PLUGIN}, conditions met) | {accepted_instances} instance(s) |")
    lines.append("")

    summary = "\n".join(lines)
    pathlib.Path(args.out).write_text(summary + "\n")
    print(summary)

    if totals["HIGH"]:
        print(
            f"[FAIL] {totals['HIGH']} HIGH alert type(s) not covered by the documented "
            "exception — the job fails, nothing suppressed",
            file=sys.stderr,
        )
        for mode, row in high_rows:
            print(f"  {mode}: {row['alert']} ({row['plugin']}) on {row['urls'][:3]}", file=sys.stderr)
        for reason in rejected_reasons:
            print(f"    reason: {reason}", file=sys.stderr)
        raise SystemExit(1)
    if alerts_written == 0:
        print(
            "[WARN] every report is empty — the scan ran but returned nothing; "
            "treat this as 'not exercised', not as 'clean'",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
