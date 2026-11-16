"""Script to pin Robinhood MCP account binding locally using Windows DPAPI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add src to sys.path if not installed
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swift_autotrader.broker.mcp_local_auth import pin_agentic_account


def main() -> int:
    parser = argparse.ArgumentParser(description="Pin operator-confirmed Robinhood MCP account.")
    parser.add_argument("--root", required=True, help="Managed install root directory")
    parser.add_argument("--account-number", required=True, help="Confirmed Robinhood account number")
    args = parser.parse_args()

    try:
        path = pin_agentic_account(Path(args.root), args.account_number)
        print(json.dumps({
            "status": "ACCOUNT_PINNED",
            "path": str(path),
            "account_length": len(args.account_number.strip()),
        }))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
