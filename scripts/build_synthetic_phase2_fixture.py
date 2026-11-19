"""Regenerate the checked-in *fictional* Phase 2 parser benchmark cases.

This script uses explicit human-assigned intended intents, never parser output.
No paid Telegram text or channel identifiers are used.
"""
from __future__ import annotations

import json
from pathlib import Path

cases: list[dict[str, str]] = []


def add(prefix: str, examples: list[str], intent: str, **extras: str) -> None:
    for i, body in enumerate(examples, start=1):
        cases.append({"id": f"{prefix}-{i:02}", "text": body, "intent": intent, **extras})


add("entry", [
    "SPY 680C 11/13 @ 1.20", "BUY SPY 680C 11/13 @ 1.20",
    "BTO SPY 680C 11/13 @ 1.20", "OPEN SPY 680C 11/13 @ 1.20",
    "SPY 680C 11/13/2026 @ 1.20", "SPY 680CALL 11/13/26 @ 1.20",
    "SPY 680 C 2026-11-13 @ 1.20", "spy 680c 11/13 @ 1.20",
    "SPY 680C 11/13 @ $1.20", "SPY   680C   11/13   @   1.20",
], "ENTRY", underlying="SPY", premium="1.20", category="STANDARD")
add("lotto", [
    "SPY LOTTO 680C 11/13 @ .95", "LOTTO SPY 680C 11/13 @ .95",
    "BUY SPY LOTTO 680C 11/13 @ .95", "SPY LOTTO 680C 11/13 @ 0.95",
], "ENTRY", underlying="SPY", premium="0.95", category="LOTTO")
add("put", [
    "SPY 680P 11/13 @ 1.20", "SPY 680PUT 11/13/2026 @ 1.20",
    "QQQ 500P 2026-11-13 @ 1.20",
], "ENTRY", premium="1.20", category="STANDARD")
add("othercategory", [
    "SPY SWING 680C 11/13 @ 1.20", "SPY SCALP 680C 11/13 @ 1.20",
    "SPY DAYTRADE 680C 11/13 @ 1.20",
], "ENTRY", premium="1.20")
add("add", [
    "SPY ADD", "SPY adding more", "SPY averaging down",
    "SPY scale in", "SPY ADDED", "SPY 680C 11/13 ADD",
    "SPY adding more, new avg .95", "SPY add NEW AVERAGE .95",
], "METADATA_ONLY")
add("average", [
    "SPY NEW AVG .95", "SPY new average 0.95",
    "SPY revised avg 0.95", "SPY revised average 0.95",
    "SPY avg now .95", "SPY 680C 11/13 NEW AVERAGE .95",
    "NEW AVG .95", "SPY NEW AVG: 0.95",
], "METADATA_ONLY")
add("close", [
    "SPY SOLD HALF", "SPY SOLD 1/3", "SPY TRIM", "SPY TRIMMED",
    "SPY SCALED OUT", "SPY RUNNERS OUT", "SPY ALL OUT", "SPY SOLD ALL",
    "SPY SOLD 25%", "SPY FULL EXIT", "SPY CLOSED ALL",
    "SPY 680C 11/13 SOLD HALF", "ALL OUT", "RUNNERS OUT",
    "SPY OUT OF IT", "SPY SOLD 1/2",
], "CLOSE_100_PERCENT")
add("ignore", [
    "BUY 20 SHARES SPY", "BUY SPY STOCK ONLY", "long SPY shares",
    "BTC crypto setup", "QQQ looks strong", "good morning everyone",
    "", "  ", "FUTURES ES levels", "SPOT BUY ETHEREUM",
    "SPY looking bullish into tomorrow", "SPY puts seem pricey",
    "No signal yet, watch earnings", "SPY HOLD",
], "IGNORE_NON_OPTION")
add("review", [
    "SPY 680C 11/13 @", "SPY 680C 11/13 @ -1.20",
    "SPY 680C 11/32 @ 1.20", "SPY 680C 11/13 @ 0",
    "SPY 680C 11/13 @ 1.20 and QQQ 500C 11/13 @ 1.10",
    "SPY correction: 680C to 681C", "SPY SOLD HALF and ADD",
    "SPY NEW AVG", "SPY 680C @ 1.20",
    "SPY 680C 11/13 @ 1.20 maybe?", "SPY new average -0.95",
    "SPY ALL OUT NEW AVG .95", "SPY LOTTO 680C 11/13 no price",
    "BUY SPY CALLS ASAP", "SPY 680C 11/13 @ 1.20 QQQ 500P 11/13",
], "REVIEW_REQUIRED")

out = Path(__file__).resolve().parents[1] / "tests/fixtures/swift_phase2_synthetic.json"
out.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Wrote {len(cases)} synthetic, explicitly labeled samples: {out.name}")
