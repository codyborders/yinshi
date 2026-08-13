"""Command-line managed runtime operational status check."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from yinshi.services.managed_operational_status import collect_managed_operational_status


def _parse_utc_timestamp(value: str) -> datetime:
    """Parse one explicit UTC timestamp."""
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone information")
    return timestamp.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    """Build checker arguments without reading application settings."""
    parser = argparse.ArgumentParser(prog="yinshi-managed-operations-check")
    parser.add_argument("--control-db", required=True)
    parser.add_argument("--backup-stale-seconds", type=int, default=86_400)
    parser.add_argument("--operation-stuck-seconds", type=int, default=3_600)
    parser.add_argument("--now", type=_parse_utc_timestamp)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Print sanitized JSON and return a monitoring-compatible status."""
    options = _parser().parse_args(arguments)
    now = options.now or datetime.now(UTC)
    database = sqlite3.connect(f"file:{options.control_db}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        status = collect_managed_operational_status(
            database,
            now=now,
            backup_stale_seconds=options.backup_stale_seconds,
            operation_stuck_seconds=options.operation_stuck_seconds,
        )
    finally:
        database.close()
    print(json.dumps(status.to_dict(), separators=(",", ":"), sort_keys=True))
    return 2 if status.critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
