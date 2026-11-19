"""Owner-issued standing mandate for unattended operation. Fail-closed throughout.

What this module is: the *application-layer* authorization that replaces typing an
approval code at the console, but ONLY when ALL of these hold for the exact order:
  1. the owner created a time-boxed, scoped mandate on this machine (typed phrase);
  2. the mandate is bound to this pinned account, source channel and broker contract;
  3. the order is inside the mandate's actions, premium ceiling and daily order cap;
  4. the digest of the order has never been used (single use);
  5. the BROKER ITSELF reports that this exact order is eligible to be placed without
     individual customer confirmation.

What it is not: it never changes broker approval settings, never retries, never
treats an unreadable/unknown broker answer as permission, and cannot be turned on by
code, tests or defaults. Broker permission and application permission are separate;
both must say yes. A mandate file is integrity-checked, not tamper-proof against
someone with write access to this PC.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MANDATE_VERSION = 1
MAX_VALIDITY_DAYS = 14
ALLOWED = frozenset({"BUY", "SELL", "CANCEL"})
LEDGER_DIGEST_KEEP = 2000


class MandateError(RuntimeError):
    """Mandate missing, invalid, expired, or inconsistent. Always blocks."""


def _canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256_hex(value: object) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def enable_phrase(expires_at: datetime) -> str:
    return "AUTHORIZE UNATTENDED TRADING UNTIL " + expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _atomic_write(path: Path, value: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="swift-mandate-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _review_expressly_unblocked(review: Mapping[str, object]) -> bool:
    """Verify broker review states no customer approval is required and checks are clean."""
    if not isinstance(review, Mapping):
        return False
    keys = [k for k in ("customer_approval_required", "approval_required") if k in review]
    if not keys or any(type(review.get(k)) is not bool for k in keys):
        return False
    if len(keys) == 2 and review["customer_approval_required"] != review["approval_required"]:
        return False
    if review[keys[0]] is not False:
        return False
    if "order_checks" not in review:
        return False
    if "order_checks" in review:
        checks = review.get("order_checks")
        if isinstance(checks, Mapping):
            if checks:
                return False
        elif isinstance(checks, list):
            for row in checks:
                if not isinstance(row, Mapping) or row.get("severity") not in {"pass", "ok", "info"}:
                    return False
        else:
            return False
    return True


@dataclass(frozen=True)
class Mandate:
    account_fingerprint: str
    source_channel: int
    issued_at: datetime
    expires_at: datetime
    allowed_actions: frozenset[str]
    max_premium_usd: Decimal
    max_orders_per_day: int
    broker_contract_sha256: str
    release_id: str | None = None
    risk_config_sha256: str | None = None

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        pin: str,
        source_channel: int,
        at: datetime,
        expected_release_id: str | None = None,
        expected_risk_config_sha256: str | None = None,
    ) -> "Mandate":
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise MandateError("mandate missing or unsafe")
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            claimed = raw.pop("body_sha256")
            if claimed != sha256_hex(raw):
                raise MandateError("mandate integrity check failed")
            if raw["version"] != MANDATE_VERSION:
                raise MandateError("unsupported mandate version")
            rel_id = raw.get("release_id")
            risk_sha = raw.get("risk_config_sha256")
            if rel_id is not None and (not isinstance(rel_id, str) or not rel_id.strip()):
                raise MandateError("invalid mandate release_id")
            if risk_sha is not None and (not isinstance(risk_sha, str) or len(risk_sha) != 64):
                raise MandateError("invalid mandate risk_config_sha256")
            m = cls(
                account_fingerprint=raw["account_fingerprint"],
                source_channel=raw["source_channel"],
                issued_at=datetime.fromisoformat(raw["issued_at_utc"]),
                expires_at=datetime.fromisoformat(raw["expires_at_utc"]),
                allowed_actions=frozenset(raw["allowed_actions"]),
                max_premium_usd=Decimal(str(raw["max_premium_usd"])),
                max_orders_per_day=raw["max_orders_per_day"],
                broker_contract_sha256=raw["broker_contract_sha256"],
                release_id=rel_id,
                risk_config_sha256=risk_sha,
            )
        except MandateError:
            raise
        except Exception as exc:
            raise MandateError("mandate malformed") from exc
        if (
            m.issued_at.utcoffset() is None
            or m.expires_at.utcoffset() is None
            or not m.issued_at < m.expires_at <= m.issued_at + timedelta(days=MAX_VALIDITY_DAYS)
            or not m.allowed_actions
            or not m.allowed_actions <= ALLOWED
            or not m.max_premium_usd.is_finite()
            or m.max_premium_usd <= 0
            or type(m.max_orders_per_day) is not int
            or not 1 <= m.max_orders_per_day <= 200
            or type(m.source_channel) is not int
            or len(m.broker_contract_sha256) != 64
        ):
            raise MandateError("mandate fields invalid")
        if m.account_fingerprint != hashlib.sha256(pin.encode()).hexdigest():
            raise MandateError("mandate bound to a different account")
        if m.source_channel != source_channel:
            raise MandateError("mandate bound to a different source channel")
        if expected_release_id is not None and m.release_id != expected_release_id:
            raise MandateError("mandate bound to a different release")
        if (
            expected_risk_config_sha256 is not None
            and m.risk_config_sha256 != expected_risk_config_sha256
        ):
            raise MandateError("mandate bound to a different risk configuration")
        if at.utcoffset() is None or not m.issued_at <= at < m.expires_at:
            raise MandateError("mandate not currently valid (expired or not yet issued)")
        return m


def create_mandate(
    path: Path,
    *,
    pin: str,
    source_channel: int,
    issued_at: datetime,
    validity_days: int,
    allowed_actions: set[str],
    max_premium_usd: Decimal,
    max_orders_per_day: int,
    broker_contract_sha256: str,
    typed_phrase: str,
    release_id: str | None = None,
    risk_config_sha256: str | None = None,
) -> Mandate:
    """Create a mandate only from an exact typed human phrase. Never overwrites."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise MandateError("mandate exists; revoke it first, never overwrite")
    if not 1 <= validity_days <= MAX_VALIDITY_DAYS:
        raise MandateError("validity out of range")
    expires = issued_at + timedelta(days=validity_days)
    if typed_phrase != enable_phrase(expires):
        raise MandateError("typed authorization phrase does not match")
    body: dict[str, object] = {
        "version": MANDATE_VERSION,
        "account_fingerprint": hashlib.sha256(pin.encode()).hexdigest(),
        "source_channel": source_channel,
        "issued_at_utc": issued_at.astimezone(UTC).isoformat(),
        "expires_at_utc": expires.astimezone(UTC).isoformat(),
        "allowed_actions": sorted(allowed_actions),
        "max_premium_usd": str(max_premium_usd),
        "max_orders_per_day": max_orders_per_day,
        "broker_contract_sha256": broker_contract_sha256,
    }
    if release_id is not None:
        body["release_id"] = release_id
    if risk_config_sha256 is not None:
        body["risk_config_sha256"] = risk_config_sha256
    _atomic_write(path, {**body, "body_sha256": sha256_hex(body)})
    return Mandate.load(
        path,
        pin=pin,
        source_channel=source_channel,
        at=issued_at,
        expected_release_id=release_id,
        expected_risk_config_sha256=risk_config_sha256,
    )


