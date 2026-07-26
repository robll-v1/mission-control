from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.core.models import Task
from app.core.proc import run_git


GITHUB_REMOTE_RE = re.compile(r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$')


class WorktreeManager:
    def __init__(self, runtime_root: str):
        self.runtime_root = Path(runtime_root)
        self.worktrees_root = self.runtime_root / 'worktrees'
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    def ensure_worktree(self, task: Task) -> tuple[str, str, str]:
        branch_name = task.branch_name or f'review/{task.id}'
        worktree_path = self._resolve_worktree_path(task)
        revision = self._resolve_revision(task)
        if worktree_path.exists():
            current_revision = self._rev_parse(str(worktree_path), 'HEAD')
            if current_revision != revision:
                cleanup = self.cleanup_worktree(task)
                if cleanup.get('exists_after'):
                    errors = '; '.join(cleanup.get('errors') or [])
                    raise RuntimeError(errors or 'failed to refresh stale git worktree')
            else:
                return branch_name, str(worktree_path), revision
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_worktree(task.repo_path, branch_name, worktree_path, revision)
        return branch_name, str(worktree_path), revision

    def cleanup_worktree(self, task: Task) -> dict[str, object]:
        branch_name = task.branch_name or f'review/{task.id}'
        managed_branch = branch_name if self._is_managed_branch(task, branch_name) else None
        return self._cleanup_path(
            repo_path=task.repo_path,
            worktree_path=self._resolve_worktree_path(task),
            branch_name=managed_branch,
            task_id=task.id,
        )

    def prune_orphaned_worktrees(self, repo_path: str, *, known_task_ids: set[str] | None = None) -> list[dict[str, object]]:
        root = self._worktrees_root_for_repo(repo_path)
        if not root.exists():
            return []
        known = known_task_ids or set()
        results: list[dict[str, object]] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in known:
                continue
            results.append(
                self._cleanup_path(
                    repo_path=repo_path,
                    worktree_path=child,
                    branch_name=f'review/{child.name}',
                    task_id=child.name,
                )
            )
        return [item for item in results if item['removed'] or item['branch_removed'] or item['errors']]

    def _resolve_worktree_path(self, task: Task) -> Path:
        if task.worktree_path:
            worktree_path = Path(task.worktree_path)
            if worktree_path.is_absolute():
                return worktree_path
            return (Path(task.repo_path) / worktree_path).resolve()
        return self._worktrees_root_for_repo(task.repo_path) / task.id

    def _worktrees_root_for_repo(self, repo_path: str) -> Path:
        root = self.worktrees_root
        if not root.is_absolute():
            root = (Path(repo_path) / root).resolve()
        return root

    def _cleanup_path(
        self,
        *,
        repo_path: str,
        worktree_path: Path,
        branch_name: str | None,
        task_id: str,
    ) -> dict[str, object]:
        errors: list[str] = []
        removed = False
        if worktree_path.exists():
            result = self._run_git(
                repo_path,
                ['worktree', 'remove', '--force', str(worktree_path)],
                timeout=120,
            )
            if result.returncode != 0 and worktree_path.exists():
                errors.append(result.stderr.strip() or 'failed to remove git worktree')
            removed = not worktree_path.exists()

        prune_result = self._run_git(repo_path, ['worktree', 'prune'], timeout=60)
        if prune_result.returncode != 0:
            stderr = prune_result.stderr.strip()
            if stderr:
                errors.append(stderr)

        branch_removed = False
        if branch_name:
            branch_removed = self._delete_branch(repo_path, branch_name, errors)

        return {
            'task_id': task_id,
            'worktree_path': str(worktree_path),
            'removed': removed,
            'exists_after': worktree_path.exists(),
            'branch_name': branch_name,
            'branch_removed': branch_removed,
            'errors': errors,
        }

    def _delete_branch(self, repo_path: str, branch_name: str, errors: list[str]) -> bool:
        listed = self._run_git(repo_path, ['branch', '--list', branch_name], timeout=30)
        if listed.returncode != 0:
            stderr = listed.stderr.strip()
            if stderr:
                errors.append(stderr)
            return False
        if not listed.stdout.strip():
            return False

        deleted = self._run_git(repo_path, ['branch', '-D', branch_name], timeout=30)
        if deleted.returncode != 0:
            stderr = deleted.stderr.strip()
            if stderr:
                errors.append(stderr)
            return False
        return True

    @staticmethod
    def _is_managed_branch(task: Task, branch_name: str) -> bool:
        return branch_name == f'review/{task.id}'

    @staticmethod
    def _run_git(repo_path: str, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return run_git(repo_path, args, timeout=timeout)

    def _resolve_revision(self, task: Task) -> str:
        if task.pr_head_sha:
            if self._commit_exists(task.repo_path, task.pr_head_sha):
                return task.pr_head_sha
            if self._fetch_pull_request_head(task) and self._commit_exists(task.repo_path, task.pr_head_sha):
                return task.pr_head_sha
            short_sha = task.pr_head_sha[:12]
            raise RuntimeError(
                f'latest PR commit is not available locally: {short_sha}. '
                'Fetch the PR into the target repository or run the review from the repo you used to push the change.'
            )
        return 'HEAD'

    @staticmethod
    def _commit_exists(repo_path: str, revision: str) -> bool:
        result = WorktreeManager._run_git(repo_path, ['cat-file', '-e', f'{revision}^{{commit}}'], timeout=20)
        return result.returncode == 0

    @staticmethod
    def _rev_parse(repo_path: str, revision: str) -> str:
        result = WorktreeManager._run_git(repo_path, ['rev-parse', revision], timeout=20)
        if result.returncode != 0:
            return ''
        return result.stdout.strip()

    def _create_worktree(self, repo_path: str, branch_name: str, worktree_path: Path, revision: str) -> None:
        if self._branch_exists(repo_path, branch_name):
            update = self._run_git(repo_path, ['branch', '-f', branch_name, revision], timeout=60)
            if update.returncode != 0:
                raise RuntimeError(update.stderr.strip() or 'failed to update review branch to latest revision')
            result = self._run_git(repo_path, ['worktree', 'add', str(worktree_path), branch_name], timeout=120)
        else:
            result = self._run_git(
                repo_path,
                ['worktree', 'add', '-b', branch_name, str(worktree_path), revision],
                timeout=120,
            )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or 'failed to create git worktree')

    @staticmethod
    def _branch_exists(repo_path: str, branch_name: str) -> bool:
        result = WorktreeManager._run_git(repo_path, ['branch', '--list', branch_name], timeout=20)
        return result.returncode == 0 and bool(result.stdout.strip())

    def _fetch_pull_request_head(self, task: Task) -> bool:
        if not task.pr_number:
            return False
        remote = self._matching_remote(task.repo_path, task.pr_owner, task.pr_repo)
        if not remote:
            return False
        local_ref = f'refs/amc/pr/{task.pr_number}'
        result = self._run_git(
            task.repo_path,
            ['fetch', '--no-tags', '--force', remote, f'refs/pull/{task.pr_number}/head:{local_ref}'],
            timeout=180,
        )
        return result.returncode == 0

    def _matching_remote(self, repo_path: str, owner: str | None, repo: str | None) -> str | None:
        remotes = self._remote_names(repo_path)
        if not remotes:
            return None
        if owner and repo:
            for remote in remotes:
                url = self._remote_url(repo_path, remote)
                if self._matches_github_repo(url, owner, repo):
                    return remote
        if 'origin' in remotes:
            return 'origin'
        return remotes[0]

    @staticmethod
    def _remote_names(repo_path: str) -> list[str]:
        result = WorktreeManager._run_git(repo_path, ['remote'], timeout=20)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _remote_url(repo_path: str, remote: str) -> str:
        result = WorktreeManager._run_git(repo_path, ['config', '--get', f'remote.{remote}.url'], timeout=20)
        if result.returncode != 0:
            return ''
        return result.stdout.strip()

    @staticmethod
    def _matches_github_repo(remote_url: str, owner: str, repo: str) -> bool:
        match = GITHUB_REMOTE_RE.search(remote_url.strip())
        if not match:
            return False
        remote_owner, remote_repo = match.groups()
        return remote_owner.lower() == owner.lower() and remote_repo.lower() == repo.lower()

    @staticmethod
    def get_diff(worktree_path: str) -> str:
        result = WorktreeManager._run_git(worktree_path, ['diff'], timeout=120)
        return result.stdout

    @staticmethod
    def get_changed_files(worktree_path: str) -> list[str]:
        result = WorktreeManager._run_git(worktree_path, ['diff', '--name-only'], timeout=120)
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]
