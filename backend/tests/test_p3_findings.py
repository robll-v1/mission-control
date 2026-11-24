"""Comprehensive P3 tests: SQLite findings table, migration backup, JSON-first parser,
cross-round FindingTracker, Likely fixed snapshot verification, user states, and API.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes_tasks
from app.core.context_compiler import CompiledContext, ContextCompiler
from app.core.db import SCHEMA_VERSION, Database
from app.core.execution import ExecutionService
from app.core.finding_tracker import FindingTracker
from app.core.mission_engine import MissionEngine
from app.core.models import (
    FindingStatus,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    Run,
    Task,
)
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source, capture_local_snapshot
from app.services.review_result_service import ReviewResultService


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 1. Schema migration & pre-migration backup
# ---------------------------------------------------------------------------


def test_db_migration_creates_backup_and_preserves_legacy_rows(tmp_path):
    db_file = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db_file)
    conn.executescript(
        '''
        CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT, seq INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE check_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        '''
    )
    legacy_task = Task(id='t_legacy', title='Legacy task', repo_path=str(tmp_path), created_at=1.0, updated_at=1.0)
    conn.execute('INSERT INTO tasks (id, data) VALUES (?, ?)', (legacy_task.id, legacy_task.model_dump_json()))
    conn.commit()
    conn.close()

    db = Database(str(db_file))
    try:
        assert db.schema_version() == SCHEMA_VERSION
        assert db.last_backup_path is not None
        assert db.last_backup_path.exists()
        # Verify backup is a valid SQLite DB with the original v0 schema and row
        bak = sqlite3.connect(db.last_backup_path)
        try:
            assert bak.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert bak.execute('PRAGMA user_version').fetchone()[0] == 0
            assert bak.execute('SELECT id FROM tasks').fetchall() == [('t_legacy',)]
        finally:
            bak.close()
        # Upgraded DB preserved rows and added findings table
        loaded = db.get_task('t_legacy')
        assert loaded is not None and loaded.title == 'Legacy task'
        assert db.list_findings('t_legacy') == []
    finally:
        db.close()

    # Re-opening an already-upgraded DB must NOT create another backup
    db2 = Database(str(db_file))
    try:
        assert db2.last_backup_path is None
        assert db2.schema_version() == SCHEMA_VERSION
    finally:
        db2.close()


def test_new_db_does_not_create_backup_file(tmp_path):
    db_file = tmp_path / 'fresh.db'
    db = Database(str(db_file))
    try:
        assert db.schema_version() == SCHEMA_VERSION
        assert db.last_backup_path is None
        assert list(tmp_path.glob('*.bak*')) == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. JSON-first / Markdown-fallback parser & prompt instructions
# ---------------------------------------------------------------------------


def test_build_prompt_includes_json_schema_in_both_languages():
    compiled = CompiledContext(
        markdown_path='c.md', json_path='c.json', markdown='# Context', payload={},
    )
    task = Task(title='t', repo_path='.', created_at=0.0, updated_at=0.0)
    en_prompt = ContextCompiler.build_prompt(task, compiled, language='en')
    zh_prompt = ContextCompiler.build_prompt(task, compiled, language='zh')
    assert 'Output in English.' in en_prompt
    assert '```json' in en_prompt and '"category"' in en_prompt
    assert '全部使用中文' in zh_prompt
    assert '```json' in zh_prompt and '"category"' in zh_prompt


def test_json_first_parser_extracts_category_and_strips_fence_from_summary():
    raw = """## Review Summary

Found a real SQL injection vulnerability in notes search.

## Findings

- critical: `notes.py:48` — SQL injection in search()

