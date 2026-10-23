from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import tempfile
from pathlib import Path

from app.core.proc import run_git


@dataclass(frozen=True)
class LocalReviewSource:
    base_branch: str
    changed_files: list[str]
    diff_text: str
    commit_messages: list[str]
    title: str
    description: str
    head_sha: str = ''
    snapshot_sha: str = ''


def resolve_base_branch(repo_path: str, requested_base: str | None = None) -> str:
    requested = (requested_base or '').strip()
    candidates = [requested] if requested else ['main', 'master', 'origin/main', 'origin/master']
    for candidate in candidates:
        result = run_git(
            repo_path,
            ['rev-parse', '--verify', '--quiet', f'{candidate}^{{commit}}'],
            timeout=10,
        )
        if result.returncode == 0:
            return candidate
    if requested:
        raise ValueError(f'base revision does not exist: {requested}')
    raise ValueError('could not detect a base branch; specify base explicitly')


def get_local_changed_files(repo_path: str, base: str) -> list[str]:
    files: set[str] = set()
    commands = [
        ['diff', '--name-only', f'{base}...HEAD'],
        ['diff', '--cached', '--name-only'],
        ['diff', '--name-only'],
        ['ls-files', '--others', '--exclude-standard'],
    ]
    for args in commands:
        result = run_git(repo_path, args, timeout=30)
        if result.returncode == 0:
            files.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(files)


