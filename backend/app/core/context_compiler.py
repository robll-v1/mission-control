from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import load_repo_config
from app.core.models import Task
from app.services.artifact_store import ArtifactStore


STOPWORDS = {
    'the', 'and', 'for', 'with', 'that', 'from', 'this', 'into', 'when', 'then', 'have', 'will', 'your',
    'issue', 'task', 'bug', 'fix', 'repo', 'code', 'work', 'about', 'after', 'before', 'should', 'would',
    'could', 'please', 'need', 'make', 'does', 'just', 'more', 'less', 'than', 'been', 'also', 'http',
    'https', 'github', 'comment', 'comments', 'title', 'description', 'body', 'state', 'author', 'labels',
    'feature', 'request', 'existing', 'checked', 'related', 'problem', 'describe', 'implementation',
    'considered', 'documentation', 'adoption', 'migration', 'strategy', 'additional', 'information',
    'markdown', 'response', 'public', 'private', 'support', 'needs', 'like', 'would', 'response',
}
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'dist', 'build', 'runtime', 'worktrees', '__pycache__', 'vendor'}
TEXT_FILE_SUFFIXES = {
    '.go', '.py', '.ts', '.tsx', '.js', '.jsx', '.java', '.c', '.cc', '.cpp', '.h', '.hpp', '.rs',
    '.md', '.txt', '.yaml', '.yml', '.toml', '.sql', '.sh', '.proto', '.json', '.xml', '.ini', '.cfg',
}
SPECIAL_KEYWORD_ALIASES = {
    'usr1': ['sigusr1', 'signal', 'signal.notify'],
    'sigusr1': ['usr1', 'signal', 'signal.notify'],
    'sigterm': ['signal', 'signal.notify'],
    'signal': ['signal.notify', 'sigusr1', 'sigterm'],
    'rotate': ['rotation', 'rotator', 'lumberjack', 'reopen'],
    'rotation': ['rotate', 'rotator', 'lumberjack'],
    'rotator': ['rotate', 'rotation', 'lumberjack'],
    'reopen': ['rotate', 'rotator'],
    'log': ['logger', 'logutil', 'logging', 'lumberjack'],
    'logger': ['log', 'logutil', 'logging'],
    'logging': ['log', 'logger', 'logutil'],
    'logutil': ['log', 'logger', 'lumberjack'],
    'service': ['mo-service', 'main.go'],
    'main': ['main.go'],
}
PHRASE_HINTS = {
    '日志': ['log', 'logger', 'logutil', 'lumberjack'],
    '日志文件': ['log', 'logger', 'logutil', 'lumberjack', 'filename'],
    '信号': ['signal', 'signal.notify'],
    '轮转': ['rotate', 'rotation', 'rotator'],
    '旋转': ['rotate', 'rotation', 'rotator'],
    '服务': ['service', 'mo-service', 'main.go'],
    '单机': ['standalone', 'launch'],
    'usr1': ['usr1', 'sigusr1', 'signal'],
    'sigusr1': ['usr1', 'sigusr1', 'signal'],
}
GITHUB_ISSUE_TEMPLATE_PATTERNS = (
    'is there an existing issue',
    'is your feature request related to a problem',
    'describe the feature you\'d like',
    'describe implementation you\'ve considered',
    'documentation, adoption, use case, migration strategy',
    'additional information',
)


@dataclass
class CompiledContext:
    markdown_path: str
    json_path: str
    markdown: str
    payload: dict[str, Any]


