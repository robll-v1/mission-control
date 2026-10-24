"""CLI onboarding regressions: exit codes, actionable errors and clean JSON stdout.

All review runs use a controlled fake adapter; no real agent CLI or network is
called. Repositories are real temporary git repositories so the preflight path
is exercised end to end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app.cli as cli
import app.sdk as sdk
from app.adapters.base import AdapterEvent

CLEAR_TEXT = (
    '## Review Summary\n\n'
    'No material correctness or regression issues found.\n'
)
CONCERNS_TEXT = (
    '## Review Summary\n\n'
    'One correctness issue was found.\n\n'
    '## Findings\n\n'
    '- high: `a.txt:1` — 处理逻辑有误\n'
    '  The value is not validated before use.\n'
)
EMOJI = ('✅', '❌', '⚠', '🔍', '📋', '🔎', '💡', '🚀', '📡', '🤖', '📌', '🌿', '🎉', '🆕', '🔄', '❔', '✓', '✔')


class FakeAdapter:
    """A subprocess adapter whose output is controlled by the test."""

    name = 'opencode'

    def __init__(self, mode: str):
        self.mode = mode

    def executable_name(self):
        return sys.executable

    def probe_command(self):
        return None

    def make_command(self, *, task, prompt):
        if self.mode == 'fail':
            return [
                sys.executable,
                '-c',
                "import sys; print('simulated backend failure', file=sys.stderr); sys.exit(3)",
            ]
        if self.mode == 'timeout':
            return [sys.executable, '-c', 'import time; time.sleep(30)']
        text = CONCERNS_TEXT if self.mode == 'concerns' else CLEAR_TEXT
        code = (
            "import json, sys; "
            "print(json.dumps({'type': 'text', 'part': {'text': sys.argv[1]}}))"
        )
        return [sys.executable, '-c', code, text]

    def parse_stdout_line(self, line):
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return [AdapterEvent(kind='agent.text', payload={'text': stripped})]
        part = obj.get('part')
        text = part.get('text', '') if isinstance(part, dict) else obj.get('text', '')
        return [AdapterEvent(kind='agent.text', payload={'text': str(text)})] if text else []

    def parse_stderr_line(self, line):
        stripped = line.rstrip()
        if not stripped:
            return []
        return [AdapterEvent(kind='agent.stderr', level='warning', payload={'text': stripped})]


@pytest.fixture(autouse=True)
def _cli_environment(isolated_config, monkeypatch):
    """Isolate config/cwd and keep assertions free of ANSI escapes."""
    monkeypatch.setattr(cli, '_USE_COLOR', False)
    monkeypatch.setenv('NO_COLOR', '1')
    monkeypatch.delenv('AMC_DEBUG', raising=False)
    cwd = os.getcwd()
    yield
    os.chdir(cwd)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(path)], check=True)
    _git(path, 'config', 'user.email', 'test@example.com')
    _git(path, 'config', 'user.name', 'Test')
    _git(path, 'config', 'commit.gpgsign', 'false')
    (path / 'a.txt').write_text('one\n', encoding='utf-8')
    _git(path, 'add', '-A')
    subprocess.run(['git', '-C', str(path), 'commit', '-q', '-m', 'init'], check=True)
    return path


def _invoke(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, 'argv', ['amc', *argv])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    return int(exc_info.value.code)


def _run_review(monkeypatch, tmp_path: Path, mode: str = 'clear') -> None:
    monkeypatch.setattr(cli, '_backend_executables', lambda: {'opencode': sys.executable})
    monkeypatch.setattr(sdk, 'get_adapter', lambda name, **kwargs: FakeAdapter(mode))
    monkeypatch.setattr(cli, '_root_dir', lambda: tmp_path)


def _assert_no_traceback(text: str) -> None:
    assert 'Traceback (most recent call last)' not in text


def test_help_and_review_help(monkeypatch, capsys):
    assert _invoke(monkeypatch, ['--help']) == 0
    captured = capsys.readouterr()
    assert 'usage: amc' in captured.out
    assert captured.err == ''

    assert _invoke(monkeypatch, ['review', '--help']) == 0
    captured = capsys.readouterr()
    assert '--format' in captured.out
    assert 'Positive integer' in captured.out
    assert 'setup/runtime errors' in captured.out
    assert 'exit 2' in captured.out


def test_missing_repository_path_is_actionable(monkeypatch, capsys, tmp_path):
    missing = tmp_path / 'does-not-exist'
    assert _invoke(monkeypatch, ['review', '--repo', str(missing)]) == 2
    captured = capsys.readouterr()
    assert 'does not exist' in captured.err
    assert '--repo PATH' in captured.err
    assert captured.out == ''
    _assert_no_traceback(captured.err)


def test_missing_repository_path_json_is_parseable(monkeypatch, capsys, tmp_path):
    missing = tmp_path / 'does-not-exist'
    assert _invoke(monkeypatch, ['review', '--repo', str(missing), '--format', 'json']) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['status'] == 'error'
    assert payload['error']['code'] == 'repo_not_found'
    assert 'does not exist' in captured.err
    _assert_no_traceback(captured.err)


def test_non_git_directory_is_reported_without_traceback(monkeypatch, capsys, tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert _invoke(monkeypatch, ['review', '--repo', str(plain), '--format', 'json']) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['error']['code'] == 'not_a_git_repo'
    assert 'git repository' in captured.err
    assert '--repo PATH' in captured.err
    _assert_no_traceback(captured.err)


def test_repository_without_commits_is_an_error(monkeypatch, capsys, tmp_path):
    repo = tmp_path / 'empty'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    assert _invoke(monkeypatch, ['review', '--repo', str(repo), '--format', 'json']) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['error']['code'] == 'no_commits'
    assert 'no commits' in captured.err
    _assert_no_traceback(captured.err)


def test_no_changes_is_parseable_and_exits_zero(monkeypatch, capsys, git_repo):
    assert _invoke(monkeypatch, ['review', '--repo', str(git_repo), '--format', 'json']) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['status'] == 'no_changes'
    assert payload['base'] == 'main'
    assert 'No changes detected' in captured.err


def test_only_untracked_changes_reach_the_reviewer(monkeypatch, capsys, git_repo, tmp_path):
    (git_repo / 'new.txt').write_text('untracked\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path)
    assert _invoke(monkeypatch, ['review', '--repo', str(git_repo), '--format', 'json']) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['status'] == 'reviewed'
    assert payload['task_id']
    assert payload['rounds_executed'] == 1
    assert 'Starting review' in captured.err


def test_unknown_backend_is_not_reported_as_missing_binary(monkeypatch, capsys, git_repo):
    assert _invoke(
        monkeypatch,
        ['review', '--repo', str(git_repo), '--backend', 'not-a-backend', '--format', 'json'],
    ) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['error']['code'] == 'unknown_backend'
    assert 'Supported backends' in captured.err
    assert 'not on PATH' not in captured.err


def test_uninstalled_backend_gives_install_hint(monkeypatch, capsys, git_repo):
    (git_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    monkeypatch.setattr(
        cli, '_backend_executables', lambda: {'opencode': 'amc-definitely-missing-binary'}
    )
    assert _invoke(monkeypatch, ['review', '--repo', str(git_repo), '--format', 'json']) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['error']['code'] == 'backend_unavailable'
    assert 'not installed' in captured.err
    assert 'Install' in captured.err


def test_backend_failure_exits_two_with_error_json(monkeypatch, capsys, git_repo, tmp_path):
    (git_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path, mode='fail')
    assert _invoke(monkeypatch, ['review', '--repo', str(git_repo), '--format', 'json']) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['status'] == 'failed'
    assert payload['verdict'] == 'failed'
    assert payload['error']['code'] == 'review_failed'
    assert 'Starting review' not in captured.out
    assert 'Review failed' in captured.err


def test_report_to_json_marks_inconclusive_error_as_failed():
    report = sdk.ReviewReport(
        task_id='task',
        verdict=sdk.ReviewVerdict.INCONCLUSIVE,
        passed=False,
        rounds_executed=0,
        error={'code': 'no_result', 'message': 'Review completed but no result extracted', 'recoverable': True},
    )
    payload = cli._report_to_json(report)
    assert payload['status'] == 'failed'
    assert payload['verdict'] == 'inconclusive'
    assert payload['error']['code'] == 'no_result'


def test_inconclusive_sdk_error_is_parseable_and_exits_two(monkeypatch, capsys, git_repo, tmp_path):
    (git_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path)

    def fake_review(self, *args, **kwargs):
        return sdk.ReviewReport(
            task_id='task',
            verdict=sdk.ReviewVerdict.INCONCLUSIVE,
            passed=False,
            rounds_executed=0,
            error={'code': 'no_result', 'message': 'Review completed but no result extracted', 'recoverable': True},
        )

    monkeypatch.setattr(sdk.ReviewEngine, 'review', fake_review)
    assert _invoke(monkeypatch, ['review', '--repo', str(git_repo), '--format', 'json']) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['status'] == 'failed'
    assert payload['verdict'] == 'inconclusive'
    assert payload['error']['code'] == 'no_result'
    assert 'Review failed' in captured.err


@pytest.mark.parametrize(
    ('flag', 'value', 'code'),
    [
        ('--rounds', '0', 'invalid_rounds'),
        ('--rounds', '-1', 'invalid_rounds'),
        ('--timeout', '0', 'invalid_timeout'),
        ('--timeout', '-1', 'invalid_timeout'),
    ],
)
def test_non_positive_rounds_and_timeout_are_rejected(monkeypatch, capsys, git_repo, flag, value, code):
    assert _invoke(
        monkeypatch,
        ['review', '--repo', str(git_repo), flag, value, '--format', 'json'],
    ) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['status'] == 'error'
    assert payload['error']['code'] == code
    assert flag in captured.err


def test_timeout_exits_two_with_timeout_error(monkeypatch, capsys, git_repo, tmp_path):
    (git_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path, mode='timeout')
    assert _invoke(
        monkeypatch,
        ['review', '--repo', str(git_repo), '--timeout', '1', '--format', 'json'],
    ) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['error']['code'] == 'timeout'
    assert 'timed out' in payload['error']['message']
    assert 'timed out' in captured.err
    time.sleep(0.5)


def test_exit_code_semantics_for_clear_and_concerns(monkeypatch, capsys, tmp_path):
    clear_repo = _make_repo(tmp_path / 'clear')
    (clear_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path, mode='clear')
    assert _invoke(monkeypatch, ['review', '--repo', str(clear_repo), '--format', 'json', '--exit-code']) == 0
    assert json.loads(capsys.readouterr().out)['verdict'] == 'clear'

    concerns_repo = _make_repo(tmp_path / 'concerns')
    (concerns_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path, mode='concerns')
    assert _invoke(monkeypatch, ['review', '--repo', str(concerns_repo), '--format', 'json', '--exit-code']) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['verdict'] == 'concerns'
    assert payload['findings'][0]['summary'] == '处理逻辑有误'


def test_markdown_output_has_no_system_emoji(monkeypatch, capsys, git_repo, tmp_path):
    (git_repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path, mode='concerns')
    assert _invoke(monkeypatch, ['review', '--repo', str(git_repo)]) == 0
    captured = capsys.readouterr()
    assert 'Verdict: concerns' in captured.out
    assert 'Findings (1)' in captured.out
    assert not any(char in captured.out or char in captured.err for char in EMOJI)
    assert 'Starting review' in captured.err


def test_path_with_spaces_and_non_ascii_json(monkeypatch, capsys, tmp_path):
    repo = _make_repo(tmp_path / 'repo with space' / '仓库')
    (repo / 'a.txt').write_text('changed\n', encoding='utf-8')
    _run_review(monkeypatch, tmp_path, mode='concerns')
    assert _invoke(monkeypatch, ['review', '--repo', str(repo), '--format', 'json']) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['findings'][0]['summary'] == '处理逻辑有误'
    assert 'repo with space' in captured.err


def test_invalid_pr_url_is_rejected(monkeypatch, capsys, git_repo):
    assert _invoke(
        monkeypatch,
        ['review', 'not-a-pr-url', '--repo', str(git_repo), '--format', 'json'],
    ) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload['error']['code'] == 'invalid_pr_url'
    assert 'github.com' in captured.err


def test_init_without_backend_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cli, '_detect_backends', lambda: [])
    assert _invoke(monkeypatch, ['init', '--global']) == 1
    captured = capsys.readouterr()
    assert 'No supported agent CLI' in captured.err
    assert 'amc init' in captured.err


def test_cli_source_has_no_system_emoji():
    source = Path(cli.__file__).read_text(encoding='utf-8')
    found = sorted({char for char in EMOJI if char in source})
    assert found == []