```json
{
  "verdict": "concerns",
  "summary": "SQL injection in NoteStore.search allows arbitrary SQL execution.",
  "findings": [
    {
      "severity": "critical",
      "category": "SQL Injection",
      "path": "./notes.py",
      "line": 48,
      "title": "Unsanitized f-string SQL query in search()",
      "detail": "Bind the keyword as a query parameter instead of interpolating it."
    }
  ]
}
```
"""
    res = ReviewResultService.parse_raw_text(raw)
    assert res.parse_mode == 'json'
    assert res.verdict == ReviewVerdict.CONCERNS
    assert res.summary == 'SQL injection in NoteStore.search allows arbitrary SQL execution.'
    assert res.finding_count == 1
    assert res.findings[0].severity == 'critical'
    assert res.findings[0].category == 'sql-injection'
    assert res.findings[0].path == 'notes.py'
    assert res.findings[0].line == 48
    assert res.findings[0].summary == 'Unsanitized f-string SQL query in search()'


def test_json_parser_handles_clear_verdict_and_multiple_fences():
    raw = """Example config:
```json
{"not_a_review": true}
```

Everything looks solid.

```json
{"verdict": "clear", "summary": "All changes look clean.", "findings": []}
```
"""
    res = ReviewResultService.parse_raw_text(raw)
    assert res.parse_mode == 'json'
    assert res.verdict == ReviewVerdict.CLEAR
    assert res.finding_count == 0
    assert res.summary == 'All changes look clean.'


def test_malformed_json_falls_back_to_markdown_parser():
    raw = """## Review Summary

Caught one off-by-one bug.

## Findings

- high: `app/payout.py:27` — Off-by-one in top_payouts
  range(count + 1) reads past the list end.

