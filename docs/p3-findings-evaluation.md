# P3 Evaluation & Real Two-Round Case Study

- **Date**: 2026-11-24
- **Branch**: `feat/p3-findings-tracking`
- **Scope**: First-class SQLite `findings` table, JSON-first / Markdown-fallback parser, cross-round `FindingTracker`, snapshot-based `Likely fixed` verification, and user terminal states (`Resolved` / `Dismissed` / `Undo`).

---

## 1. Live Two-Round Codex Case Study (Non-Empty Fix Diff)

Executed directly through `ExecutionService` on a freshly generated [`examples/first-review/make_demo_repo.py`](../examples/first-review/make_demo_repo.py) repository using local **`codex-cli 0.169.0`** (`model=gpt-6.1-terra`, `reasoning=low`, `sandbox=read-only`):

| Round | Revision (`local_snapshot_sha`) | Parser | Verdict | Tracked Findings State |
|---|---|---|---|---|
| **Round 1** (uncommitted SQLi in `notes.py:24`) | `c2091e8e34` | `json` | `concerns` | 1 finding: `[new]` `high` `sql-injection` `notes.py:24` — *"Search terms are interpolated into SQL"* |
| **Round 2** (after `--apply-fix`, uncommitted non-empty LIKE-escaping diff; `HEAD` unchanged) | `0e91cdc39b` | `json` | `clear` | 1 tracked finding: **`[likely_fixed]`** (`system_status=likely_fixed`, `user_status=None`, never auto-`resolved`) |

- **Sole source of truth check**: `<demo-repo>/.amc/reviews/` was **not** created (`exists == False`). All findings and state transitions live exclusively in SQLite.
- **Sanitized contract fixtures archived**:
  - `backend/tests/fixtures/agents/codex/v0.169.0-round1-concerns.jsonl`
  - `backend/tests/fixtures/agents/codex/v0.169.0-round2-clear.jsonl`

---

## 2. 10-Sample Parser & Cross-Round Tracker Evaluation Matrix

All 10 scenarios are continuously verified in `backend/tests/test_p3_findings.py`:

| # | Scenario | Expected Behavior | Result |
|---|---|---|---|
| 1 | Structured Markdown + trailing ```` ```json ```` block | `parse_mode=json`, normalized `category`, raw JSON stripped from summary | PASS |
| 2 | Malformed trailing JSON block after valid Markdown findings | Clean fallback to `parse_mode=markdown`; no broken JSON in summary | PASS |
| 3 | Earlier prose `{"verdict":"clear"}` example + Markdown finding + broken trailing JSON | Rejects missing-`findings` example; falls back to Markdown and keeps real finding | PASS |
| 4 | Unicode digit `line: "²"`, 64-bit overflow line, raw JSON without `summary` | No `ValueError` / SQLite `OverflowError`; raw JSON never leaks into `summary` | PASS |
| 5 | Uncommitted fix between Round 1 snapshot and Round 2 snapshot (`HEAD` unchanged) | Fixed line -> `likely_fixed` (never auto-`resolved`); untouched line -> `unverified` | PASS |
| 6 | Round 3 unrelated edit + Round 4 revert back to buggy code | Stays `likely_fixed` on unrelated edit; drops back to `unverified` on revert | PASS |
| 7 | Cross-agent & cross-language (Codex EN -> Claude ZH, line 11 -> 14) | Merged into same finding (`persistent`, `location_changed=True`, `previous_line=11`) | PASS |
| 8 | File rename + partial fix (`notes.py` -> `storage.py`, fix line 20, keep line 5) | Fixed line -> `likely_fixed`; remaining leak -> `persistent` + `location_changed` | PASS |
| 9 | Reordered duplicate titles at line 10 & line 90 with line 10 `dismissed` | Preserves `dismissed` on line 10 and `persistent` on line 90 regardless of order | PASS |
| 10 | User terminal states & empty/aborted rounds (`Resolved` re-reported, `Dismissed` re-reported, `Undo`, empty exit-0 round) | `Resolved` auto-reopens (`reopened=True`); `Dismissed` stays `dismissed` (`mentioned_after_dismiss=True`); `Undo` restores `system_status`; empty/aborted round never downgrades findings | PASS |
