#!/usr/bin/env python3
"""Zero-dependency helper for the Mission Control Agent Skill.

Finds `amc` (or falls back to `uvx`), auto-selects a locally installed reviewer
CLI different from `--exclude` (for cross-agent review), and runs `amc review`
in structured JSON mode.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

AMC_GIT_SPEC = 'git+https://github.com/robll-v1/mission-control.git'

# Ordered preference for cross-agent code review backends.
REVIEWER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ('codex', 'codex'),
    ('claude-code', 'claude'),
    ('opencode', 'opencode'),
    ('copilot', 'gh'),
)

AGENT_ALIASES: dict[str, str] = {
    'claude': 'claude-code',
    'claude-code': 'claude-code',
    'claude_code': 'claude-code',
    'codex': 'codex',
    'codex-cli': 'codex',
    'opencode': 'opencode',
    'copilot': 'copilot',
    'gh-copilot': 'copilot',
}


def normalize_agent(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip().lower()
    if not cleaned:
        return None
    return AGENT_ALIASES.get(cleaned, cleaned)


def detect_installed_reviewers(which_fn=shutil.which) -> list[str]:
    """Return installed Mission Control backend names in priority order."""
    found: list[str] = []
    for backend, binary in REVIEWER_CANDIDATES:
        if which_fn(binary):
            found.append(backend)
    return found


def choose_reviewer(
    *,
    requested: str | None = None,
    exclude: str | None = None,
    installed: list[str] | None = None,
) -> tuple[str | None, str]:
    """Choose a reviewer backend, preferring a different agent than ``exclude``."""
    req = normalize_agent(requested)
    if req:
        return req, 'explicit'

    available = installed if installed is not None else detect_installed_reviewers()
    if not available:
        return None, 'none_installed'

    excluded = normalize_agent(exclude)
    if excluded:
        cross = [b for b in available if b != excluded]
        if cross:
            return cross[0], f'cross_agent_excluding_{excluded}'
        return available[0], 'fallback_same_agent'

    return available[0], 'first_installed'


def resolve_amc_command(which_fn=shutil.which) -> list[str] | None:
    """Resolve an executable command prefix for `amc`."""
    amc_bin = which_fn('amc')
    if amc_bin:
        return [amc_bin]
    uvx_bin = which_fn('uvx')
    if uvx_bin:
        return [uvx_bin, '--from', AMC_GIT_SPEC, 'amc']
    pipx_bin = which_fn('pipx')
    if pipx_bin:
        return [pipx_bin, 'run', '--spec', AMC_GIT_SPEC, 'amc']
    return None


def build_review_argv(
    amc_cmd: list[str],
    *,
    repo: str,
    backend: str,
    model: str | None = None,
    reasoning: str | None = None,
    focus: str | None = None,
    pr_url: str | None = None,
    base: str | None = None,
    timeout: int | None = None,
) -> list[str]:
    argv = [*amc_cmd, 'review']
    if pr_url:
        argv.append(pr_url)
    argv.extend(['--repo', str(Path(repo).expanduser().resolve())])
    argv.extend(['--backend', backend, '--format', 'json', '--exit-code'])
    if model:
        argv.extend(['--model', model])
    if reasoning:
        argv.extend(['--reasoning', reasoning])
    if focus:
        argv.extend(['--focus', focus])
    if base:
        argv.extend(['--base', base])
    if timeout is not None:
        argv.extend(['--timeout', str(timeout)])
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run a cross-agent local code review via Mission Control (amc).'
    )
    parser.add_argument('--repo', default='.', help='Path to target git repository (default: .)')
    parser.add_argument('--backend', default=None, help='Explicit reviewer backend (codex, claude-code, opencode, copilot)')
    parser.add_argument('--exclude', default=None, help='Host agent to avoid for cross-agent review (e.g. claude-code, codex)')
    parser.add_argument('--model', default=None, help='Optional model override')
    parser.add_argument('--reasoning', default=None, help='Optional reasoning effort (e.g. low, medium, high)')
    parser.add_argument('--focus', default=None, help='Optional review focus area')
    parser.add_argument('--pr', default=None, help='Optional GitHub PR URL instead of local diff')
    parser.add_argument('--base', default=None, help='Optional base branch for local diff')
    parser.add_argument('--timeout', type=int, default=None, help='Per-round timeout in seconds')
    parser.add_argument('--dry-run', action='store_true', help='Print resolved command and reviewer selection as JSON without running')
    args = parser.parse_args(argv)

    amc_cmd = resolve_amc_command()
    if not amc_cmd:
        print(
            json.dumps(
                {
                    'ok': False,
                    'error': 'Mission Control (`amc`), `uvx`, and `pipx` were not found on PATH.',
                    'install_hint': f'Install uv (https://docs.astral.sh/uv/) or run: pipx install {AMC_GIT_SPEC}',
                },
                indent=2,
            )
        )
        return 2

    backend, selection_reason = choose_reviewer(requested=args.backend, exclude=args.exclude)
    if not backend:
        print(
            json.dumps(
                {
                    'ok': False,
                    'error': 'No supported coding agent CLI found on PATH (codex, claude, opencode, gh).',
                },
                indent=2,
            )
        )
        return 2

    cmd = build_review_argv(
        amc_cmd,
        repo=args.repo,
        backend=backend,
        model=args.model,
        reasoning=args.reasoning,
        focus=args.focus,
        pr_url=args.pr,
        base=args.base,
        timeout=args.timeout,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    'ok': True,
                    'selected_backend': backend,
                    'selection_reason': selection_reason,
                    'command': cmd,
                },
                indent=2,
            )
        )
        return 0

    print(f'[mission-control skill] Reviewer: {backend} ({selection_reason})', file=sys.stderr, flush=True)
    proc = subprocess.run(cmd)
    return proc.returncode


if __name__ == '__main__':
    raise SystemExit(main())
