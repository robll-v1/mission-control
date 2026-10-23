"""Regression tests for model resolution, run ordering and inline persistence."""
from __future__ import annotations

import time

import pytest

from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import ReviewResult, ReviewVerdict, Run, TaskStatus
from app.sdk import ReviewEngine
from app.services.review_history import ReviewHistoryStore, ReviewRecord, StoredFinding


# --- model resolution ------------------------------------------------------

def _engine(tmp_path, backend='opencode', model=None):
    return ReviewEngine(
        db_path=str(tmp_path / 'amc.db'),
        runtime_root=str(tmp_path / 'runtime'),
        backend=backend,
        model=model,
    )


def test_configured_model_is_applied(tmp_path, isolated_config, monkeypatch):
    """_resolve_model was a @staticmethod using `self`, so this always returned None."""
    (tmp_path / '.amc.yaml').write_text(
        "backend:\n  default: opencode\n  opencode:\n    model: 'glm-5.1'\n", encoding='utf-8'
    )
    monkeypatch.chdir(tmp_path)
    assert _engine(tmp_path).model == 'glm-5.1'


def test_backend_specific_model_wins_over_default_backend(tmp_path, isolated_config, monkeypatch):
    (tmp_path / '.amc.yaml').write_text(
        'backend:\n'
        '  default: opencode\n'
        "  opencode:\n    model: 'from-default'\n"
        "  codex:\n    model: 'from-codex'\n",
        encoding='utf-8',
    )
    monkeypatch.chdir(tmp_path)
    assert _engine(tmp_path, backend='codex').model == 'from-codex'


