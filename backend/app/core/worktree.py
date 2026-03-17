from __future__ import annotations

import subprocess
from pathlib import Path

from app.core.models import Task


class WorktreeManager:
    def __init__(self, runtime_root: str):
        self.runtime_root = Path(runtime_root)
        self.worktrees_root = self.runtime_root / 'worktrees'
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    def ensure_worktree(self, task: Task) -> tuple[str, str]:
        branch_name = task.branch_name or f'amc/{task.id}'
        worktree_path = self._resolve_worktree_path(task)
        if worktree_path.exists():
            return branch_name, str(worktree_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ['git', '-C', task.repo_path, 'worktree', 'add', '-b', branch_name, str(worktree_path), 'HEAD'],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or 'failed to create git worktree')
        return branch_name, str(worktree_path)

    def _resolve_worktree_path(self, task: Task) -> Path:
        if task.worktree_path:
            worktree_path = Path(task.worktree_path)
            if worktree_path.is_absolute():
                return worktree_path
            return (Path(task.repo_path) / worktree_path).resolve()
        root = self.worktrees_root
        if not root.is_absolute():
            root = (Path(task.repo_path) / root).resolve()
        return root / task.id

    @staticmethod
    def get_diff(worktree_path: str) -> str:
        result = subprocess.run(
            ['git', '-C', worktree_path, 'diff'],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout

    @staticmethod
    def get_changed_files(worktree_path: str) -> list[str]:
        result = subprocess.run(
            ['git', '-C', worktree_path, 'diff', '--name-only'],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]
