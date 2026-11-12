"""Inspectably export deterministic Phase 2 parser outputs for synthetic inputs.

This report contains *only generated synthetic fixture messages*.
It never reads the user's database, Telegram account or private channels.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swift_autotrader.parser.rules import ParsedAlert, parse_message  # noqa: E402


def normalized(alert: ParsedAlert) -> dict[str, str | None]:
    expiry = alert.expiry
    return {
        "intent": alert.intent.value,
        "underlying": alert.underlying,
        "right": alert.right.value if alert.right is not None else None,
        "strike": str(alert.strike) if alert.strike is not None else None,
        "expiration_hint": (
            f"{expiry.year:04d}-{expiry.month:02d}-{expiry.day:02d}"
            if expiry and expiry.year is not None
            else f"MM/DD:{expiry.month:02d}/{expiry.day:02d}" if expiry else None
        ),
        "original_premium": str(alert.premium) if alert.premium is not None else None,
        "swift_reported_average": (
            str(alert.swift_average) if alert.swift_average is not None else None
        ),
        "trade_category": alert.category,
        "reason": alert.reason,
    }


def generate() -> dict[str, object]:
    fixture = ROOT / "tests" / "fixtures" / "swift_phase2_synthetic.json"
    samples = json.loads(fixture.read_text(encoding="utf-8"))
    results = []
    for i, row in enumerate(samples, start=1):
        actual = normalized(parse_message(row["text"]))
        results.append({
            "fixture_id": row["id"],
            "synthetic_source_message_id": i,
            "synthetic_text": row["text"],
            "expected_intent": row["intent"],
            "observed": actual,
            "intent_match": actual["intent"] == row["intent"],
        })
    expected = Counter(r["expected_intent"] for r in results)
    actual = Counter(r["observed"]["intent"] for r in results)
    return {
        "dataset": "synthetic only; not real Swift channel or market data",
        "metrics_caveat": "in-sample fixtures; no real-world parser accuracy inferred",
        "sample_count": len(results),
        "intent_agreement_count": sum(r["intent_match"] for r in results),
        "expected_counts": dict(sorted(expected.items())),
        "observed_counts": dict(sorted(actual.items())),
        "cases": results,
    }


def main() -> None:
    target = ROOT / "docs/PHASE_REPORTS/PHASE_2_PARSED_EXAMPLES.json"
    target.write_text(json.dumps(generate(), indent=2) + "\n", encoding="utf-8")
    print(f"Exported {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
