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
    PreflightError,
    RoundCancelledError,
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
    assert isinstance(outcome.get('error'), RoundCancelledError)
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

    assert isinstance(outcome.get('error'), RoundCancelledError)
    assert db.get_task(task.id).status == TaskStatus.ABORTED
    assert db.list_runs(task.id) == []
    assert not execution.is_active(task.id)
    accepted = [e for e in db.list_events(task.id) if e.kind == 'control.accepted']
    # Still preflight: probe_command() is what the gate is blocking.
    assert accepted and accepted[-1].payload.get('phase') == 'preflight'
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
    assert isinstance(outcome1.get('error'), RoundCancelledError)

    # The slot was released: a fresh start is accepted, and cancellable too.
    gate2 = threading.Event()
    execution.adapters['opencode'] = ProbeAdapter(QUICK_OK, gate=gate2)
    thread2, outcome2 = _start_in_thread(execution, task.id)
    assert _wait_until(lambda: execution.is_active(task.id))
    assert execution.abort_task(task.id) is True
    gate2.set()
    thread2.join(timeout=15)
    assert isinstance(outcome2.get('error'), RoundCancelledError)

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


# --- cancellation across prepare and the worker handoff ---------------------

def _record_status_writes(monkeypatch, db, task_id):
    """Record the status of every later save of this task."""
    writes = []
    original = db.save_task

    def recording(task_obj):
        if task_obj.id == task_id:
            writes.append(task_obj.status)
        return original(task_obj)

    monkeypatch.setattr(db, 'save_task', recording)
    return writes


def _count_agent_launches(monkeypatch, execution):
    launches = []
    adapter = execution.adapters['opencode']
    original = adapter.make_command

    def counting(**kwargs):
        launches.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(adapter, 'make_command', counting)
    return launches


def test_round_is_visible_as_preparing_while_preflight_runs(git_repo, tmp_path, isolated_config):
    """The Web UI only offers 中止 for a round it can see.

    Preflight used to run before any stage change, so for up to 150s the task
    still read as ready and the abort button stayed disabled.
    """
    gate = threading.Event()
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK, gate=gate)
    thread, outcome = _start_in_thread(execution, task.id)
    try:
        assert _wait_until(lambda: db.get_task(task.id).status == TaskStatus.INGESTING), (
            f'task still reads {db.get_task(task.id).status} during preflight'
        )
        assert db.get_task(task.id).current_stage == 'preflight'
        assert execution.abort_task(task.id) is True
    finally:
        gate.set()
        thread.join(timeout=15)
    assert isinstance(outcome.get('error'), RoundCancelledError)
    assert db.get_task(task.id).status == TaskStatus.ABORTED
    db.close()


def test_abort_during_snapshot_refresh_stays_aborted(git_repo, tmp_path, isolated_config, monkeypatch):
    """Race 1: an abort accepted while the snapshot is refreshed must stay final.

    The prepare stage used to be written unconditionally after the refresh,
    and INGESTING is allowed to leave ABORTED (that is how a new round
    starts), so the cancel was overwritten and the task was left in
    ingesting/prepare_review with no Run and no worker.
    """
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK)
    in_refresh = threading.Event()
    release = threading.Event()
    original_refresh = execution._refresh_local_snapshot

    def slow_refresh(current_task):
        in_refresh.set()
        assert release.wait(timeout=15)
        return original_refresh(current_task)

    monkeypatch.setattr(execution, '_refresh_local_snapshot', slow_refresh)
    thread, outcome = _start_in_thread(execution, task.id)
    assert in_refresh.wait(timeout=10), 'refresh never started'
    assert execution.abort_task(task.id) is True
    statuses = _record_status_writes(monkeypatch, db, task.id)
    release.set()
    thread.join(timeout=15)

    assert isinstance(outcome.get('error'), RoundCancelledError)
    final = db.get_task(task.id)
    assert (final.status, final.current_stage) == (TaskStatus.ABORTED, 'aborted')
    assert set(statuses) <= {TaskStatus.ABORTED}, f'the cancel was overwritten: {statuses}'
    assert db.list_runs(task.id) == []
    assert not execution.is_active(task.id)

    # A new round still starts and completes normally after the cancel.
    monkeypatch.setattr(execution, '_refresh_local_snapshot', original_refresh)
    run = execution.start_task(task.id)
    assert run.round_index == 1, 'the cancelled attempt must not have created a Run'
    assert _wait_until(lambda: not execution.is_active(task.id), timeout=30)
    assert db.list_runs(task.id)[0].status == 'completed'
    worktrees.cleanup_worktree(db.get_task(task.id))
    db.close()


