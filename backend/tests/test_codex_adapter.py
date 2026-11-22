"""Codex adapter: a command line current codex-cli accepts, and the events a review needs.

No real CLI is run here; the argv was checked against codex-cli 0.169.0 by hand
(`codex exec <flags> --version`, which exits before any session starts).
"""
from __future__ import annotations

import json

import pytest

from app.adapters import get_adapter
from app.core.models import Event, Run, Task
from app.services.review_result_service import ReviewResultService


def _task(tmp_path):
    return Task(title='t', repo_path=str(tmp_path), worktree_path=str(tmp_path / 'wt'),
                backend='codex', created_at=0.0, updated_at=0.0)


def _parse(obj, adapter=None):
    return (adapter or get_adapter('codex')).parse_stdout_line(json.dumps(obj))


def test_command_uses_current_flags_and_a_read_only_sandbox(tmp_path):
    cmd = get_adapter('codex', model='gpt-x').make_command(task=_task(tmp_path), prompt='review this')

    assert cmd[1:3] == ['exec', '--json']
    # codex-cli 0.169 exits 2 on both: --full-auto was removed, --cwd is -C/--cd.
    assert '--full-auto' not in cmd and '--cwd' not in cmd
    assert cmd[cmd.index('--cd') + 1] == str(tmp_path / 'wt')
    assert cmd[cmd.index('--sandbox') + 1] == 'read-only'
    assert cmd[cmd.index('--model') + 1] == 'gpt-x'
    assert cmd[-1] == 'review this'


def test_without_a_model_the_cli_default_applies(tmp_path):
    cmd = get_adapter('codex').make_command(task=_task(tmp_path), prompt='p')
    assert '--model' not in cmd and '-m' not in cmd and '-c' not in cmd


def test_thread_id_is_kept_as_the_session():
    [event] = _parse({'type': 'thread.started', 'thread_id': 'th_1'})
    assert event.kind == 'agent.step_start'
    assert event.payload['session_id'] == 'th_1'


def test_agent_message_is_review_text():
    [event] = _parse({'type': 'item.completed', 'item': {
        'id': 'item_3', 'type': 'agent_message', 'text': '## Review Summary\n\nLooks fine.',
    }})
    assert (event.kind, event.payload['text']) == ('agent.text', '## Review Summary\n\nLooks fine.')


def test_probe_parses_the_round_flags_without_a_session():
    probe = get_adapter('codex', model='gpt-x').probe_command()
    assert probe[1:] == ['exec', '--json', '--sandbox', 'read-only', '--cd', '.', '--model', 'gpt-x', '--version']


@pytest.mark.parametrize('command', [
    "/bin/zsh -lc 'npm test'",
    "/bin/bash -lc 'cd backend && pytest -q'",
    'bash -c "python -m http.server"',
])
def test_blocked_command_inside_the_shell_wrapper_is_flagged(command):
    """No marker in the output: on macOS codex's login shell can bypass the PATH shims."""
    [event] = _parse({'type': 'item.completed', 'item': {
        'id': 'item_1', 'type': 'command_execution', 'command': command,
        'aggregated_output': '', 'exit_code': 0, 'status': 'completed',
    }})
    assert (event.kind, event.level) == ('agent.tool_use', 'error')
    assert event.payload['policy_violation'] is True
    assert event.payload['command_preview'] == command


def test_shim_marker_in_the_output_is_flagged():
    [event] = _parse({'type': 'item.completed', 'item': {
        'id': 'item_1', 'type': 'command_execution', 'command': '/usr/bin/true',
        'aggregated_output': 'PR Review Control policy blocked command during static review.',
        'exit_code': 97, 'status': 'failed',
    }})
    assert event.payload['policy_violation'] is True


