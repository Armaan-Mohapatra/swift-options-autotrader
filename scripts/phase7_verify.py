"""Offline, non-mutating paper-default and explicit-live boundary audit.

A passing result is *not* a substitute for CI, Windows PowerShell execution,
external supply-chain attestations or local OAuth/market field validation.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {".env", "credentials.json", "token.json", "id_rsa"}
FORBIDDEN_SUFFIXES = {".session", ".session-journal", ".sqlite", ".sqlite3",
                      ".db", ".pem", ".key", ".p12"}
REQUIRED = (
    "AGENTS.md", "README.md", "docs/PROJECT_SPEC.md",
    "docs/SOURCE_SPRINT_PLAN.md", "docs/WINDOWS_RUNBOOK.md",
    "docs/OPERATIONS.md", "docs/ROLLBACK.md",
    "src/swift_autotrader/orders/engine.py",
    "src/swift_autotrader/broker/paper.py",
    "src/swift_autotrader/broker/robinhood_mcp.py",
    "deployment/windows/setup.ps1", "deployment/windows/manage.ps1",
    "deployment/windows/secrets.ps1", "deployment/windows/common.ps1",
    ".github/workflows/ci.yml", ".github/workflows/windows-deployment.yml",
    "uv.lock", "requirements/robinhood-mcp-local.txt",
)


def _paths() -> list[Path]:
    """Inspect tracked paths only; avoid leaking local runtime files in findings."""
    git = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, timeout=10, check=False,
    )
    if git.returncode == 0:
        return [Path(x.decode("utf-8")) for x in git.stdout.split(b"\x00") if x]
    # Local reconstructed checkout: enumerate source patterns, never read secrets.
    return [p.relative_to(ROOT) for p in ROOT.rglob("*") if p.is_file()
            and not any(part in {".git", ".venv", ".pytest_cache", "__pycache__"}
                        for part in p.relative_to(ROOT).parts)]


def _function_body(source: str, class_name: str, function_name: str) -> list[ast.stmt]:
    module = ast.parse(source)
    cls = next((n for n in module.body if isinstance(n, ast.ClassDef)
                and n.name == class_name), None)
    if cls is None:
        raise ValueError("Required live-adapter class is missing")
    function = next((n for n in cls.body if isinstance(n, (ast.FunctionDef,
                     ast.AsyncFunctionDef)) and n.name == function_name), None)
    if function is None:
        raise ValueError("Required live-adapter method is missing")
    return function.body


def _unconditional_guard(statements: list[ast.stmt], guard: str) -> bool:
    """Require a top-level raise before any possibility of a real network call."""
    return bool(statements and isinstance(statements[0], ast.Raise)
                and isinstance(statements[0].exc, ast.Call)
                and isinstance(statements[0].exc.func, ast.Name)
                and statements[0].exc.func.id == guard)



def verify() -> list[str]:
    issues: list[str] = []
    for name in REQUIRED:
        if not (ROOT / name).is_file():
            issues.append("Required audited source missing: " + name)
    for path in _paths():
        if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append("Credential/session/database file in source: " + path.as_posix())
        if path.is_absolute() or ".." in path.parts:
            issues.append("Unsafe source path: " + path.as_posix())
    live = ROOT / "src/swift_autotrader/broker/robinhood_mcp.py"
    if live.is_file():
        text = live.read_text(encoding="utf-8")
        # The read adapter can NEVER write: no unlock flag, no second writer class.
        if "allow_live_writes" in text or "class RobinhoodLiveBroker" in text:
            issues.append("Read adapter exposes a write unlock or a second live writer")
        try:
            for method in ("submit_order", "cancel_order", "get_order"):
                statements = _function_body(text, "RobinhoodMCPBroker", method)
                if not _unconditional_guard(statements, "BrokerWritesDisabledError"):
                    issues.append("Read adapter must unconditionally deny " + method)
        except (SyntaxError, ValueError) as exc:
            issues.append("Unable to audit read adapter guard: " + type(exc).__name__)
        if not re.search(
            r"if name in WRITE_TOOLS\s*:\s*\n\s*raise BrokerWritesDisabledError", text,
        ):
            issues.append("MCP write-tool denial on the read adapter missing")
        if not re.search(
            r"if name not in READ_TOOLS and name not in WRITE_TOOLS\s*:\s*\n\s*raise MCPContractError",
            text,
        ):
            issues.append("MCP unknown-tool deny-by-default gate missing")
    writer = ROOT / "src/swift_autotrader/broker/live_broker.py"
    if writer.is_file():
        text = writer.read_text(encoding="utf-8")
        # The one canonical writer: writes default OFF, entries default KILLED, rehearsal
        # never writes, and only the two supported write tools exist.
        for needle, problem in (
            ("writes_enabled: bool = False", "LiveBroker writes are not off by default"),
            ("self.kill_switch = kill_switch or (lambda: True)",
             "LiveBroker entry kill switch does not default to engaged"),
            ("self.writes_enabled = writes_enabled and not rehearsal",
             "LiveBroker rehearsal can enable writes"),
            ("if name not in {'place_option_order', 'cancel_option_order'}:",
             "LiveBroker write-tool allowlist missing"),
        ):
            if needle not in text:
                issues.append(problem)
        if not re.search(
            r"if self\.rehearsal or not self\.writes_enabled\s*:\s*\n\s*raise LiveSafetyError",
            text,
        ):
            issues.append("LiveBroker write boundary does not refuse disabled writes first")
    else:
        issues.append("Required audited source missing: src/swift_autotrader/broker/live_broker.py")
    engine = ROOT / "src/swift_autotrader/orders/engine.py"
    if engine.is_file():
        source = engine.read_text(encoding="utf-8")
        # Live mode accepts only an enabled LiveBroker; paper mode only the PaperBroker.
        if ("type(broker) is LiveBroker and broker.writes_enabled" not in source
                or 'raise TypeError("live engine accepts only an enabled LiveBroker")' not in source
                or "elif type(broker) is PaperBroker:" not in source
                or 'raise TypeError("paper engine accepts ONLY the concrete PaperBroker' not in source):
            issues.append("Engine does not restrict broker to paper or enabled live")
    settings = ROOT / "src/swift_autotrader/config.py"
    if settings.is_file():
        try:
            from swift_autotrader.config import ConfigurationError, Settings

            paper = Settings.from_env({})
            if paper.live_mode or paper.mode != "PAPER_ONLY" or paper.allow_broker_writes:
                issues.append("Default configuration is not paper-only")
            synthetic = {
                "PAPER_ONLY": "false", "LIVE_ENABLED": "true",
                "ALLOW_BROKER_WRITES": "true",
                "BROKER_BACKEND": "robinhood_mcp",
                "ROBINHOOD_MCP_ACCOUNT": "SYNTHETIC-ACCOUNT-NOT-REAL",
                "LIVE_JOURNAL_PATH": str((ROOT / "nonexistent-test-journal.sqlite").resolve()),
                "MAX_PREMIUM_PER_SIGNAL_USD": "999",
                "TELEGRAM_ENABLED": "true",
            }
            # Every individually absent authorization component must be REFUSED (never
            # silently live). A partial request is a startup error.
            for field in synthetic:
                partial = dict(synthetic)
                del partial[field]
                try:
                    if Settings.from_env(partial).live_mode:
                        issues.append("Incomplete live configuration enabled writes: " + field)
                except ConfigurationError:
                    pass
            for key, value in (("SLIPPAGE_TOLERANCE_PCT", "10"),
                               ("MAX_PREMIUM_PER_SIGNAL_USD", "1000"),
                               ("MAX_DAILY_LOSS_USD", "751"),
                               ("MAX_CONTRACTS_PER_SIGNAL", "2")):
                try:
                    Settings.from_env({key: value})
                    issues.append("Hard maximum not enforced at startup: " + key)
                except ConfigurationError:
                    pass
        except (ImportError, TypeError, ValueError) as exc:
            issues.append("Cannot verify paper/live settings boundary: " + type(exc).__name__)
    dashboard = ROOT / "src/swift_autotrader/dashboard/app.py"
    if dashboard.is_file() and "127.0.0.1" not in dashboard.read_text(encoding="utf-8"):
        issues.append("Local dashboard loopback restriction missing")
    migrations = sorted((ROOT / "src/swift_autotrader/database/migrations").glob("*.sql"))
    versions = [int(p.name[:4]) for p in migrations if re.match(r"^\d{4}_", p.name)]
    if versions != [1, 2, 3, 4, 5]:
        issues.append("Database migration sequence differs from reviewed 0001–0005")
    return issues


if __name__ == "__main__":
    violations = verify()
    for item in violations:
        print(item, file=sys.stderr)
    if violations:
        raise SystemExit(1)
    print("Offline phase 7 paper/live safety boundary checks passed (not release certification)")