def get_local_diff(repo_path: str, base: str) -> str:
    parts: list[str] = []
    commands = [
        ['diff', f'{base}...HEAD'],
        ['diff', '--cached'],
        ['diff'],
    ]
    for args in commands:
        result = run_git(repo_path, args, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            parts.append(result.stdout)
    untracked = run_git(repo_path, ['ls-files', '--others', '--exclude-standard'], timeout=30)
    if untracked.returncode == 0:
        for path in (line.strip() for line in untracked.stdout.splitlines() if line.strip()):
            result = run_git(repo_path, ['diff', '--no-index', '--', '/dev/null', path], timeout=60)
            # git diff --no-index returns 1 when it successfully finds differences.
            if result.returncode in (0, 1) and result.stdout.strip():
                parts.append(result.stdout)
    return '\n'.join(parts)


def capture_local_snapshot(
    repo_path: str,
    base: str | None = None,
    *,
    generated_paths: list[str] | None = None,
) -> tuple[str, str, str, list[str], str]:
    """Capture the current worktree in a commit without touching its real index."""
    repo = str(Path(repo_path).resolve())
    head = run_git(repo, ['rev-parse', 'HEAD'], timeout=10)
    if head.returncode:
        raise RuntimeError(head.stderr.strip() or 'failed to resolve repository HEAD')
    head_sha = head.stdout.strip()
    fd, index_path = tempfile.mkstemp(prefix='amc-index-')
    os.close(fd)
    env = os.environ.copy()
    env['GIT_INDEX_FILE'] = index_path
    try:
        unmerged = run_git(repo, ['ls-files', '--unmerged'], timeout=20)
        if unmerged.returncode:
            raise RuntimeError(unmerged.stderr.strip() or 'failed to inspect repository index')
        if unmerged.stdout.strip():
            raise RuntimeError('cannot capture local review snapshot while the git index has unresolved conflicts')
        real_index = run_git(repo, ['rev-parse', '--git-path', 'index'], timeout=10)
        if real_index.returncode:
            raise RuntimeError(real_index.stderr.strip() or 'failed to locate repository index')
        real_index_path = Path(real_index.stdout.strip())
        if not real_index_path.is_absolute():
            real_index_path = Path(repo) / real_index_path
        if real_index_path.exists():
            shutil.copyfile(real_index_path, index_path)
        else:
            read_tree = run_git(repo, ['read-tree', head_sha], timeout=30, env=env)
            if read_tree.returncode:
                raise RuntimeError(read_tree.stderr.strip() or 'failed to initialize temporary git index')
        pathspecs = ['.']
        excluded_paths: list[str] = []
        exclusions = ['.amc/reviews', *(generated_paths or [])]
        for generated in dict.fromkeys(path.rstrip('/') for path in exclusions if path):
            tracked = run_git(repo, ['ls-tree', '-r', '--name-only', '-z', head_sha, '--', generated], timeout=20)
            if tracked.returncode:
                raise RuntimeError(tracked.stderr.strip() or f'failed to inspect generated path {generated}')
            if tracked.stdout:
                continue
            excluded_paths.append(generated)
            pathspecs.extend((f':(exclude){generated}/**', f':(exclude){generated}'))
        if excluded_paths:
            remove_generated = run_git(
                repo, ['rm', '-r', '--cached', '--ignore-unmatch', '--', *excluded_paths], timeout=60, env=env,
            )
            if remove_generated.returncode:
                raise RuntimeError(remove_generated.stderr.strip() or 'failed to exclude generated files from snapshot')
        add = run_git(repo, ['add', '-A', '--', *pathspecs], timeout=120, env=env)
        if add.returncode:
            raise RuntimeError(add.stderr.strip() or 'failed to stage worktree snapshot')
        tree = run_git(repo, ['write-tree'], timeout=30, env=env)
        if tree.returncode:
            raise RuntimeError(tree.stderr.strip() or 'failed to write snapshot tree')
        tree_sha = tree.stdout.strip()
        commit_env = env | {
            'GIT_AUTHOR_NAME': 'Mission Control',
            'GIT_AUTHOR_EMAIL': 'mission-control@localhost',
            'GIT_COMMITTER_NAME': 'Mission Control',
            'GIT_COMMITTER_EMAIL': 'mission-control@localhost',
        }
        commit = run_git(repo, ['commit-tree', tree_sha, '-p', head_sha, '-m', 'Mission Control local review snapshot'], timeout=30, env=commit_env)
        if commit.returncode:
            raise RuntimeError(commit.stderr.strip() or 'failed to create snapshot commit')
        snapshot_sha = commit.stdout.strip()
        base_branch = resolve_base_branch(repo, base)
        base_sha_result = run_git(repo, ['rev-parse', '--verify', f'{base_branch}^{{commit}}'], timeout=10)
        if base_sha_result.returncode:
            raise RuntimeError(base_sha_result.stderr.strip() or f'failed to resolve base {base_branch}')
        base_sha = base_sha_result.stdout.strip()
        merge_base = run_git(repo, ['merge-base', base_sha, head_sha], timeout=20)
        if merge_base.returncode:
            raise RuntimeError(merge_base.stderr.strip() or 'failed to find merge base')
        from_sha = merge_base.stdout.strip()
        names = run_git(repo, ['diff', '--name-only', '-z', from_sha, snapshot_sha], timeout=60)
        patch = run_git(
            repo,
            ['-c', 'core.quotepath=false', 'diff', '--binary', '--find-renames', from_sha, snapshot_sha],
            timeout=120,
        )
        if names.returncode or patch.returncode:
            raise RuntimeError(names.stderr.strip() or patch.stderr.strip() or 'failed to diff snapshot')
        changed = sorted({p for p in names.stdout.split('\0') if p})
        return head_sha, snapshot_sha, base_branch, changed, patch.stdout
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass


def get_commit_messages(repo_path: str, base: str) -> list[str]:
    result = run_git(repo_path, ['log', '--oneline', f'{base}..HEAD'], timeout=10)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def build_local_review_source(
    repo_path: str,
    *,
    base: str | None = None,
    review_focus: str = '',
    context: dict | None = None,
    generated_paths: list[str] | None = None,
) -> LocalReviewSource:
    inside_repo = run_git(repo_path, ['rev-parse', '--is-inside-work-tree'], timeout=10)
    if inside_repo.returncode != 0 or inside_repo.stdout.strip() != 'true':
        raise ValueError(f'repo_path is not a git repository: {repo_path}')

    base_branch = resolve_base_branch(repo_path, base)
    head_sha, snapshot_sha, base_branch, changed_files, diff_text = capture_local_snapshot(
        repo_path, base_branch, generated_paths=generated_paths,
    )
    commits = get_commit_messages(repo_path, base_branch)

    intent = ''
    if context and context.get('intent'):
        intent = str(context['intent'])
    elif commits:
        intent = commits[0]

    title = f'[Local Review] {intent[:80]}' if intent else '[Local Review]'
    description_parts = [f'Local diff review against {base_branch}']
    if intent:
        description_parts.append(f'Intent: {intent}')
    if context and context.get('requirements'):
        description_parts.append('Requirements:')
        description_parts.extend(f'  - {requirement}' for requirement in context['requirements'])
    if commits:
        description_parts.append(f'Recent commits ({len(commits)}):')
        description_parts.extend(f'  - {message}' for message in commits[:5])
    if review_focus:
        description_parts.append(f'Review focus: {review_focus}')

    return LocalReviewSource(
        base_branch=base_branch,
        changed_files=changed_files,
        diff_text=diff_text,
        commit_messages=commits,
        title=title,
        description='\n'.join(description_parts),
        head_sha=head_sha,
        snapshot_sha=snapshot_sha,
    )
