# PR Review Control

Local-first console for multi-round GitHub pull request review, powered by AI agents.

## What It Does

PR Review Control automates code review by ingesting GitHub PRs, compiling relevant context from your local repo, and running structured review rounds through an AI agent (opencode). It tracks findings, enforces review policies, and produces exportable summaries.

## Scope
- Ingest a GitHub pull request URL and a local repository path
- Compile review context from PR metadata, diff, and candidate source files
- Run repeated review rounds through a configurable backend adapter
- Stream review events in real-time via SSE
- Extract structured review results (verdict, findings with severity/path/line)
- Enforce static review policy (block dangerous commands during review)
- Capture artifacts such as context snapshots, diffs, and exported review summaries

## Architecture

```
frontend/          React + Vite UI (Linear/Vercel style)
backend/
  app/
    api/           FastAPI routes (tasks, control, stream, artifacts)
    adapters/      Agent backends (opencode)
    core/          Engine, execution, models, context compiler, worktree, review policy
    services/      Summary, diff, artifact store, review result extraction
    schemas/       Pydantic request/response schemas
scripts/           Dev and verification scripts
runtime/           Generated at runtime — worktrees, task artifacts, policy bins
```

## Review Flow
1. Create a review task from a GitHub PR URL and a local repository path.
2. Context compiler extracts keywords, scores candidate files, and builds a review prompt.
3. A git worktree is created at the PR head SHA for isolated review.
4. The agent runs a review round in static mode (dangerous commands blocked).
5. Review results are extracted: verdict (clear/concerns/failed), findings with severity.
6. Continue with additional review rounds or run validation checks.
7. Export the review summary when ready to hand off.

## Local Development
- Bootstrap everything: `./scripts/bootstrap.sh`
- Start backend service: `./scripts/dev-backend.sh`
- Start backend with built-in static console: `./scripts/dev-static.sh`
- Start React frontend: `./scripts/dev-frontend.sh`
- Run local verification: `./scripts/verify.sh`

## Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/tasks` | List all tasks |
| POST | `/api/tasks` | Create a review task |
| GET | `/api/tasks/{id}` | Task detail (events, runs, checks, artifacts) |
| GET | `/api/tasks/{id}/stream` | SSE event stream |
| POST | `/api/tasks/{id}/start` | Start a review round |
| POST | `/api/tasks/{id}/abort` | Abort running review |
| POST | `/api/tasks/{id}/validate` | Run validation checks |
| POST | `/api/tasks/{id}/export-summary` | Export review summary |
| POST | `/api/tasks/{id}/export-diff` | Export worktree diff |

## Configuration (`.amc.yaml`)

```yaml
backend:
  default: opencode        # Agent backend
  opencode:
    model: ''              # Model override (empty = opencode default)
    variant: ''            # Variant override

context:
  include_recent_commits: 8      # Recent commits for context
  candidate_files_limit: 12      # Max candidate files to include

execution:
  idle_timeout_sec: 180          # Kill agent after N seconds idle

validation:
  default_mode: standard         # standard | full
  checks:
    build:
      command: ''                # e.g. 'make build'
    test:
      command: ''                # e.g. 'go test ./...'
```

## Review Policy

In static review mode, the system creates a restricted `PATH` that blocks potentially dangerous commands (go, docker, npm, make, etc.), ensuring the agent only reads and analyzes code without executing it.

## Notes
- Set `GITHUB_TOKEN` or `GH_TOKEN` locally for higher GitHub API rate limits.
- Review tasks keep round history and artifacts under `runtime/tasks/<task-id>/`.
- Worktrees are created under `runtime/worktrees/<task-id>/` and auto-cleaned after review.
