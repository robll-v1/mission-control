"""Regression tests for P1 backend default-English review language wiring."""
from __future__ import annotations

import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_control
from app.core.context_compiler import (
    CompiledContext,
    ContextCompiler,
    normalize_review_language,
)
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import Event, Run, Task
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source
from app.services.review_result_service import ReviewResultService


def _dummy_compiled() -> CompiledContext:
    return CompiledContext(
        markdown_path='context.md',
        json_path='context.json',
        markdown='# Compiled PR Review Context\n\n- notes.py',
        payload={},
        markdown_bytes=42,
        json_bytes=2,
    )


def _dummy_task() -> Task:
    return Task(title='Test review', repo_path='.', created_at=0.0, updated_at=0.0)


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        (None, 'en'),
        ('', 'en'),
        ('  ', 'en'),
        ('en', 'en'),
        ('EN', 'en'),
        ('en-US', 'en'),
        ('zh', 'zh'),
        ('zh-CN', 'zh'),
        ('ZH-Hans', 'zh'),
        ('fr', 'en'),
    ],
)
def test_normalize_review_language_non_strict(raw, expected):
    assert normalize_review_language(raw) == expected


def test_normalize_review_language_strict_rejects_unknown():
    assert normalize_review_language('en-US', strict=True) == 'en'
    assert normalize_review_language('en-US-u-ca-gregory', strict=True) == 'en'
    assert normalize_review_language('zh-CN', strict=True) == 'zh'
    assert normalize_review_language('zh-Hans-CN', strict=True) == 'zh'
    assert normalize_review_language('', strict=True) == 'en'
    for bad in ('fr', 'zh-', 'zh-!?', 'en-US garbage', 'en--US', 'en-US-GB', 'en-u-ca-gregory-u-nu-latn', 'en-U\u212a'):
        with pytest.raises(ValueError, match='unsupported review language'):
            normalize_review_language(bad, strict=True)


def test_build_prompt_defaults_to_english_and_supports_chinese():
    task = _dummy_task()
    compiled = _dummy_compiled()

    default_prompt = ContextCompiler.build_prompt(task, compiled)
    assert 'Output in English.' in default_prompt
    assert '全部使用中文' not in default_prompt

    zh_prompt = ContextCompiler.build_prompt(task, compiled, language='zh')
    assert '全部使用中文' in zh_prompt

    zh_cn_prompt = ContextCompiler.build_prompt(task, compiled, language='zh-CN')
    assert '全部使用中文' in zh_cn_prompt

    fallback_prompt = ContextCompiler.build_prompt(task, compiled, language='unknown')
    assert 'Output in English.' in fallback_prompt


class _CapturingAdapter:
    name = 'opencode'

    def __init__(self):
        self.prompts: list[str] = []

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return None

    def make_command(self, *, task, prompt):
        self.prompts.append(prompt)
        # Exit cleanly with no output so ReviewResultService uses its fallback message.
        return [sys.executable, '-c', '']

    def parse_stdout_line(self, _line):
        return []

    def parse_stderr_line(self, _line):
        return []


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_execution_start_task_defaults_to_english_and_records_language(git_repo, tmp_path, isolated_config):
    (git_repo / 'a.txt').write_text('hello\n', encoding='utf-8')
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    adapter = _CapturingAdapter()
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

    try:
        # Round 1: default language must be English.
        execution.start_task(task.id)
        assert _wait_until(lambda: not execution.is_active(task.id))
        assert adapter.prompts and 'Output in English.' in adapter.prompts[0]
        runs = db.list_runs(task.id)
        assert runs[0].review_result is not None
        assert runs[0].review_result.summary.isascii(), runs[0].review_result.summary
        started_events = [e for e in db.list_events(task.id) if e.kind == 'review.round_started']
        assert started_events[-1].payload.get('language') == 'en'

        # Round 2: explicit zh-CN normalizes to zh and produces Chinese prompt + fallback summary.
        execution.start_task(task.id, language='zh-CN')
        assert _wait_until(lambda: not execution.is_active(task.id))
        assert len(adapter.prompts) == 2 and '全部使用中文' in adapter.prompts[1]
        runs = db.list_runs(task.id)
        assert runs[1].review_result is not None
        assert not runs[1].review_result.summary.isascii(), runs[1].review_result.summary
        started_events = [e for e in db.list_events(task.id) if e.kind == 'review.round_started']
        assert started_events[-1].payload.get('language') == 'zh'
    finally:
        worktrees.cleanup_worktree(task)
        db.close()


