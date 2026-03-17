# Agent Mission Control

Agent Mission Control is a local-first mission console for AI coding agents.

## v0 Scope
- Ingest a GitHub issue URL or local task description
- Compile task context for a target repository
- Run a coding agent through a backend adapter
- Stream execution events to a live mission console
- Capture artifacts such as logs, diffs, summaries, and validation results

## Planned Stack
- Backend: FastAPI
- Frontend: React + Vite
- Database: SQLite
- Realtime: Server-Sent Events (SSE)
- First backend adapter: opencode

## Initial Milestones
1. Repository skeleton and docs
2. Task, run, event, artifact, and validation models
3. Streaming opencode adapter
4. Mission detail UI with live timeline
5. Validation and export flow

## Local Development
- Bootstrap everything: `./scripts/bootstrap.sh`
- Start backend with static console: `./scripts/dev-static.sh`
- Start backend API only: `./scripts/dev-backend.sh`
- Start React frontend: `./scripts/dev-frontend.sh`

## Current Endpoints
- Backend health: `http://127.0.0.1:8000/api/health`
- Static console: `http://127.0.0.1:8000/`
- React dev server: `http://127.0.0.1:5173/`

## GitHub Issue Ingestion
- Task creation accepts a public GitHub issue URL and will fetch title, body, and recent comments automatically
- Set `GITHUB_TOKEN` or `GH_TOKEN` locally for higher GitHub API rate limits

## Context Compiler
- Task creation now generates `context/context.md` and `context/context.json` automatically
- Starting a task recompiles context after the task worktree is created
- Task detail now exposes generated context artifacts alongside logs and summaries

## Trial Run
- 首轮真实 issue 试跑模板：`docs/trial-run-template.md`
- MatrixOne #23587 专属试跑方案：`docs/trials/matrixone-23587.md`
- MatrixOne #23587 试跑结果：`docs/trials/matrixone-23587-result.md`
