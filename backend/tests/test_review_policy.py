"""Regression tests: the static-review shims must actually shadow real commands."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from app.core.review_policy import (
    BLOCKED_COMMANDS,
    BLOCKED_MARKER,
    forbidden_command_reason,
    prepare_static_review_env,
)


def test_shim_dir_is_prepended_to_path(tmp_path):
    env = prepare_static_review_env(str(tmp_path), base_env={'PATH': '/usr/bin'})
    first = env['PATH'].split(os.pathsep)[0]
    assert first.endswith('review_policy_bin')
    assert env['AMC_STATIC_REVIEW'] == '1'


@pytest.mark.parametrize('command', ['npm', 'docker', 'python'])
def test_blocked_command_resolves_to_the_shim(tmp_path, command):
    """On Windows an extensionless shim never matches PATHEXT, so the policy
    silently did nothing. A .cmd shim has to be emitted alongside it."""
    env = prepare_static_review_env(str(tmp_path), base_env={'PATH': os.environ.get('PATH', '')})
    resolved = shutil.which(command, path=env['PATH'])
    assert resolved is not None, f'{command} did not resolve at all'
    assert 'review_policy_bin' in resolved, (
        f'{command} resolved to {resolved}, not the policy shim'
    )


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows shim format')
def test_windows_shims_exist_for_every_blocked_command(tmp_path):
    prepare_static_review_env(str(tmp_path), base_env={'PATH': ''})
    bin_dir = tmp_path / 'review_policy_bin'
    missing = [c for c in BLOCKED_COMMANDS if not (bin_dir / f'{c}.cmd').exists()]
    assert missing == []


def test_shim_exits_97_and_prints_the_marker(tmp_path):
    env = prepare_static_review_env(str(tmp_path), base_env={'PATH': os.environ.get('PATH', '')})
    result = subprocess.run(
        'npm install', shell=True, env={**os.environ, 'PATH': env['PATH']},
        capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    assert result.returncode == 97
    assert BLOCKED_MARKER in (result.stderr or '')


@pytest.mark.parametrize('command', ['npm install', 'go test ./...', 'docker compose up', 'curl x'])
def test_forbidden_command_reason_flags_runtime_commands(command):
    assert forbidden_command_reason(command) is not None


@pytest.mark.parametrize('command', ['ls -la', 'grep -r foo .', 'cat README.md', ''])
def test_forbidden_command_reason_allows_read_only_commands(command):
    assert forbidden_command_reason(command) is None
