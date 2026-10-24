# First-review demo fixture

A synthetic, credential-free git repo used to demonstrate and verify one full
Mission Control loop: **first review → apply fix → re-review**.

The repo's `notes.py` starts from a safe, parameterized sqlite query. The
uncommitted working-tree change rewrites `search()` to interpolate the caller's
keyword into SQL with an f-string — a real, explainable SQL-injection bug, the
kind every reviewer flags.

```bash
# 1. Create the demo repo (safe base is committed; the bug is an uncommitted change)
python3 examples/first-review/make_demo_repo.py /tmp/amc-demo-notes

# 2. First review — expect a finding on notes.py near the f-string query
amc review --repo /tmp/amc-demo-notes --backend opencode --format json

# 3. Apply the fix (restores the parameterized query, still uncommitted)
python3 examples/first-review/make_demo_repo.py /tmp/amc-demo-notes --apply-fix

# 4. Re-review — the finding should no longer be reported as new
amc review --repo /tmp/amc-demo-notes --backend opencode --format json
```

Notes:

- Nothing here is committed back to the mission-control repo; the demo repo is
  created wherever you point it and can be deleted afterwards.
- Round 4 runs in incremental mode by default: a previously reported finding
  that the new round does not cover comes back as `unverified`, not `resolved`.
  Use `--no-incremental` for a full re-review that replaces prior findings.
- Full acceptance record, environment and observed results live in
  [docs/release-readiness.md](../../docs/release-readiness.md).
