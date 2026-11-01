# Preflight cancellation — QA record

Task: make a review round cancellable while its preflight probe is still
running. Baseline `5476de2` (branch `codex/preflight-cancel-glm`). All
evidence below was collected on macOS (arm64) with controlled fake probes
(`python -c` subprocesses); no real agent or network was involved.

## 1. The defect, reproduced on the baseline

`start_task` reserves the active slot, then `_preflight_check` runs the
backend probe synchronously on the caller's thread via `subprocess.run`
(up to 150s). During that window no `Run` exists and
`ActiveProcess.process is None`, so `abort_task` returned `False` (API 409)
and the probe could not be touched.

Reproduction (probe = `python -c "time.sleep(8)"`, script driven by latches):

| Observation | Value |
|---|---|
| `abort_task()` | **False** (refused instantly) |
| start thread blocked | **8.36s** (the full probe) |
| after the probe passed | the round ran anyway: 1 Run completed, task → `waiting_human` |
| net effect | the user's cancel was ignored entirely |

## 2. The fix (backend/app/core/execution.py only)

- `ActiveProcess.cancel_requested`: published by `abort_task` before anything
  else; checked by the preparing thread around the probe and before the Run
  is created.
- The probe now runs via `Popen` (same process-group treatment as the review
  agent: `start_new_session` on POSIX, `CREATE_NEW_PROCESS_GROUP` on Windows)
  and is **registered as `active.process`**, so `abort_task` can terminate
  exactly that subprocess — nothing else.
- `abort_task` gained a pre-run branch: when the slot has no Run yet, it
  publishes ABORTED under the db lock (same ordering as the live path),
  appends `control.accepted` (phase `preflight` or `prepare`), kills the
  registered probe if any, and returns `True`. A repeat abort of the same
  pre-run round returns `False`; a *fresh* round started on an
  already-ABORTED task is cancellable (per-round flag, not task-status
  based).
- The probe wait polls `communicate(timeout=0.25)` in a loop watching
  cancellation and the deadline; on either it terminates and reaps the probe
  before raising (`PreflightCancelledError` / `PreflightError`;
  renamed `RoundCancelledError` in section 7).
- `start_task`'s unwind now pops the slot only if the entry is still its own
  placeholder, and `_launch_round` refuses to create the Run/worker once
  `cancel_requested` is set — a cancelled round never writes `FAILED` (the
  ABORTED stage guard already blocks repaints) and never starts a review.
- `_preflight_check` keeps its call shape `(task, *, adapter=None,
  probe_timeout_sec=150)`; the slot is looked up internally, so monkeypatched
  wrappers in existing tests are unaffected.

## 3. Evidence after the fix (same reproduction script)

| Path | abort | latency | start thread | task | Runs | slot | probe |
|---|---|---|---|---|---|---|---|
| probe registered, mid-wait | `True` | **0.004s** | unwound in **0.02s** | `aborted/aborted` | 0 | released | killed, reaped, no leftovers |
| cancel before probe spawn | `True` | **0.022s** | unwound immediately | `aborted/aborted` | 0 | released | never spawned |
| repeat abort, same round | `False` | — | — | stays `aborted` | 0 | — | — |

Poll interval 0.25s bounds the abort latency and the timeout overshoot.
`pgrep` after each run: no surviving probe processes.

## 4. Regression coverage

`backend/tests/test_preflight_cancellation.py` (6 tests):

1. abort during preflight succeeds, kills the probe (returncode != 0), keeps
   ABORTED, creates no Run, releases the slot, abort latency < 2s;
2. abort before the probe spawns is accepted (phase `prepare`);
3. probe failure (`exit 3`) still raises `PreflightError` and the slot is
   free for a retry, which then starts and completes a round normally;
4. probe timeout terminates the probe and reports `timed out after Ns`;
5. concurrent start → rejected second start → abort → restart → abort:
   slot never lost, no Run, never `FAILED`;
6. abort with no activity returns `False`.

Acceptance run:
`pytest -q backend/tests/test_abort_semantics.py backend/tests/test_adapters_preflight.py backend/tests/test_preflight_cancellation.py`
→ 25 passed, exit 0. Full suite → **153 passed, 1 skipped, exit 0**
(baseline was 147/1). `test_abort_semantics.py` and
`test_adapters_preflight.py` required no changes.

## 5. Windows considerations

Spawn mirrors the review agent's flags (`CREATE_NEW_PROCESS_GROUP`);
termination goes through the existing `_terminate_process` Windows path
(`terminate` → wait → `kill`); `communicate(timeout=…)` is cross-platform.
Not executed on a Windows machine here — the CI matrix covers it, so no
Windows result is claimed.

## 6. Known limitations at this stage

Superseded by section 7: the worker-start window below is now cancellable.

- **Worker-start micro-window** (Run created, agent process not yet
  registered): `abort_task` still returns `False`.
- **Prepare-phase cancellation is boundary-checked**: a cancel landing during
  snapshot/worktree/compile is honoured at the next boundary, not by
  interrupting the current step. (Still true after section 7.)
- **Direct `_preflight_check` calls without a reserved slot** (tests, manual
  probes) are uncancellable by design — there is no round to cancel.

## 7. Follow-up: the two review races and the Web UI path (`fix/preflight-cancel`)

Codex's review of section 2 reproduced two races. Both, plus the gaps
below, were first reproduced by new tests failing on the code from
section 2, then fixed.

| Problem | Before | After |
|---|---|---|
| Abort during the snapshot refresh | the unconditional INGESTING write that followed overwrote ABORTED; task stuck at `ingesting/prepare_review`, no Run | every stage change checks the cancel flag under the db lock abort uses to publish flag + ABORTED; refreshes update fields instead of saving a stale `Task` |
| Abort between the last check and the agent launch | refused (`False`), or rolled back by a stale save; the round then ran to completion | the handoff (check, Run, task fields, RUNNING) is one locked section; the worker checks the flag before and right after spawning |
| Web UI during preflight | task kept its old status for up to 150s; 中止 only enabled for `running` | task shows `ingesting/preflight` once reserved; 中止 enabled for `ingesting` and `running`; the expected failure of an aborted start is not shown as an error |
| Round 2+ while preparing | task listing repainted INGESTING (and VALIDATING) to `waiting_human` because the previous Run had finished | only RUNNING is repaired |
| Failed or cancelled launch | slot released before cleanup; a cancelled prepare left its worktree | cleanup first, then release, so an immediate restart keeps its worktree |
| Preflight failure | task status unchanged, reason only in the HTTP error | `failed/preflight_failed` plus an `error` event with `phase` |
| Ctrl+C in `amc review` during preflight | probe (own session) kept running | probe is stopped on any exception |
| Double termination | abort and the probe wait could both signal a reaped pid | `_terminate_process` skips processes already reaped |

`PreflightCancelledError` was renamed `RoundCancelledError`, since it is now
raised in every phase before the agent starts. `control.accepted` carries
the actual phase (`preflight` / `prepare` / `running`).

Verification (macOS arm64, fake probes and agents, no real model):
12 new test cases; the 11 covering changed behaviour failed for the
reproduced reason before the fix, the 12th pins the RUNNING repair that
was kept; full
suite `165 passed, 1 skipped`; the cancellation and concurrency files
repeated 25 times with no failure; frontend `tsc --noEmit` and
`npm run build` pass. Not yet run: the Web flow in a browser (checklist
W09) and Windows outside CI.
