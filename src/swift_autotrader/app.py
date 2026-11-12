"""Paper-only CLI: diagnostics, explicit listener, parser replay, SQLite migration."""
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
from swift_autotrader.signals.lineage import SignalMatcher
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
    replay = commands.add_parser("parse-replay", help="Classify stored events; no broker writes")
    replay.add_argument("--limit", type=int, default=500)
    commands.add_parser("parse-health", help="Read parser decision counts without connecting")
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
                "parser_implemented": True,
                "paper_execution_implemented": False,
                "configuration": config.public_summary(),
            }
        elif args.command == "migrate":
            payload = {"status": "MIGRATED", "applied_versions": apply_migrations(config.db_path)}
        elif args.command == "telegram-health":
            if config.telegram_channel_id is None:
                raise ConfigurationError("Configure a Telegram channel ID for local health")
            payload = TelegramStore(config.db_path, config.telegram_channel_id).health()
        elif args.command in {"parse-replay", "parse-health"}:
            if config.telegram_channel_id is None:
                raise ConfigurationError("Configure a single source channel ID to classify")
            matcher = SignalMatcher(config.db_path, config.telegram_channel_id)
            if args.command == "parse-replay":
                # No live instrument adapter exists in Phase 2. Entry-like events
                # are REVIEW_REQUIRED until contract existence is verified; do not
                # pretend a synthetic test catalog validates real options.
                outcomes = matcher.process_pending(limit=args.limit)
                payload = {
                    "status": "PARSER_REPLAY_COMPLETE",
                    "processed": len(outcomes),
                    "requires_review": sum(o.status == "REVIEW_REQUIRED" for o in outcomes),
                    "orders_submitted": 0,
                    "broker_writes_attempted": 0,
                    "catalog_validation": "UNAVAILABLE_REQUIRES_PHASE4",
                }
            else:
                payload = {"status": "PARSER_HEALTH", "decisions": matcher.statistics()}
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
        # Never print private Telegram content, tokens, channel IDs, or DB paths.
        print(json.dumps({"status": "ERROR", "reason": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
