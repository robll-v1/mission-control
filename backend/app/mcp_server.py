"""
MCP Tool Server for Mission Control Agent Skill.

Exposes code review capabilities as MCP tools for AI agents.
Runs in stdio mode (launched via `amc mcp`).
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
2. Wait 30-60 seconds (reviews take time)
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
        _engine = ReviewEngine(language='en', backend='opencode')
    return _engine


# Track active reviews for status queries
_active_reviews: dict[str, dict] = {}


# ── Tools ─────────────────────────────────────────────────────────────

# Background review tasks
_background_tasks: dict[str, asyncio.Task] = {}
_background_results: dict[str, dict] = {}


@mcp.tool()
async def review_code(
    repo_path: Annotated[str, "Absolute path to the git repository to review"],
    pr_url: Annotated[str | None, "GitHub PR URL (omit for local diff mode)"] = None,
    base: Annotated[str | None, "Base branch for local diff (auto-detects if omitted)"] = None,
    focus: Annotated[str | None, "Review focus area (e.g. 'security', 'performance', 'correctness')"] = None,
    max_rounds: Annotated[int, "Maximum review rounds (default: 1)"] = 1,
    timeout: Annotated[int, "Per-round timeout in seconds (default: 600)"] = 600,
    context: Annotated[str | None, "Additional context (requirements, intent) as JSON string"] = None,
) -> str:
    """Start an AI-powered code review on a repository (non-blocking).

    Returns immediately with a task_id. Use get_review_status(task_id) to poll
    for results. Reviews typically take 30-90 seconds.

    Usage:
    1. Call review_code → get task_id
    2. Wait ~30s, then call get_review_status with the task_id
    3. If status is "completed", findings are included in the response
    """
    print(f"[mission-control] Starting review: {repo_path}", file=sys.stderr, flush=True)
    print(f"[mission-control] Mode: {'PR' if pr_url else 'local-diff'} | Max rounds: {max_rounds}", file=sys.stderr, flush=True)

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
                engine.review,
                repo_path,
                pr_url=pr_url,
                base=base,
                review_focus=focus or '',
                max_rounds=max_rounds,
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
                "rounds_executed": report.rounds_executed,
                "duration_sec": report.duration_sec,
            }
            if report.error:
                result["error"] = report.error
            _background_results[task_key] = result
            print(f"[mission-control] Review {task_key} complete — verdict: {report.verdict}", file=sys.stderr, flush=True)
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
        "message": "Review started. Call get_review_status with this task_id in ~30-60 seconds to get results.",
        "estimated_seconds": 60,
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
    2. Wait 30-60 seconds
    3. get_review_status(task_id) → full results
    """
    # Check in-memory background task first
    if task_id in _background_results:
        return json.dumps(_background_results[task_id], indent=2, ensure_ascii=False)

    if task_id in _background_tasks:
        task = _background_tasks[task_id]
        if not task.done():
            return json.dumps({
                "status": "running",
                "message": "Review is still in progress. Try again in 30 seconds.",
            }, indent=2)
        # Task done but result not stored (shouldn't happen, but handle gracefully)
        return json.dumps({"status": "completed", "message": "Review finished but no result captured."}, indent=2)

    # Fall back to DB lookup
    from app.services.database import Database
    db = Database()
    tasks = db.query(
        "SELECT id, status, created_at FROM tasks WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
        (f"%{task_id}%",),
    )

    if not tasks:
        return json.dumps({"status": "not_found", "message": f"No review found with task_id: {task_id}"})

    task_row = tasks[0]
    result = {"task_id": task_row["id"], "status": task_row["status"]}

    # If completed, include findings
    if task_row["status"] == "completed":
        from app.services.review_result_service import ReviewResultService
        full_task = db.get_task(task_row["id"])
        if full_task:
            full_task = ReviewResultService.backfill_task(db=db, task=full_task)
            if full_task.latest_review_result:
                rr = full_task.latest_review_result
                result["verdict"] = str(rr.verdict)
                result["finding_count"] = rr.finding_count
                result["findings"] = [
                    {"severity": f.severity, "path": f.path, "line": f.line, "summary": f.summary}
                    for f in (rr.findings or [])
                ]
                result["summary"] = rr.summary or ""

    return json.dumps(result, indent=2, ensure_ascii=False)


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

    engine = _get_engine()

    # Get most recent task for this repo
    from app.services.database import Database
    db = Database()
    tasks = db.query(
        "SELECT id FROM tasks WHERE repo_path = ? ORDER BY created_at DESC LIMIT 1",
        (repo_path,),
    )

    if not tasks:
        return json.dumps({"error": "No review found for this repository"})

    task_id = tasks[0]["id"]
    from app.services.review_result_service import ReviewResultService
    result_svc = ReviewResultService(db)
    result = result_svc.get_result(task_id)

    if not result:
        return json.dumps({"error": "Review has no results yet"})

    findings = [
        {
            "severity": f.severity,
            "path": f.path,
            "line": f.line,
            "summary": f.summary,
        }
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
    repo_path: Annotated[str, "Path to the repository whose review to abort"],
) -> str:
    """Abort a running review for the specified repository.

    Use this if a review is taking too long or is no longer needed.
    """
    from app.services.database import Database
    from app.core.models import TaskStatus
    db = Database()

    tasks = db.query(
        "SELECT id, status FROM tasks WHERE repo_path = ? ORDER BY created_at DESC LIMIT 1",
        (repo_path,),
    )

    if not tasks:
        return json.dumps({"error": "No review found for this repository"})

    task = tasks[0]
    if task["status"] in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.ABORTED.value):
        return json.dumps({"status": "already_finished", "final_status": task["status"]})

    db.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (TaskStatus.ABORTED.value, task["id"]),
    )

    return json.dumps({"status": "aborted", "task_id": task["id"]})


# ── Entry point ───────────────────────────────────────────────────────

def serve():
    """Run MCP server in stdio mode (blocking)."""
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    serve()
