# Frontend

React + TypeScript + Vite single-page console for PR review management.

## Features
- Task list sidebar with status indicators
- Review timeline with real-time SSE event streaming
- Runs & checks tab for review round history and validation results
- Artifacts tab for context snapshots, diffs, and summaries
- Modal-based task creation from GitHub PR URLs
- Linear/Vercel-inspired dark UI

## Run
- From the repository root: `./scripts/dev-frontend.sh`
- Default: `http://127.0.0.1:5173` (proxies API to backend on `:8000`)

## Build
- From this directory: `npm run build`
