"""Environment-wide canary scan (checklist 5.7).

The privacy promise is that a prompt is never stored: gateway mode forwards the
encrypted/marked request, keeps no copy, and the SDK path never sends plaintext
at all. A canary assertion inside one test only proves one code path; this tool
looks for the canary everywhere an engineer would look after an incident — every
text column of every table in the database, the log directory, temp/work
directories, backup dumps, and Redis values.

Usage (nothing is written, nothing is edited — this is a read-only audit):

    python scripts/canary_scan.py --canary "$CANARY" \
        --database-url "$DATABASE_URL" --redis-url "$REDIS_URL" \
        --paths /var/log/torrent-cdn /tmp --backup-dir /var/backups

Exit status: 0 when nothing matched, 1 when anything did, 2 on a usage error.
The canary itself is never printed — only its SHA-256 prefix — so running the
audit cannot be what leaks it into a report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Files worth reading in a log/temp scan. Binary media is skipped by default:
#: a canary in a JPEG is not a leak of the prompt storage path, and reading
#: gigabytes to prove it costs more than it buys.
TEXT_SUFFIXES = {
    ".log",
    ".txt",
    ".json",
    ".jsonl",
    ".ndjson",
    ".csv",
    ".yaml",
    ".yml",
    ".sql",
    ".dump",
    ".backup",
    "",
}

#: Columns that can hold attacker- or customer-controlled text.
TEXT_TYPES = {
    "text",
    "character varying",
    "character",
    "json",
    "jsonb",
    "varchar",
}


def canary_prefix(canary: str) -> str:
    return hashlib.sha256(canary.encode()).hexdigest()[:12]


def scan_bytes(data: bytes, needle: bytes) -> list[int]:
    """Offsets of every occurrence (empty list = clean)."""
    offsets: list[int] = []
    start = data.find(needle)
    while start != -1:
        offsets.append(start)
        start = data.find(needle, start + 1)
    return offsets


def scan_file(path: Path, needle: bytes) -> list[dict[str, Any]]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [{"path": str(path), "error": type(exc).__name__}]
    hits = scan_bytes(data, needle)
    return [{"path": str(path), "offsets": hits}] if hits else []


def scan_paths(paths: Iterable[str | Path], needle: bytes) -> list[dict[str, Any]]:
    """Every readable file under each path whose suffix is worth reading."""
    findings: list[dict[str, Any]] = []
    for root in paths:
        root_path = Path(root)
        if not root_path.exists():
            findings.append({"path": str(root_path), "error": "missing"})
            continue
        candidates = [root_path] if root_path.is_file() else root_path.rglob("*")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in TEXT_SUFFIXES:
                continue
            findings.extend(scan_file(candidate, needle))
    return findings


def scan_tree(obj: Any, needle: str, *, path: str = "$") -> list[str]:
    """Dotted paths inside a JSON-like structure where the canary appears.

    Used for the "database" half of the scan when a store snapshot is already in
    hand (tests, ad-hoc audits) and for any structured export.
    """
    if isinstance(obj, str):
        return [path] if needle in obj else []
    if isinstance(obj, bytes):
        return [path] if needle.encode() in obj else []
    if isinstance(obj, dict):
        found: list[str] = []
        for key, value in obj.items():
            found.extend(scan_tree(value, needle, path=f"{path}.{key}"))
        return found
    if isinstance(obj, (list, tuple, set)):
        found = []
        for index, value in enumerate(obj):
            found.extend(scan_tree(value, needle, path=f"{path}[{index}]"))
        return found
    return []


IDENTIFIER = '"'


def _quote(identifier: str) -> str:
    return IDENTIFIER + identifier.replace(IDENTIFIER, IDENTIFIER * 2) + IDENTIFIER


async def scan_postgres(dsn: str, needled_text: str) -> list[dict[str, Any]]:
    """Count matches in every text-ish column of every user table.

    Identifiers come from information_schema and are quoted; the canary is a
    bound parameter, so this query can never be told to read another table.
    """
    import asyncpg

    connection = await asyncpg.connect(dsn)
    findings: list[dict[str, Any]] = []
    try:
        columns = await connection.fetch(
            """
            SELECT table_schema, table_name, column_name
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND data_type = ANY($1::text[])
            ORDER BY table_schema, table_name, column_name
            """,
            sorted(TEXT_TYPES),
        )
        for column in columns:
            table = f"{_quote(column['table_schema'])}.{_quote(column['table_name'])}"
            identifier = _quote(column["column_name"])
            count = await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE {identifier}::text LIKE $1",
                f"%{needled_text}%",
            )
            if count:
                findings.append(
                    {
                        "table": f"{column['table_schema']}.{column['table_name']}",
                        "column": column["column_name"],
                        "rows": int(count),
                    }
                )
    finally:
        await connection.close()
    return findings


async def scan_redis(url: str, needle: bytes) -> list[dict[str, Any]]:
    """Every value reachable by SCAN, including JSON blobs inside a value."""
    import redis.asyncio as redis

    client = redis.from_url(url)
    findings: list[dict[str, Any]] = []
    try:
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, count=500)
            for key in keys:
                try:
                    value = await client.get(key)
                except Exception:
                    value = None
                if value and needle in value:
                    findings.append({"key": key.decode(errors="replace")})
            if cursor == 0:
                break
    finally:
        await client.aclose()
    return findings


async def run(args: argparse.Namespace) -> dict[str, Any]:
    canary = args.canary
    if len(canary) < 8:
        raise SystemExit("canary must be at least 8 characters")
    needle = canary.encode()

    report: dict[str, Any] = {
        "canary_sha256_prefix": canary_prefix(canary),
        "checks": [],
        "hits": [],
    }

    if args.database_url:
        rows = await scan_postgres(args.database_url, canary)
        report["checks"].append("postgres")
        report["hits"].extend({"kind": "postgres", **row} for row in rows)

    if args.redis_url:
        values = await scan_redis(args.redis_url, needle)
        report["checks"].append("redis")
        report["hits"].extend({"kind": "redis", **row} for row in values)

    file_findings = scan_paths(args.paths or [], needle)
    if args.paths:
        report["checks"].append("files")
    report["hits"].extend({"kind": "file", **row} for row in file_findings)

    if args.backup_dir:
        backup_findings = scan_paths([args.backup_dir], needle)
        report["checks"].append("backups")
        report["hits"].extend({"kind": "backup", **row} for row in backup_findings)

    report["ok"] = not report["hits"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canary", required=True, help="the marker string")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", ""))
    parser.add_argument("--paths", nargs="*", default=[])
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)

    if not args.paths and not args.database_url and not args.redis_url:
        parser.error("nothing to scan: pass --paths, --database-url or --redis-url")

    report = asyncio.run(run(args))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
