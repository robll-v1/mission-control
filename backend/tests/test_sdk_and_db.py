"""Regression tests for model resolution, run ordering and inline persistence."""
from __future__ import annotations

import time

import pytest

from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.core.models import ReviewResult, ReviewVerdict, Run
from app.sdk import ReviewEngine


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
