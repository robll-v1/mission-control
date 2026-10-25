"""Model discovery must be real or honestly unknown — never invented.

These tests pin the behaviour required for trustworthy model discovery:
hardcoded fallback lists are gone, every source is labelled, an empty list
never means the backend is missing, and explicit user config survives
detection that cannot enumerate anything. No test touches a real CLI,
cache or network: commands are stubbed and HOME points at a temp dir.
"""
from __future__ import annotations

import json
import sqlite3

from app.core.backend_detector import BackendDetector
from app.core.config import resolve_backend_config


def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    # Path.home() resolves via HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    return home


def _only_cli(monkeypatch, installed: dict[str, str]):
    import app.core.backend_detector as detector

    monkeypatch.setattr(detector.shutil, 'which', lambda name: installed.get(name))


def _stub_commands(monkeypatch, responses: dict[tuple[str, ...], tuple[bool, str]]):
    def fake_run(cmd, timeout=5):
        for key, value in responses.items():
            if tuple(cmd[: len(key)]) == key:
                return value
        return True, ''

    monkeypatch.setattr(BackendDetector, '_run_command', staticmethod(fake_run))


def _write_catalog_cache(home, catalog_body, key='models-dev:catalog'):
    db_dir = home / '.local' / 'share' / 'opencode'
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(db_dir / 'opencode.db'))
    conn.execute('CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute(
        'INSERT INTO kv (key, value) VALUES (?, ?)',
        (key, json.dumps({'body': json.dumps(catalog_body)})),
    )
    conn.commit()
    conn.close()


# --- opencode ---------------------------------------------------------------

def test_opencode_models_come_from_the_cli_when_it_answers(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/usr/local/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (True, 'header noise\nzai-coding-plan/glm-5.3\nopenai/gpt-6\n'),
    })

    info = BackendDetector._detect_opencode()

    assert info is not None
    assert info.models == ['zai-coding-plan/glm-5.3', 'openai/gpt-6']
    assert info.models_source == 'cli'
    # Enumeration is recorded separately from call availability: the CLI
    # listing proves the IDs exist, not that this account may call them.
    assert info.models_call_verified is False
    assert 'not verified' in info.models_note


def test_opencode_cli_timeout_falls_back_to_catalog_cache(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/usr/local/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (False, ''),  # failed or timed out
    })
    _write_catalog_cache(home, {'openai': {'models': {
        'gpt-6': {'status': 'active'},
        'old-model': {'status': 'deprecated'},
    }}})

    info = BackendDetector._detect_opencode()

    assert info is not None
    assert info.models == ['openai/gpt-6']
    assert info.models_source == 'catalog_cache'
    assert info.models_call_verified is False
    assert 'not verified' in info.models_note


def test_opencode_corrupt_catalog_and_dead_cli_report_unknown(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/usr/local/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (False, ''),
    })
    db_dir = home / '.local' / 'share' / 'opencode'
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(db_dir / 'opencode.db'))
    conn.execute('CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('INSERT INTO kv (key, value) VALUES (?, ?)', ('models-dev:catalog', 'not-json{'))
    conn.commit()
    conn.close()

    info = BackendDetector._detect_opencode()

    assert info is not None
    assert info.models == []
    assert info.models_source == 'unknown'
    assert 'unusable' in info.models_note
    assert 'corrupt' in info.models_note


def test_opencode_without_any_source_reports_unknown_but_present(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/usr/local/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (False, ''),
    })

    info = BackendDetector._detect_opencode()

    assert info is not None  # installed, just no model list
    assert info.models == []
    assert info.models_source == 'unknown'
    assert info.models_note


def test_opencode_cli_unrecognized_output_is_not_treated_as_models(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/usr/local/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (True, 'error: please log in first\n'),  # no provider/model lines
    })

    info = BackendDetector._detect_opencode()

    assert info.models == [] or info.models_source != 'cli'


# --- claude-code / copilot --------------------------------------------------

def test_claude_code_reports_unknown_models_with_reason(monkeypatch):
    _stub_commands(monkeypatch, {('claude', '--version'): (True, '1.0.99 (Claude Code)')})

    info = BackendDetector._detect_claude_code()

    assert info is not None
    assert info.models == []
    assert info.models_source == 'unknown'
    assert info.models_call_verified is False
    assert 'no model-listing command' in info.models_note


