# Architecture

## Product Goal
Turn `issue -> agent execution -> validation -> handoff` into a visible, controllable, local workflow.

## Core Modules
- Ingress: create tasks from issue URLs or local task files
- Context Compiler: build `context.md` and `context.json`
- Mission Engine: drive task state and lifecycle
- Runner Adapter: abstract opencode and future backends
- Event Bus: normalize and stream runtime events
- Artifact Store: save logs, diffs, summaries, and validation output
- Mission Console: web UI for timeline, logs, controls, and artifacts

## v0 State Machine
- created
- ingesting
- context_ready
- running
- waiting_human
- validating
- completed
- failed
- aborted

## v0 Execution Flow
1. Create task from issue URL or text
2. Compile context and prepare worktree
3. Start backend run
4. Stream events to API/UI
5. Run optional validation
6. Export artifacts and summary
