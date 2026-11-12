"""Safe Phase 1 CLI: diagnostics, local storage and explicit read-only listener."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence

from swift_autotrader import __version__
from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.database.repository import TelegramStore
from swift_autotrader.logging_config import configure_logging
from swift_autotrader.telegram.client import start_listener


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swift-autotrader")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("smoke", help="Side-effect-free health check")
    commands.add_parser("config-check", help="Validate fail-closed settings")
    commands.add_parser("dry-run", help="Paper-only diagnostics; never place orders")
    commands.add_parser("migrate", help="Explicitly initialize/migrate local SQLite DB")
    commands.add_parser("telegram-health", help="Read local metadata without connecting")
    commands.add_parser("telegram-listen", help="Read-only authorized Telegram listener")
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
        elif args.command == "migrate":
            payload = {"status": "MIGRATED", "applied_versions": apply_migrations(config.db_path)}
        elif args.command == "telegram-health":
            if config.telegram_channel_id is None:
                raise ConfigurationError("Configure a Telegram channel ID for local health")
            payload = TelegramStore(config.db_path, config.telegram_channel_id).health()
        else:
            configure_logging(config.log_level)
            start_listener(config)
            payload = {"status": "LISTENER_STOPPED", "mode": "PAPER_ONLY"}
        print(json.dumps(payload, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "STOPPED", "reason": "INTERRUPTED"}), file=sys.stderr)
        return 130
    except (ConfigurationError, OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"status": "ERROR", "reason": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
