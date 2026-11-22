---
name: vibe-build
description: Turn a non-technical plain-English feature, bug, audit, or review request into a small, verifiable coding task with clear boundaries and human-readable handoff.
---

# Vibe-build workflow

Follow existing repository `AGENTS.md`, `CLAUDE.md`, security and release documents; these always outrank convenience. Refer to `docs/VIBE_CODING_START_HERE.md` and `docs/AI_TOOLBOX_CATALOG.md` only when they materially help. Never load the full catalog merely because this skill exists.

1. Infer the user's intended observable result and identify the smallest safe task; only ask for details when essential.
2. State in simple English what will change, what will not change, and how success will be checked. Avoid overengineering.
3. For code edits: inspect exact branch/status, preserve dirty work, create isolated branch/worktree, do not modify unrelated trading services or live settings.
4. Use existing tools, targeted context, and small tests. Do not launch other coding agents, new Goals, extra memory services, installers or paid CI automatically.
5. If the task is high stakes (broker orders, money, real data, credentials, safety, settlement), request independent read-only review before merge/deployment.
6. Finish with four short lines: **Changed**, **Verified**, **Still blocked**, **Next approval/action**. Use concrete file/PR links, tests and SHA. Never claim complete when unverified.
7. If user asks to install a tool, first inspect official upstream instructions, deployment OS, permission/data reach, existing hooks and conflicts. Do not install entire toolbox as a dependency bundle.
