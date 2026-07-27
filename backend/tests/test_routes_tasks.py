from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import routes_tasks
from app.api.routes_tasks import create_task
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.mission_engine import MissionEngine
from app.schemas.task import CreateTaskRequest
from app.services.artifact_store import ArtifactStore


@pytest.fixture
def task_services(tmp_path):
    db = Database(str(tmp_path / 'tasks.db'))
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    yield MissionEngine(db), artifacts, ContextCompiler(artifacts)
    db.close()


def test_create_local_diff_task(git_repo, task_services):
    (git_repo / 'a.txt').write_text('local change\n', encoding='utf-8')
    (git_repo / 'untracked.txt').write_text('new file\n', encoding='utf-8')
    mission, artifacts, contexts = task_services

    payload = create_task(
        CreateTaskRequest(
            repo_path=str(git_repo),
            source_type='local_diff',
            base='main',
            review_focus='local correctness',
        ),
        mission=mission,
        artifact_store=artifacts,
        contexts=contexts,
    )

    assert payload['source_type'] == 'local_diff'
    assert payload['source_url'] is None
    assert payload['pr_base_ref'] == 'main'
    assert payload['review_paths'] == ['a.txt', 'untracked.txt']
    assert payload['status'] == 'context_ready'
    diff_text = artifacts.read_text(payload['id'], 'context/local_diff.patch')
    assert '+local change' in diff_text
    assert '+new file' in diff_text
    compiled = json.loads(artifacts.read_text(payload['id'], 'context/context.json'))
    patch_paths = {item['path'] for item in compiled['review']['file_patches']}
    assert {'a.txt', 'untracked.txt'} <= patch_paths
    assert any(event.kind == 'task.local_diff_ingested' for event in mission.db.list_events(payload['id']))


def test_pull_request_payload_remains_backward_compatible(git_repo, task_services, monkeypatch):
    pull_request = SimpleNamespace(
        task_title='[PR #7] Keep compatibility',
        owner='acme',
        repo='widget',
        pr_number=7,
        changed_files=['a.txt'],
        reviews=[],
        issue_comments=[],
        review_comments=[],
        raw_pr={
            'head': {'ref': 'feature', 'sha': 'abc123'},
            'base': {'ref': 'main'},
        },
        to_description=lambda review_focus='': f'PR description\n{review_focus}',
        to_json=lambda: '{"number": 7}',
    )
    monkeypatch.setattr(routes_tasks, 'fetch_pull_request', lambda _url: pull_request)
    mission, artifacts, contexts = task_services

    payload = create_task(
        CreateTaskRequest(
            repo_path=str(git_repo),
            pr_url='https://github.com/acme/widget/pull/7',
        ),
        mission=mission,
        artifact_store=artifacts,
        contexts=contexts,
    )

    assert payload['source_type'] == 'pull_request'
    assert payload['pr_number'] == 7
    assert payload['source_url'] == 'https://github.com/acme/widget/pull/7'


def test_pull_request_mode_requires_url(git_repo, task_services):
    mission, artifacts, contexts = task_services

    with pytest.raises(HTTPException) as exc_info:
        create_task(
            CreateTaskRequest(repo_path=str(git_repo)),
            mission=mission,
            artifact_store=artifacts,
            contexts=contexts,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == 'pr_url is required for pull_request tasks'


def test_local_diff_mode_rejects_unknown_base(git_repo, task_services):
    mission, artifacts, contexts = task_services

    with pytest.raises(HTTPException) as exc_info:
        create_task(
            CreateTaskRequest(
                repo_path=str(git_repo),
                source_type='local_diff',
                base='does-not-exist',
            ),
            mission=mission,
            artifact_store=artifacts,
            contexts=contexts,
        )

    assert exc_info.value.status_code == 400
    assert 'base revision does not exist' in exc_info.value.detail
