"""Regression tests: every adapter must advertise the program it actually runs."""
from __future__ import annotations

import os

import pytest

from app.adapters import AVAILABLE_BACKENDS, get_adapter
from app.core.execution import BackendUnavailableError, ExecutionService, PreflightError
from app.core.models import Task


def _task(backend: str, tmp_path) -> Task:
    return Task(title='t', repo_path=str(tmp_path), backend=backend,
                created_at=0.0, updated_at=0.0)


@pytest.mark.parametrize('backend', sorted(AVAILABLE_BACKENDS))
def test_executable_matches_launched_program(backend, tmp_path):
    """adapter.executable_name() must equal argv[0] of make_command().

    Preflight probes executable_name(); if it disagrees with what actually gets
    launched, an installed backend is rejected as "not installed".
    """
    adapter = get_adapter(backend)
    argv0 = adapter.make_command(task=_task(backend, tmp_path), prompt='p')[0]
    launched = os.path.splitext(os.path.basename(argv0))[0].lower()
    assert launched == adapter.executable_name().lower()


def test_known_executables_are_not_the_backend_key():
    """These two differ from their backend key — the original bug."""
    assert get_adapter('claude-code').executable_name() == 'claude'
    assert get_adapter('copilot').executable_name() == 'gh'


@pytest.mark.parametrize('backend', sorted(AVAILABLE_BACKENDS))
def test_probe_command_starts_with_the_declared_executable(backend, tmp_path):
    adapter = get_adapter(backend)
    probe = adapter.probe_command()
    if probe is None:
        return
    launched = os.path.splitext(os.path.basename(probe[0]))[0].lower()
    assert launched == adapter.executable_name().lower()


def test_preflight_reports_the_program_name_not_the_backend_key(tmp_path, monkeypatch):
    """A missing claude-code must complain about `claude`, not `claude-code`."""
    import app.core.execution as execution_module

    monkeypatch.setattr(execution_module.shutil, 'which', lambda *_a, **_k: None)
    monkeypatch.setattr(execution_module, 'resolve_executable', lambda name: name)

    service = ExecutionService(
        db=None, mission=None,
        adapters={'claude-code': get_adapter('claude-code')},
        worktrees=None, artifacts=None, contexts=None,
    )
    with pytest.raises(PreflightError) as excinfo:
        service._preflight_check(_task('claude-code', tmp_path))
    message = str(excinfo.value)
    assert 'claude ' in message or message.startswith('claude')
    assert 'claude-code' in message          # backend key still named, for context


def test_unknown_backend_raises_backend_unavailable_not_keyerror(tmp_path):
    """Distinct from "task not found" so the API can answer 409 rather than 404."""
    service = ExecutionService(
        db=None, mission=None, adapters={'opencode': get_adapter('opencode')},
        worktrees=None, artifacts=None, contexts=None,
    )
    with pytest.raises(BackendUnavailableError):
        service._preflight_check(_task('direct_api', tmp_path))


def test_cli_backend_map_comes_from_the_adapter_registry():
    """cli.py used to hardcode its own copy, which drifted from the engine's."""
    from app.cli import _backend_executables

    mapping = _backend_executables()
    for backend in AVAILABLE_BACKENDS:
        assert mapping[backend] == get_adapter(backend).executable_name()
