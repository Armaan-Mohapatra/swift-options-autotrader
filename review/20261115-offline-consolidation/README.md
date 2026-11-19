# Swift AutoTrader — consolidated offline engineering review

**Status: REVIEW ONLY / NO LIVE ACTIVATION / NOT AN INSTALLER.** One archive for comparison and independent inspection, not a request to use the laptop now.

- `baseline/`: exact three original source archives, not combined into a new runtime. Their own manifests and SHA-256 pins are verified by `verify_offline.py`.
- `paper-overlay/`: complete replacement paper pilot module and targeted regression tests for the paper archive ONLY. This revision is deliberately blocked from service startup pending separate review. Original package and receiver recovery are not altered to match the overlay; no in-place deployment is supported.
- `operations/`: original operational rehearsal and previously developed safe-mode recovery script as review text (not executed here).
- `evidence/`: actual Linux test transcripts; no real brokerage access or Windows execution. Previous Windows user-pasted results are historical evidence, not evidence of new source.
- `docs/`: defect register, deployment acceptance/rollback criteria, dependency provenance and previous startup audit.
- `BUNDLE_MANIFEST.json`: exact hashes of all outer files, excluding itself.

## Reproduce offline in an isolated trusted environment (reviewer, not a laptop instruction)

Check the externally supplied outer archive SHA-256 and manifest SHA-256 before extraction/execution. Verify the verifier's own checksum against that trusted manifest. Use Python >=3.11 with compatible pytest, jsonschema and runtime test dependencies already installed. `python -I -B verify_offline.py --manifest-sha256 <external digest>` verifies all archive layers and runs the original guarded worker against disposable copies. It runs 188 original review tests, 33 paper tests, 7 rehearsal tests, and the synthetic archive staging/rollback scenario. It never executes PowerShell, starts services, reads local Windows state, connects to a real broker or installs dependencies. Temporary files are limited to test workspaces; the original validator creates a test receipt in its configured temp location. Test guard hooks are not a sandbox against deliberately hostile code. On Windows the original baseline fixture uses the established adaptation; new paper/outer workflow has not been run natively.

The project remains at a **review checkpoint**, not a finished production build. Do not run any nested archive's prior activation/start instructions or interpret simulated evidence as authorization. No next laptop action is requested. See `docs/DEFECT_REGISTER.md` for outstanding work.
