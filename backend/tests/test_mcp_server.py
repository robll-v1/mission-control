"""Tests for the decoupled reviewer selector, McpReviewService, and stdio MCP server."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.base import RunnerAdapter
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.reviewer_selection import normalize_agent_name, select_reviewer_backend
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.mcp_service import McpReviewService


# ── Helpers ───────────────────────────────────────────────────────────


class _FakeReviewAdapter(RunnerAdapter):
    """In-process controlled adapter spawning a tiny Python child (no real CLI/LLM)."""

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


def _make_emit_script(payload: dict, *, sleep_sec: float = 0.0) -> str:
    body = json.dumps(payload)
    json_block = f'Review complete.\n\n```json\n{body}\n```'
    line = json.dumps({'type': 'text', 'part': {'text': json_block}})
    sleep_code = f'import time; time.sleep({sleep_sec}); ' if sleep_sec > 0 else ''
    return f'{sleep_code}print({line!r}, flush=True)'


def _build_service(
    tmp_path: Path,
    *,
    adapters: dict[str, RunnerAdapter],
    detected_names: list[str],
    adapter_factory=None,
) -> tuple[McpReviewService, Database]:
    db_path = tmp_path / 'shared.db'
    runtime_root = tmp_path / 'runtime'
    worktrees_root = tmp_path / 'worktrees'
    db = Database(str(db_path))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(runtime_root))
    worktrees = WorktreeManager(str(runtime_root), worktrees_root=str(worktrees_root))
    contexts = ContextCompiler(artifacts)
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters=adapters,
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        adapter_factory=adapter_factory,
    )
    caps = [
        SimpleNamespace(
            name=name,
            version=f'{name} 1.0.0',
            models=[f'{name}-default'],
            models_source='cli',
            reasoning_supported=(name == 'codex'),
            reasoning_levels=['low', 'medium', 'high'] if name == 'codex' else [],
            reasoning_default='medium' if name == 'codex' else None,
            model_reasoning={},
        )
        for name in detected_names
    ]

    def _detect_all():
        return list(caps)

    def _detect_one(name: str):
        for cap in caps:
            if cap.name == name:
                return cap
        return None

    def _probe(name: str, **_kwargs):
        installed = any(cap.name == name for cap in caps)
        return {
            'ok': installed,
            'backend': name,
            'executable': name,
            'message': f'{name} static check passed.' if installed else f'{name} not found',
        }

    service = McpReviewService(
        db=db,
        mission=mission,
        execution=execution,
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        detect_all_fn=_detect_all,
        detect_backend_fn=_detect_one,
        probe_backend_fn=_probe,
        poll_interval_sec=0.05,
    )
    return service, db


# ── 1. Pure cross-agent reviewer selection (Option A) ─────────────────


def test_reviewer_selection_option_a_policy():
    assert normalize_agent_name('Claude') == 'claude-code'
    assert normalize_agent_name('codex-cli') == 'codex'
    assert normalize_agent_name('  ') is None

    # Explicit always wins
    sel = select_reviewer_backend(requested='opencode', exclude='opencode', installed=['codex', 'opencode'])
    assert sel.backend == 'opencode'
    assert sel.reason == 'explicit'
    assert sel.note is None

    # Cross-agent avoids host when another CLI is installed
    sel2 = select_reviewer_backend(exclude='claude-code', installed=['claude-code', 'codex'])
    assert sel2.backend == 'codex'
    assert sel2.reason == 'cross_agent_excluding_claude-code'
    assert sel2.note is None

    sel3 = select_reviewer_backend(exclude='codex', installed=['codex', 'claude-code'])
    assert sel3.backend == 'claude-code'
    assert sel3.reason == 'cross_agent_excluding_claude-code'.replace('claude-code', 'codex')

    # Option A: honest fallback when only the host CLI is installed
    sel4 = select_reviewer_backend(exclude='claude', installed=['claude-code'])
    assert sel4.backend == 'claude-code'
    assert sel4.reason == 'fallback_same_agent'
    assert sel4.note and 'Only claude-code was detected locally' in sel4.note

    # No exclude -> configured default when installed, else priority order
    sel5 = select_reviewer_backend(installed=['opencode', 'codex'], configured_default='opencode')
    assert sel5.backend == 'opencode'
    assert sel5.reason == 'configured_default'

    # None installed -> structured none_installed
    sel6 = select_reviewer_backend(exclude='codex', installed=[])
    assert sel6.backend is None
    assert sel6.reason == 'none_installed'


# ── 2. FastMCP tool registration & schema contract ────────────────────


def test_mcp_entrypoint_registers_new_five_tools(isolated_config):
    server_module = importlib.import_module('app.mcp_server')
    tools = asyncio.run(server_module.mcp.list_tools())

    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {
        'check_agents',
        'start_review',
        'wait_review',
        'update_finding',
        'verify_findings',
        'abort_review',
    }
    for name, tool in by_name.items():
        schema = tool.inputSchema
        assert schema['type'] == 'object', name
        json.dumps(schema)
    assert 'task_id' in by_name['wait_review'].inputSchema['required']
    assert 'task_id' in by_name['abort_review'].inputSchema['required']
    assert {'task_id', 'finding_id', 'action'} <= set(by_name['update_finding'].inputSchema['required'])


# ── 3. check_agents tool ──────────────────────────────────────────────


def test_check_agents_reports_installed_and_rejects_unknown(tmp_path, isolated_config):
    service, _ = _build_service(tmp_path, adapters={}, detected_names=['codex', 'claude-code'])

    all_res = service.check_agents()
    assert all_res['ok'] is True
    assert all_res['installed_backends'] == ['codex', 'claude-code']
    assert all(b['models_call_verified'] is False for b in all_res['backends'])

    one_res = service.check_agents('claude')
    assert one_res['ok'] is True
    assert one_res['backends'][0]['backend'] == 'claude-code'

    bad_res = service.check_agents('not-a-real-backend')
    assert bad_res['ok'] is False
    assert bad_res['error']['code'] == 'unknown_backend'


# ── 4. Full cross-agent 2-round lifecycle + shared SQLite + update_finding ─


def test_mcp_full_two_round_lifecycle_and_shared_sqlite(git_repo, tmp_path, isolated_config):
    # Create uncommitted bug in git_repo
    target_file = git_repo / 'app.py'
    target_file.write_text('def run(x):\n    return eval(x)\n', encoding='utf-8')

    round1_payload = {
        'verdict': 'concerns',
        'summary': 'Found unsafe eval usage.',
        'findings': [
            {
                'severity': 'high',
                'category': 'security',
                'path': 'app.py',
                'line': 2,
                'title': 'Unsafe eval on untrusted input',
                'detail': 'Calling eval(x) allows arbitrary code execution.',
            }
        ],
    }
    round2_payload = {
        'verdict': 'clear',
        'summary': 'No issues found after replacing eval.',
        'findings': [],
    }

    adapters = {
        'codex': _FakeReviewAdapter('codex', _make_emit_script(round1_payload)),
        'claude-code': _FakeReviewAdapter('claude-code', _make_emit_script(round2_payload)),
    }
    service, db = _build_service(
        tmp_path,
        adapters=adapters,
        detected_names=['codex', 'claude-code'],
    )

    try:
        # Round 1: host is claude-code -> auto-selects codex
        started = service.start_review(repo_path=str(git_repo), exclude_agent='claude-code')
        assert started['ok'] is True
        assert started['selected_backend'] == 'codex'
        assert started['selection_reason'] == 'cross_agent_excluding_claude-code'
        assert started['round_index'] == 1
        task_id = started['task_id']

        res1 = service.wait_review(task_id=task_id, wait_sec=15)
        assert res1['ok'] is True
        assert res1['done'] is True
        assert res1['verdict'] == 'concerns'
        assert res1['passed'] is False
        assert res1['round_index'] == 1
        assert res1['finding_counts']['new'] == 1
        assert len(res1['findings']) == 1
        finding = res1['findings'][0]
        assert finding['path'] == 'app.py'
        assert finding['status'] == 'new'

        # Shared SQLite verification: Web Workbench reading the same DB sees the exact task & finding
        stored_task = db.get_task(task_id)
        assert stored_task is not None
        assert len(db.list_findings(task_id)) == 1

        # Fix the bug in working tree (still uncommitted!) and run Round 2 on claude-code
        target_file.write_text('def run(x):\n    return int(x)\n', encoding='utf-8')
        started2 = service.start_review(task_id=task_id, backend='claude-code', note='Replaced eval with int')
        assert started2['ok'] is True
        assert started2['selected_backend'] == 'claude-code'
        assert started2['round_index'] == 2

        res2 = service.wait_review(task_id=task_id, wait_sec=15)
        assert res2['ok'] is True
        assert res2['done'] is True
        assert res2['round_index'] == 2
        assert res2['verdict'] == 'clear'
        assert res2['passed'] is True
        # Finding must transition to likely_fixed (NEVER auto-resolved)
        assert res2['finding_counts']['likely_fixed'] == 1
        assert res2['finding_counts']['resolved'] == 0
        assert res2['findings'][0]['status'] == 'likely_fixed'

        # Human disposition via update_finding persists to SQLite
        upd = service.update_finding(task_id=task_id, finding_id=finding['id'], action='resolve')
        assert upd['ok'] is True
        assert upd['finding']['status'] == 'resolved'
        assert upd['finding_counts']['resolved'] == 1
        assert db.list_findings(task_id)[0].status.value == 'resolved'
    finally:
        service.shutdown()


# ── 5. Option A fallback when only host CLI is installed ──────────────


def test_mcp_option_a_same_agent_fallback_honestly_reported(git_repo, tmp_path, isolated_config):
    (git_repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
    clear_payload = {'verdict': 'clear', 'summary': 'Clean.', 'findings': []}
    adapters = {'claude-code': _FakeReviewAdapter('claude-code', _make_emit_script(clear_payload))}
    service, _ = _build_service(tmp_path, adapters=adapters, detected_names=['claude-code'])

    try:
        started = service.start_review(repo_path=str(git_repo), exclude_agent='claude-code')
        assert started['ok'] is True
        assert started['selected_backend'] == 'claude-code'
        assert started['selection_reason'] == 'fallback_same_agent'
        assert started['selection_note'] and 'Only claude-code' in started['selection_note']

        done = service.wait_review(task_id=started['task_id'], wait_sec=15)
        assert done['done'] is True
        assert done['verdict'] == 'clear'
        assert done['selection_reason'] == 'fallback_same_agent'
    finally:
        service.shutdown()


# ── 6. Bounded long-poll + abort_review cancels live subprocess ───────


def test_mcp_bounded_wait_and_abort_review(git_repo, tmp_path, isolated_config):
    (git_repo / 'app.py').write_text('x = 42\n', encoding='utf-8')
    slow_script = _make_emit_script({'verdict': 'clear', 'summary': 'Too late', 'findings': []}, sleep_sec=20.0)
    adapters = {'codex': _FakeReviewAdapter('codex', slow_script)}
    service, _ = _build_service(tmp_path, adapters=adapters, detected_names=['codex'])

    try:
        started = service.start_review(repo_path=str(git_repo), backend='codex')
        assert started['ok'] is True
        task_id = started['task_id']

        # Immediate wait_sec=0 returns running progress without blocking
        progress = service.wait_review(task_id=task_id, wait_sec=0)
        assert progress['ok'] is True
        assert progress['done'] is False

        # Abort terminates the active subprocess and persists aborted status
        aborted = service.abort_review(task_id=task_id)
        assert aborted['ok'] is True
        assert aborted['aborted'] is True
        assert aborted['status'] == 'aborted'

        settled = service.wait_review(task_id=task_id, wait_sec=5)
        assert settled['done'] is True
        assert settled['status'] == 'aborted'
        assert settled['verdict'] == 'inconclusive'
        assert settled['passed'] is False
    finally:
        service.shutdown()


# ── 7. Real stdio JSON-RPC framing & zero stdout pollution ────────────


def test_mcp_stdio_protocol_handshake_and_zero_stdout_pollution(tmp_path):
    """Spawn `python -m app.mcp_server`, perform MCP initialize + tools/list,
    and verify every single line written to stdout is strictly valid JSON-RPC."""
    env = {
        **os.environ,
        'AMC_DATA_DIR': str(tmp_path / 'data'),
        'AMC_NO_LEGACY_IMPORT': '1',
        'XDG_CONFIG_HOME': str(tmp_path / 'xdg'),
    }
    proc = subprocess.Popen(
        [sys.executable, '-m', 'app.mcp_server'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        init_req = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': '2024-11-05',
                'capabilities': {},
                'clientInfo': {'name': 'pytest-mcp-client', 'version': '0.1'},
            },
        }
        proc.stdin.write(json.dumps(init_req) + '\n')
        proc.stdin.flush()

        # Read initialize response with timeout guard
        responses: list[dict] = []

        def _read_lines():
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                # Every stdout line MUST be valid JSON-RPC (no stray prints!)
                parsed = json.loads(line)
                responses.append(parsed)
                if len(responses) >= 2:
                    break

        reader = threading.Thread(target=_read_lines, daemon=True)
        reader.start()

        deadline = time.monotonic() + 10.0
        while len(responses) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(responses) >= 1, 'Timed out waiting for MCP initialize response'
        assert responses[0]['id'] == 1
        assert 'result' in responses[0]

        # Send initialized notification + tools/list
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}) + '\n')
        proc.stdin.flush()

        reader.join(timeout=10.0)
        assert len(responses) >= 2, 'Timed out waiting for MCP tools/list response'
        tool_names = {t['name'] for t in responses[1]['result']['tools']}
        assert tool_names == {
            'check_agents',
            'start_review',
            'wait_review',
            'update_finding',
            'verify_findings',
            'abort_review',
        }
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