def test_abort_before_the_worker_launches_the_agent_is_honoured(git_repo, tmp_path, isolated_config, monkeypatch):
    """Race 2: the Run exists but its agent process does not yet.

    abort_task used to answer False here ("not actively running") and the
    round then ran to completion.
    """
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK)
    worker_entered = threading.Event()
    release = threading.Event()
    original_run_task = execution._run_task

    def gated_run_task(*args, **kwargs):
        worker_entered.set()
        assert release.wait(timeout=15)
        return original_run_task(*args, **kwargs)

    monkeypatch.setattr(execution, '_run_task', gated_run_task)
    launches = _count_agent_launches(monkeypatch, execution)

    run = execution.start_task(task.id)
    assert worker_entered.wait(timeout=10), 'the worker never started'
    try:
        assert execution.abort_task(task.id) is True
    finally:
        release.set()
    assert _wait_until(lambda: not execution.is_active(task.id), timeout=15)

    assert db.get_task(task.id).status == TaskStatus.ABORTED
    [stored] = db.list_runs(task.id)
    assert (stored.id, stored.status) == (run.id, 'aborted')
    assert launches == [], 'the review agent was launched after an accepted abort'
    kinds = [e.kind for e in db.list_events(task.id)]
    assert 'review.round_started' not in kinds
    assert 'error' not in kinds, 'a cancellation is not an error'
    finished = [e for e in db.list_events(task.id) if e.kind == 'review.round_finished']
    assert finished and finished[-1].payload.get('reason') == 'aborted'
    assert execution.abort_task(task.id) is False
    db.close()


def test_abort_racing_the_run_creation_is_not_overwritten(git_repo, tmp_path, isolated_config, monkeypatch):
    """Race 2: an abort landing while the Run is created must stick.

    The handoff used to save a task object read before the abort, putting
    the task back to INGESTING; RUNNING then went through and the worker
    reviewed to completion although abort_task had returned True.
    """
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK)
    in_save = threading.Event()
    release = threading.Event()
    original_save_run = db.save_run

    def gated_save_run(run_obj):
        if not in_save.is_set():
            in_save.set()
            assert release.wait(timeout=15)
        return original_save_run(run_obj)

    monkeypatch.setattr(db, 'save_run', gated_save_run)
    thread, outcome = _start_in_thread(execution, task.id)
    assert in_save.wait(timeout=10), 'the Run was never created'
    abort_result = {}
    aborter = threading.Thread(target=lambda: abort_result.setdefault('ok', execution.abort_task(task.id)))
    aborter.start()
    time.sleep(0.2)  # let the abort reach the execution service before the Run lands
    release.set()
    thread.join(timeout=15)
    aborter.join(timeout=15)
    assert _wait_until(lambda: not execution.is_active(task.id), timeout=15)

    assert abort_result.get('ok') is True
    final = db.get_task(task.id)
    assert final.status == TaskStatus.ABORTED, f'the cancel was overwritten: {final.status}'
    assert [r.status for r in db.list_runs(task.id)] in ([], ['aborted'])
    db.close()


