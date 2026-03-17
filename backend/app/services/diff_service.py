from __future__ import annotations

from app.core.worktree import WorktreeManager
from app.services.artifact_store import ArtifactStore


class DiffService:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def export_diff(self, *, task_id: str, worktree_path: str) -> str:
        diff = WorktreeManager.get_diff(worktree_path)
        return self.artifacts.write_text(task_id, 'artifacts/diff.patch', diff)

    def export_changed_files(self, *, task_id: str, worktree_path: str) -> str:
        changed = '\n'.join(WorktreeManager.get_changed_files(worktree_path))
        return self.artifacts.write_text(task_id, 'artifacts/changed_files.txt', changed)
