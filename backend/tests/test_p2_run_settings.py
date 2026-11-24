"""P2 tests: RunSettings resolver, reasoning detection, adapters, config API, and per-round switching."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters import get_adapter
from app.adapters.claude_code_adapter import ClaudeCodeAdapter
from app.adapters.codex_adapter import CodexAdapter
from app.adapters.copilot_adapter import CopilotAdapter
from app.adapters.opencode_adapter import OpenCodeAdapter
from app.api import routes_config, routes_control, routes_tasks
from app.core.backend_detector import BackendDetector
from app.core.config import UNSET, SettingsResolver, load_global_config, resolve_backend_config, save_global_config
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import Task
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import build_local_review_source


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    return home


# ── SettingsResolver & Config Layering ──────────────────────────────────────

def test_settings_resolver_precedence_and_sources(tmp_path, isolated_config, monkeypatch):
    save_global_config({
        'backend': {
            'default': 'codex',
            'codex': {'model': 'gpt-global', 'reasoning': 'medium'},
            'claude-code': {'model': 'claude-global', 'reasoning': 'high'},
        }
    })
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.amc.yaml').write_text(
        'backend:\n  codex:\n    model: gpt-repo\n    reasoning: xhigh\n',
        encoding='utf-8',
    )

    # Repo beats global
    s1 = SettingsResolver.resolve(str(repo))
    assert s1.agent == 'codex'
    assert s1.model == 'gpt-repo'
    assert s1.reasoning == 'xhigh'
    assert s1.sources == {'agent': 'global', 'model': 'repo', 'reasoning': 'repo', 'language': 'default'}

    # Review beats repo
    s2 = SettingsResolver.resolve(
        str(repo),
        review_agent='codex',
        review_model='gpt-review',
        review_reasoning='low',
        review_language='zh',
    )
    assert s2.model == 'gpt-review'
    assert s2.reasoning == 'low'
    assert s2.language == 'zh'
    assert s2.sources == {'agent': 'review', 'model': 'review', 'reasoning': 'review', 'language': 'review'}

    # Env beats review/repo/global
    monkeypatch.setenv('AMC_MODEL', 'gpt-env')
    monkeypatch.setenv('AMC_REASONING', 'max')
    s3 = SettingsResolver.resolve(str(repo), review_agent='codex', review_model='gpt-review', review_reasoning='low')
    assert s3.model == 'gpt-env'
    assert s3.reasoning == 'max'
    assert s3.sources['model'] == 'env'
    assert s3.sources['reasoning'] == 'env'

    # Round beats env, AND explicit None forces Model default / CLI default
    s4 = SettingsResolver.resolve(
        str(repo),
        round_agent='codex',
        round_model=None,
        round_reasoning=None,
        round_language='en',
        review_agent='codex',
        review_model='gpt-review',
        review_reasoning='low',
    )
    assert s4.model is None
    assert s4.reasoning is None
    assert s4.language == 'en'
    assert s4.sources == {'agent': 'round', 'model': 'round', 'reasoning': 'round', 'language': 'round'}


def test_settings_resolver_never_leaks_model_or_reasoning_across_agents(tmp_path, isolated_config):
    save_global_config({
        'backend': {
            'default': 'codex',
            'codex': {'model': 'gpt-6.2-sol', 'reasoning': 'xhigh'},
            'claude-code': {'model': 'claude-opus-5.5', 'reasoning': 'max'},
        }
    })
    repo = tmp_path / 'repo'
    repo.mkdir()

    # Task was created for codex with custom review_model & review_reasoning,
    # then Round 2 switches to claude-code without specifying round model/reasoning.
    s = SettingsResolver.resolve(
        str(repo),
        round_agent='claude-code',
        review_agent='codex',
        review_model='gpt-custom',
        review_reasoning='ultra',
    )
    assert s.agent == 'claude-code'
    assert s.model == 'claude-opus-5.5'
    assert s.reasoning == 'max'
    assert s.sources['agent'] == 'round'
    assert s.sources['model'] == 'global'
    assert s.sources['reasoning'] == 'global'

    # Switching to an agent with no saved config resolves to CLI/Model default (None), never codex's model.
    s_copilot = SettingsResolver.resolve(
        str(repo),
        round_agent='copilot',
        review_agent='codex',
        review_model='gpt-custom',
        review_reasoning='ultra',
    )
    assert s_copilot.agent == 'copilot'
    assert s_copilot.model is None
    assert s_copilot.reasoning is None
    assert s_copilot.sources['model'] == 'default'
    assert s_copilot.sources['reasoning'] == 'default'


def test_variant_remains_backward_compatible_alias_for_reasoning(tmp_path, isolated_config):
    save_global_config({
        'backend': {
            'default': 'opencode',
            'opencode': {'model': 'openai/gpt-6', 'variant': 'high'},
        }
    })
    s = SettingsResolver.resolve(str(tmp_path))
    assert s.reasoning == 'high'
    assert resolve_backend_config(str(tmp_path)) == {
        'backend': 'opencode',
        'model': 'openai/gpt-6',
        'variant': 'high',
    }


# ── BackendDetector Reasoning Discovery ─────────────────────────────────────

def test_codex_detector_reads_per_model_reasoning_from_cache(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'models_cache.json').write_text(json.dumps({
        'models': [
            {
                'slug': 'gpt-6.2-sol',
                'visibility': 'list',
                'default_reasoning_level': 'low',
                'supported_reasoning_levels': [
                    {'effort': 'low', 'description': 'Fast'},
                    {'effort': 'high', 'description': 'Deep'},
                    {'effort': 'xhigh', 'description': 'Extra'},
                ],
            },
            {
                'slug': 'gpt-6-luna',
                'visibility': 'list',
                'default_reasoning_level': 'medium',
                'supported_reasoning_levels': ['low', 'medium', 'max'],
            },
            {
                'slug': 'hidden-model',
                'visibility': 'hide',
                'default_reasoning_level': 'ultra',
                'supported_reasoning_levels': ['ultra'],
            },
        ]
    }), encoding='utf-8')
    monkeypatch.setattr(
        BackendDetector, '_run_command', staticmethod(lambda cmd, timeout=5: (True, 'codex-cli 0.169.0')),
    )

    info = BackendDetector._detect_codex()
    assert info is not None
    assert info.reasoning_supported is True
    assert info.reasoning_source == 'cache'
    assert info.reasoning_levels == ['low', 'medium', 'high', 'xhigh', 'max']
    assert 'ultra' not in info.reasoning_levels
    assert info.model_reasoning['gpt-6.2-sol'] == {'levels': ['low', 'high', 'xhigh'], 'default': 'low'}
    assert info.model_reasoning['gpt-6-luna'] == {'levels': ['low', 'medium', 'max'], 'default': 'medium'}


def test_codex_detector_hides_reasoning_when_cache_missing(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        BackendDetector, '_run_command', staticmethod(lambda cmd, timeout=5: (True, 'codex-cli 0.169.0')),
    )

    info = BackendDetector._detect_codex()
    assert info is not None
    assert info.reasoning_supported is False
    assert info.reasoning_levels == []
    assert info.reasoning_source == 'unknown'
    assert 'models_cache.json' in (info.reasoning_note or '')


def test_claude_code_detector_parses_effort_levels_from_help(monkeypatch):
    responses = {
        ('claude', '--version'): (True, '2.1.310 (Claude Code)'),
        ('claude', '--help'): (
            True,
            'Options:\n'
            '  --effort <level>  Effort level for the current session\n'
            '                    (low, medium, high, xhigh, max)\n'
            '  --model <model>   Model name\n',
        ),
    }
    monkeypatch.setattr(
        BackendDetector,
        '_run_command',
        staticmethod(lambda cmd, timeout=5: responses.get(tuple(cmd), (False, ''))),
    )

    info = BackendDetector._detect_claude_code()
    assert info is not None
    assert info.reasoning_supported is True
    assert info.reasoning_source == 'cli_help'
    assert info.reasoning_levels == ['low', 'medium', 'high', 'xhigh', 'max']


def test_copilot_detector_declares_reasoning_unsupported(monkeypatch):
    import app.core.backend_detector as detector_mod

    monkeypatch.setattr(detector_mod.shutil, 'which', lambda name: '/usr/bin/gh' if name == 'gh' else None)
    monkeypatch.setattr(
        BackendDetector, '_run_command', staticmethod(lambda cmd, timeout=5: (True, 'gh version 2.60.0')),
    )

    info = BackendDetector._detect_copilot()
    assert info is not None
    assert info.reasoning_supported is False
    assert info.reasoning_source == 'unsupported'


# ── Adapters: Flags, Safety Validation, and Non-LLM Doctor Commands ─────────

def test_adapters_pass_reasoning_and_build_non_llm_doctor_commands():
    task = Task(title='T', repo_path='/tmp/repo', backend='codex', created_at=0.0, updated_at=0.0)

    codex = get_adapter('codex', model='gpt-6.2-sol', reasoning='xhigh')
    assert isinstance(codex, CodexAdapter)
    cmd = codex.make_command(task=task, prompt='review')
    assert '--model' in cmd and 'gpt-6.2-sol' in cmd
    assert '-c' in cmd and 'model_reasoning_effort=xhigh' in cmd
    # doctor_command uses fixed arguments only (safe against Windows .cmd/.bat shims)
    assert codex.doctor_command()[-1] == '--version'
    assert 'gpt-6.2-sol' not in codex.doctor_command()
    assert 'model_reasoning_effort=xhigh' not in codex.doctor_command()

    # Default reasoning (None) passes no -c model_reasoning_effort flag
    codex_default = get_adapter('codex', model='gpt-6.2-sol', reasoning=None)
    assert '-c' not in codex_default.make_command(task=task, prompt='review')

    # Shell/TOML injection in model or reasoning rejected
    with pytest.raises(ValueError, match='invalid reasoning'):
        get_adapter('codex', reasoning='high\nsandbox=danger')
    with pytest.raises(ValueError, match='invalid model'):
        get_adapter('codex', model='gpt-6&calc.exe')

    claude = get_adapter('claude-code', model='claude-opus-5.5', reasoning='max')
    assert isinstance(claude, ClaudeCodeAdapter)
    c_cmd = claude.make_command(task=task, prompt='review')
    assert '--model' in c_cmd and 'claude-opus-5.5' in c_cmd
    assert '--effort' in c_cmd and 'max' in c_cmd
    assert claude.doctor_command()[-1] == '--version'
    assert 'claude-opus-5.5' not in claude.doctor_command()

    with pytest.raises(ValueError, match='invalid reasoning'):
        get_adapter('claude-code', reasoning='--dangerously-skip')
    with pytest.raises(ValueError, match='invalid model'):
        get_adapter('claude-code', model='sonnet|whoami')

    opencode = get_adapter('opencode', model='openai/gpt-6', reasoning='high')
    assert isinstance(opencode, OpenCodeAdapter)
    o_cmd = opencode.make_command(task=task, prompt='review')
    assert 'openai/gpt-6#high' in o_cmd
    # Static doctor command must NEVER invoke `opencode run ...` (an LLM call).
    assert opencode.doctor_command()[-1] == '--version'
    assert 'run' not in opencode.doctor_command()

    # OpenCode without model never passes #variant; adapter must reflect None
    opencode_no_model = get_adapter('opencode', model=None, reasoning='high')
    assert opencode_no_model.reasoning is None
    assert opencode_no_model.variant is None

    copilot = get_adapter('copilot', model='gpt-4', reasoning='high')
    assert isinstance(copilot, CopilotAdapter)
    assert copilot.reasoning is None
    assert copilot.doctor_command()[-2:] == ['copilot', '--help']


# ── Config API: Explicit Null Reset, Effective Settings, and Check Agent ────

def _config_client() -> TestClient:
    api = FastAPI()
    api.include_router(routes_config.router)
    return TestClient(api)


def test_config_save_distinguishes_omitted_vs_explicit_null_reset(isolated_config):
    client = _config_client()

    # Save codex model + reasoning
    r1 = client.post('/api/config/save', json={'backend': 'codex', 'model': 'gpt-6.2-sol', 'reasoning': 'xhigh'})
    assert r1.status_code == 200
    cur1 = client.get('/api/config/current').json()['config']
    assert cur1['backend'] == 'codex'
    assert cur1['model'] == 'gpt-6.2-sol'
    assert cur1['reasoning'] == 'xhigh'
    assert cur1['variant'] == 'xhigh'
    assert cur1['per_backend']['codex']['reasoning'] == 'xhigh'

    # Saving model only (reasoning omitted) MUST keep existing reasoning
    r2 = client.post('/api/config/save', json={'backend': 'codex', 'model': 'gpt-6.1-terra'})
    assert r2.status_code == 200
    cur2 = client.get('/api/config/current').json()['config']
    assert cur2['model'] == 'gpt-6.1-terra'
    assert cur2['reasoning'] == 'xhigh'

    # Explicit null MUST reset reasoning back to Model default (None)
    r3 = client.post('/api/config/save', json={'backend': 'codex', 'model': 'gpt-6.1-terra', 'reasoning': None})
    assert r3.status_code == 200
    cur3 = client.get('/api/config/current').json()['config']
    assert cur3['reasoning'] is None
    assert cur3['variant'] is None
    raw = load_global_config()
    assert 'reasoning' not in raw['backend']['codex']
    assert 'variant' not in raw['backend']['codex']

    # Backward-compatible variant field: explicit variant: null preserves saved reasoning!
    r4 = client.post('/api/config/save', json={'backend': 'opencode', 'model': 'openai/gpt-6', 'variant': 'high'})
    assert r4.status_code == 200
    r4_legacy = client.post('/api/config/save', json={'backend': 'opencode', 'model': 'openai/gpt-6', 'variant': None})
    assert r4_legacy.status_code == 200
    cur4 = client.get('/api/config/current').json()['config']
    assert cur4['backend'] == 'opencode'
    assert cur4['reasoning'] == 'high'
    assert cur4['variant'] == 'high'
    # Previous codex saved entry preserved in per_backend
    assert cur4['per_backend']['codex']['model'] == 'gpt-6.1-terra'


def test_config_effective_and_check_agent_endpoints(git_repo, tmp_path, isolated_config, monkeypatch):
    from app.core.backend_detector import BackendInfo

    client = _config_client()
    save_global_config({
        'backend': {
            'default': 'codex',
            'codex': {'model': 'gpt-6.2-sol', 'reasoning': 'high'},
        }
    })
    (git_repo / '.amc.yaml').write_text('backend:\n  codex:\n    reasoning: xhigh\n', encoding='utf-8')

    eff = client.get('/api/config/effective', params={'repo_path': str(git_repo)})
    assert eff.status_code == 200
    body = eff.json()
    assert body['backend'] == 'codex'
    assert body['model'] == 'gpt-6.2-sol'
    assert body['reasoning'] == 'xhigh'
    assert body['sources']['model'] == 'global'
    assert body['sources']['reasoning'] == 'repo'

    # Non-git directory rejected by /effective
    non_git = tmp_path / 'not_a_repo'
    non_git.mkdir()
    assert client.get('/api/config/effective', params={'repo_path': str(non_git)}).status_code == 400

    # Copilot /effective matches actual adapter behavior (reasoning degraded to None)
    eff_cop = client.get('/api/config/effective', params={'backend': 'copilot', 'reasoning': 'high'})
    assert eff_cop.status_code == 200
    assert eff_cop.json()['reasoning'] is None
    assert eff_cop.json()['sources']['reasoning'] == 'unsupported'

    # OpenCode without model degrades reasoning to None to match CLI argv
    eff_oc = client.get('/api/config/effective', params={'backend': 'opencode', 'reasoning': 'high'})
    assert eff_oc.status_code == 200
    assert eff_oc.json()['reasoning'] is None

    # Check agent runs fixed-arg static doctor_command and validates reasoning in-process
    seen_cmds: list[list[str]] = []

    def fake_run_text(cmd, timeout=15):
        seen_cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout='codex-cli-exec 0.169.0\n', stderr='')

    fake_codex_info = BackendInfo(
        'codex', 'codex', 'codex-cli 0.169.0', ['gpt-6.2-sol'],
        reasoning_supported=True,
        reasoning_levels=['low', 'medium', 'high', 'xhigh'],
        reasoning_source='cache',
        model_reasoning={'gpt-6.2-sol': {'levels': ['low', 'high', 'xhigh'], 'default': 'low'}},
    )
    monkeypatch.setattr(routes_config.shutil, 'which', lambda name: f'/usr/local/bin/{name}')
    monkeypatch.setattr(routes_config, 'run_text', fake_run_text)
    monkeypatch.setattr(routes_config.BackendDetector, 'detect_backend', staticmethod(lambda _n: fake_codex_info))

    chk = client.post('/api/config/check-agent', json={
        'backend': 'codex',
        'model': 'gpt-6.2-sol',
        'reasoning': 'xhigh',
    })
    assert chk.status_code == 200
    data = chk.json()
    assert data['ok'] is True
    assert data['exit_code'] == 0
    assert data['command'][-1] == '--version'
    # User-supplied model/reasoning must NOT be interpolated into doctor_command
    assert 'gpt-6.2-sol' not in data['command']
    assert len(seen_cmds) == 1

    # Unknown reasoning level rejected with ok=False instead of false-positive pass
    chk_bad_lvl = client.post('/api/config/check-agent', json={
        'backend': 'codex',
        'model': 'gpt-6.2-sol',
        'reasoning': 'unknownlevel',
    })
    assert chk_bad_lvl.status_code == 200
    assert chk_bad_lvl.json()['ok'] is False
    assert 'unknownlevel' in chk_bad_lvl.json()['message']

    # Invalid chars in reasoning or model rejected with 422
    assert client.post('/api/config/check-agent', json={'backend': 'codex', 'reasoning': 'bad;rm'}).status_code == 422
    assert client.post('/api/config/check-agent', json={'backend': 'codex', 'model': 'm&calc.exe'}).status_code == 422


# ── Per-Round Agent/Model/Reasoning Switching End-to-End (Mocked CLI) ───────

class _RecordingAdapter:
    def __init__(self, name: str, *, model: str | None = None, reasoning: str | None = None, supports_reasoning: bool = True):
        self.name = name
        self.model = model
        self.reasoning = reasoning if supports_reasoning else None
        self.variant = self.reasoning
        self.supports_reasoning = supports_reasoning

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return None

    def make_command(self, *, task, prompt):
        text = json.dumps({'type': 'text', 'part': {'text': 'No issues found.'}})
        return [sys.executable, '-c', f'print({text!r})']

    def parse_stdout_line(self, line):
        from app.adapters.base import AdapterEvent
        return [AdapterEvent(kind='agent.text', payload={'text': 'No issues found.'})]

    def parse_stderr_line(self, _line):
        return []


def test_per_round_agent_model_reasoning_switching_and_explicit_null_reset(git_repo, tmp_path, isolated_config):
    (git_repo / 'a.txt').write_text('changed content\n', encoding='utf-8')
    save_global_config({
        'backend': {
            'default': 'codex',
            'codex': {'model': 'gpt-6.2-sol', 'reasoning': 'high'},
            'claude-code': {'model': 'claude-opus-5.5', 'reasoning': 'max'},
        }
    })

    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    contexts = ContextCompiler(artifacts)

    built_calls: list[tuple[str, str | None, str | None]] = []

    def factory(name, *, model=None, variant=None, reasoning=None):
        eff = reasoning if reasoning is not None else variant
        built_calls.append((name, model, eff))
        return _RecordingAdapter(name, model=model, reasoning=eff, supports_reasoning=(name != 'copilot'))

    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        adapter_factory=factory,
    )

    api = FastAPI()
    api.include_router(routes_tasks.router)
    api.include_router(routes_control.router)
    api.dependency_overrides[routes_tasks.get_engine] = lambda: mission
    api.dependency_overrides[routes_tasks.get_artifacts] = lambda: artifacts
    api.dependency_overrides[routes_tasks.get_contexts] = lambda: contexts
    api.dependency_overrides[routes_control.get_execution] = lambda: execution
    client = TestClient(api)

    def _wait_idle(task_id: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not execution.is_active(task_id):
                return
            time.sleep(0.05)
        pytest.fail('round did not finish in time')

    try:
        # Create review with review-level codex defaults
        create_res = client.post('/api/tasks', json={
            'repo_path': str(git_repo),
            'source_type': 'local_diff',
            'backend': 'codex',
            'model': 'gpt-6.1-terra',
            'reasoning': 'xhigh',
            'language': 'en',
        })
        assert create_res.status_code == 200
        task_id = create_res.json()['id']

        # Round 1: inherits review defaults (codex · gpt-6.1-terra · xhigh)
        r1 = client.post(f'/api/tasks/{task_id}/start', json={'review_note': 'Check auth'})
        assert r1.status_code == 200
        _wait_idle(task_id)
        d1 = r1.json()
        assert d1['backend'] == 'codex'
        assert d1['model'] == 'gpt-6.1-terra'
        assert d1['reasoning'] == 'xhigh'
        assert d1['settings']['sources']['model'] == 'review'
        assert d1['settings']['sources']['reasoning'] == 'review'

        # Round 2: switch to claude-code WITHOUT specifying model/reasoning ->
        # must use claude-code's global config, NEVER inherit codex's gpt-6.1-terra / xhigh!
        r2 = client.post(f'/api/tasks/{task_id}/start', json={
            'backend': 'claude-code',
            'language': 'zh',
            'review_note': 'Second opinion',
        })
        assert r2.status_code == 200
        _wait_idle(task_id)
        d2 = r2.json()
        assert d2['backend'] == 'claude-code'
        assert d2['model'] == 'claude-opus-5.5'
        assert d2['reasoning'] == 'max'
        assert d2['language'] == 'zh'
        assert d2['settings']['sources']['agent'] == 'round'
        assert d2['settings']['sources']['model'] == 'global'
        assert d2['settings']['sources']['reasoning'] == 'global'

        # Round 3: explicitly reset reasoning & model to null (CLI / Model default)
        r3 = client.post(f'/api/tasks/{task_id}/start', json={
            'backend': 'codex',
            'model': None,
            'reasoning': None,
        })
        assert r3.status_code == 200
        _wait_idle(task_id)
        d3 = r3.json()
        assert d3['backend'] == 'codex'
        assert d3['model'] is None
        assert d3['reasoning'] is None
        assert d3['settings']['sources']['model'] == 'round'
        assert d3['settings']['sources']['reasoning'] == 'round'

        # Verify review.round_started events recorded exact settings per round
        started_events = [e for e in db.list_events(task_id) if e.kind == 'review.round_started']
        assert len(started_events) == 3
        assert started_events[0].payload['reasoning'] == 'xhigh'
        assert started_events[1].payload['backend'] == 'claude-code'
        assert started_events[1].payload['reasoning'] == 'max'
        assert started_events[2].payload['model'] is None
        assert started_events[2].payload['reasoning'] is None

        assert built_calls == [
            ('codex', 'gpt-6.1-terra', 'xhigh'),
            ('claude-code', 'claude-opus-5.5', 'max'),
            ('codex', None, None),
        ]
    finally:
        db.close()


def test_env_vars_do_not_leak_when_switching_agents(tmp_path, isolated_config, monkeypatch):
    save_global_config({
        'backend': {
            'default': 'codex',
            'codex': {'model': 'gpt-global', 'reasoning': 'medium'},
            'claude-code': {'model': 'sonnet', 'reasoning': 'medium'},
        }
    })
    monkeypatch.setenv('AMC_MODEL', 'gpt-env')
    monkeypatch.setenv('AMC_REASONING', 'xhigh')

    # Same agent (codex) gets AMC_MODEL / AMC_REASONING
    same = SettingsResolver.resolve(str(tmp_path), review_agent='codex')
    assert same.model == 'gpt-env'
    assert same.reasoning == 'xhigh'

    # Switching round to claude-code must NOT inherit codex's AMC_MODEL / AMC_REASONING
    switched = SettingsResolver.resolve(str(tmp_path), review_agent='codex', round_agent='claude-code')
    assert switched.agent == 'claude-code'
    assert switched.model == 'sonnet'
    assert switched.reasoning == 'medium'
    assert switched.sources['model'] == 'global'
    assert switched.sources['reasoning'] == 'global'


def test_sdk_constructor_params_do_not_override_per_round_switch_or_null_reset(git_repo, tmp_path, isolated_config):
    from app.sdk import ReviewEngine

    (git_repo / 'a.txt').write_text('sdk test\n', encoding='utf-8')
    seen: list[tuple[str, str | None, str | None, str]] = []

    class _PromptCapturingAdapter(_RecordingAdapter):
        def make_command(self, *, task, prompt):
            seen.append((self.name, self.model, self.reasoning, prompt))
            return super().make_command(task=task, prompt=prompt)

    engine = ReviewEngine(
        db_path=str(tmp_path / 'sdk.db'),
        runtime_root=str(tmp_path / 'rt'),
        backend='codex',
        model='gpt-constructor',
        reasoning='high',
    )
    engine._execution.adapter_factory = (
        lambda name, *, model=None, variant=None, reasoning=None: _PromptCapturingAdapter(
            name, model=model, reasoning=reasoning if reasoning is not None else variant,
        )
    )

    def _wait_idle(task_id: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not engine._execution.is_active(task_id):
                return
            time.sleep(0.05)
        pytest.fail('round did not finish in time')

    try:
        task = engine.create_task(str(git_repo))
        assert task.model == 'gpt-constructor'
        assert task.reasoning == 'high'

        # Round 1: explicit null resets constructor model & reasoning to CLI/Model default
        r1 = engine.start_review(task.id, model=None, reasoning=None)
        _wait_idle(task.id)
        assert r1.model is None
        assert r1.reasoning is None

        # Round 2: switch to claude-code with sonnet/low & per-round review_focus
        r2 = engine._execution.start_task(
            task.id,
            backend='claude-code',
            model='sonnet',
            reasoning='low',
            review_focus='sql-injection-only',
        )
        _wait_idle(task.id)
        assert r2.backend == 'claude-code'
        assert r2.model == 'sonnet'
        assert r2.reasoning == 'low'

        assert seen[0][:3] == ('codex', None, None)
        assert seen[1][:3] == ('claude-code', 'sonnet', 'low')
        assert 'sql-injection-only' in seen[1][3]
    finally:
        engine._db.close()


def test_codex_detector_honors_codex_home_and_current_ignores_non_string_values(tmp_path, isolated_config, monkeypatch):
    codex_home = tmp_path / 'custom_codex_home'
    codex_home.mkdir()
    (codex_home / 'models_cache.json').write_text(json.dumps({
        'models': [
            {
                'slug': 'gpt-custom-home',
                'visibility': 'list',
                'default_reasoning_level': 'high',
                'supported_reasoning_levels': ['low', 'high'],
            }
        ]
    }), encoding='utf-8')
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    monkeypatch.setattr(
        BackendDetector, '_run_command', staticmethod(lambda cmd, timeout=5: (True, 'codex-cli 0.169.0')),
    )

    info = BackendDetector._detect_codex()
    assert info is not None
    assert info.models == ['gpt-custom-home']
    assert info.reasoning_default is None  # per-model default lives in model_reasoning
    assert info.model_reasoning['gpt-custom-home'] == {'levels': ['low', 'high'], 'default': 'high'}

    # Non-string values in YAML must never be stringified by /api/config/current
    save_global_config({
        'backend': {
            'default': 'codex',
            'codex': {'model': {'nested': 'secret'}, 'reasoning': 123},
        }
    })
    cur = routes_config.get_current_config()['config']
    assert cur['model'] is None
    assert cur['reasoning'] is None
    assert 'secret' not in str(cur)

