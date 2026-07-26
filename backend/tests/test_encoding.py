"""Regression tests: non-UTF-8 locales must not silently break config or git IO.

These reproduce the failures seen on a zh-CN Windows host (cp936). They pass on
UTF-8 hosts too, because they assert on explicit encodings rather than on the
ambient locale.
"""
from __future__ import annotations

import sys

import pytest

from app.core.config import load_repo_config
from app.core.proc import run_git, run_text
from app.sdk import ReviewEngine
from app.services.artifact_store import ArtifactStore


NON_ASCII = 'em-dash — and 中文 and é'


def test_repo_config_with_non_ascii_is_honoured(tmp_path, isolated_config):
    """A .amc.yaml containing non-ASCII must not be silently dropped."""
    (tmp_path / '.amc.yaml').write_text(
        f'# {NON_ASCII}\n'
        'backend:\n'
        '  default: codex\n'
        '  codex:\n'
        "    model: 'gpt-5.4'\n",
        encoding='utf-8',
    )
    cfg = load_repo_config(str(tmp_path))
    assert cfg['backend']['default'] == 'codex'
    assert cfg['backend']['codex']['model'] == 'gpt-5.4'


def test_global_config_with_non_ascii_is_honoured(tmp_path, isolated_config, monkeypatch):
    global_dir = isolated_config / 'amc'
    global_dir.mkdir(parents=True)
    (global_dir / 'config.yaml').write_text(
        f'# {NON_ASCII}\nbackend:\n  default: claude-code\n', encoding='utf-8'
    )
    cfg = load_repo_config(str(tmp_path))
    assert cfg['backend']['default'] == 'claude-code'


def test_malformed_config_warns_and_falls_back(tmp_path, isolated_config, capsys):
    """Unparseable config should be visible, not silently swallowed."""
    (tmp_path / '.amc.yaml').write_text('backend: [this: is: not: a: mapping\n', encoding='utf-8')
    cfg = load_repo_config(str(tmp_path))
    assert cfg['backend']['default'] == 'opencode'          # fell back to defaults
    assert 'warning' in capsys.readouterr().err.lower()      # and said so


def test_load_repo_config_does_not_mutate_defaults(tmp_path, isolated_config):
    from app.core import config as config_module

    cfg = load_repo_config(str(tmp_path))
    cfg['context']['candidate_files_limit'] = 999
    assert config_module.DEFAULT_CONFIG['context']['candidate_files_limit'] != 999


def test_run_text_decodes_non_ascii_child_output():
    """run_text must decode UTF-8 bytes regardless of the host locale codepage.

    The child writes to ``stdout.buffer`` so it emits UTF-8 bytes no matter what
    its own locale is — otherwise the child would die with UnicodeEncodeError on
    a cp1252/cp936 host and we would be testing the child, not run_text.
    """
    code = (
        'import sys; '
        'sys.stdout.buffer.write("caf\\u00e9 \\u2014 \\u4e2d\\u6587".encode("utf-8"))'
    )
    result = run_text([sys.executable, '-c', code])
    assert result.returncode == 0, result.stderr
    assert result.stdout is not None
    assert result.stdout == 'café — 中文'


def test_run_git_returns_text_for_non_ascii_commit(git_repo):
    result = run_git(git_repo, ['log', '--oneline', '-1'])
    assert result.returncode == 0
    assert result.stdout is not None          # bare text=True leaves this None on cp936
    assert '—' in result.stdout


@pytest.mark.parametrize(
    'method, args',
    [
        ('_infer_review_focus', ('HEAD~1',)),
        ('_get_local_changed_files', ('HEAD~1',)),
        ('_get_commit_messages', ('HEAD~1',)),
        ('_get_local_diff', ('HEAD~1',)),
    ],
)
def test_sdk_git_helpers_survive_non_ascii_history(git_repo, method, args):
    """Each of these raised AttributeError on None before the fix."""
    fn = getattr(ReviewEngine, method)
    result = fn(str(git_repo), *args)
    assert result is not None


def test_artifact_store_roundtrips_non_ascii(tmp_path):
    """Artifacts carry PR bodies and diffs; locale-encoded writes raise on cp936."""
    store = ArtifactStore(str(tmp_path))
    payload = f'{NON_ASCII}\ndiff --git a/b b/b\n+日本語\n'
    store.write_text('task1', 'context/local_diff.patch', payload)
    assert store.read_text('task1', 'context/local_diff.patch') == payload
