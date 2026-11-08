"""Config layering: a blank backend value means "not set", not "override with blank".

Regression for 60bd465 (exposed by 935a507): the repo's own .amc.yaml, the
README example and `amc init` wrote `model: ''`, which overrode the model
saved globally (and on the Web model selection page), so the agent ran its
own default model with no sign of it in the UI.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import app.cli as cli
from app.core import config as config_module
from app.core.config import load_repo_config, resolve_backend_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_global(isolated_config: Path, text: str) -> None:
    path = isolated_config / 'amc' / 'config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _repo(tmp_path: Path, text: str | None) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    if text is not None:
        (repo / '.amc.yaml').write_text(text, encoding='utf-8')
    return repo


GLOBAL = 'backend:\n  default: opencode\n  opencode:\n    model: global-model\n    variant: high\n'


@pytest.mark.parametrize('blank', ["''", '""', "'   '", '', '~', 'null'])
def test_blank_project_model_keeps_the_global_model(tmp_path, isolated_config, blank):
    _write_global(isolated_config, GLOBAL)
    repo = _repo(tmp_path, f'backend:\n  default: opencode\n  opencode:\n    model: {blank}\n    variant: {blank}\n')

    resolved = resolve_backend_config(str(repo))

    assert resolved == {'backend': 'opencode', 'model': 'global-model', 'variant': 'high'}


def test_project_model_still_overrides_the_global_model(tmp_path, isolated_config):
    _write_global(isolated_config, GLOBAL)
    repo = _repo(tmp_path, 'backend:\n  opencode:\n    model: project-model\n')

    resolved = resolve_backend_config(str(repo))

    assert resolved['model'] == 'project-model'
    assert resolved['variant'] == 'high'  # untouched keys still inherit


def test_blank_project_default_keeps_the_global_backend(tmp_path, isolated_config):
    _write_global(isolated_config, 'backend:\n  default: codex\n  codex:\n    model: gpt-x\n')
    repo = _repo(tmp_path, "backend:\n  default: ''\n")

    assert resolve_backend_config(str(repo)) == {'backend': 'codex', 'model': 'gpt-x', 'variant': None}


def test_explicit_flag_and_env_still_beat_config(tmp_path, isolated_config, monkeypatch):
    _write_global(isolated_config, GLOBAL)
    repo = _repo(tmp_path, "backend:\n  opencode:\n    model: ''\n")

    assert resolve_backend_config(str(repo), model='flag-model')['model'] == 'flag-model'
    monkeypatch.setenv('AMC_MODEL', 'env-model')
    assert resolve_backend_config(str(repo))['model'] == 'env-model'


def test_no_model_anywhere_still_means_the_agent_default(tmp_path, isolated_config):
    repo = _repo(tmp_path, "backend:\n  default: opencode\n  opencode:\n    model: ''\n")

    assert resolve_backend_config(str(repo)) == {'backend': 'opencode', 'model': None, 'variant': None}
    assert resolve_backend_config(str(tmp_path))['backend'] == 'opencode'


def test_blank_values_outside_backend_still_override(tmp_path, isolated_config):
    """A project may turn off a globally configured check with an empty command."""
    _write_global(isolated_config, "validation:\n  checks:\n    build:\n      command: make build\n")
    repo = _repo(tmp_path, "validation:\n  checks:\n    build:\n      command: ''\n")

    assert load_repo_config(str(repo))['validation']['checks']['build']['command'] == ''


def test_defaults_carry_no_blank_backend_values():
    def blanks(mapping, prefix=''):
        for key, value in mapping.items():
            if isinstance(value, dict):
                yield from blanks(value, f'{prefix}{key}.')
            elif value is None or (isinstance(value, str) and not value.strip()):
                yield f'{prefix}{key}'

    assert list(blanks(config_module.DEFAULT_CONFIG['backend'])) == []


def test_this_repos_own_config_keeps_the_global_model(tmp_path, isolated_config):
    """The checked-in .amc.yaml is what hid the selected model in this repo."""
    _write_global(isolated_config, GLOBAL)
    repo = tmp_path / 'repo'
    repo.mkdir()
    shutil.copy(REPO_ROOT / '.amc.yaml', repo / '.amc.yaml')

    assert resolve_backend_config(str(repo))['model'] == 'global-model'


@pytest.mark.parametrize('scope', ['project', 'global'])
def test_init_without_a_model_writes_no_model_key(tmp_path, isolated_config, monkeypatch, scope):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_detect_backends', lambda: ['opencode'])
    monkeypatch.setattr(cli, '_prompt_choice', lambda *_a, **_k: 'opencode')
    monkeypatch.setattr(cli, '_prompt_input', lambda _q, default='': default)
    argv = ['amc', 'init'] + (['--global'] if scope == 'global' else [])
    monkeypatch.setattr('sys.argv', argv)
    try:
        cli.main()
    except SystemExit as exc:
        assert not exc.code

    written = (isolated_config / 'amc' / 'config.yaml') if scope == 'global' else (tmp_path / '.amc.yaml')
    text = written.read_text(encoding='utf-8')
    assert 'model' not in text
    loaded = config_module._load_yaml_file(written)
    assert loaded['backend'] == {'default': 'opencode'}


def test_init_with_a_model_writes_it(tmp_path, isolated_config, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, '_detect_backends', lambda: ['opencode'])
    monkeypatch.setattr(cli, '_prompt_choice', lambda *_a, **_k: 'opencode')
    answers = iter(['provider/model-x', 'main'])
    monkeypatch.setattr(cli, '_prompt_input', lambda _q, default='': next(answers))
    monkeypatch.setattr('sys.argv', ['amc', 'init'])
    try:
        cli.main()
    except SystemExit as exc:
        assert not exc.code

    loaded = config_module._load_yaml_file(tmp_path / '.amc.yaml')
    assert loaded['backend'] == {'default': 'opencode', 'opencode': {'model': 'provider/model-x'}}
    assert loaded['repo']['base_branch'] == 'main'
    assert loaded['validation']['checks']['build']['command'] == ''
