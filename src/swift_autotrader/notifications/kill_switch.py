"""Mobile Telegram Bot Emergency Kill Switch handler and long-polling runner.

Enforces sender allowlisting, operator status queries, pause/resume,
cancel open buys, emergency stop, and two-step confirmation for position liquidation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.orders.engine import PaperOrderEngine

logger = logging.getLogger("swift_autotrader.notifications.kill_switch")


class TelegramKillSwitchBot:
    """Mobile emergency kill switch command handler and Telegram Bot API interface."""

    def __init__(
        self,
        bot_token: str,
        *,
        allowed_chat_ids: Sequence[int],
        allowed_user_ids: Sequence[int] | None = None,
        token_ttl_seconds: float = 60.0,
        sender_fn: Callable[[int, str], None] | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.allowed_user_ids = set(allowed_user_ids) if allowed_user_ids else None
        self.token_ttl_seconds = token_ttl_seconds
        self.sender_fn = sender_fn
        self.sent_messages: list[tuple[int, str]] = []
        self.pending_tokens: dict[str, dict[str, Any]] = {}
        self.engine_control: dict[str, Any] = {
            "paused": False,
            "emergency_stop": False,
        }

    def send_message(self, chat_id: int, text: str) -> None:
        """Send message via injected sender or record locally."""
        self.sent_messages.append((chat_id, text))
        if self.sender_fn is not None:
            self.sender_fn(chat_id, text)

    def handle_command(
        self,
        chat_id: int,
        command_text: str,
        *,
        now_ts: float | None = None,
        engine: PaperOrderEngine | None = None,
        user_id: int | None = None,
    ) -> str:
        """Process incoming kill switch command from Telegram user."""
        now = now_ts if now_ts is not None else time.time()
        text = command_text.strip()

        # Sender allowlist verification (fail-closed)
        if chat_id not in self.allowed_chat_ids:
            msg = "UNAUTHORIZED: Sender chat ID not authorized for kill switch commands."
            self.send_message(chat_id, msg)
            return msg

        if self.allowed_user_ids is not None and user_id is not None and user_id not in self.allowed_user_ids:
            msg = "UNAUTHORIZED: Sender user ID not authorized for kill switch commands."
            self.send_message(chat_id, msg)
            return msg

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "/status":
            if engine is not None:
                ctrl = engine.health()
                msg = (
                    f"Mode: {ctrl['mode']} | Paused: {ctrl['paused']} | "
                    f"Emergency Stop: {ctrl['emergency_stop']} | "
                    f"Active Orders: {sum(ctrl['orders_by_state'].values())}"
                )
            else:
                paused = self.engine_control["paused"]
                stop = self.engine_control["emergency_stop"]
                msg = f"Mode: LIVE | Paused: {paused} | Emergency Stop: {stop}"
            self.send_message(chat_id, msg)
            return msg

        if cmd == "/pause":
            self.engine_control["paused"] = True
            if engine is not None:
                engine.set_paused(True, reason="KILL_SWITCH_PAUSE")
            msg = "Engine paused. New entries halted."
            self.send_message(chat_id, msg)
            return msg

        if cmd == "/resume":
            if engine is not None:
                _, emergency_stop = engine._control()
            else:
                emergency_stop = self.engine_control["emergency_stop"]

            if emergency_stop:
                msg = "Cannot resume: Emergency Stop is active. Manual reset required."
            else:
                self.engine_control["paused"] = False
                if engine is not None:
                    engine.set_paused(False, reason="KILL_SWITCH_RESUME")
                msg = "Engine resumed."
            self.send_message(chat_id, msg)
            return msg

        if cmd == "/cancel":
            canceled_count = 0
            if engine is not None:
                if hasattr(engine, "cancel_pending_buys"):
                    canceled_count = engine.cancel_pending_buys(datetime.now(UTC))
                else:
                    with engine._db() as db:
                        rows = db.execute(
                            "SELECT logical_order_id FROM order_intents WHERE side='BUY_TO_OPEN' AND state IN ('ACCEPTED', 'PARTIALLY_FILLED')"
                        ).fetchall()
                    for r in rows:
                        if engine._attempt_cancel(str(r[0]), datetime.now(UTC)):
                            canceled_count += 1
            msg = f"CANCELED: {canceled_count} open pending buy orders cancelled."
            self.send_message(chat_id, msg)
            return msg

        if cmd == "/stop":
            self.engine_control["emergency_stop"] = True
            self.engine_control["paused"] = True
            if engine is not None:
                engine.emergency_stop(reason="KILL_SWITCH_EMERGENCY_STOP")
            msg = "EMERGENCY STOP ACTIVATED. Open buys cancelled and entries halted."
            self.send_message(chat_id, msg)
            return msg

        # Flatten confirmation check: supports /confirm_flatten <token> or /flatten CONFIRM_<token> or /flatten <token>
        is_flatten_confirm = (
            cmd == "/confirm_flatten"
            or (cmd == "/flatten" and bool(args.strip()))
        )
        if is_flatten_confirm:
            token = args.strip()
            if token.startswith("CONFIRM_"):
                token = token[len("CONFIRM_"):].strip()

            if not token or token not in self.pending_tokens:
                msg = "Invalid or unknown confirmation token."
                self.send_message(chat_id, msg)
                return msg

            token_data = self.pending_tokens[token]

            # Strict caller verification: caller chat ID must match issuing chat ID
            if token_data.get("chat_id") != chat_id:
                msg = "Confirmation token was issued to a different chat ID."
                self.send_message(chat_id, msg)
                return msg

            # Only consume the token once caller authorization and identity are confirmed
            self.pending_tokens.pop(token)
            if now - token_data["created_at"] > self.token_ttl_seconds:
                msg = "Confirmation token expired. Flatten aborted."
                self.send_message(chat_id, msg)
                return msg

            # Execute flatten
            self.engine_control["emergency_stop"] = True
            self.engine_control["paused"] = True
            intents_count = 0
            if engine is not None:
                intents = engine.flatten_positions(datetime.now(UTC))
                intents_count = len(intents)

            msg = f"FLATTEN EXECUTED: Emergency stop engaged and {intents_count} liquidation orders submitted."
            self.send_message(chat_id, msg)
            return msg

        if cmd == "/flatten":
            # Generate deterministic 4-character token
            token = hashlib.sha256(f"flatten:{chat_id}:{now}".encode()).hexdigest()[:4]
            self.pending_tokens[token] = {"created_at": now, "chat_id": chat_id}
            msg = (
                f"⚠️ CONFIRM FLATTEN: Reply /confirm_flatten {token} or /flatten CONFIRM_{token} "
                f"within 60s to liquidate positions."
            )
            self.send_message(chat_id, msg)
            return msg

        msg = "Unknown command. Available commands: /status, /pause, /resume, /cancel, /stop, /flatten"
        self.send_message(chat_id, msg)
        return msg


class TelegramKillSwitchService:
    """Long-polling Telegram Bot API service managing kill switch command loop."""

    def __init__(
        self,
        bot: TelegramKillSwitchBot,
        *,
        engine: PaperOrderEngine | None = None,
        poll_interval_seconds: float = 1.0,
        poll_timeout_seconds: int = 15,
        requester: Callable[..., Any] | None = None,
    ) -> None:
        self.bot = bot
        self.engine = engine
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.requester = requester
        self.last_update_id = 0
        self.running = False

    def _call_api(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform request to Telegram Bot API or injected test requester."""
        if self.requester is not None:
            return self.requester(method, params or {})

        url = f"https://api.telegram.org/bot{self.bot.bot_token}/{method}"
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.poll_timeout_seconds + 5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll_once(self) -> int:
        """Fetch pending updates via getUpdates and dispatch commands."""
        params = {
            "offset": self.last_update_id + 1,
            "timeout": self.poll_timeout_seconds,
            "allowed_updates": ["message"],
        }
        try:
            resp = self._call_api("getUpdates", params)
            if not resp.get("ok"):
                logger.warning("Telegram Bot API getUpdates failed: %s", resp)
                return 0

            updates = resp.get("result", [])
            dispatched = 0
            for upd in updates:
                uid = upd.get("update_id", 0)
                if uid > self.last_update_id:
                    self.last_update_id = uid

                msg = upd.get("message", {})
                chat = msg.get("chat", {})
                sender = msg.get("from", {})
                text = msg.get("text", "")
                chat_id = chat.get("id")
                user_id = sender.get("id")

                if chat_id is not None and text:
                    self.bot.handle_command(
                        chat_id,
                        text,
                        engine=self.engine,
                        user_id=user_id,
                    )
                    dispatched += 1

            return dispatched
        except Exception as exc:
            logger.warning("Error during Telegram kill switch poll: %s", exc)
            return 0

    def run(self) -> None:
        """Run long-polling service until stopped."""
        self.running = True
        logger.info("Starting Telegram kill switch listener...")

        # Also inject sender_fn into bot if not already set to send real API replies
        if self.bot.sender_fn is None:
            def api_sender(chat_id: int, text: str) -> None:
                try:
                    self._call_api("sendMessage", {"chat_id": chat_id, "text": text})
                except Exception as exc:
                    logger.error("Failed to send reply to chat %s: %s", chat_id, exc)
            self.bot.sender_fn = api_sender

        while self.running:
            try:
                self.poll_once()
            except Exception as exc:
                logger.error("Unhandled error in kill switch listener loop: %s", exc)
            time.sleep(self.poll_interval_seconds)

    def stop(self) -> None:
        """Signal listener loop to stop."""
        self.running = False


