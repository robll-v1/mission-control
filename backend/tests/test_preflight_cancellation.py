"""Cancelling a round while preflight is still running.

Preflight runs synchronously on the start caller's thread and its probe can
block for up to 150s. These tests pin the required behaviour: abort during
that window succeeds, terminates exactly the probe subprocess, keeps the
task ABORTED, never creates a review round, frees the slot for retries, and
still reports timeout/probe failures correctly. No real agent is involved —
probes are plain `python -c` subprocesses.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import (
    ActiveProcess,
    ExecutionService,
    PreflightCancelledError,
    PreflightError,
)
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source

SLEEP_30 = [sys.executable, '-c', 'import time; time.sleep(30)']
QUICK_OK = [sys.executable, '-c', '']


class ProbeAdapter:
    """Adapter whose probe is whatever the test needs; the round never runs."""

    name = 'opencode'

    def __init__(self, probe_cmd, gate: threading.Event | None = None):
        self._probe_cmd = probe_cmd
        self._gate = gate

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        if self._gate is not None:
            self._gate.wait(timeout=15)
        return self._probe_cmd

    def make_command(self, **_kwargs):
        return QUICK_OK

    def parse_stdout_line(self, _line):
        return []

    def parse_stderr_line(self, _line):
        return []


def _build(tmp_path, git_repo, probe_cmd, gate=None):
    (git_repo / 'a.txt').write_text('review me\n', encoding='utf-8')
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={'opencode': ProbeAdapter(probe_cmd, gate)},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=ContextCompiler(artifacts),
    )
    task = mission.create_task(
        title=local.title,
        repo_path=str(git_repo),
        source_type='local_diff',
        backend='opencode',
        pr_base_ref=local.base_branch,
        review_paths=local.changed_files,
        local_head_sha=local.head_sha,
        local_snapshot_sha=local.snapshot_sha,
    )
    artifacts.write_text(task.id, 'context/local_diff.patch', local.diff_text)
    return db, mission, artifacts, worktrees, execution, task


def _start_in_thread(execution, task_id):
    outcome = {}

    def starter():
        try:
            outcome['run'] = execution.start_task(task_id)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome['error'] = exc

    thread = threading.Thread(target=starter)
    thread.start()
    return thread, outcome


def _wait_until(predicate, timeout=15, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _registered_probe(execution, task_id):
    """The probe Popen once preflight registered it, else None."""
    with execution._lock:
        active = execution._active.get(task_id)
    return active.process if active is not None else None


def test_abort_during_preflight_succeeds_and_kills_the_probe(git_repo, tmp_path, isolated_config):
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, SLEEP_30)
    thread, outcome = _start_in_thread(execution, task.id)

    assert _wait_until(lambda: _registered_probe(execution, task.id) is not None), 'probe never registered'
    probe_proc = _registered_probe(execution, task.id)

    t0 = time.perf_counter()
    assert execution.abort_task(task.id) is True
    abort_latency = time.perf_counter() - t0
    thread.join(timeout=15)
    unwind_latency = time.perf_counter() - t0

    assert abort_latency < 2, f'abort took {abort_latency:.2f}s'
    assert unwind_latency < 15, f'cancellation unwound in {unwind_latency:.2f}s; the probe was not killed'
    assert isinstance(outcome.get('error'), PreflightCancelledError)
    assert probe_proc.poll() is not None, 'the probe subprocess survived the abort'
    assert probe_proc.returncode != 0

    assert db.get_task(task.id).status == TaskStatus.ABORTED  # kept, not FAILED
    assert db.get_task(task.id).current_stage == 'aborted'
    assert db.list_runs(task.id) == []  # no review round was created
    assert not execution.is_active(task.id)  # slot released
    kinds = [e.kind for e in db.list_events(task.id)]
    assert 'control.accepted' in kinds
    assert 'review.round_started' not in kinds
    worktrees.cleanup_worktree(task)
    db.close()


def test_abort_before_the_probe_spawns_is_accepted(git_repo, tmp_path, isolated_config):
    gate = threading.Event()
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK, gate=gate)
    thread, outcome = _start_in_thread(execution, task.id)

    # Slot reserved, probe_command still blocked on the gate: no process yet.
    assert _wait_until(lambda: execution.is_active(task.id)), 'slot never reserved'
    assert _registered_probe(execution, task.id) is None

    assert execution.abort_task(task.id) is True
    gate.set()
    thread.join(timeout=15)

    assert isinstance(outcome.get('error'), PreflightCancelledError)
    assert db.get_task(task.id).status == TaskStatus.ABORTED
    assert db.list_runs(task.id) == []
    assert not execution.is_active(task.id)
    accepted = [e for e in db.list_events(task.id) if e.kind == 'control.accepted']
    assert accepted and accepted[-1].payload.get('phase') == 'prepare'
    db.close()


def test_probe_failure_raises_preflighterror_and_slot_is_free_for_retry(git_repo, tmp_path, isolated_config):
    probe_fail = [sys.executable, '-c', 'import sys; sys.stderr.write("boom"); sys.exit(3)']
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, probe_fail)
    thread, outcome = _start_in_thread(execution, task.id)
    thread.join(timeout=15)

    assert isinstance(outcome.get('error'), PreflightError)
    assert 'exit 3' in str(outcome['error'])
    assert not execution.is_active(task.id), 'a failed probe must release the slot'

    # Retry with a healthy probe: the round must start and complete normally.
    execution.adapters['opencode'] = ProbeAdapter(QUICK_OK)
    run = execution.start_task(task.id)
    assert run.round_index == 1  # the failed attempt created no Run at all
    assert _wait_until(lambda: db.list_runs(task.id)[0].status != 'running', timeout=30)
    assert db.list_runs(task.id)[0].status == 'completed'
    assert _wait_until(lambda: not execution.is_active(task.id), timeout=10)
    worktrees.cleanup_worktree(task)
    db.close()


def test_probe_timeout_terminates_the_probe(git_repo, tmp_path, isolated_config):
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, SLEEP_30)
    active = ActiveProcess(task_id=task.id, run_id=None, process=None, worker=None)
    # Direct call: occupy the slot manually so the probe registers there.
    with execution._lock:
        execution._active[task.id] = active
    outcome = {}

    def checker():
        try:
            execution._preflight_check(
                task, adapter=execution.adapters['opencode'], probe_timeout_sec=1,
            )
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcome['error'] = exc

    thread = threading.Thread(target=checker)
    thread.start()
    assert _wait_until(lambda: active.process is not None), 'probe never registered'
    probe_proc = active.process
    thread.join(timeout=15)

    assert isinstance(outcome.get('error'), PreflightError)
    assert 'timed out after 1s' in str(outcome['error'])
    assert probe_proc.poll() is not None, 'the timed-out probe survived'
    assert active.process is None, 'registration was not cleaned up'
    with execution._lock:
        execution._active.pop(task.id, None)
    db.close()


def test_concurrent_start_abort_and_restart_never_lose_the_slot(git_repo, tmp_path, isolated_config):
    gate = threading.Event()
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK, gate=gate)
    thread1, outcome1 = _start_in_thread(execution, task.id)
    assert _wait_until(lambda: execution.is_active(task.id))

    # A second start while the slot is held must be rejected.
    with pytest.raises(RuntimeError, match='already in progress'):
        execution.start_task(task.id)

    assert execution.abort_task(task.id) is True
    gate.set()
    thread1.join(timeout=15)
    assert isinstance(outcome1.get('error'), PreflightCancelledError)

    # The slot was released: a fresh start is accepted, and cancellable too.
    gate2 = threading.Event()
    execution.adapters['opencode'] = ProbeAdapter(QUICK_OK, gate=gate2)
    thread2, outcome2 = _start_in_thread(execution, task.id)
    assert _wait_until(lambda: execution.is_active(task.id))
    assert execution.abort_task(task.id) is True
    gate2.set()
    thread2.join(timeout=15)
    assert isinstance(outcome2.get('error'), PreflightCancelledError)

    assert db.list_runs(task.id) == []
    assert db.get_task(task.id).status == TaskStatus.ABORTED
    assert db.get_task(task.id).status != TaskStatus.FAILED
    assert not execution.is_active(task.id)
    stages = [e.payload.get('stage') for e in db.list_events(task.id) if e.kind == 'task.stage_changed']
    assert 'review_failed' not in stages
    db.close()


def test_abort_without_any_activity_returns_false(tmp_path):
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    task = mission.create_task(title='idle', repo_path=str(tmp_path))
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={},
        worktrees=WorktreeManager(str(tmp_path / 'runtime')),
        artifacts=artifacts,
        contexts=ContextCompiler(artifacts),
    )
    assert execution.abort_task(task.id) is False
    db.close()
