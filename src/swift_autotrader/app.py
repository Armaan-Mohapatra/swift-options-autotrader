"""Paper-only CLI: diagnostics, explicit listener, parser replay, SQLite migration."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sqlite3
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

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
    commands.add_parser("paper-health", help="Read only simulated order/stop state")
    commands.add_parser("paper-pause", help="Prevent new paper entries")
    commands.add_parser("paper-resume", help="Resume paper entries unless emergency-stopped")
    commands.add_parser("paper-emergency-stop", help="Stop paper entries and cancel resting buys")
    report = commands.add_parser("paper-report", help="Read confirmed paper fills and P&L")
    report.add_argument("--fee-per-side-usd", default=None)
    history = commands.add_parser("paper-replay", help="Isolated quote-evidence historical replay")
    history.add_argument("--input", required=True)
    history.add_argument("--compare", action="store_true")
    dashboard = commands.add_parser("paper-dashboard", help="Local read-only dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--fee-per-side-usd", default=None)
    commands.add_parser("paper-notify-scan", help="Queue local metadata-only alerts")
    diagnostics = commands.add_parser(
        "mcp-diagnostics", help="Robinhood MCP diagnostics; offline unless --connect"
    )
    diagnostics.add_argument("--connect", action="store_true", help="Local manual OAuth only")
    service = commands.add_parser(
        "paper-dispatch-service", help="Opt-in Windows PAPER read-only data worker"
    )
    service.add_argument("--root", required=True)
    paper_cycle = commands.add_parser("paper-cycle", help="Run one guarded local PAPER cycle")
    paper_cycle.add_argument("--confirm-simulation", action="store_true")
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
                "paper_execution_implemented": True,
                "paper_execution_mode": "SYNTHETIC_ONLY_WITH_EXPLICIT_PREFLIGHT",
                "configuration": config.public_summary(),
            }
        elif args.command == "mcp-diagnostics":
            if args.connect:
                from swift_autotrader.broker.mcp_local_auth import local_readonly_diagnostics
                payload = asyncio.run(local_readonly_diagnostics())
            else:
                from swift_autotrader.broker.mcp_transport import ROBINHOOD_MCP_ENDPOINT
                payload = {
                    "status": "REQUIRES_LOCAL_AUTH",
                    "endpoint": ROBINHOOD_MCP_ENDPOINT,
                    "sdk_available": importlib.util.find_spec("mcp") is not None,
                    "connection_attempted": False,
                    "real_broker_writes_enabled": False,
                }
        elif args.command == "paper-dispatch-service":
            from pathlib import Path

            from swift_autotrader.paper_service import run_paper_service
            asyncio.run(run_paper_service(config, Path(args.root)))
            payload = {"status": "PAPER_DISPATCH_STOPPED", "real_broker_writes_enabled": False}
        elif args.command == "paper-replay":
            from pathlib import Path

            from swift_autotrader.analytics.replay import replay_from_file

            payload = replay_from_file(Path(args.input), compare=args.compare)
        elif args.command == "paper-report":
            from swift_autotrader.analytics.metrics import CostModel, PaperAnalytics

            try:
                fee = Decimal(args.fee_per_side_usd) if args.fee_per_side_usd else None
            except InvalidOperation as exc:
                raise ValueError("invalid fee input") from exc
            payload = PaperAnalytics(
                config.db_path, costs=CostModel(fee_per_contract_side_usd=fee)
            ).snapshot()
        elif args.command == "paper-dashboard":
            from swift_autotrader.analytics.metrics import CostModel
            from swift_autotrader.dashboard.app import serve_local

            try:
                fee = Decimal(args.fee_per_side_usd) if args.fee_per_side_usd else None
            except InvalidOperation as exc:
                raise ValueError("invalid fee input") from exc
            serve_local(
                config.db_path, host=args.host, port=args.port,
                costs=CostModel(fee_per_contract_side_usd=fee),
            )
            payload = {"status": "DASHBOARD_STOPPED", "mode": "PAPER_ONLY"}
        elif args.command == "paper-notify-scan":
            from swift_autotrader.notifications.alerts import NotificationCenter

            center = NotificationCenter(config.db_path)
            payload = {
                "status": "LOCAL_NOTIFICATION_SCAN",
                "mode": "PAPER_ONLY",
                "notifications_new": center.scan(),
                "pending_codes": [n.code for n in center.pending()],
            }
        elif args.command == "migrate":
            payload = {"status": "MIGRATED", "applied_versions": apply_migrations(config.db_path)}
        elif args.command == "telegram-health":
            if config.telegram_channel_id is None:
                raise ConfigurationError("Configure a Telegram channel ID for local health")
            payload = TelegramStore(config.db_path, config.telegram_channel_id).health()
        elif args.command in {"paper-health", "paper-pause", "paper-resume",
                              "paper-emergency-stop", "paper-cycle"}:
            # No catalog/real market adapter is created here. Entries always
            # fail instrument verification in this CLI until a future reviewed
            # read-only adapter is explicitly plugged into the engine.
            from swift_autotrader.broker.paper import PaperBroker
            from swift_autotrader.orders.engine import PaperOrderEngine
            from swift_autotrader.orders.exit_policy import ExitPolicy
            from swift_autotrader.orders.risk import PaperPreflight, RiskCeilings

            engine = PaperOrderEngine(
                config.db_path, PaperBroker(config.db_path),
                preflight=PaperPreflight(),
                limits=RiskCeilings.from_environment(),
                exit_policy=ExitPolicy.from_environment(),
            )
            if args.command == "paper-pause":
                engine.set_paused(True)
            elif args.command == "paper-resume":
                if engine.health()["emergency_stop"]:
                    raise ConfigurationError("Cannot resume while emergency stop is active")
                engine.set_paused(False)
            elif args.command == "paper-emergency-stop":
                engine.emergency_stop()
            elif args.command == "paper-cycle":
                if not args.confirm_simulation:
                    raise ConfigurationError("Explicit --confirm-simulation required")
                # No independently verified option catalog is configured here.
                # Never mark authenticated Swift signals BLOCKED just because a
                # diagnostic-only CLI was invoked with an empty preflight source.
                raise ConfigurationError(
                    "Paper cycle requires a verified instrument provider; "
                    "use the isolated synthetic test harness in Phase 3"
                )
            if args.command != "paper-cycle":
                payload = {"status": "PAPER_CONTROL", **engine.health()}
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