def run_kill_switch_listener(
    config: Settings,
    engine: PaperOrderEngine | None = None,
    *,
    service_factory: Callable[..., TelegramKillSwitchService] | None = None,
) -> None:
    """Run production Telegram kill switch listener process."""
    if not config.kill_switch_enabled:
        raise ConfigurationError("Kill switch is disabled in configuration")
    if not config.kill_switch_telegram_bot_token:
        raise ConfigurationError("Kill switch bot token missing")
    if not config.kill_switch_telegram_chat_id:
        raise ConfigurationError("Kill switch chat ID missing")

    order_engine = engine
    if order_engine is None:
        from swift_autotrader.broker.paper import PaperBroker
        from swift_autotrader.orders.risk import PaperPreflight, RiskCeilings

        order_engine = PaperOrderEngine(
            config.db_path,
            PaperBroker(config.db_path),
            preflight=PaperPreflight(),
            limits=RiskCeilings.from_environment(),
            live_mode=config.is_live,
        )

    bot = TelegramKillSwitchBot(
        config.kill_switch_telegram_bot_token,
        allowed_chat_ids=(config.kill_switch_telegram_chat_id,),
    )
    if service_factory is not None:
        service = service_factory(bot, engine=order_engine)
    else:
        service = TelegramKillSwitchService(bot, engine=order_engine)

    try:
        service.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Kill switch listener stopped by signal.")
        service.stop()