def test_env_var_overrides_config(tmp_path, isolated_config, monkeypatch):
    (tmp_path / '.amc.yaml').write_text(
        "backend:\n  default: opencode\n  opencode:\n    model: 'from-yaml'\n", encoding='utf-8'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AMC_MODEL', 'from-env')
    assert _engine(tmp_path).model == 'from-env'


def test_explicit_model_overrides_everything(tmp_path, isolated_config, monkeypatch):
    (tmp_path / '.amc.yaml').write_text(
        "backend:\n  default: opencode\n  opencode:\n    model: 'from-yaml'\n", encoding='utf-8'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AMC_MODEL', 'from-env')
    assert _engine(tmp_path, model='from-flag').model == 'from-flag'


def test_no_config_means_backend_default(tmp_path, isolated_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _engine(tmp_path).model is None


def test_review_resolves_model_from_target_repo_and_explicit_backend_wins(tmp_path, isolated_config, monkeypatch):
    repo = tmp_path / 'target'
    repo.mkdir()
    (repo / '.amc.yaml').write_text(
        'backend:\n  default: codex\n  codex:\n    model: target-codex\n  opencode:\n    model: target-opencode\n',
        encoding='utf-8',
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.amc.yaml').write_text(
        'backend:\n  default: opencode\n  opencode:\n    model: caller-model\n', encoding='utf-8'
    )
    engine = _engine(tmp_path, backend=None)
    assert engine._resolve_review_config(str(repo))['backend'] == 'codex'
    assert engine._resolve_review_config(str(repo))['model'] == 'target-codex'

    explicit = _engine(tmp_path, backend='opencode', model='forced-model')
    resolved = explicit._resolve_review_config(str(repo))
    assert resolved['backend'] == 'opencode'
    assert resolved['model'] == 'forced-model'


def test_explicit_backend_does_not_inherit_other_backend_model(tmp_path, isolated_config, monkeypatch):
    (tmp_path / '.amc.yaml').write_text(
        'backend:\n  default: opencode\n  opencode:\n    model: from-opencode\n', encoding='utf-8'
    )
    monkeypatch.chdir(tmp_path)
    resolved = _engine(tmp_path, backend='codex')._resolve_review_config(str(tmp_path))
    assert resolved['backend'] == 'codex'
    assert resolved['model'] is None


def test_explicit_model_reaches_adapter_factory_over_repo_model(tmp_path, isolated_config, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.amc.yaml').write_text(
        'backend:\n  default: opencode\n  opencode:\n    model: repo-model\n', encoding='utf-8'
    )
    monkeypatch.chdir(tmp_path)
    engine = _engine(tmp_path, model='explicit-model')
    adapter = engine._execution.adapter_factory('opencode', model='repo-model', variant='fast')
    assert adapter.model == 'explicit-model'
    assert adapter.variant == 'fast'


def test_start_task_preflight_receives_explicit_model_adapter(tmp_path, isolated_config, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.amc.yaml').write_text(
        'backend:\n  default: opencode\n  opencode:\n    model: repo-model\n', encoding='utf-8'
    )
    engine = _engine(tmp_path, model='explicit-model')
    task = engine._engine.create_task(title='review', repo_path=str(repo), backend='opencode')
    captured = {}

    def capture_adapter(_task, *, adapter=None):
        captured['adapter'] = adapter
        raise RuntimeError('stop after adapter selection')

    monkeypatch.setattr(engine._execution, '_preflight_check', capture_adapter)
    with pytest.raises(RuntimeError, match='stop after adapter selection'):
        engine._execution.start_task(task.id)

    assert captured['adapter'].model == 'explicit-model'


def test_incremental_clear_keeps_unmatched_prior_finding(git_repo, tmp_path, isolated_config, monkeypatch):
    head = __import__('subprocess').run(
        ['git', '-C', str(git_repo), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True,
    ).stdout.strip()
    history = ReviewHistoryStore(str(git_repo))
    history.save_review(ReviewRecord(
        review_id='old', timestamp=1, head_sha=head, base_ref='main', mode='local_diff',
        verdict='concerns', findings=[StoredFinding(severity='high', path='a.txt', line=1, summary='validate input')],
        finding_count=1,
    ))
    engine = ReviewEngine(
        db_path=str(tmp_path / 'review.db'), runtime_root=str(tmp_path / 'runtime'), backend='opencode',
    )
    monkeypatch.setattr(engine, 'start_review', lambda _task_id: None)
    monkeypatch.setattr(engine, 'wait_for_completion', lambda task_id, **_kwargs: engine._db.get_task(task_id))
    monkeypatch.setattr(engine, 'get_result', lambda _task_id: ReviewResult(verdict=ReviewVerdict.CLEAR, summary='No issues'))
    monkeypatch.setattr(engine._db, 'list_runs', lambda _task_id: [Run(task_id='t', backend='opencode', started_at=1)])

    report = engine.review(str(git_repo), base='main')

    assert report.is_incremental
    assert report.verdict == ReviewVerdict.CONCERNS
    assert not report.passed
    assert report.unverified_findings[0].summary == 'validate input'
    assert 'remain unverified' in report.summary
    assert history.get_previous_findings()[0].summary == 'validate input'

    second_incremental = engine.review(str(git_repo), base='main')
    assert second_incremental.is_incremental
    assert len(second_incremental.unverified_findings) == 1
    assert history.get_previous_findings()[0].summary == 'validate input'

    full_review = engine.review(str(git_repo), base='main', incremental=False)

    assert not full_review.is_incremental
    assert full_review.passed
    assert full_review.unverified_findings == []
    assert history.get_previous_findings() == []


def test_wait_for_completion_waits_for_worker_cleanup(tmp_path, isolated_config, monkeypatch):
    engine = _engine(tmp_path)
    task = engine._engine.create_task(title='t', repo_path=str(tmp_path))
    engine._engine.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='awaiting_next_round')
    with engine._execution._lock:
        engine._execution._active[task.id] = None

    def finish_worker(_interval):
        with engine._execution._lock:
            engine._execution._active.pop(task.id, None)

    monkeypatch.setattr('app.sdk.time.sleep', finish_worker)

    completed = engine.wait_for_completion(task.id, timeout_sec=1, poll_interval=0.001)

    assert completed.status == TaskStatus.WAITING_HUMAN
    assert not engine._execution.is_active(task.id)


# --- run ordering ----------------------------------------------------------

def test_list_runs_is_ordered_by_round_index(tmp_path):
    db = Database(str(tmp_path / 'amc.db'))
    mission = MissionEngine(db)
    task = mission.create_task(title='t', repo_path=str(tmp_path))

    # Insert deliberately out of order.
    for round_index in (3, 1, 2):
        db.save_run(Run(task_id=task.id, backend='opencode',
                        round_index=round_index, started_at=float(round_index)))

    assert [r.round_index for r in db.list_runs(task.id)] == [1, 2, 3]
    db.conn.close()


def test_latest_result_picks_the_highest_round(tmp_path):
    from app.services.review_result_service import ReviewResultService

    db = Database(str(tmp_path / 'amc.db'))
    mission = MissionEngine(db)
    task = mission.create_task(title='t', repo_path=str(tmp_path))
    for round_index, verdict in ((2, ReviewVerdict.CLEAR), (1, ReviewVerdict.CONCERNS)):
        db.save_run(Run(
            task_id=task.id, backend='opencode', round_index=round_index,
            status='completed', started_at=float(round_index),
            review_result=ReviewResult(verdict=verdict, summary=f'round {round_index}'),
        ))

    latest = ReviewResultService.latest_result(db.list_runs(task.id))
    assert latest is not None
    assert latest.supersedes_round_index == 2
    assert latest.verdict == ReviewVerdict.CLEAR
    db.conn.close()


# --- inline review persistence --------------------------------------------

def test_review_inline_persists_run_and_latest_result(tmp_path, git_repo, isolated_config, monkeypatch):
    """A completed direct-API review must be visible to the UI and SDK getters."""
    engine = ReviewEngine(
        db_path=str(tmp_path / 'amc.db'),
        runtime_root=str(tmp_path / 'runtime'),
        language='en',
        backend='direct_api',
    )

    # Stub the config + HTTP call so the test needs no network or credentials.
    from app.adapters import direct_api_adapter

    monkeypatch.setattr(
        direct_api_adapter, 'resolve_direct_api_config',
        lambda _cfg: direct_api_adapter.DirectAPIConfig(
            base_url='https://example.invalid', api_key='k', model='m', wire_api='chat'),
    )
    monkeypatch.setattr(
        direct_api_adapter.DirectAPIAdapter, 'call_llm',
        lambda self, prompt, system='': (
            '## Review Summary\n\nOne real problem.\n\n'
            '## Findings\n\n- high: `a.txt:1` — value is never validated\n'
        ),
    )

    report = engine.review_inline(str(git_repo), base='HEAD~1')

    assert report.error is None, report.error
    assert report.finding_count == 1

    # The regression: these were all empty even though the review succeeded.
    assert engine.get_result(report.task_id) is not None
    assert len(engine.get_findings(report.task_id)) == 1
    runs = engine._db.list_runs(report.task_id)
    assert len(runs) == 1 and runs[0].status == 'completed'
    assert runs[0].metrics.get('llm_wall_time_ms') is not None

    # And review history is written, so incremental mode works on this path.
    assert (git_repo / '.amc' / 'reviews' / 'latest.json').exists()
