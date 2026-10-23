"""Abort semantics: an accepted abort stays the task's final state."""
from __future__ import annotations

import sys
import time

import pytest

from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source


class SleepAdapter:
    """Launches a process that idles long enough to be aborted mid-flight."""

    name = 'opencode'

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return None

    def make_command(self, **_kwargs):
        return [sys.executable, '-c', 'import time; time.sleep(30)']

    def parse_stdout_line(self, _line):
        return []

    def parse_stderr_line(self, _line):
        return []


@pytest.mark.parametrize('finish_during_termination', [False, True])
def test_abort_survives_worker_finalisation(git_repo, tmp_path, isolated_config, monkeypatch, finish_during_termination):
    (git_repo / 'a.txt').write_text('abort me\n', encoding='utf-8')
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={'opencode': SleepAdapter()},
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

    execution.start_task(task.id)
    deadline = time.time() + 10
    while time.time() < deadline:
        with execution._lock:
            active = execution._active.get(task.id)
        if active is not None and active.process is not None:
            break
        time.sleep(0.02)
    else:
        raise AssertionError('round never launched its process')

    if finish_during_termination:
        original_terminate = execution._terminate_process

        def terminate_and_wait(proc):
            original_terminate(proc)
            deadline = time.monotonic() + 10
            while execution.is_active(task.id) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not execution.is_active(task.id)

        monkeypatch.setattr(execution, '_terminate_process', terminate_and_wait)

    assert execution.abort_task(task.id) is True
    assert db.get_task(task.id).status == TaskStatus.ABORTED

    # The worker finalises asynchronously; wait until it releases the slot.
    deadline = time.time() + 30
    while execution.is_active(task.id) and time.time() < deadline:
        time.sleep(0.05)

    assert db.get_task(task.id).status == TaskStatus.ABORTED, 'abort was repainted as a failure'
    assert db.list_runs(task.id)[0].status == 'aborted'
    finished = [e for e in db.list_events(task.id) if e.kind == 'review.round_finished']
    assert finished and finished[-1].payload.get('reason') == 'aborted'
    worktrees.cleanup_worktree(task)
    db.close()


def test_set_stage_cannot_leave_aborted_except_new_round(tmp_path):
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    task = mission.create_task(title='t', repo_path=str(tmp_path))
    mission.set_stage(task.id, status=TaskStatus.ABORTED, stage='aborted')

    mission.set_stage(task.id, status=TaskStatus.FAILED, stage='review_failed')
    assert db.get_task(task.id).status == TaskStatus.ABORTED

    mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='awaiting_next_round')
    assert db.get_task(task.id).status == TaskStatus.ABORTED

    # Starting a new round is the one allowed way out of ABORTED.
    mission.set_stage(task.id, status=TaskStatus.INGESTING, stage='prepare_review')
    assert db.get_task(task.id).status == TaskStatus.INGESTING
    db.close()


def test_abort_during_result_parsing_is_not_lost(git_repo, tmp_path, isolated_config, monkeypatch):
    """Cancellation after the worker reads its state must survive result parsing."""
    import threading
    from app.services.review_result_service import ReviewResultService
    from test_execution_concurrency import _build, _make_local_task

    (git_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    db, mission, artifacts, worktrees, contexts, execution = _build(tmp_path, git_repo)
    task = _make_local_task(git_repo, mission, artifacts)
    parsing = threading.Event()
    release = threading.Event()
    original = ReviewResultService.extract_result

    def gated_extract(**kwargs):
        if not parsing.is_set():
            parsing.set()
            assert release.wait(10), 'test never released parsing'
        return original(**kwargs)

    monkeypatch.setattr(ReviewResultService, 'extract_result', staticmethod(gated_extract))
    execution.start_task(task.id)
    try:
        assert parsing.wait(10), 'worker never reached result parsing'
        assert execution.abort_task(task.id)
    finally:
        release.set()
        deadline = time.monotonic() + 10
        while execution.is_active(task.id) and time.monotonic() < deadline:
            time.sleep(0.01)
    assert not execution.is_active(task.id)
    assert db.get_task(task.id).status == TaskStatus.ABORTED
    assert db.list_runs(task.id)[0].status == 'aborted'
    finished = [e for e in db.list_events(task.id) if e.kind == 'review.round_finished']
    assert finished[-1].payload['reason'] == 'aborted'
    db.close()


def test_cancelled_output_cannot_certify_clear():
    from app.core.models import Event, Run
    from app.services.review_result_service import ReviewResultService

    run = Run(task_id='task', backend='opencode', status='aborted', started_at=1)
    event = Event(task_id='task', run_id=run.id, kind='agent.text', seq=1, ts=1,
                  payload={'text': 'Found no material issues'})
    result = ReviewResultService.extract_result(events=[event], run=run)
    assert result.verdict == 'inconclusive'
