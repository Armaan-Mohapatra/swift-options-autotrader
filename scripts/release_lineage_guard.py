"""Read-only ancestry gate for Swift release candidates.

This gate checks Git history and required source-file presence only.
It never reads credentials, contacts Robinhood or enables real orders.
Passing does NOT imply trading approval or Windows production readiness.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROTECTED_PATHS = (
    "src/swift_autotrader/orders/acceptance_scope.py",
    "src/swift_autotrader/orders/unattended_approval.py",
    "src/swift_autotrader/broker/mcp_local_auth.py",
    "src/swift_autotrader/broker/robinhood_mcp.py",
    "src/swift_autotrader/live_dispatch.py",
    "src/swift_autotrader/live_service.py",
    "src/swift_autotrader/orders/engine.py",
    "src/swift_autotrader/orders/exit_policy.py",
)


class GitGateError(RuntimeError):
    """An inspectable Git read failed; never treat it as a pass."""


def _git(root: Path, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitGateError("Git command unavailable or timed out") from exc
    if result.returncode and not allow_failure:
        # Never print untrusted Git stderr, which can include private filesystem paths.
        raise GitGateError(f"Git read failed at {args[0] if args else 'unknown'}")
    return result


def _rev(root: Path, ref: str) -> str:
    if not ref or not ref.strip():
        raise GitGateError("Missing protected or candidate ref")
    if ref.startswith("-") or any(c in ref for c in "\r\n\0"):
        raise GitGateError("Invalid commit ref")
    sha = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    lines = sha.stdout.strip().splitlines()
    if len(lines) != 1 or len(lines[0]) != 40 or not all(c in "0123456789abcdef" for c in lines[0]):
        raise GitGateError("Commit resolution did not return one full SHA")
    return lines[0]


def _contains(root: Path, sha: str, path: str) -> bool:
    if not path.startswith("src/") or ".." in Path(path).parts:
        raise GitGateError("Protected file path is invalid")
    result = _git(root, "cat-file", "-e", f"{sha}:{path}", allow_failure=True)
    return result.returncode == 0


def audit(root: Path, protected_ref: str, candidate_ref: str) -> dict[str, object]:
    root = root.resolve()
    if _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise GitGateError("Not a Git worktree")
    protected = _rev(root, protected_ref)
    candidate = _rev(root, candidate_ref)
    divergence = _git(root, "rev-list", "--left-right", "--count", f"{protected}...{candidate}")
    parts = divergence.stdout.strip().split()
    if len(parts) != 2:
        raise GitGateError("Could not determine ref divergence")
    try:
        protected_only, candidate_only = (int(value) for value in parts)
    except ValueError as exc:
        raise GitGateError("Invalid Git divergence count") from exc
    missing_candidate = [p for p in PROTECTED_PATHS if not _contains(root, candidate, p)]
    missing_protected = [p for p in PROTECTED_PATHS if not _contains(root, protected, p)]
    reasons = []
    if protected_only:
        reasons.append("PROTECTED_COMMITS_NOT_IN_CANDIDATE")
    if missing_candidate:
        reasons.append("PROTECTED_FILES_MISSING_IN_CANDIDATE")
    if missing_protected:
        reasons.append("PROTECTED_BASELINE_INCOMPLETE")
    return {
        "status": "BLOCKED" if reasons else "PASS_LINEAGE_ONLY",
        "protected_commit": protected,
        "candidate_commit": candidate,
        "protected_only_commits": protected_only,
        "candidate_only_commits": candidate_only,
        "missing_in_candidate": missing_candidate,
        "missing_in_baseline": missing_protected,
        "reasons": reasons,
        "scope": "Git ancestry and presence of safety modules only; NOT authorization for live trading",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--protected-ref", required=True, help="Installed release SHA or ref")
    parser.add_argument("--candidate-ref", required=True, help="Candidate release SHA or ref")
    args = parser.parse_args(argv)
    try:
        result = audit(args.repo, args.protected_ref, args.candidate_ref)
    except GitGateError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc),
                          "scope": "No live-trading authorization"}, sort_keys=True))
        return 3
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
