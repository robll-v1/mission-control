# Release readiness: first-review acceptance and messaging boundaries

Status record for making Mission Control demoable to a stranger: what was
actually verified, on which machine, with which commands — and the claims we
must NOT make. Evidence files referenced below live in a local temp dir and
are not committed.

## 1. Verified environment

| Item | Value |
|---|---|
| Date | 2026-10-24 |
| Machine | macOS (arm64), user `robll` |
| Backend CLI | opencode v2.0.14 (`~/.opencode/bin/opencode`) |
| Auth used | Existing login (Z.AI Coding Plan + free tier); nothing installed or purchased |
| Model (round 1+2) | `opencode/glm-5.3-flash-free` — free tier, no plan quota consumed |
| amc build | Branch `codex/release-readiness-glm`, base `origin/main` @ `7a093bd` (contains PR #21 fixes) |
| Isolation | `XDG_CONFIG_HOME=$ISOL/config`; user's `~/.config/amc` untouched. Note: `amc review` chdirs to the project root, so `data/` and `runtime/` land there (both gitignored); the review agent only ever touched the synthetic repo |

Real model usage: exactly **two full review rounds** plus a handful of tiny
"Reply OK" preflight probes. The Z.AI plan itself was quota-exhausted
(`provider.quota`, resets 2026-10-30), which is why the free-tier model was
used; the exhaustion was surfaced correctly in the event stream after the
error-detail fix below.

## 2. Fixes made during this acceptance (not demo code — product bugs found live)

1. **opencode adapter used flags the current CLI removed** — `opencode run`
   has no `--dir` (cwd already set by the executor) and no `--variant`
   (variants are `model#variant`). Old args made every round fail in seconds
   with the CLI's usage text as its summary.
2. **Preflight probe timeout 60s → 150s** — the first probe after opencode's
   background service cold-starts can exceed 60s (observed: cold >60s, warm
   8–20s) and failed rounds with "backend may be unreachable".
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
- Round-1 findings were **not** silently dropped: 4 came back as
  `unverified` (incl. the critical injection), 1 as `persistent`; 1 new
  low finding. The report summary explicitly lists the retained findings.
- Honest quirk observed: the fix restores `notes.py` to be byte-identical
  with `HEAD`, so the local diff is empty. The agent said so ("couldn't
  obtain the actual diff"), fell back to reading the file on disk, and still
  produced sensible findings. See §6 for the underlying limitation.

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
| First-review → fix → re-review (happy path, real model) | ✅ verified live | §3, evidence `round1.json`/`round2.json` |
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
- **Claude Code / Copilot / Codex expose no model-listing command**; the
  Settings page will show an empty model list for them until their CLIs
  offer enumeration (see §8).

## 7. Messaging boundaries (copy guidance)

Accurate phrasing for README / demo narration:

- "Local-first" means **the review runs on your machine** through your own
  agent CLI — **not** that model inference is offline.
- "Uses the agent you already have" means **no extra seat/key for Mission
  Control** — your agent's own model usage and quota still apply.
- A **cached model list is not an account permission check**; for OpenCode
  the CLI query reflects the current login, the catalog cache does not.
- The **SDK's unverified retention is CLI/SDK behaviour**, not (yet) a Web
  UI feature — say so explicitly.
- **Submodules and sandbox caveats** as in §6.
- Model lists shown in Settings are **enumerated from the installed CLIs**
  (or honestly empty with a reason) — never a curated hardcoded list.

## 8. 30-second demo script (record after #20 merges)

1. *(5s)* Terminal: `python3 examples/first-review/make_demo_repo.py /tmp/demo`
   — "a real bug, a synthetic repo".
2. *(10s)* `amc review --repo /tmp/demo --backend opencode --model
   opencode/glm-5.3-flash-free` — point at the console output as it lands:
   verdict `concerns`, the critical SQL-injection finding at `notes.py:24`.
3. *(8s)* `--apply-fix`, re-run — scroll to `unverified`: the earlier
   critical finding is retained, not forgotten. "A problem doesn't disappear
   just because the model stopped mentioning it."
4. *(7s)* Cut to the Web UI (screenshot after #20): task list → verdict
   card → findings with `file:line`.

Recording notes: reuse the fixture script for seeding (also useful for the
README GIF); record light and dark themes separately.

## 9. Frontend handoff (belongs to #20+, not touched here)

`/api/config/detect` now returns additive `models_source`
(`cli`/`catalog_cache`/`cache`/`config`/`unknown`) and `models_note` per
backend. The Settings page currently ignores both; showing the note where a
backend has no enumerable models (e.g. Claude Code) would close the loop.
`frontend/src/App.tsx`, `Settings.tsx`, `styles.css` were not modified.
