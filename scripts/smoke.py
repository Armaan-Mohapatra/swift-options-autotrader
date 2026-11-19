"""Cross-platform minimal entry point smoke test."""

from swift_autotrader.app import main

if __name__ == "__main__":
    raise SystemExit(main(["smoke"]))
