"""Pure cross-agent reviewer selection policy.

Decoupled from CLI, MCP, HTTP, and process detection so any entrypoint
(MCP, CLI, Skill, future cross-model verification) can share one deterministic,
side-effect-free selection contract.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

REVIEWER_PRIORITY: tuple[str, ...] = (
    'codex',
    'claude-code',
    'opencode',
    'copilot',
)

AGENT_ALIASES: dict[str, str] = {
    'claude': 'claude-code',
    'claude-code': 'claude-code',
    'claude_code': 'claude-code',
    'codex': 'codex',
    'codex-cli': 'codex',
    'codex_cli': 'codex',
    'opencode': 'opencode',
    'open-code': 'opencode',
    'copilot': 'copilot',
    'gh-copilot': 'copilot',
    'github-copilot': 'copilot',
}


@dataclass(frozen=True)
class ReviewerSelection:
    backend: str | None
    reason: str
    note: str | None = None


def normalize_agent_name(name: str | None) -> str | None:
    """Normalize user/host agent aliases to canonical Mission Control backend keys."""
    if not name or not isinstance(name, str):
        return None
    cleaned = name.strip().lower()
    if not cleaned:
        return None
    return AGENT_ALIASES.get(cleaned, cleaned)


def _order_installed(installed: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in installed:
        key = normalize_agent_name(raw)
        if key and key not in seen:
            seen.add(key)
            normalized.append(key)
    rank = {name: idx for idx, name in enumerate(REVIEWER_PRIORITY)}
    return sorted(normalized, key=lambda name: (rank.get(name, len(REVIEWER_PRIORITY)), name))


def select_reviewer_backend(
    *,
    requested: str | None = None,
    exclude: str | None = None,
    installed: Iterable[str] = (),
    configured_default: str | None = None,
) -> ReviewerSelection:
    """Select a reviewer backend using Option A (prefer cross-agent, honest fallback).

    Rules:
    1. Explicit ``requested`` always wins (``reason="explicit"``).
    2. With ``exclude``, pick the highest-priority installed CLI different from
       ``exclude`` (``reason="cross_agent_excluding_<host>"``).
    3. If only the excluded CLI is installed, fall back honestly to running an
       isolated fresh-context review on that CLI (``reason="fallback_same_agent"``)
       with an explanatory ``note``.
    4. Without ``exclude``, prefer ``configured_default`` when installed, else
       the highest-priority installed CLI.
    5. When no supported CLI is installed, return ``backend=None`` and
       ``reason="none_installed"``.
    """
    explicit = normalize_agent_name(requested)
    if explicit:
        return ReviewerSelection(backend=explicit, reason='explicit')

    ordered = _order_installed(installed)
    if not ordered:
        return ReviewerSelection(
            backend=None,
            reason='none_installed',
            note=(
                'No supported coding agent CLI detected on PATH '
                '(codex, claude, opencode, gh copilot).'
            ),
        )

    excluded = normalize_agent_name(exclude)
    if excluded:
        alternate = [name for name in ordered if name != excluded]
        if alternate:
            return ReviewerSelection(
                backend=alternate[0],
                reason=f'cross_agent_excluding_{excluded}',
            )
        fallback = ordered[0]
        return ReviewerSelection(
            backend=fallback,
            reason='fallback_same_agent',
            note=(
                f'Only {fallback} was detected locally; running an isolated '
                f'fresh-context review with {fallback}. Install a second CLI '
                '(e.g. codex, claude, or opencode) for cross-model review.'
            ),
        )

    default_key = normalize_agent_name(configured_default)
    if default_key and default_key in ordered:
        return ReviewerSelection(backend=default_key, reason='configured_default')

    return ReviewerSelection(backend=ordered[0], reason='first_installed')
