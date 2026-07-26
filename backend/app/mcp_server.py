"""
MCP Tool Server for Mission Control Agent Skill.

Exposes code review capabilities as MCP tools for AI agents.
Runs in stdio mode (launched via `amc mcp`).

Uses Direct API mode (no subprocess) to avoid nested agent issues.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Annotated

from mcp.server.fastmcp import FastMCP

# ── Server setup ──────────────────────────────────────────────────────

mcp = FastMCP(
    name="mission-control",
    instructions="""Mission Control code review server.

WORKFLOW (non-blocking, two-step):
1. Call review_code with repo_path → returns immediately with a task_id
2. Wait 60-120 seconds (reviews take time)
3. Call get_review_status with the task_id → returns findings when done

If get_review_status returns status="running", wait another 30s and retry.

INTERPRETING RESULTS:
- verdict="clear" + passed=true → code is fine, proceed
- verdict="concerns" + passed=false → review findings and fix issues
- finding severity: critical > high > medium > low

RULES:
- Do NOT call review_code in a loop
- Call review_code ONCE per review session
- Use get_review_status to poll (not review_code again)
""",
)

# Global engine instance (lazy init)
_engine = None


def _get_engine():
    """Lazy-initialize ReviewEngine."""
    global _engine
    if _engine is None:
        from app.sdk import ReviewEngine
        _engine = ReviewEngine(language='en', backend='direct_api')
    return _engine


# Background review tasks
_background_tasks: dict[str, asyncio.Task] = {}
_background_results: dict[str, dict] = {}


# ── Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
async def review_code(
    repo_path: Annotated[str, "Absolute path to the git repository to review"],
    pr_url: Annotated[str | None, "GitHub PR URL (omit for local diff mode)"] = None,
    base: Annotated[str | None, "Base branch for local diff (auto-detects if omitted)"] = None,
    focus: Annotated[str | None, "Review focus area (e.g. 'security', 'performance', 'correctness')"] = None,
    timeout: Annotated[int, "Timeout in seconds (default: 300)"] = 300,
    context: Annotated[str | None, "Additional context (requirements, intent) as JSON string"] = None,
) -> str:
    """Start an AI-powered code review on a repository (non-blocking).

    Returns immediately with a task_id. Use get_review_status(task_id) to poll
    for results. Reviews typically take 60-120 seconds.

    Usage:
    1. Call review_code → get task_id
    2. Wait ~60-120s, then call get_review_status with the task_id
    3. If status is "completed", findings are included in the response
    """
    # Resolve API config early to report model to caller
    from app.adapters.direct_api_adapter import resolve_direct_api_config
    from app.core.config import load_repo_config
    amc_cfg = load_repo_config(repo_path)
    api_config = resolve_direct_api_config(amc_cfg)

    if not api_config.is_valid():
        return json.dumps({
            "status": "error",
            "message": "Direct API config incomplete. Need base_url, api_key, and model. "
                       "Configure in ~/.codex/config.toml or ~/.config/amc/config.yaml (backend.direct_api section).",
        }, indent=2)

    print(f"[mission-control] Starting inline review: {repo_path}", file=sys.stderr, flush=True)
    print(f"[mission-control] Using model: {api_config.model} via {api_config.wire_api} API", file=sys.stderr, flush=True)

    engine = _get_engine()

    # Parse context if provided
    ctx_data = None
    if context:
        try:
            ctx_data = json.loads(context)
        except json.JSONDecodeError:
            ctx_data = {"intent": context}

    # Generate a short task key
    import hashlib
    task_key = hashlib.md5(f"{repo_path}:{time.time()}".encode()).hexdigest()[:8]

    async def _run_review():
        try:
            report = await asyncio.to_thread(
                engine.review_inline,
                repo_path,
                pr_url=pr_url,
                base=base,
                review_focus=focus or '',
                timeout_sec=timeout,
                context=ctx_data,
            )
            result = {
                "status": "completed",
                "passed": report.passed,
                "verdict": str(report.verdict),
                "finding_count": report.finding_count,
                "findings": [
                    {
                        "severity": f.severity,
                        "path": f.path,
                        "line": f.line,
                        "summary": f.summary,
                    }
                    for f in report.findings
                ] if report.findings else [],
                "summary": report.summary or "",
                "duration_sec": report.duration_sec,
                "model": api_config.model,
            }
            if report.error:
                result["error"] = report.error
            _background_results[task_key] = result
            print(f"[mission-control] Review {task_key} complete — verdict: {report.verdict}, findings: {report.finding_count}", file=sys.stderr, flush=True)
        except Exception as exc:
            _background_results[task_key] = {
                "status": "failed",
                "error": str(exc),
            }
            print(f"[mission-control] Review {task_key} failed: {exc}", file=sys.stderr, flush=True)

    # Launch in background
    _background_tasks[task_key] = asyncio.create_task(_run_review())

    return json.dumps({
        "status": "started",
        "task_id": task_key,
        "model": api_config.model,
        "message": f"Review started using {api_config.model} (direct API). Call get_review_status in ~60-120 seconds.",
        "estimated_seconds": 90,
    }, indent=2)


@mcp.tool()
async def get_review_status(
    task_id: Annotated[str, "Task ID returned by review_code"],
) -> str:
    """Check the status and results of a code review.

    Call this after review_code returns a task_id. If the review is still
    running, returns status="running". Once complete, returns the full
    findings and verdict.

    Typical workflow:
    1. review_code → task_id
    2. Wait 60-120 seconds
    3. get_review_status(task_id) → full results
    """
    # Check in-memory results first
    if task_id in _background_results:
        return json.dumps(_background_results[task_id], indent=2, ensure_ascii=False)

    if task_id in _background_tasks:
        task = _background_tasks[task_id]
        if not task.done():
            return json.dumps({
                "status": "running",
                "message": "Review is still in progress. Try again in 30 seconds.",
            }, indent=2)
        # Task done but result not stored (shouldn't happen)
        return json.dumps({"status": "completed", "message": "Review finished but no result captured."}, indent=2)

    return json.dumps({"status": "not_found", "message": f"No review found with task_id: {task_id}"})


@mcp.tool()
async def get_review_findings(
    repo_path: Annotated[str, "Path to the repository (used to identify the review)"],
    min_severity: Annotated[str, "Minimum severity to include: critical, high, medium, low"] = "low",
) -> str:
    """Get findings from the most recent review of a repository.

    Returns filtered findings based on minimum severity threshold.
    Useful for checking only critical/high issues.
    """
    severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    min_level = severity_order.get(min_severity.lower(), 1)

    # Search background results for matching repo
    for key, result in _background_results.items():
        if result.get("status") == "completed":
            findings = [
                f for f in result.get("findings", [])
                if severity_order.get(f.get("severity", ""), 0) >= min_level
            ]
            return json.dumps({
                "verdict": result.get("verdict", ""),
                "finding_count": len(findings),
                "findings": findings,
            }, indent=2, ensure_ascii=False)

    # Fall back to the on-disk task database.
    # NB: `tasks` rows are a single JSON `data` blob with no repo_path column,
    # so the filtering has to happen in Python rather than in SQL.
    import os

    from app.core.db import Database
    from app.services.review_result_service import ReviewResultService

    db = Database('data/amc.db')
    target = os.path.normcase(os.path.abspath(repo_path))
    matching = [
        task for task in db.list_tasks()
        if os.path.normcase(os.path.abspath(task.repo_path)) == target
    ]
    if not matching:
        return json.dumps({"error": "No review found for this repository"})

    task = max(matching, key=lambda t: t.updated_at)
    task = ReviewResultService.backfill_task(db=db, task=task)
    result = task.latest_review_result
    if not result:
        return json.dumps({"error": "Review has no results yet"})

    findings = [
        {"severity": f.severity, "path": f.path, "line": f.line, "summary": f.summary}
        for f in (result.findings or [])
        if severity_order.get(f.severity, 0) >= min_level
    ]
    return json.dumps({
        "verdict": str(result.verdict),
        "finding_count": len(findings),
        "findings": findings,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def abort_review(
    task_id: Annotated[str, "Task ID of the review to abort"],
) -> str:
    """Abort a running review.

    Use this if a review is taking too long or is no longer needed.
    """
    if task_id in _background_tasks:
        task = _background_tasks[task_id]
        if not task.done():
            task.cancel()
            _background_results[task_id] = {"status": "aborted"}
            return json.dumps({"status": "aborted", "task_id": task_id})
        return json.dumps({"status": "already_finished"})

    return json.dumps({"status": "not_found", "message": f"No active review with task_id: {task_id}"})


# ── Entry point ───────────────────────────────────────────────────────

def serve():
    """Run MCP server in stdio mode (blocking)."""
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    serve()
