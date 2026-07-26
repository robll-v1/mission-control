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
        """Persist an artifact as UTF-8.

        Artifacts routinely carry non-ASCII text — PR bodies, commit subjects,
        diffs of source files. Without an explicit encoding this uses the locale
        codepage, which on a non-English Windows install raises
        ``UnicodeEncodeError`` and aborts the whole review.
        """
        path = self.task_dir(task_id) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return str(path)

    def read_text(self, task_id: str, relative_path: str) -> str:
        """Read back an artifact written by :meth:`write_text`."""
        return (self.task_dir(task_id) / relative_path).read_text(encoding='utf-8')

    def list_files(self, task_id: str) -> list[dict[str, str]]:
        root = self.task_dir(task_id)
        items: list[dict[str, str]] = []
        for path in sorted(root.rglob('*')):
            if path.is_file():
                items.append({'path': str(path), 'relative_path': str(path.relative_to(root))})
        return items
