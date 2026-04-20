# Mission Control

Local-first console for multi-round GitHub pull request review, powered by AI agents.

## Quick Start

```bash
# macOS / Linux — one command does everything
make start

# Or use the cross-platform Python CLI (also works on Windows)
pip install -e .
amc start
```

That's it. The system will:
1. Create a virtual environment and install dependencies (first run only)
2. Install frontend packages if Node.js is available
3. Find free ports automatically (avoids conflicts)
4. Start the backend API and frontend dev server
5. Wait for health check, then open the browser

### Stop / Status

```bash
amc stop      # graceful shutdown — aborts active reviews first
amc status    # show running services and endpoints
```

### CLI Options

```bash
amc start --port 9000          # custom backend port
amc start --ui-port 9173       # custom frontend port
amc start --no-ui              # backend only (no frontend)
amc start --no-browser         # don't auto-open browser
make start PORT=9000           # same via Makefile
```

## What It Does

Mission Control automates code review by ingesting GitHub PRs, compiling relevant context from your local repo, and running structured review rounds through an AI agent ([OpenCode](https://github.com/opencode-ai/opencode)). It tracks findings, enforces review policies, and produces exportable summaries.

### Key Features
- **Multi-round review**: run repeated review rounds, each building on previous findings
- **Context-aware**: keyword extraction + file scoring to build focused review prompts
- **Static review policy**: blocks dangerous commands (go, docker, npm, etc.) during review
- **Real-time streaming**: SSE event stream for live review progress
- **Structured results**: verdict (clear/concerns/failed), findings with severity/path/line
- **Preflight check**: verifies AI backend is installed and responsive before starting review
- **Graceful shutdown**: `amc stop` aborts active reviews cleanly, no orphaned processes
- **Crash recovery**: on restart, detects and marks stale tasks from previous crashes

## Prerequisites

| Tool | Required | Notes |
|------|----------|-------|
| Python 3.11+ | ✅ | Backend runtime |
| [OpenCode](https://github.com/opencode-ai/opencode) | ✅ | AI review agent |
| Node.js 18+ | Optional | Frontend dev server (UI) |
| `GITHUB_TOKEN` | Recommended | Higher GitHub API rate limits |

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
    cli.py         Cross-platform CLI (amc start/stop/status)
scripts/           Shell helper scripts (macOS/Linux)
runtime/           Generated at runtime — worktrees, task artifacts, policy bins
data/              SQLite database (auto-created)
```

## Review Flow

1. Create a review task from a GitHub PR URL and a local repository path
2. **Preflight check** — verify OpenCode is installed and the LLM is reachable
3. Context compiler extracts keywords, scores candidate files, builds review prompt
4. A git worktree is created at the PR head SHA for isolated review
5. The agent runs a review round in static mode (dangerous commands blocked)
6. Review results are extracted: verdict + findings with severity
7. Continue with additional review rounds or run validation checks
8. Export the review summary when ready to hand off

## API Endpoints

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
| POST | `/api/admin/shutdown` | Graceful shutdown (abort tasks + exit) |

## Configuration (`.amc.yaml`)

```yaml
repo:
  path: .                        # Repository path
  base_branch: main              # Base branch for PR context

backend:
  default: opencode              # Agent backend
  opencode:
    model: ''                    # Model override (empty = opencode default)
    variant: ''                  # Variant override

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

## Reliability

### Preflight Check
Before every review round, the system verifies that the AI backend (OpenCode) is installed and can respond. If not, it fails fast with a clear error message instead of starting a broken review.

### Graceful Shutdown
When you run `amc stop` or press Ctrl+C during `amc start`:
1. CLI calls `POST /api/admin/shutdown` to notify the backend
2. Backend aborts all active review tasks (kills OpenCode subprocesses, updates task status)
3. Backend exits cleanly via SIGTERM
4. CLI force-kills any remaining processes as fallback

### Crash Recovery
If the server is killed unexpectedly (power loss, `kill -9`, etc.):
- On next startup, `_recover_stale_tasks()` scans the database
- Any tasks stuck in `running` or `ingesting` status are marked as `FAILED`
- A `task.recovered` event is logged for auditability

## Cross-Platform Support

| Feature | macOS/Linux | Windows |
|---------|-------------|---------|
| `make start` | ✅ | ❌ (no make) |
| `amc start` | ✅ | ✅ |
| Process management | `killpg` / `SIGTERM` | `taskkill /T` / `terminate()` |
| Port detection | `socket.connect_ex` | `socket.connect_ex` |
| Browser opening | `webbrowser.open` | `webbrowser.open` |

## Notes
- Set `GITHUB_TOKEN` or `GH_TOKEN` for higher GitHub API rate limits
- Review tasks keep round history and artifacts under `runtime/tasks/<task-id>/`
- Worktrees are created under `runtime/worktrees/<task-id>/` and auto-cleaned after review
- Logs are saved to `.run/backend.log` and `.run/frontend.log` during dev mode

## Agent Skill (MCP / CLI / SDK)

Mission Control can be used as an **AI agent skill** — your coding agent calls it to get
code review feedback without a browser or server.

### Quick Examples

```bash
# Review local uncommitted changes
amc review

# Review a specific PR
amc review https://github.com/org/repo/pull/123

# JSON output for programmatic use
amc review --format json --exit-code

# Focused review
amc review --focus "security" --rounds 2
```

### MCP Server (for AI agents)

Start as an MCP tool server (stdio transport):

```bash
amc mcp
```

Configure in your agent's MCP settings (e.g. Claude Desktop, OpenCode):

```json
{
  "mcpServers": {
    "mission-control": {
      "command": "amc",
      "args": ["mcp"],
      "cwd": "/path/to/mission-control"
    }
  }
}
```

Available MCP tools:
| Tool | Description |
|------|-------------|
| `review_code` | Run full code review (PR or local diff) |
| `get_review_findings` | Get findings from most recent review |
| `abort_review` | Cancel a running review |

### SDK (Python)

```python
from app.sdk import ReviewEngine

engine = ReviewEngine(language='en')
report = engine.review('/path/to/repo')

if report.passed:
    print("All clear!")
else:
    for f in report.findings:
        print(f"[{f.severity}] {f.path}:{f.line} — {f.summary}")
```

### CLI Options

```
amc review [PR_URL] [OPTIONS]

  PR_URL              GitHub PR URL (omit for local diff mode)
  --repo, -r PATH     Repository path (default: cwd)
  --base, -b BRANCH   Base branch for local diff (default: auto-detect)
  --rounds N          Max review rounds (default: 1)
  --format FORMAT     Output: markdown (default) or json
  --focus, -f TEXT    Review focus area
  --timeout SECONDS   Per-round timeout (default: 600)
  --exit-code         Exit 1 if review has concerns (useful in CI)
```

### Modes

| Mode | Trigger | What it reviews |
|------|---------|-----------------|
| **PR mode** | Provide PR URL | Full pull request diff from GitHub |
| **Local diff** | No URL | `git diff` against base branch |

Both modes produce identical output format and use the same AI review engine.
