# AI toolbox catalog — options, not automatic installs

This list is a **menu of independently opt-in GitHub projects**, not permission to install/start all of them. The agent must check current upstream documentation, licensing, maintenance, compatibility, privacy, costs, permissions, and rollback before enabling each one. Cross-agent means either agent can use a separately installed CLI; it does not mean every package can be added to both Python and Rust dependencies.

Current known base: Windows ARMAAN; Codex + Claude Code, RTK 1.1.0 hooks and ccusage installed; both projects have draft optimization PRs. **Do not reinstall or duplicate existing hooks.**

## FOUNDATION / USAGE

| Tool | Scope | State | Why / boundary |
|---|---|---|---|
| [RTK](https://github.com/rtk-ai/rtk) | Both | Already installed | Compress terminal output; preserve raw failure logs |
| [ccusage](https://github.com/ccusage/ccusage) | Both | Already running | Read local session usage; not subscription quota |
| [cdxusage](https://github.com/Krablante/cdxusage) | Codex | Optional | Alternative local Codex reports; avoid duplicate polling |
| [AgentsView](https://github.com/kenn-io/agentsview) | Both | Optional | Visual historical agent/session analysis |
| [Caveman](https://github.com/JuliusBrussee/caveman) | Both | Sandbox | Avoid cache keepalive, unreviewed proxies and auto-routing |
| [Headroom](https://github.com/headroomlabs-ai/headroom) | Both | Sandbox | Additional context proxy; may conflict with RTK |
| [Codex Run Budget](https://github.com/easyvibecoding/codex-run-budget) | Codex | Pilot | Hook-based task budget; not a guaranteed hard cutoff |

## PLAN / COORDINATE

| Tool | Scope | State | Why / boundary |
|---|---|---|---|
| [Vibe Kanban](https://github.com/BloopAI/vibe-kanban) | Both | Pilot | Visual tasks, agent selection, Git worktrees; disable automatic launching |
| [Beads](https://github.com/gastownhall/beads) | Both | Pilot | Durable task dependencies; init may edit AGENTS.md |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | Both | Pilot | Human-readable requirement -> design -> implementation checklists |
| [Superpowers](https://github.com/obra/superpowers) | Both | Sandbox | AI skills/planning; potential subagent/token expansion |
| [ECC](https://github.com/affaan-m/ECC) | Both | Existing on Claude | Review existing installation; do not duplicate |
| [Serena](https://github.com/oraios/serena) | Both | Pilot | Symbol-level source navigation; requires language-server setup |
| [Graphify](https://github.com/blockflix/Graphify) | Both | Sandbox | Code graph; indexing overhead; verify project activity |
| [Repomix](https://github.com/yamadashy/repomix) | Both | Pilot | Selective code package for cross-agent handoffs; do not pack entire repo |
| [Claude-Mem](https://github.com/thedotmack/claude-mem) | Both | Sandbox | Persistent memory worker using inference; privacy/usage review first |

## BEGINNER-FRIENDLY INTERFACES

| Tool | Scope | State | Why / boundary |
|---|---|---|---|
| [GitHub Desktop](https://github.com/desktop/desktop) | Host | Recommended | Visual Git branches and history |
| [Cline](https://github.com/cline/cline) | Host | Optional | Approval-based coding UI with checkpoints; separate model cost |
| [Roo Code](https://github.com/RooCodeInc/Roo-Code) | Host | Optional | Visual coding modes; extra agent/runtime cost |
| [Aider](https://github.com/Aider-AI/aider) | Host | Optional | Terminal/voice coding with Git undo |
| [OpenHands](https://github.com/OpenHands/OpenHands) | Host | Sandbox | Self-hosted agent UI; Docker/server/resources/security |
| [bolt.diy](https://github.com/stackblitz-labs/bolt.diy) | Web apps | Sandbox | Build web apps with model choice; not a Rust backend fix |
| [Plandex](https://github.com/plandex-ai/plandex) | Host | Sandbox | Plan/diff sandbox; another coding agent |
| [Continue](https://github.com/continuedev/continue) | IDE | Review maintenance | Legacy repo reports read-only; assess current project before adoption |

## GENERAL DEVELOPMENT

| Tool | Scope | State | Why / boundary |
|---|---|---|---|
| [ast-grep](https://github.com/ast-grep/ast-grep) | Both | Recommended | Structural code search, Python/Rust syntax |
| [Gitleaks](https://github.com/gitleaks/gitleaks) | Both | Recommended | Local secrets scan; never publish findings containing secrets |
| [Semgrep](https://github.com/semgrep/semgrep) | Both | Recommended | Targeted static analysis; verify rules and avoid cloud uploads |
| [just](https://github.com/casey/just) | Both | Recommended | Simple one-command recipes for build/test/audit |
| [mise](https://github.com/jdx/mise) | Host | Optional | Dev tool versions and tool installation; carefully manage environment |
| [Lazygit](https://github.com/jesseduffield/lazygit) | Host | Optional | Easy terminal Git UI |
| [delta](https://github.com/dandavison/delta) | Host | Optional | Readable Git diffs |
| [Playwright](https://github.com/microsoft/playwright) | UI-only | Optional | Automatic browser/UI acceptance tests |
| [DuckDB](https://github.com/duckdb/duckdb) | Phoenix research | Optional | Local SQL over Parquet/history; preserve original data |

## SWIFT PYTHON (do not add to Rust runtime)

| Tool | Scope | State | Why / boundary |
|---|---|---|---|
| [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | Swift | Pilot dev dependency | Property tests for alert/partial-fill/restart invariants |
| [pytest-timeout](https://github.com/pytest-dev/pytest-timeout) | Swift | Pilot dev dependency | Detect hung unit tests |
| [uv](https://github.com/astral-sh/uv) | Swift | Already present | Fast reproducible Python dependency management |
| [Ruff](https://github.com/astral-sh/ruff) | Swift | Already present | Fast lint/style checks |

## PHOENIX RUST (do not add to Python runtime)

| Tool | Scope | State | Why / boundary |
|---|---|---|---|
| [cargo-nextest](https://github.com/nextest-rs/nextest) | Phoenix | Pilot | Fast test runner; does not cover every test type |
| [cargo-deny](https://github.com/EmbarkStudios/cargo-deny) | Phoenix | Pilot | Dependency/license/advisory check |
| [cargo-llvm-cov](https://github.com/taiki-e/cargo-llvm-cov) | Phoenix | Pilot | Test coverage analysis |
| [proptest](https://github.com/proptest-rs/proptest) | Phoenix | Pilot dev dependency | Property-based Rust fuzz-like tests |

## Decisions before enabling anything

1. **Common installation**: GitHub Desktop, just, Gitleaks, ast-grep, Beads, OpenSpec, optional Vibe Kanban. Install to a developer account, **not** a trading service environment.
2. **Agent extras**: Serena and Repomix only for named tasks; Superpowers/ECC/Caveman/Headroom/Claude-Mem should be evaluated one at a time; do not stack hooks, memory daemons, or remote proxies.
3. **Project tests**: Swift can trial Hypothesis/pytest-timeout as *dev-only*; Phoenix can trial nextest, cargo-deny, llvm-cov and proptest in *isolated development*. No broker/writer/collector behavior changes.
4. **No unattended AI**: no background multiagent work, auto-resume Goals, auto-run issue executors, paid CI, cache-refresh traffic, or public binding of local control dashboards.
5. **Verify before claiming ready**: test each new feature in a disposable clone/worktree, document exact Windows compatibility, observed inference cost and rollback; only then opt in.
6. **Stop before a large or destructive install**: if a new package writes `AGENTS.md`, `CLAUDE.md`, hooks, startup entries, project dependencies, protected Git refs or credentials, inspect diff and get separate authorization.

See [Vibe Coding Start Here](VIBE_CODING_START_HERE.md) and [read-only toolbox inventory](../scripts/ai-toolbox.ps1).