def test_copilot_reports_unknown_models_with_reason(monkeypatch):
    _only_cli(monkeypatch, {'gh': '/opt/homebrew/bin/gh'})
    _stub_commands(monkeypatch, {
        ('gh', 'copilot', '--help'): (True, 'Runs the GitHub Copilot CLI.'),
        ('gh', '--version'): (True, 'gh version 2.60.0'),
    })

    info = BackendDetector._detect_copilot()

    assert info is not None
    assert info.models == []
    assert info.models_source == 'unknown'
    assert info.models_note


# --- codex ------------------------------------------------------------------

def test_codex_models_from_its_own_cache(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'models_cache.json').write_text(json.dumps({'models': [
        {'slug': 'gpt-6', 'visibility': 'list'},
        {'slug': 'gpt-6-hidden', 'visibility': 'hidden'},
    ]}), encoding='utf-8')
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    info = BackendDetector._detect_codex()

    assert info is not None
    assert info.models == ['gpt-6']
    assert info.models_source == 'cache'
    assert info.models_call_verified is False
    assert 'not verified' in info.models_note


def test_codex_corrupt_cache_falls_back_to_configured_model(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'models_cache.json').write_text('{corrupt', encoding='utf-8')
    (codex_dir / 'config.toml').write_text('model = "gpt-5.2-codex"\n', encoding='utf-8')
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    info = BackendDetector._detect_codex()

    assert info.models == ['gpt-5.2-codex']
    assert info.models_source == 'config'
    assert info.models_call_verified is False
    assert 'not verified' in info.models_note


def test_codex_with_no_cache_or_config_reports_unknown(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    info = BackendDetector._detect_codex()

    assert info.models == []
    assert info.models_source == 'unknown'
    assert info.models_note


# --- detection vs. enumeration ----------------------------------------------

def test_missing_cli_means_backend_absent(monkeypatch):
    _only_cli(monkeypatch, {})  # nothing installed
    assert BackendDetector.detect_all() == []


def test_unknown_model_list_does_not_clear_explicit_config(tmp_path, monkeypatch, isolated_config):
    # The user has explicitly configured a model for a backend whose models we
    # cannot enumerate (claude-code). Detection returning [] must not affect it.
    from app.core.config import save_global_config

    save_global_config({'backend': {
        'default': 'claude-code',
        'claude-code': {'model': 'my-explicit-model', 'variant': ''},
    }})
    _stub_commands(monkeypatch, {('claude', '--version'): (True, '1.0.99')})

    info = BackendDetector._detect_claude_code()
    resolved = resolve_backend_config(str(tmp_path))

    assert info.models == []
    assert resolved['backend'] == 'claude-code'
    assert resolved['model'] == 'my-explicit-model'


def test_no_enumeration_source_ever_claims_verified_calls(tmp_path, monkeypatch):
    """Whatever the source, detection makes no model calls.

    Every backend that returns a non-empty list must say so: call_verified
    False plus an explicit note. This keeps 'where the list came from' and
    'is it usable' as two separate recorded facts.
    """
    home = _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/bin/opencode', 'codex': '/bin/codex'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (True, 'prov/one\nprov/two\n'),
        ('codex', '--version'): (True, 'codex-cli 0.4.0'),
    })
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'models_cache.json').write_text(
        json.dumps({'models': [{'slug': 'gpt-6', 'visibility': 'list'}]}), encoding='utf-8'
    )

    for info in BackendDetector.detect_all():
        if info.models:
            assert info.models_call_verified is False
            assert 'not verified' in (info.models_note or '')
        else:
            assert info.models_call_verified is False


# --- acceptance follow-up: parsing precision and structural safety -------------

def test_codex_config_ignores_model_prefixed_keys_and_inline_comments(tmp_path, monkeypatch):
    """model_provider="my-proxy" is not a model; comments are not part of the value."""
    home = _isolate_home(tmp_path, monkeypatch)
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'config.toml').write_text(
        'model = "gpt-5.2"  # via work proxy\n'
        'model_provider = "my-proxy"\n'
        'model_reasoning_effort = "high"\n',
        encoding='utf-8',
    )
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    info = BackendDetector._detect_codex()

    assert info.models == ['gpt-5.2']
    assert 'my-proxy' not in info.models
    assert 'high' not in info.models
    assert '#' not in info.models[0]


