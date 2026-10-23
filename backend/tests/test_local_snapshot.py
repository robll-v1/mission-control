from __future__ import annotations

import subprocess

from app.core.context_compiler import ContextCompiler
from app.core.db import Database
from app.core.execution import ExecutionService
from app.core.mission_engine import MissionEngine
from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore
from app.services.local_review_service import capture_local_snapshot
from app.services.local_review_service import build_local_review_source
from app.services.review_history import ReviewHistoryStore, ReviewRecord, StoredFinding


def _git(repo, *args):
    return subprocess.run(
        ['git', '-C', str(repo), *args], capture_output=True, text=True,
        encoding='utf-8', errors='replace', check=False,
    )


def test_snapshot_uses_final_worktree_without_touching_real_index(git_repo, tmp_path):
    repo = git_repo
    head_before = _git(repo, 'rev-parse', 'HEAD').stdout.strip()

    (repo / 'a.txt').write_text('staged version\n', encoding='utf-8')
    _git(repo, 'add', 'a.txt')
    (repo / 'a.txt').write_text('final unstaged version\n', encoding='utf-8')
    (repo / '目录').mkdir()
    (repo / '目录' / '新文件.txt').write_text('unicode file\n', encoding='utf-8')
    (repo / 'a.txt').rename(repo / 'renamed.txt')
    (repo / '.amc' / 'reviews').mkdir(parents=True)
    (repo / '.amc' / 'reviews' / 'latest.json').write_text('generated\n', encoding='utf-8')
    (repo / 'runtime').mkdir()
    (repo / 'runtime' / 'owned.txt').write_text('user code\n', encoding='utf-8')
    (repo / 'runtime' / 'tasks' / 'old-task').mkdir(parents=True)
    (repo / 'runtime' / 'tasks' / 'old-task' / 'context.json').write_text('generated\n', encoding='utf-8')
    _git(repo, 'add', '-f', 'runtime/tasks/old-task/context.json')
    (repo / '.gitignore').write_text('forced.txt\n', encoding='utf-8')
    (repo / 'forced.txt').write_text('force-added ignored content\n', encoding='utf-8')
    _git(repo, 'add', '-f', 'forced.txt')
    index_before = _git(repo, 'write-tree').stdout.strip()
    staged_patch_before = _git(repo, 'diff', '--cached', '--binary').stdout

    head_sha, snapshot_sha, base, changed_files, patch = capture_local_snapshot(
        str(repo), 'main', generated_paths=['runtime/tasks', 'data/amc.db'],
    )

    assert base == 'main'
    assert head_sha == head_before
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == head_before
    assert _git(repo, 'write-tree').stdout.strip() == index_before
    assert _git(repo, 'diff', '--cached', '--binary').stdout == staged_patch_before
    assert 'a.txt' in changed_files and 'renamed.txt' in changed_files
    assert 'a.txt' in changed_files
    assert '目录/新文件.txt' in changed_files
    assert 'forced.txt' in changed_files
    assert '.amc/reviews/latest.json' not in changed_files
    assert 'runtime/owned.txt' in changed_files
    assert 'runtime/tasks/old-task/context.json' not in changed_files
    assert '+final unstaged version' in patch
    assert '+staged version' not in patch
    assert _git(repo, 'show', f'{snapshot_sha}:renamed.txt').stdout == 'final unstaged version\n'
    assert _git(repo, 'show', f'{snapshot_sha}:目录/新文件.txt').stdout == 'unicode file\n'
    assert _git(repo, 'show', f'{snapshot_sha}:forced.txt').stdout == 'force-added ignored content\n'


def test_unmatched_findings_remain_unverified_across_incremental_rounds(git_repo):
    store = ReviewHistoryStore(str(git_repo))
    old = StoredFinding(severity='high', path='a.txt', line=1, summary='validate input')
    store.save_review(ReviewRecord(
        review_id='r1', timestamp=1, head_sha='old', base_ref='main', mode='local_diff',
        verdict='concerns', findings=[old], finding_count=1,
    ))

    first = store.compare_findings([])
    assert [status.finding.summary for status in first['unverified']] == ['validate input']
    store.save_review(ReviewRecord(
        review_id='r2', timestamp=2, head_sha='new', base_ref='old', mode='local_diff',
        verdict='concerns', findings=[status.finding for status in first['unverified']], finding_count=1,
    ))

    second = store.compare_findings([])
    assert [status.finding.summary for status in second['unverified']] == ['validate input']


def test_worktree_and_compiled_context_use_the_captured_snapshot(git_repo, tmp_path, isolated_config):
    (git_repo / 'a.txt').write_text('snapshot source line\n', encoding='utf-8')
    (git_repo / '目录').mkdir()
    (git_repo / '目录' / '新文件.txt').write_text('new source line\n', encoding='utf-8')
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    task = mission.create_task(
        title=local.title,
        repo_path=str(git_repo),
        source_type='local_diff',
        pr_base_ref=local.base_branch,
        review_paths=local.changed_files,
        local_head_sha=local.head_sha,
        local_snapshot_sha=local.snapshot_sha,
    )
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    artifacts.write_text(task.id, 'context/local_diff.patch', local.diff_text)
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    _, worktree_path, revision = worktrees.ensure_worktree(task)
    task.branch_name = f'review/{task.id}'
    task.worktree_path = worktree_path
    compiled = ContextCompiler(artifacts).compile_task(task, worktree_path=worktree_path)

    assert revision == local.snapshot_sha
    assert (tmp_path / 'runtime' / 'worktrees' / task.id / 'a.txt').read_text(encoding='utf-8') == 'snapshot source line\n'
    paths = {item['path'] for item in compiled.payload['review']['file_patches']}
    assert {'a.txt', '目录/新文件.txt'} <= paths
    snippets = {item['path'] for item in compiled.payload['related_snippets']}
    assert 'a.txt' in snippets
    assert any('snapshot source line' in item.get('text', '') for item in compiled.payload['related_snippets'])
    worktrees.cleanup_worktree(task)
    db.close()


def test_execution_refreshes_local_snapshot_each_round(git_repo, tmp_path, isolated_config):
    (git_repo / 'a.txt').write_text('first round\n', encoding='utf-8')
    local = build_local_review_source(str(git_repo), base='main')
    db = Database(str(tmp_path / 'db.sqlite'))
    mission = MissionEngine(db)
    artifacts = ArtifactStore(str(tmp_path / 'runtime'))
    worktrees = WorktreeManager(str(tmp_path / 'runtime'))
    task = mission.create_task(
        title=local.title,
        repo_path=str(git_repo),
        source_type='local_diff',
        pr_base_ref=local.base_branch,
        review_paths=local.changed_files,
        local_head_sha=local.head_sha,
        local_snapshot_sha=local.snapshot_sha,
    )
    artifacts.write_text(task.id, 'context/local_diff.patch', local.diff_text)
    execution = ExecutionService(
        db=db,
        mission=mission,
        adapters={},
        worktrees=worktrees,
        artifacts=artifacts,
        contexts=ContextCompiler(artifacts),
    )

    (git_repo / 'a.txt').write_text('second round changed line\n', encoding='utf-8')
    (git_repo / 'new.txt').write_text('new second round file\n', encoding='utf-8')
    refreshed = execution._refresh_local_snapshot(task)
    patch = artifacts.read_text(task.id, 'context/local_diff.patch')

    assert refreshed.local_head_sha == _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    assert refreshed.local_snapshot_sha != local.snapshot_sha
    assert {'a.txt', 'new.txt'} <= set(refreshed.review_paths)
    assert '+second round changed line' in patch and '+new second round file' in patch
    db.close()
