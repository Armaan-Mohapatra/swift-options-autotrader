"""Phase 0 CLI: diagnostics and opt-in local schema migration only."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from swift_autotrader import __version__
from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.database.migrate import apply_migrations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swift-autotrader")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("smoke", help="Side-effect-free health check")
    commands.add_parser("config-check", help="Validate fail-closed settings")
    commands.add_parser("dry-run", help="Output paper-only diagnostics; never place orders")
    commands.add_parser("migrate", help="Explicitly initialize/migrate local SQLite DB")
    args = parser.parse_args(argv)
    try:
        config = Settings.from_env()
        if args.command == "smoke":
            payload = {"status": "OK", "version": __version__, "mode": "PAPER_ONLY"}
        elif args.command == "config-check":
            payload = {"status": "VALID", "configuration": config.public_summary()}
        elif args.command == "dry-run":
            payload = {
                "status": "DRY_RUN_ONLY",
                "mode": "PAPER_ONLY",
                "broker_writes_attempted": 0,
                "telegram_connections_attempted": 0,
                "orders_submitted": 0,
                "parser_implemented": False,
                "paper_execution_implemented": False,
                "configuration": config.public_summary(),
            }
        else:
            versions = apply_migrations(config.db_path)
            payload = {"status": "MIGRATED", "applied_versions": versions}
        print(json.dumps(payload, sort_keys=True))
        return 0
    except (ConfigurationError, OSError, ValueError) as exc:
        # Exception values are controlled (configuration values are never echoed).
        print(json.dumps({"status": "ERROR", "reason": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
