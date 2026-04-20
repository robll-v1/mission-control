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
    'pull', 'review', 'reviewer', 'conversation',
}
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'dist', 'build', 'runtime', 'worktrees', '__pycache__', 'vendor'}
TEXT_FILE_SUFFIXES = {
    '.go', '.py', '.ts', '.tsx', '.js', '.jsx', '.java', '.c', '.cc', '.cpp', '.h', '.hpp', '.rs',
    '.md', '.txt', '.yaml', '.yml', '.toml', '.sql', '.sh', '.proto', '.json', '.xml', '.ini', '.cfg',
}
DOC_DIRS = {'docs', '.github'}
DOC_BASENAMES = {'readme.md', 'build.md', 'agents.md', 'code_of_conduct.md', 'contributing.md'}
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
        keywords = self._keywords(task.title, task.description, task.review_paths)
        pr_context = self._pull_request_context(task.id)

        payload: dict[str, Any] = {
            'task': {
                'id': task.id,
                'title': task.title,
                'description': task.description,
                'source_type': task.source_type,
                'source_url': task.source_url,
                'backend': task.backend,
                'review_focus': task.review_focus,
            },
            'review': {
                'owner': task.pr_owner,
                'repo': task.pr_repo,
                'pr_number': task.pr_number,
                'base_ref': task.pr_base_ref,
                'head_ref': task.pr_head_ref,
                'head_sha': task.pr_head_sha,
                'changed_files': task.review_paths,
                'file_patches': pr_context.get('file_patches', []),
                'review_count': pr_context.get('review_count', 0),
                'comment_count': pr_context.get('comment_count', 0),
            },
            'repo': {
                'path': task.repo_path,
                'worktree_path': worktree_path,
                'top_level_entries': self._top_level_entries(repo_root),
                'git_branch': self._git_one_line(task.repo_path, ['rev-parse', '--abbrev-ref', 'HEAD']),
                'remote_url': self._git_one_line(task.repo_path, ['config', '--get', 'remote.origin.url']),
                'recent_commits': self._recent_commits(task.repo_path, recent_commit_limit),
            },
            'keywords': keywords,
            'candidate_files': self._candidate_files(target_root, keywords, task.review_paths, candidate_limit),
            'artifacts': self._existing_context_artifacts(task.id),
        }

        markdown = self._render_markdown(payload)
        json_path = self.artifacts.write_text(task.id, 'context/context.json', json.dumps(payload, indent=2, ensure_ascii=False))
        markdown_path = self.artifacts.write_text(task.id, 'context/context.md', markdown)
        return CompiledContext(markdown_path=markdown_path, json_path=json_path, markdown=markdown, payload=payload)

    @staticmethod
    def build_prompt(task: Task, compiled: CompiledContext, review_note: str | None = None, language: str = 'zh') -> str:
        if language == 'en':
            return ContextCompiler._build_prompt_en(compiled, review_note)
        return ContextCompiler._build_prompt_zh(compiled, review_note)

    @staticmethod
    def _build_prompt_zh(compiled: CompiledContext, review_note: str | None = None) -> str:
        extra_note = f'\n\n本轮补充说明：\n{review_note.strip()}\n' if review_note and review_note.strip() else ''
        return (
            '你正在执行静态 PR Review。请把下面的编译上下文当作主要事实来源。'
            '只检查 PR diff 及其周边代码，不要修改文件。'
            '除非用户明确要求，否则不要启动服务、不要跑 build、不要跑单元测试、不要跑集成测试、不要做交叉编译。'
            '除非 PR 直接改到了这些文档，或者代码确实依赖它们，否则避免主动阅读 README、AGENTS、BUILD 文档、docs/、.github/ 这类仓库文档和配置。'
            '优先基于已提供的 patch 和附近代码做只读审查。'
            '如果某个判断必须依赖运行时证据，请把它写成风险或待确认问题，不要尝试启动服务去验证。'
            '最终输出必须使用中文。第一段给出中文结论摘要。'
            '如果发现问题，请按固定格式列出：`- 严重:`、`- 高:`、`- 中:`、`- 低:`。'
            '如果没有发现实质性问题，请明确写出：`未发现明显正确性或回归问题。`'
            '每条问题尽量带文件路径和行号，并优先关注 bug、回归风险、危险假设和缺失测试。'
            f'{extra_note}\n\n{compiled.markdown}\n'
        )

    @staticmethod
    def _build_prompt_en(compiled: CompiledContext, review_note: str | None = None) -> str:
        extra_note = f'\n\nAdditional note for this round:\n{review_note.strip()}\n' if review_note and review_note.strip() else ''
        return (
            'You are performing a static code review. Use the compiled context below as your primary source of truth. '
            'Only inspect the diff and surrounding code — do NOT modify any files. '
            'Do NOT start services, run builds, run unit tests, integration tests, or cross-compile unless explicitly asked. '
            'Do NOT read README, AGENTS, BUILD docs, docs/, or .github/ unless the diff directly touches them. '
            'Prefer read-only analysis based on the provided patches and nearby code. '
            'If a judgment requires runtime evidence, flag it as a risk or unconfirmed issue — do NOT attempt to verify by running services. '
            'Output your response in English. Start with a conclusion summary paragraph. '
            'If issues are found, list each using this exact format: `- critical:`, `- high:`, `- medium:`, `- low:`. '
            'If no material issues are found, explicitly write: `No material correctness or regression issues found.` '
            'For each finding, include file path and line number where possible. '
            'Prioritize: bugs, regression risks, dangerous assumptions, and missing tests.'
            f'{extra_note}\n\n{compiled.markdown}\n'
        )

    def _existing_context_artifacts(self, task_id: str) -> list[dict[str, str]]:
        files = []
        for item in self.artifacts.list_files(task_id):
            if item['relative_path'].startswith('context/') and item['relative_path'] not in {'context/context.md', 'context/context.json'}:
                files.append(item)
        return files

    def _pull_request_context(self, task_id: str) -> dict[str, Any]:
        path = self.artifacts.task_dir(task_id) / 'context' / 'pull_request.json'
        if not path.exists():
            return {'file_patches': [], 'review_count': 0, 'comment_count': 0}
        try:
            payload = json.loads(path.read_text())
        except Exception:
            return {'file_patches': [], 'review_count': 0, 'comment_count': 0}

        files = payload.get('files', []) if isinstance(payload, dict) else []
        patches: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            patches.append({
                'path': str(item.get('filename', '')).strip(),
                'status': str(item.get('status', '')).strip(),
                'additions': int(item.get('additions', 0) or 0),
                'deletions': int(item.get('deletions', 0) or 0),
                'patch': self._truncate_patch(str(item.get('patch', '') or '').strip()),
            })
        return {
            'file_patches': [item for item in patches if item.get('path')],
            'review_count': len(payload.get('reviews', [])) if isinstance(payload.get('reviews', []), list) else 0,
            'comment_count': (
                len(payload.get('issue_comments', [])) if isinstance(payload.get('issue_comments', []), list) else 0
            ) + (
                len(payload.get('review_comments', [])) if isinstance(payload.get('review_comments', []), list) else 0
            ),
        }

    @staticmethod
    def _truncate_patch(patch: str, limit: int = 1800) -> str:
        if len(patch) <= limit:
            return patch
        return patch[:limit].rstrip() + '\n... [truncated]'

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

    def _candidate_files(self, root: Path, keywords: list[str], review_paths: list[str], limit: int) -> list[dict[str, Any]]:
        if not root.exists():
            return []
        preferred_paths = {path.strip().lstrip('./') for path in review_paths if path.strip()}
        preferred: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for rel_path in review_paths:
            normalized = rel_path.strip().lstrip('./')
            if not normalized or normalized in seen_paths:
                continue
            full_path = root / normalized
            if full_path.is_file():
                preferred.append({
                    'path': normalized,
                    'score': 100,
                    'path_score': 100,
                    'structure_score': 0,
                    'content_score': 0,
                })
                seen_paths.add(normalized)
        scored: list[tuple[int, str, dict[str, int]]] = []
        for full_path in self._iter_repo_files(root):
            rel_path = os.path.relpath(full_path, root)
            if rel_path in seen_paths:
                continue
            if self._is_documentation_or_config(rel_path) and rel_path not in preferred_paths:
                continue
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
                if (root / candidate).exists() and (candidate in preferred_paths or not self._is_documentation_or_config(candidate)):
                    fallback.append({'path': candidate, 'score': 1})
            return (preferred + fallback)[:limit]
        combined = preferred + [
            {
                'path': path,
                'score': score,
                'path_score': breakdown['path'],
                'structure_score': breakdown['structure'],
                'content_score': breakdown['content'],
            }
            for score, path, breakdown in scored[:limit]
        ]
        return combined[:limit]

    def _keywords(self, title: str, description: str, review_paths: list[str]) -> list[str]:
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

        for rel_path in review_paths:
            for token in re.findall(r'[A-Za-z_][A-Za-z0-9_.-]{1,}', rel_path.lower()):
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
    def _is_documentation_or_config(path: str) -> bool:
        rel = Path(path)
        lowered = rel.as_posix().lower()
        if any(part.lower() in DOC_DIRS for part in rel.parts):
            return True
        basename = rel.name.lower()
        if basename in DOC_BASENAMES:
            return True
        if rel.suffix.lower() in {'.md', '.txt'}:
            return True
        return False

    @staticmethod
    def _render_markdown(payload: dict[str, Any]) -> str:
        task = payload['task']
        review = payload['review']
        repo = payload['repo']
        candidates = payload['candidate_files']
        artifacts = payload.get('artifacts', [])
        keywords = payload.get('keywords', [])
        lines = [
            '# Compiled PR Review Context',
            '',
            '## Review Target',
            f'- Title: {task["title"]}',
            f'- PR URL: {task.get("source_url") or "(none)"}',
            f'- Repository: {review.get("owner") or ""}/{review.get("repo") or ""}',
            f'- PR Number: #{review.get("pr_number") or "?"}',
            f'- Backend: {task["backend"]}',
            '',
            '## Review Mode',
            '- Mode: static logic review',
            '- Service startup: disabled by default',
            '- Build/test execution: disabled by default',
            '',
            '## Review Brief',
            task['description'] or '(empty)',
            '',
            '## Pull Request',
            f'- Base Ref: {review.get("base_ref") or "(unknown)"}',
            f'- Head Ref: {review.get("head_ref") or "(unknown)"}',
            f'- Head SHA: {review.get("head_sha") or "(unknown)"}',
            f'- Existing Reviews: {review.get("review_count", 0)}',
            f'- Existing Comments: {review.get("comment_count", 0)}',
            '',
            '### Changed Files',
        ]
        changed_files = review.get('changed_files', []) or []
        if changed_files:
            lines.extend(f'- {item}' for item in changed_files)
        else:
            lines.append('- (none)')
        file_patches = review.get('file_patches', []) or []
        if file_patches:
            lines.extend(['', '### Changed File Patches'])
            for item in file_patches:
                lines.append(
                    f"- {item['path']} [{item.get('status') or 'modified'}] (+{item.get('additions', 0)} -{item.get('deletions', 0)})"
                )
                patch = item.get('patch') or ''
                if patch:
                    lines.append('```diff')
                    lines.append(patch)
                    lines.append('```')
                else:
                    lines.append('- (patch unavailable)')
        lines.extend([
            '',
            '## Repo Snapshot',
            f'- Repo Path: {repo.get("path") or ""}',
            f'- Worktree Path: {repo.get("worktree_path") or "(not created yet)"}',
            f'- Current Branch: {repo.get("git_branch") or "(unknown)"}',
            f'- Remote URL: {repo.get("remote_url") or "(unknown)"}',
            '',
            '### Top-Level Entries',
        ])
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
        if artifacts:
            lines.extend(['', '## Existing Context Artifacts'])
            lines.extend(f'- {item["relative_path"]}' for item in artifacts)
        lines.extend([
            '',
            '## Review Guidance',
            '- Focus on correctness, regressions, risky assumptions, and missing tests.',
            '- Start with files changed by the PR, then expand only when the change requires more context.',
            '- Do not modify files; produce review findings only.',
            '- Do not start services, run builds, run unit tests, run integration tests, or cross-compile unless the user explicitly asks.',
            '- If runtime evidence would be needed, surface it as an open question instead of trying to prove it by execution.',
        ])
        return '\n'.join(lines).strip() + '\n'
