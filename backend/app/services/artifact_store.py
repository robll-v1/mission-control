from __future__ import annotations

from pathlib import Path


class ArtifactStore:
    def __init__(self, runtime_root: str):
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        root = self.runtime_root / 'tasks' / task_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    def write_text(self, task_id: str, relative_path: str, content: str) -> str:
        path = self.task_dir(task_id) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    def list_files(self, task_id: str) -> list[dict[str, str]]:
        root = self.task_dir(task_id)
        items: list[dict[str, str]] = []
        for path in sorted(root.rglob('*')):
            if path.is_file():
                items.append({'path': str(path), 'relative_path': str(path.relative_to(root))})
        return items
