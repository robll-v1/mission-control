from __future__ import annotations

import os
import re
from pathlib import Path


BLOCKED_MARKER = 'PR Review Control policy blocked command during static review.'
BLOCKED_COMMANDS = (
    'go',
    'make',
    'docker',
    'docker-compose',
    'podman',
    'npm',
    'pnpm',
    'yarn',
    'pytest',
    'python',
    'python3',
    'uvicorn',
    'cargo',
    'mvn',
    'gradle',
    'java',
    'gh',
    'curl',
    'wget',
    'nohup',
)
BLOCKED_PATTERN = re.compile(
    r'(^|\s)(go\s+(test|run|build|install)\b|make\b|docker\b|docker-compose\b|podman\b|npm\b|pnpm\b|yarn\b|pytest\b|python3?\b|uvicorn\b|cargo\b|mvn\b|gradle\b|java\b|gh\b|curl\b|wget\b|nohup\b|\.\/mo-service\b)',
    re.IGNORECASE,
)


def prepare_static_review_env(runtime_root: str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    bin_dir = Path(runtime_root)
    if not bin_dir.is_absolute():
        bin_dir = bin_dir.resolve()
    bin_dir = bin_dir / 'review_policy_bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    for command in BLOCKED_COMMANDS:
        path = bin_dir / command
        path.write_text(
            '#!/usr/bin/env bash\n'
            f'echo "{BLOCKED_MARKER}" >&2\n'
            'echo "Command: $0 $*" >&2\n'
            'exit 97\n'
        )
        path.chmod(0o755)
    legacy_node = bin_dir / 'node'
    if legacy_node.exists():
        legacy_node.unlink()
    env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'
    env['AMC_STATIC_REVIEW'] = '1'
    return env


def forbidden_command_reason(command: str) -> str | None:
    if not command.strip():
        return None
    if BLOCKED_PATTERN.search(command):
        return 'runtime/build/test command is blocked in static review mode'
    return None
