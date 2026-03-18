# PR Review Control

PR Review Control is a local-first console for multi-round GitHub pull request review.

## Scope
- Ingest a GitHub pull request URL and a local repository path
- Compile review context from PR metadata and the local repository
- Run repeated review rounds through a backend adapter
- Stream review events to a live console
- Capture artifacts such as context snapshots and exported review summaries

## Local Development
- Bootstrap everything: `./scripts/bootstrap.sh`
- Start backend service: `./scripts/dev-backend.sh`
- Start backend service with the built-in static console entry: `./scripts/dev-static.sh`
- Start React frontend: `./scripts/dev-frontend.sh`
- Run the local verification flow: `./scripts/verify.sh`

## Endpoints
- Backend health: `http://127.0.0.1:8000/api/health`
- Built-in static console: `http://127.0.0.1:8000/`
- React dev server: `http://127.0.0.1:5173/`

## Review Flow
1. Create a review task from a GitHub PR URL and a local repository path.
2. Inspect the compiled PR review context and suggested files.
3. Start the first review round.
4. Continue with additional review rounds as needed.
5. Export the review summary when the conversation is ready to hand off.

## Notes
- Set `GITHUB_TOKEN` or `GH_TOKEN` locally for higher GitHub API rate limits.
- Review tasks keep round history and artifacts under `runtime/tasks/<task-id>/`.
