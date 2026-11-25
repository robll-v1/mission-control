"""P6 backend tests: safe Open-in-Editor, file snippets, atomic deletion & concurrency, diff, storage & repo status."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import app as app_module
from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.models import FindingStatus, TrackedFinding
from app.core.validation import ValidationService
from app.core.worktree import WorktreeManager
from app.services import editor_service
from app.services.artifact_store import ArtifactStore
from app.services.diff_service import DiffService


# ── 1. Safe Open-in-Editor, Native Binary Rules & Bounded Snippets ───────


def test_open_in_editor_blocks_traversal_symlink_and_unlisted_editors(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    inside = repo / 'notes.py'
    inside.write_text('line1\nline2\nline3\n', encoding='utf-8')
    outside = tmp_path / 'secret.txt'
    outside.write_text('top secret\n', encoding='utf-8')
    symlink = repo / 'escape.py'
    try:
        symlink.symlink_to(outside)
        has_symlink = True
    except OSError:
        has_symlink = False

    fake_code_bin = (
        r'C:\Program Files\Microsoft VS Code\Code.exe'
        if sys.platform == 'win32'
        else '/usr/local/bin/code'
    )
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        editor_service,
        '_locate_native_editor_binary',
        lambda key, **kw: fake_code_bin if key == 'code' else None,
    )
    monkeypatch.setattr(editor_service, '_spawn_editor', lambda argv: spawned.append(argv))

    # Valid relative file + line works with explicit argv (never shell)
    res = editor_service.open_in_editor(str(repo), 'notes.py', line=2, editor='code')
    assert res == {'ok': True, 'editor': 'code', 'path': 'notes.py', 'line': 2}
    assert spawned == [[fake_code_bin, '--goto', f'{inside.resolve()}:2']]

    # Path traversal blocked
    with pytest.raises(editor_service.PathAccessError):
        editor_service.open_in_editor(str(repo), '../secret.txt', editor='code')

    # Absolute outside path blocked
    with pytest.raises(editor_service.PathAccessError):
        editor_service.open_in_editor(str(repo), str(outside), editor='code')

    # Symlink escaping repo root blocked
    if has_symlink:
        with pytest.raises(editor_service.PathAccessError):
            editor_service.open_in_editor(str(repo), 'escape.py', editor='code')

    # Directory target blocked
    subdir = repo / 'sub'
    subdir.mkdir()
    with pytest.raises(editor_service.PathAccessError):
        editor_service.open_in_editor(str(repo), 'sub', editor='code')

    # Non-allowlisted editor / command injection blocked
    for bad_editor in ('sh', 'bash', 'python', 'rm -rf /', 'code; id', '../../bin/sh'):
        with pytest.raises(editor_service.EditorNotAllowedError):
            editor_service.open_in_editor(str(repo), 'notes.py', editor=bad_editor)

    # macOS fallback `open` always passes `-t` so scripts/bundles are never executed
    assert editor_service.build_editor_argv('open', '/usr/bin/open', inside.resolve(), line=2, platform='darwin') == [
        '/usr/bin/open',
        '-t',
        str(inside.resolve()),
    ]


def test_windows_rejects_cmd_bat_wrappers_and_requires_native_exe():
    # Reject .cmd/.bat/.ps1 shims on Windows even for allowlisted editor names
    assert editor_service._is_trusted_native_binary('C:\\Program Files\\VS Code\\bin\\code.cmd', platform='win32') is False
    assert editor_service._is_trusted_native_binary('C:\\tools\\code.bat', platform='win32') is False
    assert editor_service._is_trusted_native_binary('code.exe', platform='win32') is False  # relative path
    # Accept native absolute .exe on Windows
    assert editor_service._is_trusted_native_binary('C:\\Program Files\\Microsoft VS Code\\Code.exe', platform='win32') is True
    # Relative POSIX binaries rejected
    assert editor_service._is_trusted_native_binary('code', platform='darwin') is False
    assert editor_service._is_trusted_native_binary('/usr/local/bin/code', platform='darwin') is True


def test_read_file_snippet_highlights_line_and_rejects_binary_oversize_or_escape(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    src = repo / 'notes.py'
    src.write_text('a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n', encoding='utf-8')
    binary = repo / 'blob.bin'
    binary.write_bytes(b'\x00\x01\x02\x03')
    huge = repo / 'huge.txt'
    huge.write_bytes(b'x' * (editor_service._MAX_SNIPPET_FILE_BYTES + 10))

    snippet = editor_service.read_file_snippet(str(repo), 'notes.py', line=3, context=1)
    assert snippet['path'] == 'notes.py'
    assert snippet['target_line'] == 3
    assert snippet['lines'] == [
        {'number': 2, 'text': 'b = 2', 'highlighted': False},
        {'number': 3, 'text': 'c = 3', 'highlighted': True},
        {'number': 4, 'text': 'd = 4', 'highlighted': False},
    ]

    with pytest.raises(ValueError, match='binary'):
        editor_service.read_file_snippet(str(repo), 'blob.bin', line=1)

    with pytest.raises(ValueError, match='too large'):
        editor_service.read_file_snippet(str(repo), 'huge.txt', line=1)

    with pytest.raises(editor_service.PathAccessError):
        editor_service.read_file_snippet(str(repo), '../outside.py', line=1)


# ── 2. DB Savepoint Behavior Inside Outer Transactions ─────────────────────


def test_db_delete_task_respects_outer_transaction(tmp_path):
    db = Database(str(tmp_path / 'tx.db'))
    mission = MissionEngine(db)
    t1 = mission.create_task(title='T1', repo_path=str(tmp_path), source_type='local_diff')
    t2 = mission.create_task(title='T2', repo_path=str(tmp_path), source_type='local_diff')

    # Start an explicit outer transaction with an uncommitted insert/update
    db.conn.execute('BEGIN')
    db.conn.execute("UPDATE tasks SET data = data WHERE id = ?", (t2.id,))
    assert db.conn.in_transaction is True

    # delete_task must NOT commit the outer transaction
    assert db.delete_task(t1.id) is True
    assert db.conn.in_transaction is True

    # Rolling back the outer transaction restores t1
    db.conn.rollback()
    assert db.get_task(t1.id) is not None
    assert db.get_task(t2.id) is not None

    # Standalone delete commits normally
    assert db.delete_task(t1.id) is True
    assert db.get_task(t1.id) is None
    db.close()


# ── 3. Task & Config Endpoints + Diff/Symlink/Worktree Cleanup Safety ────


def test_p6_task_and_config_endpoints(git_repo, tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'p6.db'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'rt'))
    worktrees = WorktreeManager(str(tmp_path / 'rt'), worktrees_root=str(tmp_path / 'wt'))
    contexts = ContextCompiler(artifacts)
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
    )
    (git_repo / 'notes.py').write_text('def hello():\n    return 1\n', encoding='utf-8')

    task = mission.create_task(
        title='P6 Test Review',
        repo_path=str(git_repo),
        source_type='local_diff',
        review_paths=['notes.py'],
    )
    artifacts.write_text(task.id, DiffService.EXPORT_DIFF_REL, '--- a/notes.py\n+++ b/notes.py\n+def hello():\n')
    db.save_findings([
        TrackedFinding(
            task_id=task.id,
            fingerprint='fp1',
            severity='high',
            path='notes.py',
            line=2,
            title='Return value hardcoded',
            first_round=1,
            last_round=1,
            status=FindingStatus.NEW,
            system_status=FindingStatus.NEW,
        )
    ], task_id=task.id, synced_round=1)

    monkeypatch.setattr(app_module, 'engine', mission)
    monkeypatch.setattr(app_module, 'db', db)
    monkeypatch.setattr(app_module, 'artifacts', artifacts)
    monkeypatch.setattr(app_module, 'execution', execution)

    fake_code_bin = (
        r'C:\Program Files\Microsoft VS Code\Code.exe'
        if sys.platform == 'win32'
        else '/usr/bin/code'
    )
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        editor_service,
        '_locate_native_editor_binary',
        lambda key, **kw: fake_code_bin if key == 'code' else None,
    )
    monkeypatch.setattr(editor_service, '_spawn_editor', lambda argv: spawned.append(argv))

    client = TestClient(app_module.app)
    hdrs = {'host': '127.0.0.1:8000'}

    # Snippet endpoint
    r_snip = client.get(f'/api/tasks/{task.id}/snippet?path=notes.py&line=2&context=1', headers=hdrs)
    assert r_snip.status_code == 200
    assert r_snip.json()['lines'][-1]['highlighted'] is True
    assert client.get(f'/api/tasks/{task.id}/snippet?path=../escape.py&line=1', headers=hdrs).status_code == 400

    # Diff endpoint reads shared DiffService.EXPORT_DIFF_REL and ignores pull_request.md
    r_diff = client.get(f'/api/tasks/{task.id}/diff', headers=hdrs)
    assert r_diff.status_code == 200
    assert r_diff.json()['source'] == 'export_diff'
    assert '+def hello():' in r_diff.json()['diff']
    assert r_diff.json()['changed_files'] == ['notes.py']

    # PR task with only pull_request.md returns source='none' (never treats markdown as diff)
    pr_task = mission.create_task(title='PR Only', repo_path=str(git_repo), source_type='pull_request')
    artifacts.write_text(pr_task.id, 'context/pull_request.md', '# PR body')
    r_pr_diff = client.get(f'/api/tasks/{pr_task.id}/diff', headers=hdrs)
    assert r_pr_diff.status_code == 200
    assert r_pr_diff.json()['source'] == 'none'
    assert r_pr_diff.json()['diff'] == ''

    # Open in editor endpoint
    r_open = client.post(
        f'/api/tasks/{task.id}/open-in-editor',
        json={'path': 'notes.py', 'line': 2, 'editor': 'code'},
        headers=hdrs,
    )
    assert r_open.status_code == 200
    assert spawned and spawned[0][:2] == [fake_code_bin, '--goto']
    assert client.post(
        f'/api/tasks/{task.id}/open-in-editor',
        json={'path': '../outside', 'line': 1},
        headers=hdrs,
    ).status_code == 400
    assert client.post(
        f'/api/tasks/{task.id}/open-in-editor',
        json={'path': 'notes.py', 'editor': 'bash'},
        headers=hdrs,
    ).status_code == 400

    # Storage & repo-status endpoints (and verify safe read-only git flags)
    r_storage = client.get('/api/config/storage', headers=hdrs)
    assert r_storage.status_code == 200
    assert 'data_dir' in r_storage.json() and 'config_path' in r_storage.json()

    recorded_cmds: list[list[str]] = []
    from app.core import proc as proc_mod
    real_run_text = proc_mod.run_text

    def spy_run_text(cmd, *args, **kwargs):
        recorded_cmds.append(list(cmd))
        return real_run_text(cmd, *args, **kwargs)

    monkeypatch.setattr(proc_mod, 'run_text', spy_run_text)
    r_repo = client.get('/api/config/repo-status', params={'repo_path': str(git_repo)}, headers=hdrs)
    assert r_repo.status_code == 200
    assert r_repo.json()['is_git_repo'] is True
    assert r_repo.json()['branch'] == 'main'
    assert r_repo.json()['changed_files'] >= 1
    git_cmds = [c for c in recorded_cmds if c and c[0] == 'git']
    assert git_cmds
    for cmd in git_cmds:
        assert '--no-optional-locks' in cmd
        assert 'core.fsmonitor=false' in cmd

    # Worktree cleanup failure aborts DELETE (500) and keeps task retryable
    task.worktree_path = str(tmp_path / 'wt' / task.id)
    db.save_task(task)
    monkeypatch.setattr(worktrees, 'cleanup_worktree', lambda _t: {'exists_after': True})
    assert client.delete(f'/api/tasks/{task.id}', headers=hdrs).status_code == 500
    assert db.get_task(task.id) is not None

    # Symlinked task artifact directory: DELETE unlinks only symlink, preserves neighbor target
    monkeypatch.setattr(worktrees, 'cleanup_worktree', lambda _t: {'exists_after': False})
    victim_dir = tmp_path / 'victim_neighbor'
    victim_dir.mkdir()
    victim_file = victim_dir / 'keep.txt'
    victim_file.write_text('precious', encoding='utf-8')
    sym_task = mission.create_task(title='Sym Task', repo_path=str(git_repo), source_type='local_diff')
    tasks_root = artifacts.runtime_root / 'tasks'
    tasks_root.mkdir(parents=True, exist_ok=True)
    sym_entry = tasks_root / sym_task.id
    try:
        sym_entry.symlink_to(victim_dir)
        has_dir_symlink = True
    except OSError:
        has_dir_symlink = False
    if has_dir_symlink:
        assert client.delete(f'/api/tasks/{sym_task.id}', headers=hdrs).status_code == 200
        assert not sym_entry.exists() and not sym_entry.is_symlink()
        assert victim_file.read_text(encoding='utf-8') == 'precious'

    # Normal DELETE succeeds and purges DB + artifacts
    r_del = client.delete(f'/api/tasks/{task.id}', headers=hdrs)
    assert r_del.status_code == 200
    assert db.get_task(task.id) is None
    assert db.list_findings(task.id) == []
    assert not (artifacts.runtime_root / 'tasks' / task.id).exists()
    db.close()


# ── 4. Barrier-Synchronized Concurrency: Start vs Delete & Validation vs Delete ─


def test_concurrent_delete_and_start_are_mutually_exclusive(git_repo, tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'race_start.db'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'rt'))
    worktrees = WorktreeManager(str(tmp_path / 'rt'), worktrees_root=str(tmp_path / 'wt'))
    contexts = ContextCompiler(artifacts)
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
        adapter_factory=lambda *a, **kw: SimpleNamespace(name='codex'),
    )
    task = mission.create_task(title='Race Start', repo_path=str(git_repo), source_type='local_diff')
    task.worktree_path = str(tmp_path / 'wt' / task.id)
    db.save_task(task)

    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()

    def slow_cleanup(_t):
        entered_cleanup.set()
        release_cleanup.wait(timeout=5)
        return {'exists_after': False}

    monkeypatch.setattr(worktrees, 'cleanup_worktree', slow_cleanup)
    monkeypatch.setattr(app_module, 'engine', mission)
    monkeypatch.setattr(app_module, 'db', db)
    monkeypatch.setattr(app_module, 'artifacts', artifacts)
    monkeypatch.setattr(app_module, 'execution', execution)

    client = TestClient(app_module.app)
    hdrs = {'host': '127.0.0.1:8000'}

    del_result: dict[str, int] = {}

    def run_delete():
        thread_client = TestClient(app_module.app)
        res = thread_client.delete(f'/api/tasks/{task.id}', headers=hdrs)
        del_result['status'] = res.status_code

    t = threading.Thread(target=run_delete)
    t.start()
    assert entered_cleanup.wait(timeout=5), 'DELETE did not enter worktree cleanup'

    # While DELETE holds the exclusive slot, start_task MUST be rejected with 409
    r_start_during = client.post(f'/api/tasks/{task.id}/start', json={'language': 'en'}, headers=hdrs)
    assert r_start_during.status_code == 409

    release_cleanup.set()
    t.join(timeout=5)
    assert del_result.get('status') == 200
    assert db.get_task(task.id) is None

    # After DELETE finishes, start_task returns 404 (never revives the deleted task)
    r_start_after = client.post(f'/api/tasks/{task.id}/start', json={'language': 'en'}, headers=hdrs)
    assert r_start_after.status_code == 404
    db.close()


def test_concurrent_validation_blocks_delete_until_finished(git_repo, tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'race_val.db'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'rt'))
    worktrees = WorktreeManager(str(tmp_path / 'rt'), worktrees_root=str(tmp_path / 'wt'))
    contexts = ContextCompiler(artifacts)
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=contexts,
    )
    validation = ValidationService(db=db, mission=mission, artifacts=artifacts, execution=execution)
    task = mission.create_task(title='Race Validation', repo_path=str(git_repo), source_type='local_diff')

    entered_val = threading.Event()
    release_val = threading.Event()

    def slow_validate(tid: str, mode: str | None) -> None:
        entered_val.set()
        release_val.wait(timeout=5)

    monkeypatch.setattr(validation, '_validate_task', slow_validate)
    monkeypatch.setattr(app_module, 'engine', mission)
    monkeypatch.setattr(app_module, 'db', db)
    monkeypatch.setattr(app_module, 'artifacts', artifacts)
    monkeypatch.setattr(app_module, 'execution', execution)
    monkeypatch.setattr(app_module, 'validation', validation)

    client = TestClient(app_module.app)
    hdrs = {'host': '127.0.0.1:8000'}

    r_val = client.post(f'/api/tasks/{task.id}/validate', json={'mode': 'standard'}, headers=hdrs)
    assert r_val.status_code == 200
    assert entered_val.wait(timeout=5), 'Validation thread did not start'

    # While validation holds the shared slot, DELETE and overlapping validate both return 409
    assert client.delete(f'/api/tasks/{task.id}', headers=hdrs).status_code == 409
    assert client.post(f'/api/tasks/{task.id}/validate', json={'mode': 'standard'}, headers=hdrs).status_code == 409

    release_val.set()
    # Wait for validation thread's finally block to release the reservation
    for _ in range(250):
        if not execution.is_active(task.id):
            break
        threading.Event().wait(0.02)
    assert not execution.is_active(task.id)

    # Now DELETE succeeds cleanly
    assert client.delete(f'/api/tasks/{task.id}', headers=hdrs).status_code == 200
    assert db.get_task(task.id) is None
    db.close()