def test_codex_config_respects_sections(tmp_path, monkeypatch):
    """A `model` key inside a [profiles.*] section is not the configured model."""
    home = _isolate_home(tmp_path, monkeypatch)
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'config.toml').write_text(
        '[profiles.fast]\nmodel = "profile-only-model"\n',
        encoding='utf-8',
    )
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    info = BackendDetector._detect_codex()

    assert info.models == []
    assert 'profile-only-model' not in info.models
    assert info.models_source == 'unknown'


def test_codex_cache_wrong_shapes_degrade_instead_of_crashing(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    (codex_dir / 'models_cache.json').write_text('{"models": null}', encoding='utf-8')
    info = BackendDetector._detect_codex()
    assert info.models == []  # null list must not raise

    (codex_dir / 'models_cache.json').write_text(
        json.dumps({'models': [{'slug': None}, {'slug': 'gpt-6'}, 'not-a-dict']}), encoding='utf-8'
    )
    info = BackendDetector._detect_codex()
    assert info.models == ['gpt-6']  # null slug and stray strings skipped
    assert info.models_source == 'cache'


def test_opencode_catalog_array_body_degrades_without_killing_detect(tmp_path, monkeypatch):
    """A structurally wrong catalog must not take the whole detect API down."""
    home = _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/bin/opencode', 'codex': '/bin/codex'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (False, ''),
        ('codex', '--version'): (True, 'codex-cli 0.4.0'),
    })
    db_dir = home / '.local' / 'share' / 'opencode'
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(db_dir / 'opencode.db'))
    conn.execute('CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)')
    # valid JSON, wrong shape: body is an array, not a mapping
    conn.execute(
        'INSERT INTO kv (key, value) VALUES (?, ?)',
        ('models-dev:catalog', json.dumps({'body': '[]'})),
    )
    conn.commit()
    conn.close()
    codex_dir = home / '.codex'
    codex_dir.mkdir()
    (codex_dir / 'models_cache.json').write_text(
        json.dumps({'models': [{'slug': 'gpt-6', 'visibility': 'list'}]}), encoding='utf-8'
    )

    backends = {b.name: b for b in BackendDetector.detect_all()}

    assert set(backends) == {'opencode', 'codex'}  # both survived
    assert backends['opencode'].models == []
    assert backends['opencode'].models_source == 'unknown'
    assert 'mapping' in backends['opencode'].models_note
    assert backends['codex'].models == ['gpt-6']


def test_opencode_catalog_ids_keep_provider_namespace(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (False, ''),
    })
    _write_catalog_cache(home, {
        'openai': {'models': {'gpt-6': {'status': 'active'}}},
        'anthropic': {'models': {'claude-sonnet': {'status': 'active'}}},
    })

    info = BackendDetector._detect_opencode()

    assert info.models == ['openai/gpt-6', 'anthropic/claude-sonnet']
    assert all('/' in model for model in info.models)  # executable after save


def test_opencode_cli_keeps_multi_segment_ids_and_dedupes(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _only_cli(monkeypatch, {'opencode': '/bin/opencode'})
    _stub_commands(monkeypatch, {
        ('opencode', '--version'): (True, 'opencode 1.2.3'),
        ('opencode', 'models'): (True,
            'openrouter/anthropic/claude-sonnet\n'
            'zai-coding-plan/glm-5.3\n'
            'zai-coding-plan/glm-5.3\n'   # duplicate line
            'bare-model-no-provider\n'     # no namespace: ignored
            'header noise\n'),
    })

    info = BackendDetector._detect_opencode()

    assert info.models == ['openrouter/anthropic/claude-sonnet', 'zai-coding-plan/glm-5.3']
    assert info.models_source == 'cli'


def test_detect_all_degrades_when_one_detector_crashes(monkeypatch):
    """One backend's unexpected crash must not sink the rest of detection."""
    import app.core.backend_detector as detector

    _only_cli(monkeypatch, {'opencode': '/bin/opencode', 'codex': '/bin/codex'})

    def exploding():
        raise RuntimeError('unanticipated cache shape')

    monkeypatch.setattr(detector.BackendDetector, '_detect_opencode', staticmethod(exploding))
    _stub_commands(monkeypatch, {('codex', '--version'): (True, 'codex-cli 0.4.0')})

    backends = {b.name: b for b in detector.BackendDetector.detect_all()}

    assert set(backends) == {'opencode', 'codex'}
    assert backends['opencode'].models == []
    assert 'unanticipated cache shape' in backends['opencode'].models_note
