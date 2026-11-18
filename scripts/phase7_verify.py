"""Offline, non-mutating final PAPER release boundary audit.

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



def _guarded_by_write_flag(statements: list[ast.stmt], attribute: str) -> bool:
    """First executable statement must refuse writes unless explicitly enabled."""
    body = list(statements)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body or not isinstance(body[0], ast.If):
        return False
    test = body[0].test
    if not isinstance(test, ast.UnaryOp) or not isinstance(test.op, ast.Not):
        return False
    if ast.unparse(test.operand) != attribute:
        return False
    first = body[0].body[0] if body[0].body else None
    return (isinstance(first, ast.Raise) and isinstance(first.exc, ast.Call)
            and isinstance(first.exc.func, ast.Name)
            and first.exc.func.id == "BrokerWritesDisabledError")


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
        try:
            for cls, guard_attr in (
                ("RobinhoodMCPBroker", "self.allow_live_writes"),
                ("RobinhoodLiveBroker", "self.mcp.allow_live_writes"),
            ):
                for method in ("submit_order", "cancel_order"):
                    statements = _function_body(text, cls, method)
                    if not _guarded_by_write_flag(statements, guard_attr):
                        issues.append("Missing pre-write authorization guard: " + cls + "." + method)
            statements = _function_body(text, "RobinhoodMCPBroker", "get_order")
            if not _unconditional_guard(statements, "BrokerWritesDisabledError"):
                issues.append("Read-only adapter get_order must deny direct broker access")
        except (SyntaxError, ValueError) as exc:
            issues.append("Unable to audit real broker guard: " + type(exc).__name__)
        # Explicit write opt-in and an unknown-tool denylist both precede network I/O.
        if not re.search(
            r"if name in WRITE_TOOLS and not self\.allow_live_writes\s*:\s*\n\s*raise BrokerWritesDisabledError",
            text,
        ):
            issues.append("MCP write-tool authorization gate missing")
        if not re.search(
            r"if name not in READ_TOOLS and name not in WRITE_TOOLS\s*:\s*\n\s*raise MCPContractError",
            text,
        ):
            issues.append("MCP unknown-tool deny-by-default gate missing")
    engine = ROOT / "src/swift_autotrader/orders/engine.py"
    if engine.is_file():
        source = engine.read_text(encoding="utf-8")
        if "type(broker) is not PaperBroker" not in source:
            issues.append("Paper engine missing concrete PaperBroker-only guard")
    settings = ROOT / "src/swift_autotrader/config.py"
    if settings.is_file():
        source = settings.read_text(encoding="utf-8")
        if "not self.paper_only or self.live_enabled or self.allow_broker_writes" not in source:
            issues.append("Fail-closed live trading configuration guard missing")
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
    print("Offline phase 7 PAPER safety boundary checks passed (not release certification)")
