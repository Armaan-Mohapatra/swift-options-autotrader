"""Strict, auditable Swift-style pattern definitions; no language-model inference."""
from __future__ import annotations

import re

# A complete single-leg contract is mandatory for an entry. Supported expiry
# syntax: MM/DD, MM/DD/YY, MM/DD/YYYY or YYYY-MM-DD.
SYMBOL = r"[A-Z][A-Z0-9.]{0,7}"
STRIKE = r"\d+(?:\.\d{1,4})?"
AMOUNT = r"(?:\d+(?:\.\d{1,4})?|\.\d{1,4})"
RIGHT = r"(?:CALL|PUT|C|P)"
DATE = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
CONTRACT = rf"(?P<strike>{STRIKE})\s*(?P<right>{RIGHT})\s+(?P<expiry>{DATE})"
PREFIX = (
    # Real Swift alerts open with a "OPTION - <CATEGORY>" header line.
    rf"(?:OPTIONS?\s*[:\-]\s*(?=(?:LOTTO|SWING|SCALP|DAYTRADE)\b))?"
    rf"(?:(?:BUY|BTO|OPEN)\s+)?"
    rf"(?:(?P<lead_category>LOTTO|SWING|SCALP|DAYTRADE)(?:\s*[:\-]\s*|\s+))?"
    rf"\$?(?P<symbol>{SYMBOL})(?:\s+(?P<category>LOTTO|SWING|SCALP|DAYTRADE))?"
)
ENTRY = re.compile(
    rf"^\s*{PREFIX}\s+{CONTRACT}\s*@\s*\$?(?P<premium>{AMOUNT})\s*$",
    re.IGNORECASE,
)
CONTRACT_REFERENCE = re.compile(
    rf"(?<![A-Z0-9.])(?P<strike>{STRIKE})\s*(?P<right>{RIGHT})\s+(?P<expiry>{DATE})(?!\d)",
    re.IGNORECASE,
)
EXPLICIT_CONTRACT = re.compile(
    rf"(?<![A-Z0-9.])(?P<symbol>{SYMBOL})\s+(?P<strike>{STRIKE})\s*"
    rf"(?P<right>{RIGHT})\s+(?P<expiry>{DATE})(?!\d)",
    re.IGNORECASE,
)
NO_ADD = re.compile(
    r"\b(?:ADDING\s+MORE|BUY\s+MORE|AVERAGING\s+DOWN|DOUBLE\s+DOWN|"
    r"SCALE\s+IN|SCALED\s+IN|ADDED|ADD)\b",
    re.IGNORECASE,
)
AVG = re.compile(
    r"\b(?:NEW\s+(?:AVG|AVERAGE)|REVISED\s+(?:AVG|AVERAGE)|AVG\s+NOW)\b"
    rf"\s*(?:[:=@]|IS)?\s*\$?(?P<average>{AMOUNT})\b",
    re.IGNORECASE,
)
AVG_MARKER = re.compile(
    r"\b(?:NEW\s+(?:AVG|AVERAGE)|REVISED\s+(?:AVG|AVERAGE)|AVG\s+NOW)\b",
    re.IGNORECASE,
)
EXIT = re.compile(
    r"\b(?:SOLD\s+(?:HALF|\d+\s*/\s*\d+|\d+\s*%|"
    r"\d+\s*CONTRACTS?|SOME|ALL)|TRIM(?:MED)?|SCALED\s+OUT|"
    r"RUNNERS\s+OUT|ALL\s+OUT|CLOSED\s+ALL|FULL\s+EXIT|OUT\s+OF\s+(?:IT|THIS))(?=$|[\s.,!?])",
    re.IGNORECASE,
)
CORRECTION = re.compile(r"\b(?:CORRECT(?:ION|ED)?|FIX(?:ED)?|REPLACE(?:D)?|INSTEAD)\b", re.I)
NON_OPTION = re.compile(
    r"\b(?:SHARES?|STOCK\s+ONLY|EQUITY\s+ONLY|FUTURES?|PERPS?|CRYPTO|"
    r"BITCOIN|ETHEREUM|SPOT\s+BUY)\b",
    re.IGNORECASE,
)
ENTRY_LIKE = re.compile(r"\b(?:BUY|BTO|OPEN|LOTTO)\b|@|\d+[CP]\b", re.I)
