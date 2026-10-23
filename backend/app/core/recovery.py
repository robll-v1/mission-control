"""Crash recovery: reconcile state left behind when the process died."""
from __future__ import annotations

import logging
import time

from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import TaskStatus

logger = logging.getLogger(__name__)


def recover_stale_tasks(database: Database, mission: MissionEngine) -> int:
    """Mark tasks left mid-flight as FAILED and close orphaned runs/checks.

    A crash can leave three kinds of debris: a task in INGESTING / RUNNING /
    VALIDATING, a Run stuck at 'running', and a CheckRun stuck at 'running'.
    VALIDATING matters doubly because worktree pruning treats it as active,
    so an unrecovered validating task also keeps its worktree forever. At
    startup nothing is legitimately running, so every row can be reconciled.

    Returns the number of tasks that were moved out of an active state.
    """
    stale_statuses = {TaskStatus.INGESTING, TaskStatus.RUNNING, TaskStatus.VALIDATING}
    recovered = 0
    for task in database.list_tasks():
        if task.status in stale_statuses:
            logger.warning(
                'Recovering stale task %s (was %s → marking FAILED)',
                task.id, task.status,
            )
            mission.set_stage(task.id, status=TaskStatus.FAILED, stage='interrupted')
            mission.append_event(
                task_id=task.id,
                run_id=None,
                kind='task.recovered',
                payload={
                    'previous_status': task.status,
                    'reason': 'Process terminated unexpectedly. Task was recovered on restart.',
                },
            )
            recovered += 1
        for run in database.list_runs(task.id):
            if run.status == 'running':
                run.status = 'failed'
                run.ended_at = run.ended_at or time.time()
                run.metrics['recovered_interrupted'] = True
                database.save_run(run)
        for check in database.list_check_runs(task.id):
            if check.status == 'running':
                check.status = 'failed'
                check.exit_code = -1 if check.exit_code is None else check.exit_code
                database.save_check_run(check)
    if recovered:
        logger.info('Recovered %d stale task(s)', recovered)
    return recovered
