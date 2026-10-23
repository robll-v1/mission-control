"""Crash recovery reconciles tasks, runs and check runs left mid-flight."""
from __future__ import annotations

from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import CheckRun, Run, TaskStatus
from app.core.recovery import recover_stale_tasks


def _setup(tmp_path):
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    return db, mission


def test_recovery_handles_all_active_statuses_and_orphaned_rows(tmp_path):
    db, mission = _setup(tmp_path)

    running = mission.create_task(title='running', repo_path=str(tmp_path))
    mission.set_stage(running.id, status=TaskStatus.RUNNING, stage='review_in_progress')
    run = Run(task_id=running.id, backend='opencode', started_at=1.0, status='running')
    db.save_run(run)

    validating = mission.create_task(title='validating', repo_path=str(tmp_path))
    mission.set_stage(validating.id, status=TaskStatus.VALIDATING, stage='validate')
    check = CheckRun(task_id=validating.id, name='build', command='make', status='running')
    db.save_check_run(check)

    ingesting = mission.create_task(title='ingesting', repo_path=str(tmp_path))
    mission.set_stage(ingesting.id, status=TaskStatus.INGESTING, stage='prepare_review')

    terminal = mission.create_task(title='terminal', repo_path=str(tmp_path))
    mission.set_stage(terminal.id, status=TaskStatus.WAITING_HUMAN, stage='awaiting_next_round')
    finished_run = Run(task_id=terminal.id, backend='opencode', started_at=1.0, status='completed')
    db.save_run(finished_run)

    assert recover_stale_tasks(db, mission) == 3

    assert db.get_task(running.id).status == TaskStatus.FAILED
    assert db.get_task(validating.id).status == TaskStatus.FAILED
    assert db.get_task(ingesting.id).status == TaskStatus.FAILED
    assert db.get_task(terminal.id).status == TaskStatus.WAITING_HUMAN

    # Orphaned rows are closed out, not left 'running' forever.
    recovered_run = db.list_runs(running.id)[0]
    assert recovered_run.status == 'failed'
    assert recovered_run.ended_at is not None
    assert recovered_run.metrics.get('recovered_interrupted') is True
    recovered_check = db.list_check_runs(validating.id)[0]
    assert recovered_check.status == 'failed'

    # Terminal tasks' finished rows are untouched.
    assert db.list_runs(terminal.id)[0].status == 'completed'

    # Recovery is idempotent — a second pass changes nothing.
    assert recover_stale_tasks(db, mission) == 0
    events = [e.kind for e in db.list_events(running.id)]
    assert events.count('task.recovered') == 1
    db.close()


def test_recovery_leaves_aborted_tasks_alone(tmp_path):
    db, mission = _setup(tmp_path)
    aborted = mission.create_task(title='aborted', repo_path=str(tmp_path))
    mission.set_stage(aborted.id, status=TaskStatus.ABORTED, stage='aborted')

    assert recover_stale_tasks(db, mission) == 0
    assert db.get_task(aborted.id).status == TaskStatus.ABORTED
    db.close()