class ContextCompiler:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def compile_task(self, task: Task, *, worktree_path: str | None = None) -> CompiledContext:
        repo_root = Path(task.repo_path)
        target_root = Path(worktree_path or task.repo_path)
        repo_cfg = load_repo_config(task.repo_path)
        context_cfg = repo_cfg.get('context', {}) if isinstance(repo_cfg, dict) else {}
        recent_commit_limit = int(context_cfg.get('include_recent_commits', 8) or 8)
        candidate_limit = int(context_cfg.get('candidate_files_limit', 12) or 12)
        keywords = self._keywords(task.title, task.description)

        payload: dict[str, Any] = {
            'task': {
                'id': task.id,
                'title': task.title,
                'description': task.description,
                'source_type': task.source_type,
                'source_url': task.source_url,
                'backend': task.backend,
            },
            'repo': {
                'path': task.repo_path,
                'worktree_path': worktree_path,
                'top_level_entries': self._top_level_entries(repo_root),
                'git_branch': self._git_one_line(task.repo_path, ['rev-parse', '--abbrev-ref', 'HEAD']),
                'remote_url': self._git_one_line(task.repo_path, ['config', '--get', 'remote.origin.url']),
                'recent_commits': self._recent_commits(task.repo_path, recent_commit_limit),
            },
            'validation': self._validation_checks(repo_cfg),
            'keywords': keywords,
            'candidate_files': self._candidate_files(target_root, keywords, candidate_limit),
            'artifacts': self._existing_context_artifacts(task.id),
        }

        markdown = self._render_markdown(payload)
        json_path = self.artifacts.write_text(task.id, 'context/context.json', json.dumps(payload, indent=2, ensure_ascii=False))
        markdown_path = self.artifacts.write_text(task.id, 'context/context.md', markdown)
        return CompiledContext(markdown_path=markdown_path, json_path=json_path, markdown=markdown, payload=payload)

    @staticmethod
    def build_prompt(task: Task, compiled: CompiledContext) -> str:
        return (
            'You are executing a repository task. Use the compiled context below as the primary source of truth. '
            'Inspect the repository, make careful changes, and explain important steps as you go.\n\n'
            f'{compiled.markdown}\n'
        )

    def _existing_context_artifacts(self, task_id: str) -> list[dict[str, str]]:
        files = []
        for item in self.artifacts.list_files(task_id):
            if item['relative_path'].startswith('context/') and item['relative_path'] not in {'context/context.md', 'context/context.json'}:
                files.append(item)
        return files

    @staticmethod
    def _top_level_entries(repo_root: Path) -> list[str]:
        if not repo_root.exists():
            return []
        entries = []
        for item in sorted(repo_root.iterdir(), key=lambda path: path.name.lower()):
            if item.name in SKIP_DIRS:
                continue
            suffix = '/' if item.is_dir() else ''
            entries.append(f'{item.name}{suffix}')
            if len(entries) >= 20:
                break
        return entries

    @staticmethod
    def _recent_commits(repo_path: str, limit: int) -> list[str]:
        if limit <= 0:
            return []
        result = subprocess.run(
            ['git', '-C', repo_path, 'log', f'-{limit}', '--oneline'],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _git_one_line(repo_path: str, args: list[str]) -> str:
        result = subprocess.run(
            ['git', '-C', repo_path, *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ''
        return result.stdout.strip()

    @staticmethod
    def _validation_checks(repo_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        validation_cfg = repo_cfg.get('validation', {}) if isinstance(repo_cfg, dict) else {}
        checks = validation_cfg.get('checks', {}) if isinstance(validation_cfg, dict) else {}
        items = []
        for name, config in checks.items():
            if not isinstance(config, dict):
                continue
            items.append({
                'name': name,
                'command': str(config.get('command', '')).strip(),
                'required': bool(config.get('required', False)),
                'modes': list(config.get('modes', ['standard'])),
            })
        return items

    @staticmethod
    def _iter_repo_files(root: Path) -> list[Path]:
        try:
            result = subprocess.run(
                ['git', '-C', str(root), 'ls-files'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                files = []
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    rel = Path(line.strip())
                    if any(part in SKIP_DIRS for part in rel.parts):
                        continue
                    full = root / rel
                    if full.is_file():
                        files.append(full)
                return files
        except Exception:
            pass

        files: list[Path] = []
        seen = 0
        for current_root, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            for name in names:
                seen += 1
                if seen > 12000:
                    break
                full = Path(current_root) / name
                if full.is_file():
                    files.append(full)
            if seen > 12000:
                break
        return files

    def _candidate_files(self, root: Path, keywords: list[str], limit: int) -> list[dict[str, Any]]:
        if not root.exists():
            return []
        scored: list[tuple[int, str, dict[str, int]]] = []
        for full_path in self._iter_repo_files(root):
            rel_path = os.path.relpath(full_path, root)
            path_score = self._score_path(rel_path, keywords)
            structure_score = self._score_structure(rel_path, keywords)
            content_score = self._score_content(full_path, keywords)
            total = path_score + structure_score + content_score
            if total > 0:
                scored.append((total, rel_path, {
                    'path': path_score,
                    'structure': structure_score,
                    'content': content_score,
                }))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            fallback = []
            for candidate in ('README.md', 'package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod'):
                if (root / candidate).exists():
                    fallback.append({'path': candidate, 'score': 1})
            return fallback[:limit]
        return [
            {
                'path': path,
                'score': score,
                'path_score': breakdown['path'],
                'structure_score': breakdown['structure'],
                'content_score': breakdown['content'],
            }
            for score, path, breakdown in scored[:limit]
        ]

    def _keywords(self, title: str, description: str) -> list[str]:
        raw_text = f'{title}\n{description}'
        cleaned = self._clean_issue_template(raw_text)
        raw_tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_.-]{1,}', cleaned.lower())
        tokens: list[str] = []
        for token in raw_tokens:
            normalized = token.strip('._-')
            if len(normalized) < 2:
                continue
            if normalized in STOPWORDS:
                continue
            if normalized not in tokens:
                tokens.append(normalized)

        for phrase, hints in PHRASE_HINTS.items():
            if phrase in raw_text.lower() or phrase in raw_text:
                for hint in hints:
                    if hint not in tokens:
                        tokens.append(hint)

        expanded: list[str] = []
        for token in tokens:
            if token not in expanded:
                expanded.append(token)
            for alias in SPECIAL_KEYWORD_ALIASES.get(token, []):
                if alias not in expanded:
                    expanded.append(alias)

        return expanded[:30]

    @staticmethod
    def _clean_issue_template(text: str) -> str:
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if lowered.startswith('### '):
                if any(pattern in lowered for pattern in GITHUB_ISSUE_TEMPLATE_PATTERNS):
                    continue
            if stripped in {'```', '```Markdown', '_No response_'}:
                continue
            lines.append(line)
        return '\n'.join(lines)

    @staticmethod
    def _score_path(path: str, keywords: list[str]) -> int:
        lowered = path.lower()
        basename = lowered.rsplit('/', 1)[-1]
        score = 0
        for keyword in keywords:
            if keyword in lowered:
                score += 4
            if basename == keyword:
                score += 6
            if basename.startswith(keyword):
                score += 2
        return score

    @staticmethod
    def _score_structure(path: str, keywords: list[str]) -> int:
        lowered = path.lower()
        basename = lowered.rsplit('/', 1)[-1]
        score = 0
        if lowered.startswith('cmd/') and any(key in keywords for key in ('service', 'mo-service', 'signal', 'sigusr1', 'usr1')):
            score += 6
        if lowered.startswith('pkg/logutil/') and any(key in keywords for key in ('log', 'logger', 'logutil', 'rotate', 'rotator', 'lumberjack')):
            score += 8
        if basename == 'main.go' and any(key in keywords for key in ('service', 'mo-service', 'signal', 'usr1', 'sigusr1')):
            score += 8
        if basename == 'internal.go' and any(key in keywords for key in ('log', 'logutil', 'rotate', 'rotator', 'lumberjack')):
            score += 7
        if basename.endswith('_test.go') and any(key in keywords for key in ('log', 'logutil', 'rotate', 'signal', 'usr1', 'sigusr1')):
            score += 4
        return score

    @staticmethod
    def _score_content(path: Path, keywords: list[str]) -> int:
        if path.suffix.lower() not in TEXT_FILE_SUFFIXES and path.name not in {'Makefile', 'Dockerfile'}:
            return 0
        try:
            if path.stat().st_size > 256 * 1024:
                return 0
            content = path.read_text(errors='ignore')[:65536].lower()
        except Exception:
            return 0
        score = 0
        for keyword in keywords[:18]:
            if keyword in content:
                score += min(content.count(keyword), 3) * 2
        if any(key in keywords for key in ('signal', 'usr1', 'sigusr1')) and 'signal.notify' in content:
            score += 10
        if any(key in keywords for key in ('log', 'rotate', 'rotator', 'lumberjack')) and 'lumberjack' in content:
            score += 12
        if any(key in keywords for key in ('rotate', 'rotator')) and '.rotate(' in content:
            score += 8
        return score

    @staticmethod
    def _render_markdown(payload: dict[str, Any]) -> str:
        task = payload['task']
        repo = payload['repo']
        validations = payload['validation']
        candidates = payload['candidate_files']
        artifacts = payload.get('artifacts', [])
        keywords = payload.get('keywords', [])
        lines = [
            '# Compiled Task Context',
            '',
            '## Task',
            f'- Title: {task["title"]}',
            f'- Source Type: {task["source_type"]}',
            f'- Source URL: {task.get("source_url") or "(none)"}',
            f'- Backend: {task["backend"]}',
            '',
            '## Goal',
            task['description'] or '(empty)',
            '',
            '## Repo Snapshot',
            f'- Repo Path: {repo.get("path") or ""}',
            f'- Worktree Path: {repo.get("worktree_path") or "(not created yet)"}',
            f'- Current Branch: {repo.get("git_branch") or "(unknown)"}',
            f'- Remote URL: {repo.get("remote_url") or "(unknown)"}',
            '',
            '### Top-Level Entries',
        ]
        entries = repo.get('top_level_entries', []) or []
        if entries:
            lines.extend(f'- {entry}' for entry in entries)
        else:
            lines.append('- (none)')
        lines.extend(['', '### Search Keywords'])
        if keywords:
            lines.append('- ' + ', '.join(keywords))
        else:
            lines.append('- (none)')
        lines.extend(['', '### Suggested Candidate Files'])
        if candidates:
            for item in candidates:
                breakdown = f"score={item['score']} path={item.get('path_score', 0)} structure={item.get('structure_score', 0)} content={item.get('content_score', 0)}"
                lines.append(f"- {item['path']} ({breakdown})")
        else:
            lines.append('- (none)')
        lines.extend(['', '### Recent Commits'])
        commits = repo.get('recent_commits', []) or []
        if commits:
            lines.extend(f'- {commit}' for commit in commits)
        else:
            lines.append('- (none)')
        lines.extend(['', '## Validation Commands'])
        if validations:
            for item in validations:
                lines.append(f'- {item["name"]}: `{item["command"] or "(empty)"}` required={item["required"]}')
        else:
            lines.append('- (none configured)')
        if artifacts:
            lines.extend(['', '## Existing Context Artifacts'])
            lines.extend(f'- {item["relative_path"]}' for item in artifacts)
        lines.extend([
            '',
            '## Guidance',
            '- Start with the smallest relevant repository area.',
            '- Prefer candidate files that score on both structure and content, not only filename overlap.',
            '- Use the smallest relevant validation commands before broader checks.',
        ])
        return '\n'.join(lines).strip() + '\n'
