# Vibe coding — start here (no programming knowledge needed)

## Your new workflow in plain English

1. Open a task in GitHub or use the `Vibe coding request` issue template.
2. Write what you want to **see happen**, what would count as success, and what must **never** change. The agent should translate this into a small plan rather than asking you to know filenames.
3. Choose **one builder**: usually Claude for a Swift task, Codex for a Phoenix task (switch if bench tests say otherwise).
4. The builder makes a separate development branch and performs targeted tests. It gives you a link to a draft pull request.
5. For trading, money, privacy, data loss, broker actions, or live system behavior, use the **other** agent only as a read-only reviewer. For simple documentation fixes, skip review.
6. Read the short result in everyday language: what changed, what works, what was tested, what is still broken, and where to click to undo.
7. Approve a merge/deployment only when the report proves its stated acceptance criteria and safety checks.

## Say this for a new feature

> Build [what I want]. A normal user should be able to [observable behavior]. Keep the current [critical behavior] unchanged. Write a short plan, modify only the relevant code, test it, make a draft PR, explain what I should click and verify. Don't merge or deploy.

## Say this for a bug

> Fix [what is wrong]. Reproduce it first with a focused failing test, then fix the smallest root cause. Show me what caused it, the proof that it is fixed, and a draft PR. Do not alter unrelated features.

## Say this for a review

> Independently review PR [link/number] without editing anything. Look for broken promises, regression risk, safety issues and missing tests. Rank only evidenced findings by severity and explain them in plain English.

## Say this for an audit

> Tell me what is working, what is broken, what is unverified, and the next three safest actions. Read only; do not start, stop, or change services.

## Your visual tools

- [Vibe Kanban](https://github.com/BloopAI/vibe-kanban): a task board for Codex/Claude, but turn off automatic background task launches until usage is controlled.
- [GitHub Desktop](https://github.com/desktop/desktop): view commits and branches, undo/compare without memorizing Git commands.
- [Cline](https://github.com/cline/cline): a review-before-action coding interface; requires its own model account/cost.
- [Aider](https://github.com/Aider-AI/aider): Git-aware command-line editing with optional voice input.

## Safety rules that matter more than coding expertise

- "The AI said it passed" is not proof; ask for the **exact tests run**, exit codes, and commit SHA.
- "Draft PR" means proposed code, **not** installed software or active trading.
- A tool with ten plugins can be worse than one reliable one when several run automatically and use your quota.
- If work touches real money or production data: preview, backup, independent read-only review, and owner approval first.
- Never paste broker tokens, email passwords, paid Telegram text, raw trade history or private data into public GitHub issues.
- Do not run unknown one-liner scripts that download and execute remote code blindly.

## Beginner glossary

- **Repository**: the project folder on GitHub.
- **Branch**: a separate version where the AI can safely experiment.
- **Pull request (PR)**: a proposed change you can inspect before merging.
- **Merge**: combining the proposed change into another branch.
- **Test**: a repeatable check that specific behavior works; good tests do not guarantee profitability or live safety.
- **Rollback**: restoring the last known-good version.
