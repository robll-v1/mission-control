"""Thin FastMCP stdio transport adapter for Mission Control.

All orchestration, SQLite persistence, cross-agent selection, and process
cleanup live in :mod:`app.services.mcp_service`. This module only exposes
the 5 orthogonal MCP tools and guards the stdio JSON-RPC stdout channel.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from io import TextIOWrapper
from typing import Annotated

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from app.services.mcp_service import McpReviewService

mcp = FastMCP(
    name='mission-control',
    instructions="""Mission Control — local cross-agent code review server.

WORKFLOW:
1. Optional: call check_agents() to inspect locally installed agent CLIs and reasoning levels (zero LLM calls).
2. Call start_review(repo_path=..., exclude_agent=<your-own-agent>) to snapshot uncommitted local changes (or a GitHub PR) and launch a review on a second local CLI.
3. Call wait_review(task_id=...) — it long-polls up to 25s and returns either progress (done=false) or structured findings (done=true). Repeat wait_review while done=false.
4. Optional: call verify_findings(task_id=...) to run 1-pass Cold Falsification on open findings with a different local CLI, attaching confirmed / disputed / uncertain evidence to each finding.
5. After fixing confirmed issues, call start_review(task_id=..., repo_path=...) to run Round 2 on the updated working tree and track findings as new / persistent / unverified / likely_fixed.
6. Use update_finding(task_id, finding_id, action='resolve'|'dismiss'|'reopen') for human dispositions, or abort_review(task_id) to cancel an active round or verification.

