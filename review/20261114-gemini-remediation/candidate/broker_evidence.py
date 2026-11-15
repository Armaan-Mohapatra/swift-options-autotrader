"""Read-only Robinhood evidence decoder for observed November 2026 response shapes.

A matched brokerage order ID is evidence of an order, not proof of ownership.
Callers must independently bind that ID to a persisted, acknowledged order.
No RPC, execution, or broker writes are implemented here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


class EvidenceError(ValueError):
    pass


def identifier(x):
    if not isinstance(x, str) or not x.strip() or len(x) > 256:
        raise EvidenceError('Invalid identifier')
    return x


def money(x):
    if isinstance(x, bool) or not isinstance(x, (str, int, Decimal)):
        raise EvidenceError('Missing exact decimal')
    try:
        d = Decimal(x)
    except (InvalidOperation, ValueError) as exc:
        raise EvidenceError('Invalid decimal') from exc
    if not d.is_finite() or d < 0:
        raise EvidenceError('Invalid decimal')
    return d


def count(x):
    d = money(x)
    if d != d.to_integral_value():
        raise EvidenceError('Fractional option quantity')
    return int(d)


def instant(x):
    if not isinstance(x, str):
        raise EvidenceError('Missing timestamp')
    try:
        t = datetime.fromisoformat(x.replace('Z', '+00:00'))
    except ValueError as exc:
        raise EvidenceError('Invalid timestamp') from exc
    if t.utcoffset() is None:
        raise EvidenceError('Naive timestamp')
    return t.astimezone(timezone.utc)


@dataclass(frozen=True)
class Execution:
    id: str
    quantity: int
    price: Decimal
    timestamp: datetime
    fees: Decimal | None = None


@dataclass(frozen=True)
class OrderEvidence:
    broker_id: str
    option_id: str
    state: str
    quantity: int
    processed_quantity: int
    price: Decimal
    created_at: datetime
    updated_at: datetime
    accepted_at: datetime | None
    executions: tuple[Execution, ...]
    reference: str | None
    ownership_verified: bool = False
    regulatory_fees: Decimal | None = None
    total_fee: Decimal | None = None


WORKING = {'queued', 'confirmed', 'partially_filled', 'pending_cancelled'}
TERMINAL = {'filled', 'cancelled', 'rejected', 'failed', 'voided'}


def _extract_order_total_fee(row: Mapping) -> Decimal | None:
    fees_obj = row.get('fees')
    total_fee: Decimal | None = None
    if isinstance(fees_obj, Mapping):
        if fees_obj.get('total_fee') is not None:
            total_fee = money(fees_obj['total_fee'])
        if fees_obj.get('estimated') is not None:
            est = money(fees_obj['estimated'])
            if total_fee is not None and total_fee != est:
                raise EvidenceError('Conflicting order fee fields')
            total_fee = est
    elif fees_obj is not None:
        raise EvidenceError('Malformed order fees field')
    if row.get('total_fee') is not None:
        top_total = money(row['total_fee'])
        if total_fee is not None and total_fee != top_total:
            raise EvidenceError('Conflicting order total_fee fields')
        total_fee = top_total
    return total_fee


def decode_order(row, *, expected_id, expected_option_id, side, position_effect,
                 quantity, limit_price, expected_ref=None):
    """Require exact persisted identity and reconcile totals; never infer a missing ref."""
    if not isinstance(row, Mapping):
        raise EvidenceError('Malformed order')
    if side not in {'buy', 'sell'} or (side, position_effect) not in {('buy', 'open'), ('sell', 'close')}:
        raise EvidenceError('Unsupported action')
    oid = identifier(row.get('id'))
    if oid != identifier(expected_id):
        raise EvidenceError('Order ID mismatch')
    if row.get('type') != 'limit' or row.get('direction') != ('debit' if side == 'buy' else 'credit'):
        raise EvidenceError('Order type or direction mismatch')
    legs = row.get('legs')
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
        raise EvidenceError('Only exact single leg supported')
    leg = legs[0]
    option = identifier(leg.get('option_id'))
    if (option != identifier(expected_option_id)
            or leg.get('side') != side
            or leg.get('position_effect') != position_effect
            or count(leg.get('ratio_quantity')) != 1):
        raise EvidenceError('Order leg mismatch')
    total = count(row.get('quantity'))
    if total != count(quantity) or total != 1:
        raise EvidenceError('Quantity mismatch; only one contract supported')
    price = money(row.get('price'))
    if price <= 0 or price != money(limit_price):
        raise EvidenceError('Limit mismatch')
    processed = count(row.get('processed_quantity'))
    if 'filled_quantity' in row and row['filled_quantity'] is not None and count(row['filled_quantity']) != processed:
        raise EvidenceError('Conflicting fill totals')
    if processed > total:
        raise EvidenceError('Overfill')
    state = row.get('state')
    if not isinstance(state, str) or state not in WORKING | TERMINAL:
        raise EvidenceError('Unknown state')
    if (state == 'filled' and processed != total
            or state in {'queued', 'confirmed'} and processed != 0
            or state == 'partially_filled' and not 0 < processed < total):
        raise EvidenceError('State/quantity conflict')
    created = instant(row.get('created_at'))
    updated = instant(row.get('updated_at'))
    accepted = instant(row['accepted_at']) if row.get('accepted_at') is not None else None
    if updated < created or (accepted is not None and not created <= accepted <= updated):
        raise EvidenceError('Invalid order chronology')
    ref = identifier(row['ref_id']) if row.get('ref_id') is not None else None
    if expected_ref is not None:
        identifier(expected_ref)
    if expected_ref is not None and ref is not None and ref != expected_ref:
        raise EvidenceError('Reference mismatch')
    rows = leg.get('executions', [])
    if not isinstance(rows, list):
        raise EvidenceError('Malformed executions')
    fills = []
    ids = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise EvidenceError('Malformed execution')
        fid = identifier(raw.get('id'))
        filled = count(raw.get('quantity'))
        fp = money(raw.get('price'))
        at = instant(raw.get('timestamp'))
        if fid in ids or filled < 1 or filled > total or fp <= 0 or not created <= at <= updated:
            raise EvidenceError('Invalid execution')
        if (side == 'buy' and fp > price) or (side == 'sell' and fp < price):
            raise EvidenceError('Execution violates limit')
        ids.add(fid)
        fee = money(raw['fees']) if raw.get('fees') is not None else None
        fills.append(Execution(fid, filled, fp, at, fee))
    if sum(f.quantity for f in fills) != processed:
        raise EvidenceError('Executions do not reconcile')
    reg_fees = money(row['regulatory_fees']) if row.get('regulatory_fees') is not None else None
    total_fee = _extract_order_total_fee(row)
    return OrderEvidence(
        oid,
        option,
        state,
        total,
        processed,
        price,
        created,
        updated,
        accepted,
        tuple(fills),
        ref,
        False,
        reg_fees,
        total_fee,
    )


@dataclass(frozen=True)
class PositionEvidence:
    option_id: str
    quantity: int
    average_price: Decimal
    multiplier: int
    pending_buy_quantity: int = 0
    pending_sell_quantity: int = 0


def decode_long_position(row):
    if not isinstance(row, Mapping) or row.get('type') != 'long':
        raise EvidenceError('Unsupported position')
    option = identifier(row.get('option_id'))
    qty = count(row.get('quantity'))
    avg = money(row.get('average_price'))
    mult = count(row.get('trade_value_multiplier'))
    pending_buy = count(row['pending_buy_quantity']) if row.get('pending_buy_quantity') is not None else 0
    pending_sell = count(row['pending_sell_quantity']) if row.get('pending_sell_quantity') is not None else 0
    if mult < 1 or (qty > 0 and avg <= 0):
        raise EvidenceError('Unknown position basis')
    return PositionEvidence(option, qty, avg, mult, pending_buy, pending_sell)


def decode_long_positions(rows):
    """Do not assume the units of average_price or calculate exposure from field types."""
    positions = [decode_long_position(row) for row in rows]
    if len({p.option_id for p in positions}) != len(positions):
        raise EvidenceError('Duplicate position across pages')
    return tuple(positions)