"""Offline evidence inventory; no network, local account data, or activation."""
from __future__ import annotations


def review_status(commands: frozenset[str]) -> dict[str, object]:
    return {
        "status": "REVIEW_ONLY_NOT_LIVE_CERTIFIED",
        "owner_policy": {
            "standing_intent_for_eligible_swift_alerts": True,
            "broker_approval_independently_required": True,
            "default_contracts": 1,
            "lotto_eligible": True,
            "average_down": False,
            "partial_exit_means_full_owned_exit": True,
            "slippage_tolerance_pct": "10",
            "daily_loss_basis": "netted_realized_price_pnl",
            "broker_fees_in_current_loss_measure": False,
            "current_repository_loss_default_usd": "750",
            "proposed_aggregate_ceiling_usd": "10000",
            "live_limits_activated": False,
            "mobile_bot_required_for_mvp": False,
            "advanced_automatic_restart_required_for_mvp": False,
        },
        "cli": {"live_dispatch_registered": "live-dispatch" in commands,
                "paper_dispatch_registered": "paper-dispatch-service" in commands},
        "unverified": ["broker_approval_and_lifecycle", "production_launcher",
                       "production_installation", "native_windows_acceptance",
                       "live_journal_identity_and_restart_reconciliation"],
        "broker_contacted": False,
        "workers_started": False,
        "real_order_submission": False,
        "live_certified": False,
    }


def startup_check(commands: frozenset[str]) -> dict[str, object]:
    """Check installed Python entrypoint wiring without loading configuration or services."""
    from swift_autotrader.live_dispatch import LiveDispatcher
    from swift_autotrader.paper_service import run_paper_service
    from swift_autotrader.supervisor import run_supervisor
    required = frozenset({"mvp-review", "mvp-startup-check", "supervisor", "paper-dispatch-service", "telegram-listen"})
    missing = sorted(required - commands)
    imports_ok = all(callable(x) for x in (LiveDispatcher.run_once, run_paper_service, run_supervisor))
    return {"status": "OFFLINE_STARTUP_CHECK_ONLY", "offline_check_passed": not missing and imports_ok,
            "missing_review_commands": missing, "dispatcher_importable": imports_ok,
            "live_dispatch_registered": "live-dispatch" in commands,
            "live_startup_ready": False, "workers_started": False,
            "broker_contacted": False, "live_certified": False}