def test_read_command_is_informational():
    [event] = _parse({'type': 'item.completed', 'item': {
        'id': 'item_2', 'type': 'command_execution', 'command': "bash -lc 'sed -n 1,40p app.py'",
        'aggregated_output': 'import os\n', 'exit_code': 0, 'status': 'completed',
    }})
    assert (event.kind, event.level) == ('agent.tool_use', 'info')
    assert 'policy_violation' not in event.payload


def test_failed_turn_reports_its_reason():
    [event] = _parse({'type': 'turn.failed', 'error': {'message': 'model gpt-x does not exist'}})
    assert (event.kind, event.level) == ('agent.error', 'error')
    assert event.payload['error_message'] == 'model gpt-x does not exist'


def test_stream_error_is_a_warning():
    [event] = _parse({'type': 'error', 'message': 'Reconnecting... 1/5'})
    assert (event.kind, event.level) == ('agent.error', 'warning')


def test_turn_completed_reports_tokens():
    [event] = _parse({'type': 'turn.completed', 'usage': {
        'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20, 'reasoning_output_tokens': 5,
    }})
    assert event.kind == 'agent.step_finish'
    assert (event.payload['token_total'], event.payload['token_output'], event.payload['token_reasoning']) == (120, 20, 5)


@pytest.mark.parametrize('item', [
    {'id': 'i', 'type': 'mcp_tool_call', 'server': 'node_repl', 'tool': 'run', 'status': 'completed'},
    {'id': 'i', 'type': 'web_search', 'query': 'cve'},
    {'id': 'i', 'type': 'file_change', 'changes': [{'path': 'a.py', 'kind': 'update'}], 'status': 'completed'},
])
def test_actions_the_sandbox_does_not_cover_are_surfaced_as_warnings(item):
    [event] = _parse({'type': 'item.completed', 'item': item})
    assert (event.kind, event.level) == ('agent.tool_use', 'warning')
    assert event.payload['warning_reason']


def test_items_a_review_does_not_need_are_skipped():
    assert _parse({'type': 'item.completed', 'item': {'id': 'i', 'type': 'reasoning', 'text': 'thinking'}}) == []
    assert _parse({'type': 'item.started', 'item': {'id': 'i', 'type': 'command_execution'}}) == []
    assert _parse({'type': 'item.completed', 'item': {'id': 'i', 'type': 'agent_message', 'text': '  '}}) == []


def _round_result(stream, status):
    """Feed codex JSONL through the adapter and the result service, as a round does."""
    run = Run(task_id='t', backend='codex', status=status, started_at=0.0)
    adapter = get_adapter('codex')
    events = [
        Event(task_id='t', run_id=run.id, seq=seq, ts=0.0, kind=event.kind, payload=event.payload)
        for seq, event in enumerate((e for obj in stream for e in _parse(obj, adapter)), start=1)
    ]
    return ReviewResultService.extract_result(events=events, run=run)


def _message(text, phase=None):
    item = {'id': 'm', 'type': 'agent_message', 'text': text}
    if phase:
        item['phase'] = phase
    return {'type': 'item.completed', 'item': item}


FINAL = '## Review Summary\n\nOne bug.\n\n## Findings\n\n- high: `a.py:3` — off by one'


def test_completed_round_uses_the_final_answer():
    result = _round_result([_message("I'll read the patch first.", 'commentary'), _message(FINAL, 'final_answer'),
                            {'type': 'turn.completed', 'usage': {}}], 'completed')
    assert (result.verdict, result.finding_count, result.summary) == ('concerns', 1, 'One bug.')


def test_interim_commentary_is_never_taken_as_the_result():
    result = _round_result([_message("I'll read the patch first.", 'commentary'),
                            {'type': 'turn.failed', 'error': {'message': 'quota exceeded'}}], 'failed')
    assert result.verdict == 'failed'
    assert 'read the patch' not in result.summary


def test_messages_without_a_phase_still_count():
    """Older codex versions send no phase."""
    result = _round_result([_message(FINAL)], 'completed')
    assert result.finding_count == 1