def test_cancelled_prepare_cleans_up_before_releasing_the_slot(git_repo, tmp_path, isolated_config, monkeypatch):
    """The worktree is released, and only then the slot.

    Releasing the slot first would let an immediate restart reserve it and
    then lose its fresh worktree to the cancelled attempt's cleanup.
    """
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, QUICK_OK)
    in_compile = threading.Event()
    release = threading.Event()
    original_compile = execution.contexts.compile_task

    def slow_compile(*args, **kwargs):
        in_compile.set()
        assert release.wait(timeout=15)
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(execution.contexts, 'compile_task', slow_compile)
    slot_held_during_cleanup = []
    original_cleanup = worktrees.cleanup_worktree

    def recording_cleanup(task_obj):
        slot_held_during_cleanup.append(execution.is_active(task.id))
        return original_cleanup(task_obj)

    monkeypatch.setattr(worktrees, 'cleanup_worktree', recording_cleanup)
    thread, outcome = _start_in_thread(execution, task.id)
    assert in_compile.wait(timeout=10), 'compile never started'
    assert execution.abort_task(task.id) is True
    release.set()
    thread.join(timeout=15)

    assert isinstance(outcome.get('error'), RoundCancelledError)
    final = db.get_task(task.id)
    assert (final.status, final.current_stage) == (TaskStatus.ABORTED, 'aborted')
    assert db.list_runs(task.id) == []
    assert not (tmp_path / 'runtime' / 'worktrees' / task.id).exists(), 'worktree leaked after a cancelled prepare'
    assert final.worktree_path is None
    assert slot_held_during_cleanup and all(slot_held_during_cleanup), (
        'the slot was released before the cancelled attempt finished cleaning up'
    )
    assert not execution.is_active(task.id)
    db.close()


def test_preflight_failure_leaves_a_visible_failed_state(git_repo, tmp_path, isolated_config):
    """Once preflight shows as INGESTING, a failure must not leave it there."""
    probe_fail = [sys.executable, '-c', 'import sys; sys.stderr.write("no credentials"); sys.exit(3)']
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, probe_fail)
    with pytest.raises(PreflightError):
        execution.start_task(task.id)

    final = db.get_task(task.id)
    assert (final.status, final.current_stage) == (TaskStatus.FAILED, 'preflight_failed')
    errors = [e for e in db.list_events(task.id) if e.kind == 'error']
    assert errors and 'no credentials' in errors[-1].payload['message']
    assert errors[-1].payload['phase'] == 'preflight'
    assert not execution.is_active(task.id)
    db.close()


def test_an_interrupted_preflight_does_not_leave_the_probe_running(git_repo, tmp_path, isolated_config, monkeypatch):
    """Ctrl+C in `amc review` while the probe runs.

    The probe runs in its own session, so the terminal's SIGINT never reaches
    it; whoever started it has to stop it.
    """
    db, mission, artifacts, worktrees, execution, task = _build(tmp_path, git_repo, SLEEP_30)
    captured = {}

    def interrupted_wait(probe_proc, **_kwargs):
        captured['proc'] = probe_proc
        raise KeyboardInterrupt

    monkeypatch.setattr(execution, '_await_probe', interrupted_wait)
    try:
        with pytest.raises(KeyboardInterrupt):
            execution.start_task(task.id)
        assert captured['proc'].returncode is not None, 'the probe outlived the interrupted start'
        assert not execution.is_active(task.id)
    finally:
        if 'proc' in captured and captured['proc'].poll() is None:
            captured['proc'].kill()
            captured['proc'].wait(timeout=5)
    db.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='process groups are POSIX-only')
def test_terminate_never_signals_a_process_that_was_already_reaped(monkeypatch):
    """Abort and the probe wait can both stop the probe.

    Once one of them has reaped it, its pid may belong to an unrelated new
    process group, so the other must not send a signal at all.
    """
    import subprocess

    import app.core.execution as execution_module

    proc = subprocess.Popen([sys.executable, '-c', ''], start_new_session=True)
    proc.wait(timeout=10)
    signalled = []
    monkeypatch.setattr(execution_module.os, 'killpg', lambda *args: signalled.append(args))
    ExecutionService._terminate_process(proc)
    assert signalled == []
