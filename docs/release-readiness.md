# Release readiness: first-review acceptance and messaging boundaries

Status record for making Mission Control demoable to a stranger: what was
actually verified, on which machine, with which commands — and the claims we
must NOT make. Evidence files referenced below live in a local temp dir and
are not committed.

## 1. Verified environment

| Item | Value |
|---|---|
| Date | 2026-10-24 |
| Machine | macOS (arm64) |
| Backend CLI | opencode v2.0.14 (`~/.opencode/bin/opencode`) |
| Auth used | Existing CLI login; no new account or purchase |
| Model (round 1+2) | `opencode/glm-5.3-flash-free` — the model identifier reported by the CLI |
| amc build | Branch `codex/release-readiness-glm`, base `origin/main` @ `7a093bd` (contains PR #21 fixes) |
| Isolation | `XDG_CONFIG_HOME=$ISOL/config`; the user's `~/.config/amc` was never touched (verified). The review agent only ever saw the synthetic repo |

**Where db/runtime actually landed (verified, not assumed):** `amc review`
(`cmd_review`) does `os.chdir(project_root)` internally, so a temp cwd does
NOT relocate its database or runtime — both were created inside the
mission-control worktree (`data/amc.db`, gitignored; confirmed by reading
the run's events back from that exact file), while review worktrees were
created under `$ISOL/demo/runtime`. For strict isolation in future runs,
invoke the SDK directly with explicit paths —
`ReviewEngine(db_path=<tmp>/amc.db, runtime_root=<tmp>/runtime)` — or give
each run an independent code checkout. The chdir behaviour itself is in
`cli.py`, which belongs to another workstream and was not modified here.

Real model usage: exactly **two full review rounds** plus a handful of tiny
"Reply OK" preflight probes. The initially selected provider returned quota exhaustion
(`provider.quota`), so a different model exposed by the CLI was used; the exhaustion was surfaced correctly in the event stream after the
error-detail fix below.

**Scope of the real-call conclusion:** only `opencode/glm-5.3-flash-free` —
the model actually used in both rounds — is smoke-verified as callable.
Every other ID returned by `opencode models` (or any cache) is
enumeration-only for this machine; per-model call availability was not and
cannot be verified by detection, which makes no model calls.

## 2. Fixes made during this acceptance (not demo code — product bugs found live)

1. **opencode adapter used flags removed by opencode v2.0.14** — on the
   tested v2.0.14 CLI, `opencode run`
   has no `--dir` (cwd already set by the executor) and no `--variant`
   (variants are `model#variant`). Old args made every round fail in seconds
   with the CLI's usage text as its summary.
2. **Preflight probe timeout 60s → 150s** — the first probe after opencode's
   background service cold-starts can exceed 60s (observed: cold >60s, warm
   8–20s) and failed rounds with "backend may be unreachable". The preflight
   subprocess has no cancellation path, so aborting during this wait does not
   stop it; the caller must wait for the timeout or probe to finish.
3. **Stream error events kept no detail** — `{"type":"error","error":{…}}`
   lost the nested message, hiding a clear "Weekly/Monthly Limit Exhausted"
   behind a content-less failure.

## 3. The reproducible demo

Fixture: [`examples/first-review/`](../examples/first-review/) — a synthetic,
credential-free repo whose `notes.py` starts from a safe parameterized query;
the uncommitted working-tree change rewrites `search()` into an f-string SQL
interpolation (a real, explainable injection bug). One script creates the
repo and applies the fix.

Exact commands (from any directory; `$DEMO` is where the demo repo lives):

```bash
python3 examples/first-review/make_demo_repo.py $DEMO
amc review --repo $DEMO --backend opencode --model opencode/glm-5.3-flash-free --format json
python3 examples/first-review/make_demo_repo.py $DEMO --apply-fix
amc review --repo $DEMO --backend opencode --model opencode/glm-5.3-flash-free --format json
```

### Observed — round 1 (bug present)

- 37.9s, verdict `concerns`, backend `opencode`, model `opencode/glm-5.3-flash-free`.
- **critical — `notes.py:24` — SQL injection via f-string interpolation of
  `keyword`** (the planted bug, exact line), plus 4 further reasonable
  findings (quote-crash regression, missing tests, misleading comment,
  unescaped `LIKE` wildcards).
- Summary correctly traced the taint from CLI argv into the query.

### Observed — round 2 (after `--apply-fix`)

- 197.4s, incremental (base = previous reviewed SHA), verdict `concerns`.
- The model **was** called (call #2 of the budget): real agent output, and
  round-1 findings were **not** silently dropped — 4 came back as
  `unverified` (incl. the critical injection), 1 as `persistent`, 1 new
  low finding.
- **But this was not a genuine fix-diff re-review, and must not be claimed
  as one.** The original fixture's `--apply-fix` restored `notes.py`
  byte-identical to `HEAD`, so the local diff was empty; the agent said so
  itself ("couldn't obtain the actual diff"), fell back to reading files on
  disk, and inferred the rest. A true "review the fixed diff" round has
  therefore **not** been exercised live — see the correction below.

### Fixture correction (post-acceptance, offline-verified only)

`make_demo_repo.py --apply-fix` now restores the parameterized query AND
escapes LIKE wildcards (addressing round 1's low finding), deliberately
leaving a real, harmless change in the working tree so the re-review has a
non-empty diff to verify. Offline validation: both versions compile, the
post-fix diff vs `HEAD` is non-empty, and the behaviour genuinely changes
(`find "%"` matches only a literal-% title instead of every note). Not
re-run with the model: the 2-call budget was already spent on the rounds
above.

## 4. CLI/SDK vs Web — what each actually does (do not conflate)

| Behaviour | CLI / SDK (`amc review`) | Web UI (`amc start`) |
|---|---|---|
| Cross-round finding status (new/persistent/unverified) | Aggregated into the report; `unverified` keeps verdict at `concerns` | Per-round view of the latest run's result; no cross-round unverified aggregation |
| Full re-review that replaces prior findings | `--no-incremental` | Start a new round |
| Incremental base | Last reviewed `head_sha` from `.amc/reviews` (local-diff mode only, same-mode records) | n/a (rounds are per task) |

Marketing must not present the SDK's unverified-retention as a Web feature.

## 5. Acceptance matrix

| Scenario | Status | Evidence |
|---|---|---|
| First review (real model) | ✅ verified live | §3, evidence `round1.json` |
| Round 2 = real model call | ✅ verified live (call #2) | §3, evidence `round2.json` |
| Fix-diff re-review (intended flow) | ❌ not exercised live: original fixture made the worktree byte-identical to HEAD, round 2 degraded to file-reading; fixture corrected, offline-validated | §3 correction |
| Backend cold start (>60s probe) | ✅ verified live, fixed (150s) | §2.2 |
| Quota exhaustion mid-flow | ✅ verified live, now diagnosable | §1, §2.3 |
| Synthetic-fixture offline flow (no model) | ✅ verified | generator round-trip incl. compile + diff check |
| Model discovery truthfulness | ✅ unit-tested (CLI missing / timeout / corrupt cache / unknown list / explicit model kept) | `backend/tests/test_backend_detector.py` |
| Fresh install (`pip install -e .` → `amc init --global` → review) | ❌ not verified this round | needs a clean machine/VM |
| No AI CLI installed end-to-end | ❌ not verified live (covered only by unit tests) | |
| Repo with no changes | ❌ not verified this round (early-exit path exists) | |
| Execution failure / cancel via Web UI | ❌ not verified live (abort semantics unit-tested on #21) | |
| Web UI full flow with real model | ❌ not verified (kept within the 2-round budget) | |

## 6. Known limitations found during acceptance (document, don't hide)

- **Incremental base ignores prior uncommitted state.** History records
  `head_sha` only. If round 1 reviewed uncommitted changes and the fix makes
  the worktree byte-identical to `HEAD`, round 2's diff is empty and the
  agent degrades to reading files (observed, §3). A future fix would record
  the previous snapshot SHA as the incremental base.
- **Submodules:** uncommitted content inside a submodule is not part of the
  parent-repo snapshot. "Reviews what is on disk" means the parent repo.
- **PATH shim is a guard-rail, not a sandbox** (absolute paths bypass it) —
  already documented in the README.
- **Model enumeration differs by backend.** Codex can enumerate IDs from its
  local cache and configuration; these are still not account-callability
  checks. Claude Code and Copilot do not expose model enumeration here, so
  their lists can be empty while the backend default or a saved model remains
  selectable.

## 7. Messaging boundaries (copy guidance)

Accurate phrasing for README / demo narration:

- "Local-first" means **the review runs on your machine** through your own
  agent CLI — **not** that model inference is offline.
- "Uses the agent you already have" means **no extra seat/key for Mission
  Control** — your agent's own model usage and quota still apply.
- A model list — whether from a CLI query, catalog/cache, or local
  configuration — is **enumeration, not a permission check**. `opencode models` proves which IDs the CLI
  exposes under the current configuration; it does not prove this account
  may call each one. Neither does the catalog cache. Only a model that has
  actually run a round is smoke-verified.
- The **SDK's unverified retention is CLI/SDK behaviour**, not (yet) a Web
  UI feature — say so explicitly.
- **Submodules and sandbox caveats** as in §6.
- Model lists in Settings can come from a CLI, catalog cache, Codex cache, or
  local configuration. Where a backend cannot enumerate models, the list is
  honestly empty with a reason; enumeration never proves account permission.

## 8. 30-second demo plan (record only after the fixed-diff round is accepted)

1. Seed the synthetic repo and start the first review. Show only the result
   actually observed: the initial run reported SQL injection at `notes.py:24`.
2. Apply the fix and confirm the next review has a real, non-empty diff before
   recording it. This corrected flow has not yet had a live model run; do not
   predict its verdict, findings, or cross-round statuses.
3. Show the model selection page and a review result in the merged PR #20 UI.

The 30 seconds describes the edited video length, not review runtime. Waits
must be cut down and identified as edited rather than presented as model speed.
Recheck the flow with a fresh run and a current screenshot before filming.

Recording notes: reuse the fixture script for seeding (also useful for the
README GIF); record light and dark themes separately.

## 9. Frontend integration status

`/api/config/detect` now returns additive `models_source`
(`cli`/`catalog_cache`/`cache`/`config`/`unknown`), `models_note` and
`models_call_verified: false` per backend — source and call-availability are
separate facts, and detection never verifies calls. The integration page now
shows the model-list source and warns that enumeration does not verify account
call permission. When no list is available, it explains that the backend
default or an already configured model can still be selected. PR #20 is merged;
the current frontend also has the follow-up compatibility treatment for older
API responses that omit source metadata.

## 10. Integrated local acceptance (2026-10-24)

The final integrated working tree was checked locally after combining the GLM,
Qoder, and WorkBuddy work:

- Backend full suite: `pytest -q` — **147 passed, 1 skipped**, 34.04 seconds,
  exit code 0.
- Frontend: `npx tsc --noEmit` and `npm run build` — both passed.
- Qoder CLI focused tests: **23 passed**; demo-fixture focused tests:
  **4 passed**. These are automated checks, not a live model review.
- An independent reviewer accepted the fixture overwrite guard. The guard
  only applies the fix to the recognized generated fixture and preserves
  unknown file bytes.
- The branch has not been pushed; **there is no CI result for this integrated
  tree**.

The release blocker remains the real fixed-diff second review: the earlier
second model call reviewed an empty diff, and the corrected non-empty-diff
flow has not been run with a model. Also not verified here: clean-machine
installation and a full Web UI flow. The 147-test result and focused tests do
not substitute for those live checks.