RULES:
- Pass exclude_agent (e.g. 'claude-code' or 'codex') so Mission Control picks a different installed CLI when available.
- Never claim a finding is fixed when its status is 'unverified'.
- Never fabricate a clean review if ok=false or verdict is 'failed'/'inconclusive'.
""",
)

_service: McpReviewService | None = None


def get_service() -> McpReviewService:
    """Return the lazily initialized shared-SQLite MCP review service."""
    global _service
    if _service is None:
        _service = McpReviewService.from_shared_paths()
    return _service


def set_service(service: McpReviewService | None) -> None:
    """Inject a custom McpReviewService (primarily for isolated tests)."""
    global _service
    _service = service


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ── Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
async def check_agents(
    backend: Annotated[
        str | None,
        "Optional specific backend to probe ('codex', 'claude-code', 'opencode', 'copilot'). Omit to check all installed CLIs.",
    ] = None,
) -> str:
    """Check locally installed coding agent CLIs, models, and reasoning capabilities (zero LLM calls)."""
    result = await asyncio.to_thread(get_service().check_agents, backend)
    return _dumps(result)


@mcp.tool()
async def start_review(
    repo_path: Annotated[
        str | None,
        'Path to the local git repository (required for a new task; optional when continuing an existing task_id).',
    ] = None,
    task_id: Annotated[
        str | None,
        'Existing task_id to run the next review round on (preserves cross-round finding history).',
    ] = None,
    pr_url: Annotated[
        str | None,
        'Optional GitHub PR URL. Omit to review uncommitted local changes via a zero-touch snapshot.',
    ] = None,
    base: Annotated[
        str | None,
        'Optional base branch for local diff review (auto-detected if omitted).',
    ] = None,
    backend: Annotated[
        str | None,
        "Optional explicit reviewer backend ('codex', 'claude-code', 'opencode', 'copilot').",
    ] = None,
    exclude_agent: Annotated[
        str | None,
        "Host agent to avoid for cross-agent review (e.g. 'claude-code', 'codex'). Falls back honestly if only one CLI is installed.",
    ] = None,
    model: Annotated[str | None, 'Optional model ID override for this round.'] = None,
    reasoning: Annotated[str | None, "Optional reasoning effort override (e.g. 'low', 'medium', 'high')."] = None,
    focus: Annotated[str | None, "Optional review focus (e.g. 'security, concurrency, edge cases')."] = None,
    note: Annotated[str | None, 'Optional note for this review round (e.g. what changed since round 1).'] = None,
    language: Annotated[str, "Review output language: 'en' (default) or 'zh-CN'."] = 'en',
) -> str:
    """Start a new local/PR code review or launch the next round on an existing task_id."""
    result = await asyncio.to_thread(
        get_service().start_review,
        repo_path=repo_path,
        task_id=task_id,
        pr_url=pr_url,
        base=base,
        backend=backend,
        exclude_agent=exclude_agent,
        model=model,
        reasoning=reasoning,
        focus=focus,
        note=note,
        language=language,
    )
    return _dumps(result)


@mcp.tool()
async def wait_review(
    task_id: Annotated[str, 'Task ID returned by start_review.'],
    wait_sec: Annotated[
        int,
        'Maximum seconds to long-poll before returning progress (default 25, clamped to 0..45 to stay under 60s client timeouts).',
    ] = 25,
    min_severity: Annotated[
        str | None,
        "Optional minimum severity filter: 'critical', 'high', 'medium', or 'low'.",
    ] = None,
) -> str:
    """Wait for a review round to complete (bounded long-poll) and return progress or structured findings."""
    result = await asyncio.to_thread(
        get_service().wait_review,
        task_id=task_id,
        wait_sec=wait_sec,
        min_severity=min_severity,
    )
    return _dumps(result)


@mcp.tool()
async def update_finding(
    task_id: Annotated[str, 'Task ID owning the finding.'],
    finding_id: Annotated[str, 'Finding ID returned by wait_review.'],
    action: Annotated[str, "Disposition action: 'resolve', 'dismiss', or 'reopen'."],
) -> str:
    """Record a human disposition (resolve, dismiss, or reopen) on a tracked finding in shared SQLite."""
    result = await asyncio.to_thread(
        get_service().update_finding,
        task_id=task_id,
        finding_id=finding_id,
        action=action,
    )
    return _dumps(result)


@mcp.tool()
async def verify_findings(
    task_id: Annotated[str, 'Task ID whose open findings should be verified.'],
    backend: Annotated[
        str | None,
        "Optional explicit verifier CLI ('codex', 'claude-code', 'opencode', 'copilot', or 'auto'). Omit or pass 'auto' to pick a different CLI from the primary reviewer.",
    ] = None,
    model: Annotated[str | None, 'Optional verifier model ID override.'] = None,
    reasoning: Annotated[str | None, "Optional verifier reasoning effort override (e.g. 'low', 'medium', 'high')."] = None,
    finding_ids: Annotated[
        list[str] | None,
        'Optional subset of finding IDs to verify. Omit to verify all open findings on the task.',
    ] = None,
    language: Annotated[str, "Verification rationale language: 'en' (default) or 'zh-CN'."] = 'en',
    wait_sec: Annotated[
        int,
        'Maximum seconds to wait for verification completion before returning (default 25, clamped 0..45).',
    ] = 25,
) -> str:
    """Run 1-pass adversarial Cold Falsification on findings using a second local CLI on the same snapshot."""
    result = await asyncio.to_thread(
        get_service().verify_findings,
        task_id=task_id,
        backend=backend,
        model=model,
        reasoning=reasoning,
        finding_ids=finding_ids,
        language=language,
        wait_sec=wait_sec,
    )
    return _dumps(result)


@mcp.tool()
async def run_blind_jury(
    task_id: Annotated[
        str | None,
        'Existing task_id to run blind dual-review on (optional if repo_path is given).',
    ] = None,
    repo_path: Annotated[
        str | None,
        'Path to local git repository (used to snapshot and create a task when task_id is omitted).',
    ] = None,
    pr_url: Annotated[str | None, 'Optional GitHub PR URL when creating a new task.'] = None,
    base: Annotated[str | None, 'Optional base branch for local diff when creating a new task.'] = None,
    backends: Annotated[
        list[str] | None,
        "Optional list of 2 distinct local CLIs (e.g. ['codex', 'claude-code']). Omit to auto-pick the top 2 installed CLIs.",
    ] = None,
    falsify_disagreements: Annotated[
        bool,
        'When true (default), automatically run 1-pass Cold Falsification on 1/2 unique findings using the other juror (0 extra tokens on 2/2 consensus items).',
    ] = True,
    focus: Annotated[str | None, 'Optional review focus area when creating a new task.'] = None,
    language: Annotated[str, "Review output language: 'en' (default) or 'zh-CN'."] = 'en',
    wait_sec: Annotated[
        int,
        'Maximum seconds to wait before returning progress (default 25, clamped 0..45).',
    ] = 25,
) -> str:
    """Run same-snapshot blind dual-review across 2 distinct local CLIs with optional disagreement falsification."""
    result = await asyncio.to_thread(
        get_service().run_blind_jury,
        task_id=task_id,
        repo_path=repo_path,
        pr_url=pr_url,
        base=base,
        backends=backends,
        falsify_disagreements=falsify_disagreements,
        focus=focus,
        language=language,
        wait_sec=wait_sec,
    )
    return _dumps(result)


@mcp.tool()
async def abort_review(
    task_id: Annotated[str, 'Task ID of the active review round to cancel.'],
) -> str:
    """Cancel an active preflight or review round and terminate its subprocess tree."""
    result = await asyncio.to_thread(get_service().abort_review, task_id=task_id)
    return _dumps(result)


# ── Stdio transport with strict stdout isolation ──────────────────────


async def _run_stdio_isolated() -> None:
    """Run FastMCP stdio transport on a dedicated dup'd stdout fd.

    Redirects fd 1 and ``sys.stdout`` to ``stderr`` so accidental prints or
    library output can never corrupt the JSON-RPC stream.
    """
    sys.stdout.flush()
    raw_stdout_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    raw_binary = os.fdopen(raw_stdout_fd, 'wb', buffering=0)
    text_stdout = TextIOWrapper(raw_binary, encoding='utf-8')
    protocol_stdout = anyio.wrap_file(text_stdout)
    try:
        async with stdio_server(stdout=protocol_stdout) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )
    finally:
        if _service is not None:
            _service.shutdown()
        try:
            text_stdout.flush()
        except Exception:
            pass
        try:
            text_stdout.close()
        except Exception:
            pass


def serve() -> None:
    """Start the Mission Control MCP server over stdio."""
    asyncio.run(_run_stdio_isolated())


if __name__ == '__main__':
    serve()
