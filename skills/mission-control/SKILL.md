---
name: mission-control
description: "Run cross-agent local code review before git push using Mission Control (amc). Drives a second coding agent CLI already installed on this machine (Codex, Claude Code, OpenCode, or Copilot CLI) against an isolated snapshot of uncommitted local changes or a GitHub PR, returns structured file:line findings in JSON, and tracks findings across rounds (New, Persistent, Unverified, Likely fixed) without modifying the working tree or git index. Use when the user asks to cross-review local changes before pushing, get a second-opinion AI code review, verify whether previous review findings were actually fixed, or review a GitHub pull request locally."
license: MIT
metadata:
  version: "0.1.1"
  author: robll-v1
---

# Mission Control — Cross-Agent Local Code Review

Use Mission Control (`amc`) to review uncommitted local changes or a GitHub PR with a **second** coding agent CLI installed on the user's machine, then fix verified issues and re-review until clean.

## Why use a second agent

Never let the coding agent that just wrote a patch be the only reviewer when another CLI is available locally:

- **Running in Claude Code** -> prefer `codex` (with `--reasoning high` when reviewing risky security/concurrency changes), then `opencode`.
- **Running in Codex CLI** -> prefer `claude-code`, then `opencode`.
- **Running in OpenCode or Cursor** -> prefer `codex` or `claude-code`.

Mission Control snapshots the exact working tree (staged, unstaged, and untracked files) via a temporary `GIT_INDEX_FILE` and runs the reviewer in an isolated worktree — your working files, git index, and `HEAD` are never touched.

## Fast review workflow

1. **Run a cross-agent review round** using the bundled helper script (replace `<skill-dir>` with this skill's installed directory):

   ```bash
   python3 <skill-dir>/scripts/run_review.py --repo . --exclude <your-own-agent>
   ```

   Optional flags:
   - `--backend codex|claude-code|opencode|copilot` — pin a specific reviewer CLI
   - `--model <model-id>` — pin a specific model
   - `--reasoning <level>` — e.g. `low`, `medium`, `high` (omit to use `Model default`)
   - `--focus "<area>"` — e.g. `"security, concurrency, edge cases"`
   - `--pr <github-pr-url>` — review a GitHub pull request instead of local changes

   Need to reduce false positives on high-stakes diffs? Run `amc review --verify-with auto` (or MCP `verify_findings`) to run a 1-pass Cold Falsification check against the same frozen snapshot with a second local CLI (`confirmed` / `disputed` / `uncertain`, never auto-dismissing findings).

   The helper locates `amc` on `PATH` or falls back to `uvx --from agent-mission-control amc`, picks an installed reviewer CLI different from `--exclude`, and emits structured JSON to stdout.

2. **Inspect the structured JSON receipt** (`verdict`, `passed`, `finding_count`, `findings[]`):
   - `verdict == "clear"` and `passed == true` -> report clean review and stop.
   - `verdict == "concerns"` -> triage each finding by `severity` (`critical` > `high` > `medium` > `low`) and inspect the cited `path:line` directly in the repository before editing.
   - `verdict == "failed"` or `"inconclusive"` -> report the error transparently (do **not** claim the diff is clean). If a reasoning level was rejected by the CLI, retry once without `--reasoning` (`Model default`).

3. **Fix confirmed issues, then run Round 2 (max 2 fix rounds)**:
   - Apply minimal, targeted fixes for confirmed `critical` / `high` (and valid `medium`) findings and run the repository's own tests.
   - Keep the updated fixes in the working tree (no need to commit first) and rerun step 1 for Round 2.
   - Check cross-round status carefully:
     - `persistent` -> still present; address before finishing.
     - `likely_fixed` -> omitted in Round 2 **and** code at that location changed between snapshots; verify with tests and let the user confirm.
     - `unverified` -> omitted in Round 2 while code did not change; **never** report this as confirmed fixed.

## Optional: inspect in the local Web Workbench

If the user wants to browse multi-round diffs, inline snippets, or mark findings `Resolved` / `Dismissed` visually:

```bash
amc start
```

(Or `uvx --from agent-mission-control amc start` — no Node.js required.)

## Rules and honest reporting

- Never fabricate a clean review when `amc` fails or no reviewer CLI is installed; report the exact error and install hint.
- Never claim a finding is confirmed `resolved` solely because Round 2 stopped mentioning it.
- Do not modify `.git` state, stash user changes, or push branches unless the user explicitly asks.