```json
{ "verdict": "concerns", "findings": [ BROKEN JSON
```
"""
    res = ReviewResultService.parse_raw_text(raw)
    assert res.parse_mode == 'markdown'
    assert res.verdict == ReviewVerdict.CONCERNS
    assert res.finding_count == 1
    assert res.findings[0].path == 'app/payout.py'
    assert res.findings[0].line == 27
    assert 'BROKEN JSON' not in res.summary


def test_raw_json_without_markdown_fence_parses_cleanly():
    payload = json.dumps({
        'verdict': 'concerns',
        'summary': 'Leaked DB handle.',
        'findings': [{
            'severity': 'medium',
            'category': 'resource-leak',
            'path': 'db.py',
            'line': 12,
            'title': 'Connection never closed',
            'detail': 'Use a context manager.',
        }],
    })
    res = ReviewResultService.parse_raw_text(payload)
    assert res.parse_mode == 'json'
    assert res.finding_count == 1
    assert res.findings[0].category == 'resource-leak'


# ---------------------------------------------------------------------------
# 3. Cross-round FindingTracker, Likely fixed (snapshot_sha), and user states
# ---------------------------------------------------------------------------


def _make_notes_repo(git_repo: Path) -> None:
    lines = [f'line_{i} = {i}' for i in range(1, 31)]
    lines[4] = 'conn = open_db()  # line 5: never closed'
    lines[19] = 'query = f"SELECT * FROM notes WHERE title LIKE \'%{kw}%\'"  # line 20: sql injection'
    (git_repo / 'notes.py').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def test_likely_fixed_compares_snapshot_revisions_not_head_and_never_auto_resolves(git_repo, tmp_path):
    """Uncommitted fix between Round 1 snapshot and Round 2 snapshot marks
    finding as `likely_fixed` (never `resolved`), while untouched bug stays
    `unverified` if omitted or `persistent` if re-reported. Reverting drops
    `likely_fixed` back to `unverified`.
    """
    _make_notes_repo(git_repo)
    head1, snap1, base1, files1, _ = capture_local_snapshot(str(git_repo), 'main')

    db = Database(str(tmp_path / 'amc.db'))
    mission = MissionEngine(db)
    task = mission.create_task(
        title='Local review',
        repo_path=str(git_repo),
        source_type='local_diff',
        backend='codex',
        pr_base_ref=base1,
        review_paths=files1,
        local_head_sha=head1,
        local_snapshot_sha=snap1,
    )

    try:
        # Round 1 reports both SQL injection (line 20) and resource leak (line 5)
        r1 = Run(
            task_id=task.id,
            backend='codex',
            round_index=1,
            review_revision=snap1,
            status='completed',
            started_at=10.0,
            ended_at=11.0,
            review_result=ReviewResult(
                verdict=ReviewVerdict.CONCERNS,
                parse_mode='json',
                findings=[
                    ReviewFinding(
                        severity='critical',
                        category='sql-injection',
                        path='notes.py',
                        line=20,
                        summary='SQL injection in notes search',
                        detail='Use parameterized query.',
                    ),
                    ReviewFinding(
                        severity='medium',
                        category='resource-leak',
                        path='notes.py',
                        line=5,
                        summary='DB connection never closed',
                        detail='Close conn in finally.',
                    ),
                ],
                finding_count=2,
            ),
        )
        db.save_run(r1)
        f1 = FindingTracker.sync_round(db=db, task=task, run=r1)
        assert len(f1) == 2
        assert {f.status for f in f1} == {FindingStatus.NEW}

        # Fix ONLY line 20 (still uncommitted — HEAD does not change at all!)
        content = (git_repo / 'notes.py').read_text(encoding='utf-8').splitlines()
        content[19] = 'query = "SELECT * FROM notes WHERE title LIKE ?"  # parameterized'
        (git_repo / 'notes.py').write_text('\n'.join(content) + '\n', encoding='utf-8')
        head2, snap2, _, _, _ = capture_local_snapshot(str(git_repo), 'main')
        assert head2 == head1  # HEAD never moved!
        assert snap2 != snap1

        # Round 2 omits both (e.g. agent only checked the new diff hunk -> missed line 5 too)
        r2 = Run(
            task_id=task.id,
            backend='codex',
            round_index=2,
            review_revision=snap2,
            status='completed',
            started_at=20.0,
            ended_at=21.0,
            review_result=ReviewResult(
                verdict=ReviewVerdict.CLEAR,
                parse_mode='json',
                findings=[],
                finding_count=0,
            ),
        )
        db.save_run(r2)
        f2 = FindingTracker.sync_round(db=db, task=task, run=r2)
        by_cat = {f.category: f for f in f2}
        # Line 20 changed between snap1..snap2 -> LIKELY_FIXED (NEVER RESOLVED!)
        assert by_cat['sql-injection'].status == FindingStatus.LIKELY_FIXED
        assert by_cat['sql-injection'].system_status == FindingStatus.LIKELY_FIXED
        assert by_cat['sql-injection'].user_status is None
        # Line 5 did NOT change between snap1..snap2 -> UNVERIFIED
        assert by_cat['resource-leak'].status == FindingStatus.UNVERIFIED

        # Round 3 modifies an UNRELATED file; line 20 is still modified relative to snap1 (last_round=1)
        (git_repo / 'other.txt').write_text('unrelated edit\n', encoding='utf-8')
        _, snap3, _, _, _ = capture_local_snapshot(str(git_repo), 'main')
        r3 = Run(
            task_id=task.id,
            backend='codex',
            round_index=3,
            review_revision=snap3,
            status='completed',
            started_at=30.0,
            ended_at=31.0,
            review_result=ReviewResult(verdict=ReviewVerdict.CLEAR, parse_mode='json', findings=[]),
        )
        db.save_run(r3)
        f3 = FindingTracker.sync_round(db=db, task=task, run=r3)
        by_cat3 = {f.category: f for f in f3}
        assert by_cat3['sql-injection'].status == FindingStatus.LIKELY_FIXED
        assert by_cat3['resource-leak'].status == FindingStatus.UNVERIFIED

        # Round 4 reverts line 20 back to the buggy code -> snap1..snap4 no longer touches line 20 -> UNVERIFIED
        _make_notes_repo(git_repo)
        _, snap4, _, _, _ = capture_local_snapshot(str(git_repo), 'main')
        r4 = Run(
            task_id=task.id,
            backend='codex',
            round_index=4,
            review_revision=snap4,
            status='completed',
            started_at=40.0,
            ended_at=41.0,
            review_result=ReviewResult(verdict=ReviewVerdict.CLEAR, parse_mode='json', findings=[]),
        )
        db.save_run(r4)
        f4 = FindingTracker.sync_round(db=db, task=task, run=r4)
        by_cat4 = {f.category: f for f in f4}
        assert by_cat4['sql-injection'].status == FindingStatus.UNVERIFIED
    finally:
        db.close()


def test_cross_agent_cross_language_dedup_location_changed_and_user_states(git_repo, tmp_path):
    db = Database(str(tmp_path / 'amc.db'))
    mission = MissionEngine(db)
    task = mission.create_task(title='Cross-agent', repo_path=str(git_repo), backend='codex')
    try:
        # Round 1 (Codex, English): two distinct issues at line 11
        r1 = Run(
            task_id=task.id,
            backend='codex',
            round_index=1,
            status='completed',
            started_at=1.0,
            ended_at=2.0,
            review_result=ReviewResult(
                verdict=ReviewVerdict.CONCERNS,
                findings=[
                    ReviewFinding(
                        severity='critical',
                        category='sql-injection',
                        path='app/accounts.py',
                        line=11,
                        summary='SQL injection in get_user',
                    ),
                    ReviewFinding(
                        severity='critical',
                        category='resource-leak',
                        path='app/accounts.py',
                        line=11,
                        summary='Connection leak in get_user',
                    ),
                ],
                finding_count=2,
            ),
        )
        db.save_run(r1)
        f1 = FindingTracker.sync_round(db=db, task=task, run=r1)
        assert len(f1) == 2
        by_cat = {f.category: f for f in f1}
        sql_id = by_cat['sql-injection'].id
        leak_id = by_cat['resource-leak'].id

        # User marks SQL injection as RESOLVED and resource leak as DISMISSED
        resolved = FindingTracker.set_user_status(db=db, task_id=task.id, finding_id=sql_id, action='resolve')
        dismissed = FindingTracker.set_user_status(db=db, task_id=task.id, finding_id=leak_id, action='dismiss')
        assert resolved.status == FindingStatus.RESOLVED
        assert dismissed.status == FindingStatus.DISMISSED

        # Round 2 (Claude Code, Chinese, shifted to line 14, no category on SQLi):
        # - SQLi re-reported -> auto-reopens to PERSISTENT (reopened=True, location_changed=True)
        # - Resource leak re-reported -> stays DISMISSED (mentioned_after_dismiss=True, last_round=2)
        r2 = Run(
            task_id=task.id,
            backend='claude-code',
            round_index=2,
            status='completed',
            started_at=3.0,
            ended_at=4.0,
            review_result=ReviewResult(
                verdict=ReviewVerdict.CONCERNS,
                findings=[
                    ReviewFinding(
                        severity='critical',
                        category='sql-injection',
                        path='app/accounts.py',
                        line=14,
                        summary='get_user 存在 SQL 注入漏洞',
                    ),
                    ReviewFinding(
                        severity='critical',
                        category='resource-leak',
                        path='app/accounts.py',
                        line=11,
                        summary='get_user 未关闭数据库连接',
                    ),
                ],
                finding_count=2,
            ),
        )
        db.save_run(r2)
        f2 = FindingTracker.sync_round(db=db, task=task, run=r2)
        assert len(f2) == 2
        by_id = {f.id: f for f in f2}

        sql_after = by_id[sql_id]
        assert sql_after.status == FindingStatus.PERSISTENT
        assert sql_after.user_status is None
        assert sql_after.reopened is True
        assert sql_after.first_round == 1 and sql_after.last_round == 2
        assert sql_after.location_changed is True
        assert sql_after.previous_line == 11 and sql_after.line == 14

        leak_after = by_id[leak_id]
        assert leak_after.status == FindingStatus.DISMISSED
        assert leak_after.user_status == FindingStatus.DISMISSED
        assert leak_after.mentioned_after_dismiss is True
        assert leak_after.last_round == 2

        # Undo dismissed -> restores underlying system_status (PERSISTENT)
        undone = FindingTracker.set_user_status(db=db, task_id=task.id, finding_id=leak_id, action='undo')
        assert undone.status == FindingStatus.PERSISTENT
        assert undone.user_status is None
        assert undone.mentioned_after_dismiss is False

        # Failed / aborted Round 3 must NOT overwrite existing findings
        r3_aborted = Run(
            task_id=task.id,
            backend='codex',
            round_index=3,
            status='aborted',
            started_at=5.0,
            ended_at=6.0,
            review_result=ReviewResult(verdict=ReviewVerdict.INCONCLUSIVE, findings=[]),
        )
        db.save_run(r3_aborted)
        f3 = FindingTracker.sync_round(db=db, task=task, run=r3_aborted)
        assert {f.status for f in f3} == {FindingStatus.PERSISTENT}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. HTTP API routes & legacy backfill
# ---------------------------------------------------------------------------


def test_task_findings_api_backfill_filter_and_patch(git_repo, tmp_path):
    db = Database(str(tmp_path / 'api.db'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    task = mission.create_task(title='API task', repo_path=str(git_repo), backend='codex')
    # Simulate a pre-P3 completed run whose findings are only in Run.review_result
    run = Run(
        task_id=task.id,
        backend='codex',
        round_index=1,
        status='completed',
        started_at=1.0,
        ended_at=2.0,
        review_result=ReviewResult(
            verdict=ReviewVerdict.CONCERNS,
            parse_mode='json',
            findings=[
                ReviewFinding(severity='high', category='auth-bypass', path='auth.py', line=9, summary='Missing auth check'),
                ReviewFinding(severity='low', category='style', path='auth.py', line=40, summary='Unused import'),
            ],
            finding_count=2,
        ),
    )
    db.save_run(run)

    app = FastAPI()
    app.include_router(routes_tasks.router)
    app.dependency_overrides[routes_tasks.get_engine] = lambda: mission
    app.dependency_overrides[routes_tasks.get_artifacts] = lambda: artifacts
    client = TestClient(app)

    try:
        # GET /api/tasks/{id} automatically backfills SQLite findings
        resp = client.get(f'/api/tasks/{task.id}')
        assert resp.status_code == 200
        body = resp.json()
        assert len(body['findings']) == 2
        assert body['finding_counts']['new'] == 2
        assert body['finding_counts']['open'] == 2
        high_id = next(f['id'] for f in body['findings'] if f['severity'] == 'high')

        # Filter by severity
        filtered = client.get(f'/api/tasks/{task.id}/findings?severity=high')
        assert filtered.status_code == 200
        assert len(filtered.json()['findings']) == 1
        assert filtered.json()['findings'][0]['id'] == high_id

        # PATCH resolve
        patched = client.patch(f'/api/tasks/{task.id}/findings/{high_id}', json={'action': 'resolve'})
        assert patched.status_code == 200
        assert patched.json()['finding']['status'] == 'resolved'
        assert patched.json()['counts']['resolved'] == 1
        assert patched.json()['counts']['open'] == 1

        # Audit event emitted
        events = db.list_events(task.id)
        assert any(e.kind == 'finding.status_changed' and e.payload['finding_id'] == high_id for e in events)

        # PATCH {"status": null} undoes back to underlying system_status ('new')
        reopened = client.patch(f'/api/tasks/{task.id}/findings/{high_id}', json={'status': None})
        assert reopened.status_code == 200
        assert reopened.json()['finding']['status'] == 'new'
        assert reopened.json()['counts']['open'] == 2

        # Validation errors
        assert client.patch(f'/api/tasks/{task.id}/findings/{high_id}', json={'action': 'bogus'}).status_code == 400
        assert client.patch(f'/api/tasks/{task.id}/findings/missing', json={'action': 'resolve'}).status_code == 404
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. End-to-end 2-round ExecutionService integration (fake JSON agent)
# ---------------------------------------------------------------------------


class _FakeJsonAgentAdapter:
    name = 'codex'

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._call = 0

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return None

    def make_command(self, *, task, prompt):
        text = self._responses[min(self._call, len(self._responses) - 1)]
        self._call += 1
        payload = json.dumps({'type': 'agent_text', 'text': text})
        return [sys.executable, '-c', f'import sys; print({payload!r})']

    def parse_stdout_line(self, line: str):
        from app.adapters.base import AdapterEvent
        try:
            obj = json.loads(line)
        except Exception:
            return []
        if obj.get('type') == 'agent_text':
            return [AdapterEvent(kind='agent.text', payload={'text': obj['text']})]
        return []

    def parse_stderr_line(self, _line: str):
        return []


def test_execution_service_two_round_tracks_likely_fixed_and_persistent(git_repo, tmp_path, isolated_config):
    _make_notes_repo(git_repo)
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'exec.db'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))

    round1_text = (
        '## Review Summary\n\nFound SQLi and leak.\n\n'
        '```json\n'
        + json.dumps({
            'verdict': 'concerns',
            'summary': 'Found SQL injection and leaked connection.',
            'findings': [
                {
                    'severity': 'critical',
                    'category': 'sql-injection',
                    'path': 'notes.py',
                    'line': 20,
                    'title': 'SQL injection in search',
                    'detail': 'Use bound params.',
                },
                {
                    'severity': 'medium',
                    'category': 'resource-leak',
                    'path': 'notes.py',
                    'line': 5,
                    'title': 'Connection leak',
                    'detail': 'Close connection.',
                },
            ],
        })
        + '\n```'
    )
    round2_text = (
        '## Review Summary\n\nSQLi fixed, leak remains.\n\n'
        '```json\n'
        + json.dumps({
            'verdict': 'concerns',
            'summary': 'Connection leak still remains.',
            'findings': [
                {
                    'severity': 'medium',
                    'category': 'resource-leak',
                    'path': 'notes.py',
                    'line': 5,
                    'title': 'Connection still not closed',
                    'detail': 'Use context manager.',
                },
            ],
        })
        + '\n```'
    )
    adapter = _FakeJsonAgentAdapter([round1_text, round2_text])
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={'codex': adapter},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=ContextCompiler(artifacts),
    )
    task = mission.create_task(
        title=local.title,
        repo_path=str(git_repo),
        source_type='local_diff',
        backend='codex',
        pr_base_ref=local.base_branch,
        review_paths=local.changed_files,
        local_head_sha=local.head_sha,
        local_snapshot_sha=local.snapshot_sha,
    )
    artifacts.write_text(task.id, 'context/local_diff.patch', local.diff_text)

    try:
        # Round 1
        execution.start_task(task.id)
        assert _wait_until(lambda: not execution.is_active(task.id))
        f1 = db.list_findings(task.id)
        assert len(f1) == 2
        assert {f.status for f in f1} == {FindingStatus.NEW}
        extracted_events = [e for e in db.list_events(task.id) if e.kind == 'review.result_extracted']
        assert extracted_events[-1].payload['parse_mode'] == 'json'

        # Fix line 20 in working tree (uncommitted)
        lines = (git_repo / 'notes.py').read_text(encoding='utf-8').splitlines()
        lines[19] = 'query = "SELECT * FROM notes WHERE title LIKE ?"  # fixed'
        (git_repo / 'notes.py').write_text('\n'.join(lines) + '\n', encoding='utf-8')

        # Round 2
        execution.start_task(task.id)
        assert _wait_until(lambda: not execution.is_active(task.id))
        f2 = db.list_findings(task.id)
        by_cat = {f.category: f for f in f2}
        assert by_cat['sql-injection'].status == FindingStatus.LIKELY_FIXED
        assert by_cat['resource-leak'].status == FindingStatus.PERSISTENT
        assert by_cat['resource-leak'].first_round == 1
        assert by_cat['resource-leak'].last_round == 2
        assert not (git_repo / '.amc' / 'reviews').exists()
    finally:
        worktrees.cleanup_worktree(task)
        db.close()
