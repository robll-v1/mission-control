"""Comprehensive tests for v0.2.0 Cross-Model Cold Falsification.

Covers:
1. SQLite Schema v3 -> v4 backup/migration, attestation persistence & cascade delete.
2. Pure `verification_protocol.py` prompt isolation & defensive JSON parser.
3. `VerificationService` same-snapshot worktree execution, auto cross-model selection,
   Option A same-agent fallback (`cross_model=False`), shared task-slot mutex, and abort.
4. REST API (`POST /api/tasks/{id}/verify`), MCP `verify_findings`, and CLI
   `amc review --verify-with auto --format json --exit-code` (conservative exit 1).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import RunnerAdapter
from app.core.context_compiler import ContextCompiler
from app.core.db import SCHEMA_VERSION, Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import (
    AttestationStance,
    FindingAttestation,
    FindingStatus,
    Run,
    TaskStatus,
    TrackedFinding,
)
from app.core.verification_protocol import (
    build_falsification_prompt,
    parse_falsification_output,
    summarize_attestations,
)
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source
from app.services.mcp_service import McpReviewService
from app.services.verification_service import VerificationService


class _FakeAdapter(RunnerAdapter):
    def __init__(self, name: str, script_body: str, *, model: str | None = None, reasoning: str | None = None):
        self.name = name
        self.executable = sys.executable
        self.model = model
        self.reasoning = reasoning
        self.variant = reasoning
        self._script_body = script_body
        self.captured_prompts: list[str] = []

    def executable_name(self) -> str:  # type: ignore[override]
        return sys.executable

    def probe_command(self) -> list[str] | None:
        return [sys.executable, '-c', "print('OK')"]

    def make_command(self, *, task, prompt: str) -> list[str]:
        self.captured_prompts.append(prompt)
        return [sys.executable, '-c', self._script_body]


def _emit_json_script(payload: dict, *, sleep_sec: float = 0.0) -> str:
    body = json.dumps(payload)
    line = json.dumps({'type': 'text', 'part': {'text': f'```json\n{body}\n```'}})
    sleep_code = f'import time; time.sleep({sleep_sec}); ' if sleep_sec > 0 else ''
    return f'{sleep_code}print({line!r}, flush=True)'


# ── 1. Schema v3 -> v4 migration & cascade delete ─────────────────────


def test_sqlite_v3_to_v4_migration_and_attestation_cascade(tmp_path):
    db_file = tmp_path / 'amc.db'
    conn = sqlite3.connect(db_file)
    conn.executescript(
        '''
        CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT, seq INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE check_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE findings (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL, path TEXT, line INTEGER,
          title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', first_round INTEGER NOT NULL,
          last_round INTEGER NOT NULL, status TEXT NOT NULL, system_status TEXT NOT NULL,
          user_status TEXT, location_changed INTEGER NOT NULL DEFAULT 0,
          reopened INTEGER NOT NULL DEFAULT 0, mentioned_after_dismiss INTEGER NOT NULL DEFAULT 0,
          previous_path TEXT, previous_line INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(task_id, fingerprint)
        );
        CREATE TABLE finding_sync_state (task_id TEXT PRIMARY KEY, last_synced_round INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE task_event_seq (task_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0);
        PRAGMA user_version = 3;
        '''
    )
    conn.close()

    db = Database(str(db_file))
    try:
        assert db.schema_version() == SCHEMA_VERSION == 4
        assert db.last_backup_path is not None and db.last_backup_path.is_file()

        mission = MissionEngine(db)
        task = mission.create_task(title='t', repo_path=str(tmp_path), description='d', backend='codex')
        finding = TrackedFinding(
            id='f1',
            task_id=task.id,
            fingerprint='fp1',
            severity='high',
            path='app.py',
            line=10,
            title='Bug',
            created_at=time.time(),
            updated_at=time.time(),
        )
        db.save_findings([finding], task_id=task.id, synced_round=1)

        att = FindingAttestation(
            task_id=task.id,
            finding_id='f1',
            stance=AttestationStance.CONFIRMED,
            backend='claude-code',
            cross_model=True,
            rationale='Confirmed via direct code read.',
            evidence='app.py:10',
            trigger_path='call f(None)',
            created_at=time.time(),
        )
        db.save_attestations([att])

        loaded = db.list_findings(task.id)
        assert len(loaded) == 1
        assert len(loaded[0].attestations) == 1
        assert loaded[0].attestations[0].stance == AttestationStance.CONFIRMED
        assert loaded[0].attestations[0].backend == 'claude-code'

        # Deleting task cascades to finding_attestations
        assert db.delete_task(task.id) is True
        assert db.list_attestations(task.id) == []
    finally:
        db.close()


# ── 2. Pure protocol layer (prompt & parser) ──────────────────────────


def test_falsification_prompt_and_defensive_parser():
    findings = [
        TrackedFinding(
            id='f_real',
            task_id='t1',
            fingerprint='fp_a',
            severity='high',
            category='security',
            path='notes.py',
            line=24,
            title='SQL injection',
            detail='f-string interpolation',
        ),
        TrackedFinding(
            id='f_false',
            task_id='t1',
            fingerprint='fp_b',
            severity='medium',
            category='null-check',
            path='notes.py',
            line=40,
            title='Possible None deref',
            detail='x might be None',
        ),
        TrackedFinding(
            id='f_omitted',
            task_id='t1',
            fingerprint='fp_c',
            severity='low',
            path='notes.py',
            line=50,
            title='Minor issue',
        ),
    ]
    prompt = build_falsification_prompt(findings, language='en')
    assert 'COLD FALSIFICATION' in prompt
    assert 'f_real' in prompt and 'f_false' in prompt and 'f_omitted' in prompt
    # Cold prompt never reveals primary reviewer brand
    assert 'codex' not in prompt.lower() and 'claude' not in prompt.lower()

    raw = (
        'Inspected notes.py.\n```json\n'
        + json.dumps(
            {
                'verifications': [
                    {
                        'finding_id': 'f_real',
                        'stance': 'confirmed',
                        'rationale': 'User input flows directly into SQL.',
                        'evidence': 'notes.py:24',
                        'trigger_path': 'search("\' OR 1=1 --")',
                    },
                    {
                        'finding_id': 'f_false',
                        'stance': 'disputed',
                        'rationale': 'Guarded by `if x is None: return` on line 38.',
                        'evidence': 'notes.py:38',
                        'trigger_path': 'should be cleared for disputed',
                    },
                    {
                        'finding_id': 'f_hallucinated_id',
                        'stance': 'confirmed',
                        'rationale': 'Should be ignored.',
                    },
                ]
            }
        )
        + '\n```'
    )
    parsed, mode = parse_falsification_output(raw, ordered_finding_ids=['f_real', 'f_false', 'f_omitted'])
    assert mode == 'json'
    assert [p.finding_id for p in parsed] == ['f_real', 'f_false', 'f_omitted']
    assert parsed[0].stance == AttestationStance.CONFIRMED
    assert parsed[0].trigger_path == 'search("\' OR 1=1 --")'
    assert parsed[1].stance == AttestationStance.DISPUTED
    assert parsed[1].trigger_path == ''  # cleared for non-confirmed
    assert parsed[2].stance == AttestationStance.UNCERTAIN
    assert parsed[2].omitted is True

    # Malformed output degrades cleanly to uncertain for every finding
    bad_parsed, bad_mode = parse_falsification_output('no json here at all', ordered_finding_ids=['f_real'])
    assert bad_mode == 'fallback_uncertain'
    assert bad_parsed[0].stance == AttestationStance.UNCERTAIN

    # summarize_attestations
    findings[0].attestations = [
        FindingAttestation(
            task_id='t1', finding_id='f_real', stance=AttestationStance.CONFIRMED,
            backend='claude-code', cross_model=True,
        )
    ]
    findings[1].attestations = [
        FindingAttestation(
            task_id='t1', finding_id='f_false', stance=AttestationStance.DISPUTED,
            backend='claude-code', cross_model=True,
        )
    ]
    summary = summarize_attestations(findings)
    assert summary == {
        'confirmed': 1,
        'disputed': 1,
        'uncertain': 0,
        'unattested': 1,
        'cross_model_verified': 2,
    }


# ── 3. VerificationService E2E: same snapshot, cross-model, fallback, mutex, abort ─


def _seed_local_task_with_findings(git_repo: Path, tmp_path: Path, *, primary_backend: str = 'codex'):
    target_file = git_repo / 'vuln.py'
    target_file.write_text('def run(x):\n    return eval(x)\n', encoding='utf-8')

    db = Database(str(tmp_path / 'amc.db'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'), worktrees_root=str(tmp_path / 'wt'))
    contexts = ContextCompiler(artifacts)

    local = build_local_review_source(str(git_repo))
    task = mission.create_task(
        title=local.title,
        repo_path=str(git_repo),
        description=local.description,
        source_type='local_diff',
        backend=primary_backend,
        pr_base_ref=local.base_branch,
        review_paths=local.changed_files,
        local_head_sha=local.head_sha,
        local_snapshot_sha=local.snapshot_sha,
    )
    run = Run(
        task_id=task.id,
        backend=primary_backend,
        round_index=1,
        review_revision=local.snapshot_sha,
        status='completed',
        started_at=time.time(),
        ended_at=time.time(),
    )
    db.save_run(run)
    mission.set_stage(task.id, status=TaskStatus.WAITING_HUMAN, stage='review_completed')
    f1 = TrackedFinding(
        id='f_sqli',
        task_id=task.id,
        fingerprint='fp1',
        severity='high',
        path='vuln.py',
        line=2,
        title='Unsafe eval',
        detail='eval(x) executes arbitrary code',
        created_at=time.time(),
        updated_at=time.time(),
    )
    f2 = TrackedFinding(
        id='f_fp',
        task_id=task.id,
        fingerprint='fp2',
        severity='low',
        path='vuln.py',
        line=1,
        title='Spurious warning',
        detail='Not a real bug',
        created_at=time.time(),
        updated_at=time.time(),
    )
    db.save_findings([f1, f2], task_id=task.id, synced_round=1)
    return db, mission, artifacts, worktrees, contexts, task, target_file


def test_verification_service_cross_model_same_snapshot_and_never_auto_dismisses(
    git_repo, tmp_path, isolated_config
):
    db, mission, artifacts, worktrees, contexts, task, target_file = _seed_local_task_with_findings(
        git_repo, tmp_path, primary_backend='codex'
    )
    # Mutate working tree AFTER snapshot was captured; verifier MUST inspect frozen snapshot!
    target_file.write_text('def run(x):\n    return int(x)\n', encoding='utf-8')

    # Verifier script reads vuln.py inside its cwd (the frozen worktree) to prove same-snapshot!
    verify_script = (
        "import json; from pathlib import Path; "
        "src = Path('vuln.py').read_text(encoding='utf-8'); "
        "assert 'eval(x)' in src, f'Expected frozen snapshot with eval(x), got {src!r}'; "
        "out = {'verifications': ["
        "{'finding_id': 'f_sqli', 'stance': 'confirmed', 'rationale': 'eval present in snapshot', 'evidence': 'vuln.py:2', 'trigger_path': 'run(\"__import__(...)\")'},"
        "{'finding_id': 'f_fp', 'stance': 'disputed', 'rationale': 'false positive', 'evidence': 'vuln.py:1', 'trigger_path': ''}"
        "]}; "
        "print(json.dumps({'type': 'text', 'part': {'text': '```json\\n' + json.dumps(out) + '\\n```'}}), flush=True)"
    )
    codex_adapter = _FakeAdapter('codex', _emit_json_script({'verdict': 'clear', 'summary': 'ok', 'findings': []}))
    claude_adapter = _FakeAdapter('claude-code', verify_script)
    adapters = {'codex': codex_adapter, 'claude-code': claude_adapter}
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    verifier = VerificationService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
    )

    try:
        res = verifier.run_verification_sync(task.id)
        assert res['ok'] is True
        assert res['primary_backend'] == 'codex'
        assert res['verifier_backend'] == 'claude-code'
        assert res['cross_model'] is True
        assert res['review_revision'] == task.local_snapshot_sha
        assert res['summary']['confirmed'] == 1
        assert res['summary']['disputed'] == 1
        assert res['summary']['cross_model_verified'] == 2

        # Disputed finding MUST NOT be auto-dismissed or deleted!
        findings = {f.id: f for f in db.list_findings(task.id)}
        assert findings['f_fp'].status == FindingStatus.NEW
        assert findings['f_fp'].user_status is None
        assert findings['f_fp'].attestations[-1].stance == AttestationStance.DISPUTED
        assert findings['f_sqli'].attestations[-1].stance == AttestationStance.CONFIRMED
    finally:
        db.close()


def test_verification_service_option_a_same_agent_fallback_marks_cross_model_false(
    git_repo, tmp_path, isolated_config
):
    db, mission, artifacts, worktrees, contexts, task, _ = _seed_local_task_with_findings(
        git_repo, tmp_path, primary_backend='codex'
    )
    payload = {
        'verifications': [
            {'finding_id': 'f_sqli', 'stance': 'confirmed', 'rationale': 'Confirmed', 'evidence': 'vuln.py:2', 'trigger_path': 'x'},
            {'finding_id': 'f_fp', 'stance': 'disputed', 'rationale': 'Disputed', 'evidence': 'vuln.py:1'},
        ]
    }
    adapters = {'codex': _FakeAdapter('codex', _emit_json_script(payload))}
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    verifier = VerificationService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex')],
    )
    try:
        res = verifier.run_verification_sync(task.id)
        assert res['ok'] is True
        assert res['verifier_backend'] == 'codex'
        assert res['cross_model'] is False
        assert res['selection_reason'] == 'fallback_same_agent'
        assert res['summary']['cross_model_verified'] == 0
    finally:
        db.close()


def test_verification_service_shared_mutex_and_abort(git_repo, tmp_path, isolated_config):
    db, mission, artifacts, worktrees, contexts, task, _ = _seed_local_task_with_findings(
        git_repo, tmp_path, primary_backend='codex'
    )
    slow_claude = _FakeAdapter('claude-code', _emit_json_script({'verifications': []}, sleep_sec=20.0))
    adapters = {'codex': _FakeAdapter('codex', ''), 'claude-code': slow_claude}
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    verifier = VerificationService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
    )
    try:
        started = verifier.start_verification(task.id)
        assert started['ok'] is True
        # Shared task slot prevents concurrent start_task or second verification
        with pytest.raises(RuntimeError, match='already in progress|busy'):
            verifier.start_verification(task.id)

        assert verifier.abort_verification(task.id) is True
        settled = verifier.wait(task.id, timeout=5.0)
        assert settled is not None
        assert settled.get('aborted') is True
        # Slot is cleanly released and task restored to interactive status
        assert execution.is_active(task.id) is False
        assert db.get_task(task.id).status == TaskStatus.WAITING_HUMAN
    finally:
        db.close()


# ── 4. REST API + MCP verify_findings + CLI --verify-with ─────────────


def test_mcp_verify_findings_end_to_end(git_repo, tmp_path, isolated_config):
    db, mission, artifacts, worktrees, contexts, task, _ = _seed_local_task_with_findings(
        git_repo, tmp_path, primary_backend='codex'
    )
    payload = {
        'verifications': [
            {'finding_id': 'f_sqli', 'stance': 'confirmed', 'rationale': 'Real eval', 'evidence': 'vuln.py:2', 'trigger_path': 'run(x)'},
            {'finding_id': 'f_fp', 'stance': 'disputed', 'rationale': 'Safe', 'evidence': 'vuln.py:1'},
        ]
    }
    adapters = {
        'codex': _FakeAdapter('codex', _emit_json_script({'verdict': 'clear', 'summary': 'ok', 'findings': []})),
        'claude-code': _FakeAdapter('claude-code', _emit_json_script(payload)),
    }
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    service = McpReviewService(
        db=db,
        mission=mission,
        execution=execution,
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
        poll_interval_sec=0.05,
    )
    try:
        vres = service.verify_findings(task_id=task.id, wait_sec=15)
        assert vres['ok'] is True
        assert vres['done'] is True
        assert vres['cross_model'] is True
        assert vres['summary']['confirmed'] == 1
        assert vres['summary']['disputed'] == 1

        # wait_review now includes attestations on every finding!
        wres = service.wait_review(task_id=task.id, wait_sec=0)
        assert wres['done'] is True
        assert wres['attestation_counts']['confirmed'] == 1
        assert wres['attestation_counts']['disputed'] == 1
        by_id = {f['id']: f for f in wres['findings']}
        assert by_id['f_sqli']['attestations'][-1]['stance'] == 'confirmed'
        assert by_id['f_fp']['attestations'][-1]['stance'] == 'disputed'
    finally:
        service.shutdown()
        db.close()


def test_rest_api_verify_endpoint_and_task_detail(git_repo, tmp_path, isolated_config, monkeypatch):
    from app.api import app as api_app_mod
    from app.api import routes_control, routes_tasks

    db, mission, artifacts, worktrees, contexts, task, _ = _seed_local_task_with_findings(
        git_repo, tmp_path, primary_backend='codex'
    )
    payload = {
        'verifications': [
            {'finding_id': 'f_sqli', 'stance': 'confirmed', 'rationale': 'Confirmed', 'evidence': 'vuln.py:2', 'trigger_path': 'x'},
            {'finding_id': 'f_fp', 'stance': 'disputed', 'rationale': 'Disputed', 'evidence': 'vuln.py:1'},
        ]
    }
    adapters = {
        'codex': _FakeAdapter('codex', ''),
        'claude-code': _FakeAdapter('claude-code', _emit_json_script(payload)),
    }
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    verifier = VerificationService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
    )

    api_app_mod.app.dependency_overrides[routes_tasks.get_engine] = lambda: mission
    api_app_mod.app.dependency_overrides[routes_tasks.get_artifacts] = lambda: artifacts
    api_app_mod.app.dependency_overrides[routes_control.get_execution] = lambda: execution
    api_app_mod.app.dependency_overrides[routes_control.get_verification] = lambda: verifier
    try:
        client = TestClient(api_app_mod.app)
        resp = client.post(f'/api/tasks/{task.id}/verify', json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body['ok'] is True
        assert body['verifier_backend'] == 'claude-code'
        assert body['cross_model'] is True

        settled = verifier.wait(task.id, timeout=10.0)
        assert settled is not None and settled['ok'] is True

        detail = client.get(f'/api/tasks/{task.id}').json()
        assert detail['attestation_counts']['confirmed'] == 1
        assert detail['attestation_counts']['disputed'] == 1
        assert detail['findings'][0]['attestations']
    finally:
        api_app_mod.app.dependency_overrides.clear()
        db.close()


def test_cli_review_verify_with_keeps_conservative_exit_code_1(
    git_repo, tmp_path, isolated_config, monkeypatch, capsys
):
    """Even when Cold Falsification disputes every finding, `--exit-code` MUST stay 1."""
    from app import cli as cli_mod
    from app.core.models import ReviewFinding, ReviewVerdict
    from app.sdk import ReviewEngine, ReviewReport

    db, mission, artifacts, worktrees, contexts, task, _ = _seed_local_task_with_findings(
        git_repo, tmp_path, primary_backend='codex'
    )
    all_disputed_payload = {
        'verifications': [
            {'finding_id': 'f_sqli', 'stance': 'disputed', 'rationale': 'False alarm', 'evidence': 'vuln.py:2'},
            {'finding_id': 'f_fp', 'stance': 'disputed', 'rationale': 'False alarm', 'evidence': 'vuln.py:1'},
        ]
    }
    fake_claude = _FakeAdapter('claude-code', _emit_json_script(all_disputed_payload))
    fake_codex = _FakeAdapter('codex', '')
    adapters_map = {'codex': fake_codex, 'claude-code': fake_claude}
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters_map, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )

    class _StubEngine:
        def __init__(self, **_kwargs):
            self.model = None
            self.reasoning = None
            self._db = db
            self._mission = mission
            self._worktrees = worktrees
            self._artifacts = artifacts
            self._execution = execution

        def review(self, *_args, **_kwargs):
            return ReviewReport(
                task_id=task.id,
                verdict=ReviewVerdict.CONCERNS,
                passed=False,
                summary='2 findings',
                findings=[
                    ReviewFinding(severity='high', path='vuln.py', line=2, summary='Unsafe eval'),
                    ReviewFinding(severity='low', path='vuln.py', line=1, summary='Spurious warning'),
                ],
                finding_count=2,
                rounds_executed=1,
                duration_sec=0.5,
            )

    monkeypatch.setattr('app.sdk.ReviewEngine', _StubEngine)
    monkeypatch.setattr('app.adapters.get_adapter', lambda name, **_kw: adapters_map.get(name, fake_codex))
    monkeypatch.setattr('app.core.backend_detector.BackendDetector.detect_all', lambda: [
        SimpleNamespace(name='codex'),
        SimpleNamespace(name='claude-code'),
    ])
    monkeypatch.setattr('shutil.which', lambda name: sys.executable)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'amc',
            'review',
            '--repo',
            str(git_repo),
            '--backend',
            'codex',
            '--verify-with',
            'auto',
            '--format',
            'json',
            '--exit-code',
        ],
    )

    try:
        with pytest.raises(SystemExit) as exc_info:
            cli_mod.main()
        # Conservative CI policy: exit 1 even when all findings are disputed!
        assert exc_info.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out['verdict'] == 'concerns'
        assert out['passed'] is False
        assert out['verification']['ok'] is True
        assert out['verification']['verifier_backend'] == 'claude-code'
        assert out['verification']['cross_model'] is True
        assert out['verification']['summary']['disputed'] == 2
    finally:
        db.close()
