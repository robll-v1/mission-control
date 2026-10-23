"""Concurrency guards around starting review rounds."""
from __future__ import annotations

import sys
import threading
import time

from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source


class FakeAdapter:
    """Minimal adapter: passes preflight and launches an instantly-exiting process."""

    name = 'opencode'

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return None

    def make_command(self, **_kwargs):
        return [sys.executable, '-c', '']

    def parse_stdout_line(self, _line):
        return []

    def parse_stderr_line(self, _line):
        return []


def _build(tmp_path, git_repo):
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    contexts = ContextCompiler(artifacts)
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={'opencode': FakeAdapter()},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
    )
    return db, mission, artifacts, worktrees, contexts, execution


def _make_local_task(git_repo, mission, artifacts):
    local = build_local_review_source(str(git_repo), base='main')
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
    return task


def test_concurrent_starts_launch_exactly_one_round(git_repo, tmp_path, isolated_config, monkeypatch):
    (git_repo / 'a.txt').write_text('review me\n', encoding='utf-8')
    db, mission, artifacts, worktrees, contexts, execution = _build(tmp_path, git_repo)
    task = _make_local_task(git_repo, mission, artifacts)

    # Hold the first start inside prepare long enough for the second to arrive.
    gate = threading.Event()
    original_compile = contexts.compile_task

    def slow_compile(*args, **kwargs):
        gate.wait(timeout=10)
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(contexts, 'compile_task', slow_compile)

    results: dict[str, tuple] = {}

    def starter(key):
        try:
            results[key] = ('ok', execution.start_task(task.id))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            results[key] = ('err', exc)

    first = threading.Thread(target=starter, args=('first',))
    first.start()
    deadline = time.time() + 10
    while not execution.is_active(task.id) and time.time() < deadline:
        time.sleep(0.01)
    assert execution.is_active(task.id), 'first start never reserved the task'

    second = threading.Thread(target=starter, args=('second',))
    second.start()
    second.join(timeout=10)
    gate.set()
    first.join(timeout=30)

    assert results['first'][0] == 'ok'
    assert results['second'][0] == 'err'
    assert 'already in progress' in str(results['second'][1])
    assert len(db.list_runs(task.id)) == 1

    deadline = time.time() + 30
    while db.list_runs(task.id)[0].status == 'running' and time.time() < deadline:
        time.sleep(0.05)
    assert db.list_runs(task.id)[0].status == 'completed'
    # The worker releases the slot in its finally block, slightly after the run
    # status flips; wait for that too before asserting the guard is down.
    deadline = time.time() + 10
    while execution.is_active(task.id) and time.time() < deadline:
        time.sleep(0.05)
    assert not execution.is_active(task.id), 'slot was not released after the round'
    worktrees.cleanup_worktree(task)
    db.close()


def test_prepare_failure_releases_the_slot_for_retry(git_repo, tmp_path, isolated_config, monkeypatch):
    (git_repo / 'a.txt').write_text('review me too\n', encoding='utf-8')
    db, mission, artifacts, worktrees, contexts, execution = _build(tmp_path, git_repo)
    task = _make_local_task(git_repo, mission, artifacts)

    def broken_compile(*_args, **_kwargs):
        raise RuntimeError('compile exploded')

    monkeypatch.setattr(contexts, 'compile_task', broken_compile)

    try:
        execution.start_task(task.id)
    except RuntimeError as exc:
        assert 'compile exploded' in str(exc)
    else:
        raise AssertionError('start_task should propagate the prepare failure')

    assert not execution.is_active(task.id), 'failed prepare must release the reserved slot'
    assert db.list_runs(task.id) == []
    worktrees.cleanup_worktree(task)
    db.close()
