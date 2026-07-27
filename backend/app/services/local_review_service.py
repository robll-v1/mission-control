from __future__ import annotations

from dataclasses import dataclass

from app.core.proc import run_git


@dataclass(frozen=True)
class LocalReviewSource:
    base_branch: str
    changed_files: list[str]
    diff_text: str
    commit_messages: list[str]
    title: str
    description: str


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
) -> LocalReviewSource:
    inside_repo = run_git(repo_path, ['rev-parse', '--is-inside-work-tree'], timeout=10)
    if inside_repo.returncode != 0 or inside_repo.stdout.strip() != 'true':
        raise ValueError(f'repo_path is not a git repository: {repo_path}')

    base_branch = resolve_base_branch(repo_path, base)
    changed_files = get_local_changed_files(repo_path, base_branch)
    diff_text = get_local_diff(repo_path, base_branch)
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
    )
