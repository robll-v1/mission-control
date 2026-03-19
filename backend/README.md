# Backend

FastAPI service for PR review task management, multi-round review execution, real-time event streaming, and artifact export.

## Modules

- `api/` — REST routes: task CRUD, review control (start/abort/validate), SSE streaming, artifact export
- `adapters/` — Agent backend adapters (opencode CLI integration)
- `core/` — Mission engine (task lifecycle), execution service (subprocess management), context compiler (keyword extraction, candidate file scoring), worktree manager (git worktree isolation), review policy (static mode command blocking)
- `services/` — Review result extraction, diff generation, summary export, artifact storage
- `schemas/` — Pydantic request/response models

## Run
- From the repository root: `./scripts/dev-backend.sh`
- Default: `http://127.0.0.1:8000`

## Key Design
- Event sourcing: all state changes recorded as immutable events
- Background execution: review rounds run in worker threads with idle timeout
- SSE streaming: real-time event delivery to frontend
- Artifact-centric: all outputs stored as files under `runtime/tasks/<task-id>/`
