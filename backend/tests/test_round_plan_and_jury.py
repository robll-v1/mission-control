"""Tests for Direction 2: MIT Axiomatic Design decoupling (RoundPlan / Disposition / Continuation)
and Same-Snapshot Blind Dual-Review Jury + Targeted Disagreement Cold Falsification.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import RunnerAdapter
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.finding_tracker import DiffRevisionState
from app.core.mission_engine import MissionEngine
from app.core.models import (
    AttestationStance,
    FindingStatus as TrackedStatus,
    ReviewFinding,
    ReviewResult,
    ReviewVerdict,
    Run,
    TaskStatus,
)
from app.core.round_plan import (
    RoundPlan,
    compute_round_disposition,
    evaluate_continuation,
)
from app.core.worktree import WorktreeManager
from app.sdk import ReviewEngine
from app.services.artifact_store import ArtifactStore
from app.services.blind_jury_service import BlindJuryService, merge_jury_findings
from app.services.local_review_service import build_local_review_source
from app.services.mcp_service import McpReviewService
from app.services.review_history import StoredFinding


class _FakeAdapter(RunnerAdapter):
    def __init__(self, name: str, script_body: str, *, model: str | None = None, reasoning: str | None = None):
        self.name = name
        self.executable = sys.executable
        self.model = model
        self.reasoning = reasoning
        self.variant = reasoning
        self._script_body = script_body

    def executable_name(self) -> str:  # type: ignore[override]
        return sys.executable

    def probe_command(self) -> list[str] | None:
        return [sys.executable, '-c', "print('OK')"]

    def make_command(self, *, task, prompt: str) -> list[str]:
        return [sys.executable, '-c', self._script_body]


def _emit_text(payload: dict, *, sleep_sec: float = 0.0) -> str:
    body = json.dumps(payload)
    line = json.dumps({'type': 'text', 'part': {'text': f'```json\n{body}\n```'}})
    sleep_code = f'import time; time.sleep({sleep_sec}); ' if sleep_sec > 0 else ''
    return f'{sleep_code}print({line!r}, flush=True)'


# ── Part A: Pure RoundPlan / Disposition / Continuation decoupling ────


def test_disposition_separates_raw_verdict_from_aggregate_and_classifies_likely_fixed():
    prior = [
        StoredFinding(severity='high', path='auth.py', line=20, summary='Missing token expiry check'),
        StoredFinding(severity='medium', path='db.py', line=8, summary='Unclosed cursor'),
    ]
    raw_clear = ReviewResult(verdict=ReviewVerdict.CLEAR, summary='No new issues.', findings=[], finding_count=0)

    # Case 1: No diff touching auth.py or db.py -> both stay unverified, aggregate flipped to CONCERNS
    untouched = compute_round_disposition(raw_clear, is_incremental=True, previous_findings=prior, diff_state=None)
    assert untouched.raw_verdict == ReviewVerdict.CLEAR
    assert untouched.aggregate_verdict == ReviewVerdict.CONCERNS
    assert untouched.passed is False
    assert len(untouched.unverified_findings) == 2
    assert len(untouched.likely_fixed_findings) == 0

    # Case 2: Diff touched auth.py:20 ONLY -> auth.py becomes likely_fixed, db.py remains unverified
    partial_diff = DiffRevisionState(
        modified_files={'auth.py'},
        deleted_files=set(),
        renames={},
        hunks_by_path={'auth.py': [(19, 3, 19, 4)]},
        ranges_complete=True,
    )
    partial = compute_round_disposition(raw_clear, is_incremental=True, previous_findings=prior, diff_state=partial_diff)
    assert partial.raw_verdict == ReviewVerdict.CLEAR
    assert partial.aggregate_verdict == ReviewVerdict.CONCERNS
    assert partial.passed is False
    assert [f.path for f in partial.likely_fixed_findings] == ['auth.py']
    assert [f.path for f in partial.unverified_findings] == ['db.py']

    # Case 3: Diff touched BOTH auth.py:20 and db.py:8 -> both likely_fixed, aggregate CLEAR
    full_diff = DiffRevisionState(
        modified_files={'auth.py', 'db.py'},
        deleted_files=set(),
        renames={},
        hunks_by_path={'auth.py': [(19, 3, 19, 4)], 'db.py': [(7, 2, 7, 3)]},
        ranges_complete=True,
    )
    all_touched = compute_round_disposition(raw_clear, is_incremental=True, previous_findings=prior, diff_state=full_diff)
    assert all_touched.raw_verdict == ReviewVerdict.CLEAR
    assert all_touched.aggregate_verdict == ReviewVerdict.CLEAR
    assert all_touched.passed is True
    assert len(all_touched.likely_fixed_findings) == 2
    assert len(all_touched.unverified_findings) == 0


def test_continuation_policy_resolves_raw_clear_vs_aggregate_concerns_cycle():
    prior = [StoredFinding(severity='high', path='a.py', line=5, summary='Still unverified')]
    raw_clear = ReviewResult(verdict=ReviewVerdict.CLEAR, summary='Clean diff.', findings=[], finding_count=0)
    disp = compute_round_disposition(raw_clear, is_incremental=True, previous_findings=prior, diff_state=None)

    # Default policy (`stop_on_aggregate_clear`): does NOT stop early when unverified findings keep aggregate CONCERNS
    c1 = evaluate_continuation(disposition=disp, rounds_executed=1, max_rounds=2, policy='stop_on_aggregate_clear')
    assert c1.should_continue is True
    assert c1.stop_reason == 'continue_next_round'

    # Explicit `stop_on_raw_clear` policy stops with explicit reason (no hidden contradiction)
    c2 = evaluate_continuation(disposition=disp, rounds_executed=1, max_rounds=2, policy='stop_on_raw_clear')
    assert c2.should_continue is False
    assert c2.stop_reason == 'raw_clear_retained_unverified'

    # At max_rounds -> stops cleanly
    c3 = evaluate_continuation(disposition=disp, rounds_executed=2, max_rounds=2, policy='stop_on_aggregate_clear')
    assert c3.should_continue is False
    assert c3.stop_reason == 'max_rounds_reached'


def test_sdk_freezes_round_plan_and_respects_aggregate_continuation_policy(
    git_repo, tmp_path, isolated_config, monkeypatch
):
    """Verify SDK breaks 1<->5 (freezes RoundPlan) and 4<->6 (uses reconciled disposition for loop stop)."""
    (git_repo / 'app.py').write_text('def f(x):\n    return eval(x)\n', encoding='utf-8')
    engine = ReviewEngine(
        db_path=str(tmp_path / 'amc.db'),
        runtime_root=str(tmp_path / 'runtime'),
        backend='codex',
    )
    # Round 1 seeds a finding in SQLite
    monkeypatch.setattr(engine, 'start_review', lambda _tid: None)
    monkeypatch.setattr(engine, 'wait_for_completion', lambda tid, **_kw: engine._db.get_task(tid))
    monkeypatch.setattr(
        engine,
        'get_result',
        lambda _tid: ReviewResult(
            verdict=ReviewVerdict.CONCERNS,
            summary='Found eval.',
            findings=[ReviewFinding(severity='high', path='app.py', line=2, summary='Unsafe eval')],
            finding_count=1,
            severity_counts={'high': 1},
        ),
    )
    r1 = engine.review(str(git_repo), base='main', incremental=False)
    assert r1.round_plan is not None
    assert r1.round_plan.base_reason == 'explicit_base'
    assert r1.round_plan.incremental_active is False
    assert r1.raw_verdict == ReviewVerdict.CONCERNS
    assert r1.disposition is not None and r1.continuation is not None

    # Now simulate incremental run where model returns raw CLEAR on an UNTOUCHED file with max_rounds=2:
    # Old SDK broke after 1 round on raw CLEAR yet returned CONCERNS!
    # New SDK with default `stop_on_aggregate_clear` executes round 2 because aggregate is CONCERNS.
    calls = {'count': 0}

    def _start_counter(_tid):
        calls['count'] += 1

    monkeypatch.setattr(engine, 'start_review', _start_counter)
    monkeypatch.setattr(
        engine,
        'get_result',
        lambda _tid: ReviewResult(verdict=ReviewVerdict.CLEAR, summary='No new issues.', findings=[], finding_count=0),
    )
    r2 = engine.review(str(git_repo), base='main', max_rounds=2, incremental=True)
    assert calls['count'] == 2
    assert r2.rounds_executed == 2
    assert r2.raw_verdict == ReviewVerdict.CLEAR
    assert r2.verdict == ReviewVerdict.CONCERNS
    assert r2.passed is False
    assert r2.continuation is not None and r2.continuation.stop_reason == 'max_rounds_reached'
    assert r2.round_plan is not None and r2.round_plan.incremental_active is True

    # Event stream recorded frozen RoundPlan
    events = engine._db.list_events(r2.task_id)
    assert any(ev.kind == 'review.plan_frozen' for ev in events)


# ── Part B: Blind Dual-Review Jury + Targeted Disagreement Falsification ─


def test_merge_jury_findings_identifies_consensus_and_unique_catches():
    fa = [
        ReviewFinding(severity='high', category='security', path='notes.py', line=24, summary='SQL injection in search'),
        ReviewFinding(severity='medium', category='concurrency', path='exec.py', line=100, summary='Race on delete slot'),
    ]
    fb = [
        ReviewFinding(severity='critical', category='security', path='notes.py', line=25, summary='SQL injection vulnerability in search'),
        ReviewFinding(severity='low', category='style', path='utils.py', line=12, summary='Unused variable'),
    ]
    merged = merge_jury_findings('codex', fa, 'claude-code', fb)
    assert len(merged) == 3
    consensus = [(f, r) for f, r in merged if len(r) == 2]
    unique_a = [(f, r) for f, r in merged if r == ['codex']]
    unique_b = [(f, r) for f, r in merged if r == ['claude-code']]
    assert len(consensus) == 1
    assert consensus[0][0].severity == 'critical'  # keeps higher severity
    assert len(unique_a) == 1 and unique_a[0][0].path == 'exec.py'
    assert len(unique_b) == 1 and unique_b[0][0].path == 'utils.py'


def _make_jury_env(git_repo: Path, tmp_path: Path):
    (git_repo / 'notes.py').write_text('def search(q):\n    return f"SELECT * FROM t WHERE x={q}"\n', encoding='utf-8')
    (git_repo / 'exec.py').write_text('def delete_it():\n    return True\n', encoding='utf-8')
    db = Database(str(tmp_path / 'jury.db'))
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
        backend='codex',
        pr_base_ref=local.base_branch,
        review_paths=local.changed_files,
        local_head_sha=local.head_sha,
        local_snapshot_sha=local.snapshot_sha,
    )
    mission.set_stage(task.id, status=TaskStatus.CONTEXT_READY, stage='review_ready')
    return db, mission, artifacts, worktrees, contexts, task


def test_blind_jury_parallel_consensus_and_targeted_disagreement_falsification(
    git_repo, tmp_path, isolated_config
):
    """2/2 consensus skips extra falsification; 1/2 unique finding is falsified by the OTHER juror."""
    db, mission, artifacts, worktrees, contexts, task = _make_jury_env(git_repo, tmp_path)

    # Smart script: if prompt is a COLD FALSIFICATION prompt, emit falsification verdict;
    # otherwise emit blind review findings!
    def _dual_mode_script(blind_payload: dict, falsify_stance: str) -> str:
        return (
            'import json, sys\n'
            f'blind = {blind_payload!r}\n'
            f'stance = {falsify_stance!r}\n'
            'prompt = sys.argv[1]\n'
            'if "COLD FALSIFICATION" in prompt:\n'
            '    import re\n'
            '    ids = re.findall(r\'"finding_id":\\s*"([^"]+)"\', prompt)\n'
            '    out = {"verifications": [{"finding_id": i, "stance": stance, "rationale": f"checked by verifier ({stance})", "evidence": "exec.py:1", "trigger_path": "call()" if stance == "confirmed" else ""} for i in ids]}\n'
            'else:\n'
            '    out = blind\n'
            'print(json.dumps({"type": "text", "part": {"text": "```json\\n" + json.dumps(out) + "\\n```"}}), flush=True)\n'
        )

    class _PromptPassingAdapter(RunnerAdapter):
        def __init__(self, name: str, script: str):
            self.name = name
            self.executable = sys.executable
            self.model = f'{name}-v1'
            self.reasoning = None
            self.variant = None
            self._script = script
            self.prompts: list[str] = []

        def executable_name(self) -> str:  # type: ignore[override]
            return sys.executable

        def make_command(self, *, task, prompt: str) -> list[str]:
            self.prompts.append(prompt)
            return [sys.executable, '-c', self._script, prompt]

    codex_blind = {
        'verdict': 'concerns',
        'summary': 'Codex found SQLi and race.',
        'findings': [
            {'severity': 'high', 'category': 'security', 'path': 'notes.py', 'line': 2, 'title': 'SQL injection in search', 'detail': 'f-string'},
            {'severity': 'medium', 'category': 'concurrency', 'path': 'exec.py', 'line': 1, 'title': 'Delete race', 'detail': 'race'},
        ],
    }
    claude_blind = {
        'verdict': 'concerns',
        'summary': 'Claude found SQLi only.',
        'findings': [
            {'severity': 'critical', 'category': 'security', 'path': 'notes.py', 'line': 2, 'title': 'SQL injection in search', 'detail': 'f-string'},
        ],
    }

    codex_adp = _PromptPassingAdapter('codex', _dual_mode_script(codex_blind, 'confirmed'))
    claude_adp = _PromptPassingAdapter('claude-code', _dual_mode_script(claude_blind, 'disputed'))
    adapters = {'codex': codex_adp, 'claude-code': claude_adp}
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    jury = BlindJuryService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
    )

    try:
        res = jury.run_jury_sync(task.id, falsify_disagreements=True, parallel=True)
        assert res['ok'] is True
        assert res['jurors'] == ['codex', 'claude-code']
        assert res['total_findings'] == 2
        assert res['consensus_count'] == 1
        assert len(res['unique_by_juror']['codex']) == 1
        assert len(res['unique_by_juror']['claude-code']) == 0

        # Codex ran ONLY 1 blind review (Claude had 0 unique findings to send to Codex)
        assert len(codex_adp.prompts) == 1
        # Claude ran 1 blind review + 1 targeted falsification ONLY for Codex's unique finding!
        assert len(claude_adp.prompts) == 2
        assert 'COLD FALSIFICATION' in claude_adp.prompts[1]
        assert 'Delete race' in claude_adp.prompts[1]
        assert 'SQL injection' not in claude_adp.prompts[1]  # 2/2 consensus NOT re-checked!

        findings = db.list_findings(task.id)
        by_path = {f.path: f for f in findings}
        # Consensus finding has 2 independent_review attestations (cross_model=True)
        sqli_atts = by_path['notes.py'].attestations
        assert len(sqli_atts) == 2
        assert {a.backend for a in sqli_atts} == {'codex', 'claude-code'}
        assert all(a.kind == 'independent_review' and a.cross_model for a in sqli_atts)

        # Unique finding has 1 independent_review (codex) + 1 falsification (claude-code, disputed),
        # and is NEVER auto-dismissed!
        race_atts = by_path['exec.py'].attestations
        assert len(race_atts) == 2
        assert race_atts[0].kind == 'independent_review' and race_atts[0].backend == 'codex'
        assert race_atts[1].kind == 'falsification' and race_atts[1].backend == 'claude-code'
        assert race_atts[1].stance == AttestationStance.DISPUTED
        assert by_path['exec.py'].status == TrackedStatus.NEW

        # Ephemeral jury worktrees were cleaned up
        wt_root = worktrees._worktrees_root_for_repo(str(git_repo))
        leftover_jury_wts = [p for p in wt_root.glob(f'{task.id}-jury-*')] if wt_root.exists() else []
        assert leftover_jury_wts == []
    finally:
        db.close()


def test_blind_jury_rejects_single_installed_cli_honestly(git_repo, tmp_path, isolated_config):
    db, mission, artifacts, worktrees, contexts, task = _make_jury_env(git_repo, tmp_path)
    adapters = {'codex': _FakeAdapter('codex', '')}
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    jury = BlindJuryService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex')],
    )
    try:
        with pytest.raises(RuntimeError, match='at least 2 installed'):
            jury.start_jury(task.id)
    finally:
        db.close()


def test_blind_jury_degrades_gracefully_on_falsification_failure_and_supports_abort(
    git_repo, tmp_path, isolated_config
):
    db, mission, artifacts, worktrees, contexts, task = _make_jury_env(git_repo, tmp_path)

    # First test graceful degradation when falsification fails (exit 3)
    codex_blind = {
        'verdict': 'concerns',
        'summary': 'Found 1 unique bug.',
        'findings': [{'severity': 'high', 'path': 'exec.py', 'line': 1, 'title': 'Unique bug', 'detail': 'd'}],
    }
    claude_script = (
        'import json, sys\n'
        'if "COLD FALSIFICATION" in sys.argv[1]:\n'
        '    sys.exit(3)\n'
        'out = {"verdict": "clear", "summary": "ok", "findings": []}\n'
        'print(json.dumps({"type": "text", "part": {"text": "```json\\n" + json.dumps(out) + "\\n```"}}), flush=True)\n'
    )

    class _ArgAdapter(RunnerAdapter):
        def __init__(self, name: str, script: str):
            self.name = name
            self.executable = sys.executable
            self.model = None
            self.reasoning = None
            self.variant = None
            self._script = script

        def executable_name(self) -> str:  # type: ignore[override]
            return sys.executable

        def make_command(self, *, task, prompt: str) -> list[str]:
            return [sys.executable, '-c', self._script, prompt]

    adapters = {
        'codex': _FakeAdapter('codex', _emit_text(codex_blind)),
        'claude-code': _ArgAdapter('claude-code', claude_script),
    }
    execution = ExecutionService(
        db=db, mission=mission, adapters=adapters, worktrees=worktrees, artifacts=artifacts, contexts=contexts,
    )
    jury = BlindJuryService(
        db=db,
        mission=mission,
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        adapters=adapters,
        execution=execution,
        detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
    )
    try:
        res = jury.run_jury_sync(task.id, falsify_disagreements=True, parallel=False)
        assert res['ok'] is True
        assert res['total_findings'] == 1
        findings = db.list_findings(task.id)
        assert len(findings) == 1
        # Original 1/2 finding preserved + degraded falsification marked uncertain
        assert findings[0].attestations[-1].kind == 'falsification'
        assert findings[0].attestations[-1].stance == AttestationStance.UNCERTAIN

        # Now test abort on slow parallel jurors
        slow_adapters = {
            'codex': _FakeAdapter('codex', _emit_text({'verdict': 'clear', 'summary': '', 'findings': []}, sleep_sec=20.0)),
            'claude-code': _FakeAdapter('claude-code', _emit_text({'verdict': 'clear', 'summary': '', 'findings': []}, sleep_sec=20.0)),
        }
        slow_jury = BlindJuryService(
            db=db,
            mission=mission,
            worktrees=worktrees,
            artifacts=artifacts,
            contexts=contexts,
            adapters=slow_adapters,
            execution=execution,
            detect_all_fn=lambda: [SimpleNamespace(name='codex'), SimpleNamespace(name='claude-code')],
        )
        started = slow_jury.start_jury(task.id)
        assert started['ok'] is True
        assert slow_jury.abort_jury(task.id) is True
        aborted_res = slow_jury.wait(task.id, timeout=6.0)
        assert aborted_res is not None and aborted_res.get('aborted') is True
        assert execution.is_active(task.id) is False
    finally:
        db.close()


def test_mcp_and_rest_api_blind_jury_integration(git_repo, tmp_path, isolated_config):
    from app.api import app as api_app_mod
    from app.api import routes_control, routes_tasks

    db, mission, artifacts, worktrees, contexts, task = _make_jury_env(git_repo, tmp_path)
    shared_payload = {
        'verdict': 'concerns',
        'summary': 'Found SQLi.',
        'findings': [{'severity': 'high', 'path': 'notes.py', 'line': 2, 'title': 'SQL injection', 'detail': 'f-string'}],
    }
    adapters = {
        'codex': _FakeAdapter('codex', _emit_text(shared_payload)),
        'claude-code': _FakeAdapter('claude-code', _emit_text(shared_payload)),
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
    api_app_mod.app.dependency_overrides[routes_tasks.get_engine] = lambda: mission
    api_app_mod.app.dependency_overrides[routes_tasks.get_artifacts] = lambda: artifacts
    api_app_mod.app.dependency_overrides[routes_control.get_execution] = lambda: execution
    api_app_mod.app.dependency_overrides[routes_control.get_blind_jury] = lambda: service.blind_jury
    try:
        # Test MCP tool run_blind_jury
        mcp_res = service.run_blind_jury(task_id=task.id, wait_sec=15)
        assert mcp_res['ok'] is True
        assert mcp_res['done'] is True
        assert mcp_res['consensus_count'] == 1

        # Test REST API POST /api/tasks/{id}/jury
        client = TestClient(api_app_mod.app)
        resp = client.post(f'/api/tasks/{task.id}/jury', json={'falsify_disagreements': False})
        assert resp.status_code == 200
        assert resp.json()['jurors'] == ['codex', 'claude-code']
        settled = service.blind_jury.wait(task.id, timeout=10.0)
        assert settled is not None and settled['ok'] is True
    finally:
        api_app_mod.app.dependency_overrides.clear()
        service.shutdown()
        db.close()
