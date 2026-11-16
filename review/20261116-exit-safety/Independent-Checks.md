# Independent exit safety checks

The 17 hermetic tests in tests/unit/test_exit_safety_interlock.py passed on Python 3.13.5, Linux, 0.10s. Exact local Git blob hashes matched candidate GitHub objects:
- exposure_state.py: 0da87aee2a45f012dd827acac00649a1535293c1
- test_exit_safety_interlock.py: 680aa22e8a77beb23730ca41b44acc11b3bb9e41

This tests SQLite interlock mathematics and persistence only. Subsequent atomic refusal recording and a full synthetic refusal/reconstruction test are authored here, not executed in that isolated check. Run full regression in locked Python 3.11 on the Windows host before any operational decision. Native 808/2 evidence belongs to the parent 86828420, not this candidate. No live broker calls, approval changes, deployment, or trading authorization.

The interlock does not automatically resubmit a rejected or uncertain sell. Provider refusals can require manual action. This review is deliberately protective and does not certify unattended position closure. Resolve exit attribution through supported reconciliation; do not hand-edit journal/fill state. 