class BrokerEligibilityProbe:
    contract_sha256: str = ""

    async def eligible_without_confirmation(
        self, payload: Mapping[str, object], details: Mapping[str, object] | None = None
    ) -> bool:  # pragma: no cover
        raise NotImplementedError


def _dig(obj: Any, path: list[str]) -> Any:
    for part in path:
        if not isinstance(obj, Mapping) or part not in obj:
            raise KeyError(part)
        obj = obj[part]
    return obj


def _validate_probe_spec(spec: Mapping[str, Any]) -> None:
    if (
        not isinstance(spec.get("tool"), str)
        or not isinstance(spec.get("args"), Mapping)
        or not isinstance(spec.get("eligible_path"), list)
        or not spec["eligible_path"]
        or "eligible_value" not in spec
    ):
        raise MandateError("broker approval contract unusable (per-order scope and field mapping required)")
    name = spec["tool"]
    if any(w in name for w in ("place", "cancel", "submit", "create", "delete", "decline", "exercise")):
        raise MandateError("probe tool must be read-only")


class ConfiguredApprovalProbe(BrokerEligibilityProbe):
    """Calls read-only tool(s) in an owner-captured contract and verifies eligibility.

    Supports both:
    1. Single-tool per-order contract:
       {"scope": "per_order", "tool": str, "args": {...}, "eligible_path": [...], "eligible_value": ...}
    2. Composite per-order + account-setting contract (with optional "prechecks" and "cancel_probe"):
       - Verifies account-level `get_trade_approval_setting` (`setting.human_must_approve_trades == False`)
         and `get_trade_approvals` (`approvals == []`) when configured in `prechecks`,
       - Verifies per-order `review_option_order` (`customer_approval_required == False`) for BUY/SELL,
       - Uses `cancel_probe` (or `prechecks`) for CANCEL requests that only carry `{account_number, order_id}`.
    """

    def __init__(
        self,
        client: Any,
        contract: Mapping[str, Any],
        tools: Mapping[str, Mapping] | None,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.client, self.contract, self.tools, self.timeout = (
            client,
            dict(contract),
            tools or {},
            timeout,
        )
        self.contract_sha256 = sha256_hex(self.contract)
        if self.contract.get("scope") != "per_order":
            raise MandateError("broker approval contract unusable (per-order scope and field mapping required)")
        _validate_probe_spec(self.contract)
        prechecks = self.contract.get("prechecks", [])
        if not isinstance(prechecks, list):
            raise MandateError("broker approval prechecks must be a list")
        for check in prechecks:
            if not isinstance(check, Mapping):
                raise MandateError("malformed precheck specification")
            _validate_probe_spec(check)
        cancel_probe = self.contract.get("cancel_probe")
        if cancel_probe is not None:
            if not isinstance(cancel_probe, Mapping):
                raise MandateError("malformed cancel_probe specification")
            _validate_probe_spec(cancel_probe)

    def _build_args(
        self,
        spec: Mapping[str, Any],
        payload: Mapping[str, object],
        details: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        leg = payload["legs"][0] if isinstance(payload.get("legs"), list) and payload["legs"] else {}
        details_map = details if isinstance(details, Mapping) else {}
        contract_info = details_map.get("contract") if isinstance(details_map.get("contract"), Mapping) else {}
        broker_rev = details_map.get("broker_review") if isinstance(details_map.get("broker_review"), Mapping) else {}
        values: dict[str, object] = {
            "account_number": payload.get("account_number"),
            "order_id": payload.get("order_id"),
            "option_id": leg.get("option_id"),
            "side": leg.get("side"),
            "position_effect": leg.get("position_effect"),
            "ratio_quantity": leg.get("ratio_quantity", 1),
            "legs": payload.get("legs"),
            "quantity": payload.get("quantity"),
            "price": payload.get("price"),
            "type": payload.get("type"),
            "time_in_force": payload.get("time_in_force"),
            "market_hours": payload.get("market_hours"),
            "ref_id": payload.get("ref_id"),
            "chain_symbol": payload.get("chain_symbol") or contract_info.get("symbol"),
            "underlying_type": (
                payload.get("underlying_type")
                or details_map.get("underlying_type")
                or broker_rev.get("underlying_type")
            ),
        }
        out: dict[str, object] = {}
        for k, v in spec["args"].items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                key = v[1:-1]
                if key not in values or values[key] is None:
                    raise MandateError("probe placeholder unavailable")
                out[k] = values[key]
            else:
                out[k] = v
        return out

    def _args(self, payload: Mapping[str, object], details: Mapping[str, object] | None = None) -> dict[str, object]:
        return self._build_args(self.contract, payload, details)

    async def _run_single_spec(
        self,
        spec: Mapping[str, Any],
        payload: Mapping[str, object],
        details: Mapping[str, object] | None = None,
    ) -> bool:
        tool_name = spec["tool"]
        tool = self.tools.get(tool_name)
        if tool is None:
            return False
        schema = tool.get("inputSchema") or tool.get("input_schema")
        args = self._build_args(spec, payload, details)
        if isinstance(schema, Mapping):
            import jsonschema

            jsonschema.validate(args, schema)
        else:
            return False
        resp = await asyncio.wait_for(self.client.call_tool(tool_name, args), timeout=self.timeout)
        data = _structured(resp)
        actual = _dig(data, spec["eligible_path"])
        expected = spec["eligible_value"]
        if actual != expected or not isinstance(actual, type(expected)):
            return False
        if tool_name == "review_option_order" and isinstance(data, Mapping):
            if not _review_expressly_unblocked(data):
                return False
        return True

    async def eligible_without_confirmation(
        self, payload: Mapping[str, object], details: Mapping[str, object] | None = None
    ) -> bool:
        try:
            for pre in self.contract.get("prechecks", []):
                if await self._run_single_spec(pre, payload, details) is not True:
                    return False
            is_cancel = "order_id" in payload and "legs" not in payload
            if is_cancel and isinstance(self.contract.get("cancel_probe"), Mapping):
                return await self._run_single_spec(self.contract["cancel_probe"], payload, details)
            return await self._run_single_spec(self.contract, payload, details)
        except Exception:
            return False


def _structured(resp: Any) -> Any:
    if isinstance(resp, Mapping):
        if resp.get("isError") or resp.get("is_error"):
            raise ValueError("MCP tool error")
        sc = resp.get("structuredContent") or resp.get("structured_content")
        if isinstance(sc, Mapping):
            return sc.get("data") if isinstance(sc.get("data"), Mapping) else sc
        if isinstance(resp.get("data"), Mapping):
            return resp["data"]
        return resp
    if getattr(resp, "isError", False) or getattr(resp, "is_error", False):
        raise ValueError("MCP tool error")
    sc = getattr(resp, "structuredContent", None) or getattr(resp, "structured_content", None)
    if isinstance(sc, Mapping):
        return sc.get("data") if isinstance(sc.get("data"), Mapping) else sc
    raise ValueError("no structured response")


class UnattendedAuthorizer:
    def __init__(
        self,
        mandate: Mandate,
        pin: str,
        probe: BrokerEligibilityProbe,
        ledger_path: Path,
        stop_path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        notify: Callable[..., None] | None = None,
    ) -> None:
        if probe.contract_sha256 != mandate.broker_contract_sha256:
            raise MandateError("mandate was issued for a different broker contract")
        self.mandate, self.pin, self.probe = mandate, pin, probe
        self.ledger_path, self.stop_path, self.clock = Path(ledger_path), Path(stop_path), clock
        self.notify = notify or (lambda *a, **k: None)
        self.last_denial: str | None = None

    def _ledger(self, at: datetime) -> dict[str, Any]:
        day = at.astimezone(NY).date().isoformat()
        try:
            led = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.exists() else {}
        except Exception as exc:
            raise MandateError("ledger unreadable; unattended approval blocked") from exc
        if led.get("day") != day:
            led = {"day": day, "orders_today": 0, "used_digests": led.get("used_digests", [])}
        led.setdefault("used_digests", [])
        return led

    def _deny(self, reason: str) -> bool:
        self.last_denial = reason
        print(json.dumps({"status": "UNATTENDED_DENIED", "reason": reason}), flush=True)
        self.notify("UNATTENDED_ORDER_DENIED", dedupe_key=None, reason=reason)
        return False

    @staticmethod
    def classify(payload: Mapping[str, object]) -> str | None:
        if "order_id" in payload and "legs" not in payload:
            return "CANCEL"
        legs = payload.get("legs")
        if isinstance(legs, list) and len(legs) == 1:
            return {"buy": "BUY", "sell": "SELL"}.get(str(legs[0].get("side")))
        return None

    async def __call__(self, request: Mapping[str, object], details: Mapping[str, object], pin: str) -> bool:
        try:
            at = self.clock()
            if not self.mandate.issued_at <= at < self.mandate.expires_at:
                return self._deny("mandate expired")
            if os.path.lexists(self.stop_path):
                return self._deny("emergency stop active")
            if pin != self.pin or request.get("account_number") != self.pin:
                return self._deny("account mismatch")
            kind = self.classify(request)
            if kind is None or kind not in self.mandate.allowed_actions:
                return self._deny("action not permitted by mandate")
            if kind in ("BUY", "SELL"):
                review = details.get("broker_review") if isinstance(details, Mapping) else None
                if not isinstance(review, Mapping) or not _review_expressly_unblocked(review):
                    return self._deny("broker review did not state approval_required is false")
            if kind == "BUY":
                try:
                    premium = Decimal(str(request["price"])) * 100
                except (InvalidOperation, KeyError):
                    return self._deny("premium unreadable")
                if not premium.is_finite() or premium <= 0 or premium > self.mandate.max_premium_usd:
                    return self._deny("premium above mandate ceiling")
            digest = sha256_hex(request)
            led = self._ledger(at)
            if digest in led["used_digests"]:
                return self._deny("order digest already used")
            if led["orders_today"] >= self.mandate.max_orders_per_day:
                return self._deny("daily order cap reached")
            try:
                probe_ok = await self.probe.eligible_without_confirmation(request, details)
            except TypeError:
                # Backward compatibility with 1-arg custom test probes
                probe_ok = await self.probe.eligible_without_confirmation(request)
            if probe_ok is not True:
                return self._deny("broker did not confirm order is eligible without individual confirmation")
            led["orders_today"] += 1
            led["used_digests"] = (led["used_digests"] + [digest])[-LEDGER_DIGEST_KEEP:]
            _atomic_write(self.ledger_path, led)
            print(json.dumps({"status": "UNATTENDED_AUTHORIZED", "action": kind, "order_digest": digest[:12]}), flush=True)
            return True
        except Exception as exc:
            return self._deny("authorizer error: " + type(exc).__name__)


def capture_tool_catalog(tools: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure helper: reduce a tools/list result to names + input schemas for the owner to
    inspect when writing the broker approval contract. Performs no broker call."""
    out = {}
    for t in tools:
        name = t.get("name")
        if isinstance(name, str):
            out[name] = {
                "description": str(t.get("description", ""))[:300],
                "inputSchema": t.get("inputSchema") or t.get("input_schema"),
            }
    return {"tool_count": len(out), "tools": out}