def test_start_task_route_wires_language_and_validates():
    calls: list[dict[str, object]] = []

    class FakeRun:
        def model_dump(self):
            return {'id': 'run1', 'status': 'running'}

    class FakeExecution:
        def start_task(self, task_id, *, prompt=None, review_note=None, language='en'):
            calls.append({
                'task_id': task_id,
                'prompt': prompt,
                'review_note': review_note,
                'language': language,
            })
            return FakeRun()

    app = FastAPI()
    app.include_router(routes_control.router)
    app.dependency_overrides[routes_control.get_execution] = lambda: FakeExecution()
    client = TestClient(app)

    assert routes_control.StartTaskRequest().language == 'en'
    schema_props = routes_control.StartTaskRequest.model_json_schema()['properties']
    assert schema_props['language']['default'] == 'en'

    assert client.post('/api/tasks/t1/start', json={}).status_code == 200
    assert calls[-1]['language'] == 'en'

    assert client.post('/api/tasks/t1/start', json={'language': None}).status_code == 200
    assert calls[-1]['language'] == 'en'

    assert client.post('/api/tasks/t1/start', json={'language': ''}).status_code == 200
    assert calls[-1]['language'] == 'en'

    assert client.post('/api/tasks/t1/start', json={'language': 'en'}).status_code == 200
    assert calls[-1]['language'] == 'en'

    assert client.post('/api/tasks/t1/start', json={'language': 'en-US-u-ca-gregory'}).status_code == 200
    assert calls[-1]['language'] == 'en'

    assert client.post('/api/tasks/t1/start', json={'language': 'zh'}).status_code == 200
    assert calls[-1]['language'] == 'zh'

    assert client.post('/api/tasks/t1/start', json={'language': 'zh-CN'}).status_code == 200
    assert calls[-1]['language'] == 'zh'

    for bad_tag in ('fr', 'zh-', 'zh-!?', 'en-US garbage', 'en-US-GB', 'en-u-ca-gregory-u-nu-latn', 'en-U\u212a'):
        bad = client.post('/api/tasks/t1/start', json={'language': bad_tag})
        assert bad.status_code == 422, f'expected 422 for {bad_tag!r}, got {bad.status_code}'


def test_backfill_task_uses_recorded_round_language(tmp_path):
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    task = mission.create_task(title='t', repo_path=str(tmp_path))
    run_zh = Run(task_id=task.id, backend='opencode', round_index=1, started_at=1.0, status='failed')
    run_en = Run(task_id=task.id, backend='opencode', round_index=2, started_at=2.0, status='failed')
    run_legacy = Run(task_id=task.id, backend='opencode', round_index=3, started_at=3.0, status='failed')
    for run in (run_zh, run_en, run_legacy):
        db.save_run(run)
    db.append_event(
        Event(
            task_id=task.id,
            run_id=run_zh.id,
            seq=1,
            ts=1.0,
            kind='review.round_started',
            payload={'language': 'zh'},
        )
    )
    db.append_event(
        Event(
            task_id=task.id,
            run_id=run_en.id,
            seq=2,
            ts=2.0,
            kind='review.round_started',
            payload={'language': 'en'},
        )
    )
    # Round 3 has a legacy round_started event with no 'language' key -> must fall back to English.
    db.append_event(
        Event(
            task_id=task.id,
            run_id=run_legacy.id,
            seq=3,
            ts=3.0,
            kind='review.round_started',
            payload={'backend': 'opencode'},
        )
    )

    ReviewResultService.backfill_task(db=db, task=task)
    updated = db.list_runs(task.id)
    assert updated[0].review_result is not None and not updated[0].review_result.summary.isascii()
    assert updated[1].review_result is not None and updated[1].review_result.summary.isascii()
    assert updated[2].review_result is not None and updated[2].review_result.summary.isascii()
    db.close()
