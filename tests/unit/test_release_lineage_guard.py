"""Synthetic, offline regression tests for the read-only release lineage gate."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_lineage_guard.py"
spec = importlib.util.spec_from_file_location("release_lineage_guard", SCRIPT)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class LineageGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.cmd("init", "-q", "-b", "main")
        self.cmd("config", "user.email", "test@example.invalid")
        self.cmd("config", "user.name", "Synthetic Test")
        for path in guard.PROTECTED_PATHS:
            p = self.repo / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# safe synthetic fixture\n", encoding="utf-8")
        self.cmd("add", ".")
        self.cmd("commit", "-qm", "baseline")
        self.baseline = self.cmd("rev-parse", "HEAD").strip()

    def cmd(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       text=True, stderr=subprocess.DEVNULL)

    def commit(self, filename, message):
        p = self.repo / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(message, encoding="utf-8")
        self.cmd("add", ".")
        self.cmd("commit", "-qm", message)
        return self.cmd("rev-parse", "HEAD").strip()

    def test_descendant_preserves_all_protected_files(self):
        candidate = self.commit("feature.txt", "some safe change")
        result = guard.audit(self.repo, self.baseline, candidate)
        self.assertEqual("PASS_LINEAGE_ONLY", result["status"])
        self.assertEqual(0, result["protected_only_commits"])
        self.assertEqual(1, result["candidate_only_commits"])

    def test_diverged_branch_fails_closed(self):
        self.cmd("checkout", "-qb", "candidate", self.baseline)
        candidate = self.commit("candidate.txt", "candidate")
        self.cmd("checkout", "-q", "main")
        protected = self.commit("protected.txt", "protected")
        result = guard.audit(self.repo, protected, candidate)
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual((1, 1), (result["protected_only_commits"],
                                 result["candidate_only_commits"]))
        self.assertIn("PROTECTED_COMMITS_NOT_IN_CANDIDATE", result["reasons"])

    def test_missing_protected_module_fails(self):
        self.cmd("rm", "src/swift_autotrader/orders/acceptance_scope.py")
        self.cmd("commit", "-qm", "unsafe removal")
        candidate = self.cmd("rev-parse", "HEAD").strip()
        result = guard.audit(self.repo, self.baseline, candidate)
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("src/swift_autotrader/orders/acceptance_scope.py",
                      result["missing_in_candidate"])

    def test_historical_baseline_without_new_receipt_can_pass(self):
        receipt = "src/swift_autotrader/orders/unattended_approval.py"
        self.cmd("rm", receipt)
        self.cmd("commit", "-qm", "historic installed release predating receipt")
        historical = self.cmd("rev-parse", "HEAD").strip()
        p = self.repo / receipt
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# restored in integrated candidate\n", encoding="utf-8")
        self.cmd("add", ".")
        self.cmd("commit", "-qm", "add modern owner approval")
        result = guard.audit(self.repo, historical, "HEAD")
        self.assertEqual("PASS_LINEAGE_ONLY", result["status"])
        self.assertEqual([], result["missing_in_baseline"])

    def test_ref_injection_and_unknown_ref_deny(self):
        for invalid in ("--exec=rm", "missing-branch", "\nnot_a_ref"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(guard.GitGateError):
                    guard.audit(self.repo, self.baseline, invalid)

    def test_cli_cannot_report_live_authorization(self):
        self.assertEqual(0, guard.main(["--repo", str(self.repo),
                                        "--protected-ref", self.baseline,
                                        "--candidate-ref", self.baseline]))


if __name__ == "__main__":
    unittest.main()
