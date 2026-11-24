"""Regression tests for cross-platform subprocess tree termination.

Covers:
- Parent exits on SIGTERM (or ignores SIGTERM) while a grandchild ignores
  SIGTERM and holds inherited stdout/stderr open.
- Shared reap ownership: concurrent ``terminate_process_tree`` and
  ``wait_for_process_exit`` never race to reap the leader before ``SIGKILL``.
- macOS Python 3.11/3.12 compatibility (when ``os.waitid`` does not exist).
- Restricted sandbox fallback (when process-table inspection is denied).
- Both preflight cancellation, mid-run cancellation, and mid-run idle timeout.
- Windows Job Object teardown, non-zero ``taskkill /T`` without premature
  ``proc.terminate()``, and missing ``taskkill`` fallback.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService, RoundCancelledError
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.core.proc import (
    _state_for_proc,
    mark_termination_requested,
    register_managed_process,
    spawn_managed_process,
    terminate_process_tree,
    wait_for_process_exit,
)
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source


def _is_pid_alive_non_zombie(pid: int) -> bool:
    """Return True if ``pid`` exists and is NOT a zombie."""
    proc_stat = Path(f'/proc/{pid}/stat')
    if proc_stat.exists():
        try:
            text = proc_stat.read_text(encoding='utf-8', errors='replace')
            rparen = text.rfind(')')
            if rparen != -1:
                fields = text[rparen + 2:].split()
                if fields:
                    return fields[0] != 'Z'
        except OSError:
            return False
    try:
        res = subprocess.run(
            ['ps', '-o', 'stat=', '-p', str(pid)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return not res.stdout.strip().startswith('Z')
        if res.returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _stubborn_grandchild_script(
    pid_file: Path,
    *,
    parent_exits_on_sigterm: bool = True,
    stop_file: Path | None = None,
) -> str:
    """Spawn a SIGTERM-ignoring grandchild holding stdout open.

    ``stop_file`` lets test teardown stop a leaked grandchild without relying
    solely on signalling a bare PID.
    """
    if parent_exits_on_sigterm:
        parent_handler = 'signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))'
    else:
        parent_handler = 'signal.signal(signal.SIGTERM, signal.SIG_IGN)'
    stop_lines = (
        f'    if pathlib.Path({str(stop_file)!r}).exists():\n        break\n'
        if stop_file is not None
        else ''
    )
    child_code = (
        'import os, pathlib, signal, time\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")\n'
        'deadline = time.monotonic() + 60\n'
        'while time.monotonic() < deadline:\n'
        f'{stop_lines}'
        '    time.sleep(0.05)\n'
    )
    return (
        'import signal, subprocess, sys, time\n'
        f'{parent_handler}\n'
        f'child = subprocess.Popen([sys.executable, "-c", {child_code!r}])\n'
        'print(f"spawned {child.pid}", flush=True)\n'
        'time.sleep(60)\n'
    )


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
@pytest.mark.parametrize('parent_exits_on_sigterm', [True, False])
def test_terminate_process_tree_kills_sigterm_ignoring_grandchild(tmp_path, parent_exits_on_sigterm):
    pid_file = tmp_path / 'grandchild.pid'
    stop_file = tmp_path / 'stop.flag'
    script = _stubborn_grandchild_script(
        pid_file, parent_exits_on_sigterm=parent_exits_on_sigterm, stop_file=stop_file
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    register_managed_process(proc)
    grandchild_pid: int | None = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())
        assert _is_pid_alive_non_zombie(grandchild_pid)

        terminate_process_tree(proc, grace_sec=0.3)

        assert proc.returncode is not None, 'leader was not reaped'
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'grandchild {grandchild_pid} survived terminate_process_tree'
        )
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_terminate_process_tree_kills_grandchild_without_os_waitid(tmp_path, monkeypatch):
    """Simulate macOS Python 3.11/3.12 where os.waitid does not exist."""
    import app.core.proc as proc_module

    monkeypatch.setattr(proc_module, '_probe_leader_without_reap', lambda _pid: 'unsupported')

    pid_file = tmp_path / 'grandchild_no_waitid.pid'
    stop_file = tmp_path / 'stop_no_waitid.flag'
    script = _stubborn_grandchild_script(pid_file, parent_exits_on_sigterm=True, stop_file=stop_file)
    proc = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    register_managed_process(proc)
    grandchild_pid: int | None = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())
        assert _is_pid_alive_non_zombie(grandchild_pid)

        terminate_process_tree(proc, grace_sec=0.3)

        assert proc.returncode is not None
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'grandchild {grandchild_pid} leaked without os.waitid'
        )
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
@pytest.mark.parametrize('ps_denied', [False, True])
def test_terminate_process_tree_fast_path_for_cooperative_process(monkeypatch, ps_denied):
    import app.core.proc as proc_module

    if ps_denied:
        monkeypatch.setattr(proc_module, '_pgroup_has_live_members', lambda _pgid, **_kw: (None, False))

    proc = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    register_managed_process(proc)
    try:
        t0 = time.monotonic()
        terminate_process_tree(proc, grace_sec=5.0)
        elapsed = time.monotonic() - t0
        assert proc.returncode is not None
        assert elapsed < 2.0, f'cooperative process waited {elapsed:.2f}s (ps_denied={ps_denied})'
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_terminate_and_wait_for_exit_share_ownership_without_reap_race(tmp_path, monkeypatch):
    """A concurrent wait_for_process_exit must never reap while teardown holds ownership."""
    pid_file = tmp_path / 'race_grandchild.pid'
    stop_file = tmp_path / 'race_stop.flag'
    script = _stubborn_grandchild_script(pid_file, parent_exits_on_sigterm=True, stop_file=stop_file)
    proc = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    register_managed_process(proc)

    real_killpg = os.killpg
    signalled_after_reap: list[tuple[int, int]] = []
    sigterm_sent = threading.Event()

    def tracking_killpg(pgid, sig):
        if proc.returncode is not None:
            signalled_after_reap.append((pgid, sig))
        res = real_killpg(pgid, sig)
        if sig == signal.SIGTERM:
            sigterm_sent.set()
        return res

    monkeypatch.setattr('app.core.proc.os.killpg', tracking_killpg)

    errors: list[Exception] = []
    wait_result: dict[str, int] = {}

    def terminator():
        try:
            terminate_process_tree(proc, grace_sec=0.3)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def waiter():
        try:
            assert sigterm_sent.wait(timeout=5), 'SIGTERM was never sent'
            # Immediately attempt to reap while teardown is in its grace window.
            wait_result['rc'] = wait_for_process_exit(proc, timeout=10)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    grandchild_pid: int | None = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())

        t_term = threading.Thread(target=terminator)
        t_wait = threading.Thread(target=waiter)
        t_term.start()
        t_wait.start()
        t_term.join(timeout=10)
        t_wait.join(timeout=10)

        assert not t_term.is_alive(), 'terminator thread hung'
        assert not t_wait.is_alive(), 'waiter thread hung'
        assert errors == []
        assert signalled_after_reap == [], f'signalled after reap: {signalled_after_reap}'
        assert proc.returncode is not None
        assert wait_result.get('rc') == proc.returncode
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'grandchild {grandchild_pid} leaked under concurrent wait_for_process_exit'
        )
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def test_terminate_process_tree_windows_job_object_and_no_premature_terminate(monkeypatch):
    import app.core.proc as proc_module

    calls: list[list[str]] = []
    job_calls: list[tuple[str, int]] = []

    class FakeProc:
        pid = 42424
        returncode = None
        _handle = 999

        def terminate(self):
            calls.append(['terminate'])

        def kill(self):
            calls.append(['kill'])
            self.returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 1
            return 1

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        # Simulate taskkill /T failing with non-zero exit code.
        return subprocess.CompletedProcess(cmd, 128, '', 'not found')

    monkeypatch.setattr(proc_module.sys, 'platform', 'win32')
    monkeypatch.setattr(proc_module.subprocess, 'run', fake_run)
    monkeypatch.setattr(proc_module, '_create_and_assign_windows_job', lambda _p: (777, None))
    monkeypatch.setattr(
        proc_module,
        '_terminate_windows_job',
        lambda handle: (job_calls.append(('terminate', handle)) or True),
    )
    monkeypatch.setattr(
        proc_module,
        '_close_windows_job',
        lambda handle: job_calls.append(('close', handle)),
    )

    fake = FakeProc()
    register_managed_process(fake)  # type: ignore[arg-type]
    t0 = time.monotonic()
    terminate_process_tree(fake, grace_sec=5.0)  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0

    # Must NEVER call proc.terminate() prematurely when taskkill /T fails, and
    # must not burn the full grace window after taskkill /T already rejected it.
    assert elapsed < 1.0, f'rejected gentle taskkill /T still burned {elapsed:.2f}s of grace window'
    assert ['terminate'] not in calls
    assert ['taskkill', '/T', '/PID', '42424'] in calls
    assert ('terminate', 777) in job_calls
    assert ('close', 777) in job_calls
    assert _state_for_proc(fake).job_handle is None  # type: ignore[arg-type]


def test_terminate_process_tree_windows_missing_taskkill_falls_back_to_kill(monkeypatch):
    import app.core.proc as proc_module

    calls: list[str] = []

    class FakeProc:
        pid = 51515
        returncode = None

        def terminate(self):
            calls.append('terminate')

        def kill(self):
            calls.append('kill')
            self.returncode = 1

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is not None:
                return self.returncode
            raise subprocess.TimeoutExpired(cmd='fake', timeout=timeout or 0)

    def missing_taskkill(*_args, **_kwargs):
        raise FileNotFoundError('taskkill')

    monkeypatch.setattr(proc_module.sys, 'platform', 'win32')
    monkeypatch.setattr(proc_module.subprocess, 'run', missing_taskkill)

    fake = FakeProc()
    terminate_process_tree(fake, grace_sec=0.05)  # type: ignore[arg-type]

    assert 'terminate' not in calls
    assert 'kill' in calls
    assert fake.returncode == 1


class _GrandchildAdapter:
    """Adapter that spawns a SIGTERM-exiting parent + SIGTERM-ignoring grandchild."""

    name = 'opencode'

    def __init__(self, *, probe_script: str | None = None, run_script: str | None = None):
        self._probe_script = probe_script
        self._run_script = run_script

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return [sys.executable, '-c', self._probe_script] if self._probe_script else None

    def make_command(self, **_kwargs):
        return [sys.executable, '-c', self._run_script or '']

    def parse_stdout_line(self, _line):
        return []

    def parse_stderr_line(self, _line):
        return []


def _make_execution(tmp_path, git_repo, adapter):
    (git_repo / 'a.txt').write_text('review me\n', encoding='utf-8')
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={'opencode': adapter},
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
    return db, worktrees, execution, task


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_preflight_abort_kills_sigterm_ignoring_grandchild(git_repo, tmp_path, isolated_config):
    pid_file = tmp_path / 'probe_grandchild.pid'
    stop_file = tmp_path / 'probe_stop.flag'
    adapter = _GrandchildAdapter(
        probe_script=_stubborn_grandchild_script(pid_file, parent_exits_on_sigterm=True, stop_file=stop_file)
    )
    db, worktrees, execution, task = _make_execution(tmp_path, git_repo, adapter)

    outcome: dict[str, object] = {}

    def starter():
        try:
            outcome['run'] = execution.start_task(task.id)
        except Exception as exc:  # noqa: BLE001
            outcome['error'] = exc

    thread = threading.Thread(target=starter)
    thread.start()
    grandchild_pid: int | None = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())
        assert _is_pid_alive_non_zombie(grandchild_pid)

        assert execution.abort_task(task.id) is True
        thread.join(timeout=15)
        assert not thread.is_alive()

        assert isinstance(outcome.get('error'), RoundCancelledError)
        assert db.get_task(task.id).status == TaskStatus.ABORTED
        assert not execution.is_active(task.id)
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'preflight grandchild {grandchild_pid} leaked after abort'
        )
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        worktrees.cleanup_worktree(task)
        db.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_running_round_abort_kills_sigterm_ignoring_grandchild(git_repo, tmp_path, isolated_config):
    pid_file = tmp_path / 'run_grandchild.pid'
    stop_file = tmp_path / 'run_stop.flag'
    adapter = _GrandchildAdapter(
        run_script=_stubborn_grandchild_script(pid_file, parent_exits_on_sigterm=True, stop_file=stop_file)
    )
    db, worktrees, execution, task = _make_execution(tmp_path, git_repo, adapter)

    grandchild_pid: int | None = None
    try:
        execution.start_task(task.id)
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())
        assert _is_pid_alive_non_zombie(grandchild_pid)

        assert execution.abort_task(task.id) is True
        assert _wait_until(lambda: not execution.is_active(task.id), timeout=15), (
            'worker did not release slot after aborting a round with stubborn grandchild'
        )
        assert db.get_task(task.id).status == TaskStatus.ABORTED
        assert db.list_runs(task.id)[0].status == 'aborted'
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'running-round grandchild {grandchild_pid} leaked after abort'
        )
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        worktrees.cleanup_worktree(task)
        db.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_running_round_idle_timeout_kills_sigterm_ignoring_grandchild(git_repo, tmp_path, isolated_config, monkeypatch):
    pid_file = tmp_path / 'idle_grandchild.pid'
    stop_file = tmp_path / 'idle_stop.flag'
    adapter = _GrandchildAdapter(
        run_script=_stubborn_grandchild_script(pid_file, parent_exits_on_sigterm=True, stop_file=stop_file)
    )
    db, worktrees, execution, task = _make_execution(tmp_path, git_repo, adapter)
    monkeypatch.setattr(execution, '_idle_timeout_seconds', lambda _repo: 1)

    grandchild_pid: int | None = None
    try:
        execution.start_task(task.id)
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())
        assert _is_pid_alive_non_zombie(grandchild_pid)

        assert _wait_until(lambda: not execution.is_active(task.id), timeout=15), (
            'worker did not exit after idle timeout with stubborn grandchild'
        )
        assert db.get_task(task.id).status == TaskStatus.FAILED
        assert db.list_runs(task.id)[0].exit_code == -2
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'idle-timed-out grandchild {grandchild_pid} leaked'
        )
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        worktrees.cleanup_worktree(task)
        db.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_abort_kills_detached_pipe_grandchild_when_parent_exits_before_terminate(
    git_repo, tmp_path, isolated_config, monkeypatch
):
    """Exact race reproduced in review: abort accepted, then parent exits and
    pipes close (grandchild uses DEVNULL) BEFORE _terminate_process starts.

    Because abort_task claims teardown ownership under self._lock via
    mark_termination_requested, wait_for_process_exit yields instead of reaping
    the parent, and _terminate_process still kills the grandchild.
    """
    pid_file = tmp_path / 'detached_grandchild.pid'
    stop_file = tmp_path / 'detached_stop.flag'
    exit_parent_file = tmp_path / 'exit_parent.flag'

    child_code = (
        'import os, pathlib, signal, time\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")\n'
        'deadline = time.monotonic() + 60\n'
        'while time.monotonic() < deadline:\n'
        f'    if pathlib.Path({str(stop_file)!r}).exists():\n'
        '        break\n'
        '    time.sleep(0.05)\n'
    )
    parent_script = (
        'import pathlib, subprocess, sys, time\n'
        f'subprocess.Popen([sys.executable, "-c", {child_code!r}], '
        'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        'deadline = time.monotonic() + 30\n'
        'while time.monotonic() < deadline:\n'
        f'    if pathlib.Path({str(exit_parent_file)!r}).exists():\n'
        '        sys.exit(0)\n'
        '    time.sleep(0.02)\n'
    )

    adapter = _GrandchildAdapter(run_script=parent_script)
    db, worktrees, execution, task = _make_execution(tmp_path, git_repo, adapter)

    import app.core.execution as execution_module

    orig_append_event = execution.mission.append_event
    abort_event_written = threading.Event()
    release_abort = threading.Event()
    worker_entered_wait = threading.Event()

    def paused_append_event(*args, **kwargs):
        result = orig_append_event(*args, **kwargs)
        if kwargs.get('kind') == 'control.accepted':
            abort_event_written.set()
            release_abort.wait(timeout=5)
        return result

    orig_wait_for_exit = execution_module.wait_for_process_exit

    def observed_wait_for_exit(proc, *args, **kwargs):
        worker_entered_wait.set()
        return orig_wait_for_exit(proc, *args, **kwargs)

    monkeypatch.setattr(execution.mission, 'append_event', paused_append_event)
    monkeypatch.setattr(execution_module, 'wait_for_process_exit', observed_wait_for_exit)

    grandchild_pid: int | None = None
    abort_result: dict[str, object] = {}
    try:
        execution.start_task(task.id)
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())
        assert _is_pid_alive_non_zombie(grandchild_pid)

        def do_abort():
            abort_result['ok'] = execution.abort_task(task.id)

        t_abort = threading.Thread(target=do_abort)
        t_abort.start()
        assert abort_event_written.wait(timeout=5), 'abort_task never wrote control.accepted'

        # Let the parent exit and close stdout/stderr right after control.accepted
        # is published, BEFORE abort_task reaches _terminate_process, and wait
        # deterministically until the worker enters wait_for_process_exit.
        exit_parent_file.write_text('exit', encoding='utf-8')
        assert worker_entered_wait.wait(timeout=5), 'worker never entered wait_for_process_exit'
        # Give the worker a moment inside wait_for_process_exit so a missing
        # ownership claim would have reaped the parent here.
        time.sleep(0.1)
        release_abort.set()
        t_abort.join(timeout=10)
        assert not t_abort.is_alive()
        assert abort_result.get('ok') is True

        assert _wait_until(lambda: not execution.is_active(task.id), timeout=15)
        assert db.get_task(task.id).status == TaskStatus.ABORTED
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0), (
            f'detached-pipe grandchild {grandchild_pid} leaked after abort'
        )
    finally:
        release_abort.set()
        stop_file.write_text('stop', encoding='utf-8')
        worktrees.cleanup_worktree(task)
        db.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_external_reap_echild_never_sends_sigkill_to_pgid(monkeypatch):
    """If the leader was externally reaped (ECHILD), never send SIGKILL to its PGID."""
    import app.core.proc as proc_module

    class FakeProc:
        pid = 98765
        returncode = None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(proc_module.os, 'killpg', lambda pgid, sig: signalled.append((pgid, sig)))
    monkeypatch.setattr(proc_module, '_probe_leader_without_reap', lambda _pid: 'reaped')
    monkeypatch.setattr(proc_module, '_pgroup_has_live_members', lambda _pgid, **_kw: (None, False))

    terminate_process_tree(FakeProc(), grace_sec=0.1)  # type: ignore[arg-type]
    assert signalled == [], f'must not signal PGID after ECHILD: {signalled}'


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process-group semantics')
def test_stalled_ps_shares_cleanup_budget(tmp_path, monkeypatch):
    """A hung ps must share the cleanup budget instead of multiplying timeouts."""
    import app.core.proc as proc_module

    pid_file = tmp_path / 'stalled_ps_grandchild.pid'
    stop_file = tmp_path / 'stalled_ps_stop.flag'
    script = _stubborn_grandchild_script(pid_file, parent_exits_on_sigterm=True, stop_file=stop_file)
    proc = spawn_managed_process(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    real_run = proc_module.subprocess.run

    def stalled_ps(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == 'ps':
            timeout = kwargs.get('timeout', 0.2)
            time.sleep(min(timeout, 0.2))
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        return real_run(cmd, *args, **kwargs)

    grandchild_pid: int | None = None
    try:
        assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
        grandchild_pid = int(pid_file.read_text(encoding='utf-8').strip())

        monkeypatch.setattr(proc_module.subprocess, 'run', stalled_ps)
        t0 = time.monotonic()
        terminate_process_tree(proc, grace_sec=0.3)
        elapsed = time.monotonic() - t0
        monkeypatch.undo()

        assert proc.returncode is not None
        assert elapsed < 2.0, f'stalled ps stretched cleanup to {elapsed:.2f}s'
        assert _wait_until(lambda: not _is_pid_alive_non_zombie(grandchild_pid), timeout=3.0)
    finally:
        stop_file.write_text('stop', encoding='utf-8')
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def test_spawn_managed_process_windows_binds_job_before_resume(monkeypatch):
    """On Windows, spawn_managed_process must pass CREATE_SUSPENDED, bind the Job, then resume."""
    import app.core.proc as proc_module

    events: list[tuple[str, object]] = []

    class FakeProc:
        pid = 61616
        returncode = None
        _handle = 1234

        def kill(self):
            events.append(('kill', None))

        def wait(self, timeout=None):
            return 1

    def fake_popen(cmd, **kwargs):
        events.append(('popen', kwargs.get('creationflags')))
        return FakeProc()

    monkeypatch.setattr(proc_module.sys, 'platform', 'win32')
    monkeypatch.setattr(proc_module.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(
        proc_module,
        '_create_and_assign_windows_job',
        lambda _p: (events.append(('bind_job', _p.pid)) or (888, None)),
    )
    monkeypatch.setattr(
        proc_module,
        '_resume_windows_process',
        lambda _p: (events.append(('resume', _p.pid)) or True),
    )

    proc = spawn_managed_process(['dummy.exe'])
    assert events[0][0] == 'popen'
    flags = int(events[0][1])  # type: ignore[arg-type]
    assert flags & proc_module._CREATE_SUSPENDED, 'CREATE_SUSPENDED must be set before Job binding'
    assert events[1] == ('bind_job', 61616)
    assert events[2] == ('resume', 61616)
    assert _state_for_proc(proc).job_handle == 888


def test_spawn_managed_process_windows_fails_closed_when_job_bind_fails(monkeypatch):
    """If Job Object binding fails on Windows, never resume the suspended child."""
    import app.core.proc as proc_module

    events: list[str] = []

    class FakeProc:
        pid = 62626
        returncode = None
        _handle = 1234

        def kill(self):
            events.append('kill')
            self.returncode = 1

        def wait(self, timeout=None):
            events.append('wait')
            return 1

    monkeypatch.setattr(proc_module.sys, 'platform', 'win32')
    monkeypatch.setattr(proc_module.subprocess, 'Popen', lambda _cmd, **_kw: FakeProc())
    monkeypatch.setattr(
        proc_module,
        '_create_and_assign_windows_job',
        lambda _p: (None, 'AssignProcessToJobObject failed (winerror=5)'),
    )
    monkeypatch.setattr(
        proc_module,
        '_resume_windows_process',
        lambda _p: (events.append('resume') or True),
    )

    with pytest.raises(RuntimeError, match='AssignProcessToJobObject failed'):
        spawn_managed_process(['dummy.exe'])

    assert 'resume' not in events, 'suspended child must never be resumed after Job bind failure'
    assert 'kill' in events and 'wait' in events


def test_wait_for_process_exit_waits_for_windows_job_teardown(monkeypatch):
    """wait_for_process_exit must not return or set done before TerminateJobObject finishes."""
    import app.core.proc as proc_module

    job_entered = threading.Event()
    release_job = threading.Event()
    wait_returned = threading.Event()

    class FakeProc:
        pid = 63636
        returncode = None
        _handle = 1234

        def poll(self):
            # Simulate parent exiting during grace window BEFORE TerminateJobObject runs.
            self.returncode = 0
            return 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = 1

    def blocking_job_terminate(_handle):
        job_entered.set()
        release_job.wait(timeout=5)
        return True

    monkeypatch.setattr(proc_module.sys, 'platform', 'win32')
    monkeypatch.setattr(
        proc_module.subprocess,
        'run',
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, '', ''),
    )
    monkeypatch.setattr(proc_module, '_create_and_assign_windows_job', lambda _p: (999, None))
    monkeypatch.setattr(proc_module, '_terminate_windows_job', blocking_job_terminate)
    monkeypatch.setattr(proc_module, '_close_windows_job', lambda _h: None)

    fake = FakeProc()
    register_managed_process(fake)  # type: ignore[arg-type]

    t_term = threading.Thread(target=terminate_process_tree, args=(fake,), kwargs={'grace_sec': 0.05})
    t_term.start()
    assert job_entered.wait(timeout=5)

    second_term_returned = threading.Event()

    def do_wait():
        wait_for_process_exit(fake, timeout=5)  # type: ignore[arg-type]
        wait_returned.set()

    def do_second_terminate():
        terminate_process_tree(fake, grace_sec=0.05)  # type: ignore[arg-type]
        second_term_returned.set()

    t_wait = threading.Thread(target=do_wait)
    t_term2 = threading.Thread(target=do_second_terminate)
    t_wait.start()
    t_term2.start()
    try:
        # Simulate idle-timeout teardown interleaved with a late abort calling
        # mark_termination_requested after fake.returncode is already 0: it must
        # NOT publish done prematurely, and neither wait_for_process_exit nor a
        # second terminate_process_tree may return early.
        assert mark_termination_requested(fake) is True  # type: ignore[arg-type]
        assert not _state_for_proc(fake).done.is_set()  # type: ignore[arg-type]
        assert not wait_returned.wait(timeout=0.2), 'wait_for_process_exit returned before Job teardown finished'
        assert not second_term_returned.is_set(), 'second terminate_process_tree returned before Job teardown finished'
        assert not _state_for_proc(fake).done.is_set()  # type: ignore[arg-type]

        # A concurrent caller with a short join timeout must raise TimeoutError
        # rather than silently returning while teardown is still running.
        with pytest.raises(TimeoutError, match='active process-tree teardown'):
            terminate_process_tree(fake, grace_sec=0.05, join_timeout_sec=0.05)  # type: ignore[arg-type]
    finally:
        release_job.set()
        t_term.join(timeout=5)
        t_term2.join(timeout=5)
        t_wait.join(timeout=5)

    assert wait_returned.is_set()
    assert second_term_returned.is_set()
    assert _state_for_proc(fake).done.is_set()  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows Job Object test')
@pytest.mark.parametrize('explicit_terminate', [False, True])
def test_windows_native_job_object_kills_child_after_fast_wrapper_exit(tmp_path, explicit_terminate):
    """Native Windows CI check: wrapper spawns sleeping child and exits immediately;
    Job Object teardown must kill the child (verified via WaitForSingleObject)."""
    import ctypes

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    pid_file = tmp_path / f'win_child_{explicit_terminate}.pid'
    child_code = (
        'import os, pathlib, time\n'
        f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")\n'
        'time.sleep(60)\n'
    )
    wrapper_code = (
        'import subprocess, sys, time\n'
        f'subprocess.Popen([sys.executable, "-c", {child_code!r}], '
        'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        + ('time.sleep(60)\n' if explicit_terminate else '')
    )
    proc = spawn_managed_process(
        [sys.executable, '-c', wrapper_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert _state_for_proc(proc).job_handle is not None
    assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding='utf-8').strip().isdigit())
    child_pid = int(pid_file.read_text(encoding='utf-8').strip())
    child_handle = kernel32.OpenProcess(SYNCHRONIZE, False, child_pid)
    assert child_handle, f'failed to open handle for child PID {child_pid}'
    try:
        if explicit_terminate:
            terminate_process_tree(proc, grace_sec=0.3)
        else:
            wait_for_process_exit(proc, timeout=10)
        wait_res = kernel32.WaitForSingleObject(child_handle, 5000)
        assert wait_res == WAIT_OBJECT_0, f'Windows child {child_pid} survived Job Object teardown (wait={wait_res})'
    finally:
        kernel32.CloseHandle(child_handle)
