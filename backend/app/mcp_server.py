"""
MCP Tool Server for Mission Control Agent Skill.

Exposes code review capabilities as MCP tools for AI agents.
Runs in stdio mode (launched via `amc mcp`).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated

from mcp.server.fastmcp import FastMCP

# ── Server setup ──────────────────────────────────────────────────────

mcp = FastMCP(
    name="mission-control",
    instructions="""Mission Control code review server.
Use the review_code tool to perform AI-powered code review on a repository.
The tool works in two modes:
- PR mode: provide a GitHub PR URL to review a pull request
- Local diff mode: omit pr_url to review uncommitted changes against the base branch

IMPORTANT behavioral rules for agents:
- Call review_code ONCE and wait for it to complete (it may take 1-5 minutes)
- Do NOT call review_code in a loop
- Use get_review_status to check on long-running reviews
- Interpret the 'passed' field to decide next action:
  - passed=true → no significant issues, proceed
  - passed=false → review findings and fix code before continuing
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
    """Perform AI-powered code review on a repository.

    Returns a JSON object with:
    - passed: bool — whether the code passes review
    - verdict: string — review verdict (approve/concern/block)
    - findings: list — issues found (severity, path, line, summary)
    - summary: string — overall review summary
    - can_continue: bool — whether more rounds could help

    Usage:
    - For PR review: provide pr_url
    - For local changes: omit pr_url (reviews uncommitted diff against base branch)
    - Set focus to narrow review scope (e.g. "security" or "error handling")
    """
    engine = _get_engine()

    # Parse context if provided
    ctx = None
    if context:
        try:
            ctx = json.loads(context)
        except json.JSONDecodeError:
            ctx = {"intent": context}

    # Run review (synchronous SDK call in thread)
    report = await asyncio.to_thread(
        engine.review,
        repo_path,
        pr_url=pr_url,
        base=base,
        review_focus=focus or '',
        max_rounds=max_rounds,
        timeout_sec=timeout,
        context=ctx,
    )

    # Build response
    result = {
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
        "can_continue": report.can_continue,
    }

    if report.error:
        result["error"] = report.error

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
