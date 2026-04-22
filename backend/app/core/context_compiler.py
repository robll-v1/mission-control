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
    markdown_bytes: int = 0
    json_bytes: int = 0


class ContextCompiler:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def compile_task(self, task: Task, *, worktree_path: str | None = None) -> CompiledContext:
        repo_root = Path(task.repo_path)
        target_root = Path(worktree_path or task.repo_path)
        repo_cfg = load_repo_config(task.repo_path)
        context_cfg = repo_cfg.get('context', {}) if isinstance(repo_cfg, dict) else {}
        keywords = self._keywords(task.title, task.description, task.review_paths)
        review_context = self._review_context(task)
        budget = self._context_budget(
            context_cfg=context_cfg,
            review_paths=task.review_paths,
            file_patches=review_context.get('file_patches', []),
        )
        display_keywords = keywords[:budget['keywords_limit']]
        snippets: list[dict[str, Any]] = []
        if context_cfg.get('hunk_snippets_enabled', True):
            snippets = self._collect_hunk_snippets(
                repo_root=target_root,
                file_patches=review_context.get('file_patches', []),
                file_limit=max(0, int(context_cfg.get('hunk_snippet_file_limit', 3) or 3)),
                hunks_per_file=max(0, int(context_cfg.get('hunk_snippet_hunks_per_file', 1) or 1)),
                context_lines=max(0, int(context_cfg.get('hunk_snippet_context_lines', 8) or 8)),
            )
        candidate_files = self._candidate_files(target_root, keywords, task.review_paths, budget['candidate_files_limit'])
        if snippets:
            snippet_paths = {item.get('path') for item in snippets if item.get('path')}
            candidate_files = [item for item in candidate_files if item.get('path') not in snippet_paths][:2]

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
                'file_patches': review_context.get('file_patches', []),
                'review_count': review_context.get('review_count', 0),
                'comment_count': review_context.get('comment_count', 0),
            },
            'repo': {
                'path': task.repo_path,
                'worktree_path': worktree_path,
                'top_level_entries': (
                    self._top_level_entries(repo_root)[:budget['top_level_entries_limit']]
                    if budget['include_top_level_entries']
                    else []
                ),
                'git_branch': self._git_one_line(task.repo_path, ['rev-parse', '--abbrev-ref', 'HEAD']),
                'remote_url': self._git_one_line(task.repo_path, ['config', '--get', 'remote.origin.url']),
                'recent_commits': self._recent_commits(task.repo_path, budget['recent_commit_limit']),
            },
            'keywords': display_keywords,
            'candidate_files': candidate_files,
            'related_snippets': snippets,
            'artifacts': self._existing_context_artifacts(task.id),
            'context_budget': budget,
        }

        payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
        markdown = self._render_markdown(payload)
        json_path = self.artifacts.write_text(task.id, 'context/context.json', payload_json)
        markdown_path = self.artifacts.write_text(task.id, 'context/context.md', markdown)
        return CompiledContext(
            markdown_path=markdown_path,
            json_path=json_path,
            markdown=markdown,
            payload=payload,
            markdown_bytes=len(markdown.encode('utf-8')),
            json_bytes=len(payload_json.encode('utf-8')),
        )

    @staticmethod
    def build_prompt(task: Task, compiled: CompiledContext, review_note: str | None = None, language: str = 'zh') -> str:
        if language == 'en':
            return ContextCompiler._build_prompt_en(compiled, review_note)
        return ContextCompiler._build_prompt_zh(compiled, review_note)

    @staticmethod
    def _build_prompt_zh(compiled: CompiledContext, review_note: str | None = None) -> str:
        extra_note = f'\n\n本轮补充说明：\n{review_note.strip()}\n' if review_note and review_note.strip() else ''
        return (
            '# 角色\n\n'
            '你是一个资深代码审查专家。你的目标是帮助开发者在合入前发现真正的问题。\n\n'
            '# 规则\n\n'
            '1. 只审查 PR diff 及其直接相关代码。不修改文件，不执行命令。\n'
            '2. 不要启动服务、不要跑 build/test、不要做交叉编译——除非用户明确要求。\n'
            '3. 不要主动阅读 README、docs/、.github/ 等——除非 diff 直接涉及它们。\n'
            '4. 如果某个判断需要运行时证据才能确认，标记为"待确认风险"，不要猜测。\n\n'
            '# 审查重点（按优先级）\n\n'
            '1. **正确性 bug** — 逻辑错误、边界情况、nil/空指针、并发问题\n'
            '2. **回归风险** — 破坏现有行为、不兼容的接口变更\n'
            '3. **安全隐患** — 注入、越权、信息泄露、硬编码密钥\n'
            '4. **危险假设** — 未验证的前提条件、缺失的错误处理\n'
            '5. **缺失测试** — 关键路径无测试覆盖\n\n'
            '# 严重程度定义\n\n'
            '- **critical**: 会导致数据丢失、安全漏洞、或生产宕机\n'
            '- **high**: 大概率导致运行时错误或严重的功能缺陷\n'
            '- **medium**: 潜在问题，在特定条件下可能触发\n'
            '- **low**: 改进建议，代码健壮性或可维护性\n\n'
            '# 输出格式\n\n'
            '全部使用中文。按以下格式输出：\n\n'
            '```\n'
            '## 审查结论\n\n'
            '<一段话总结本次审查的整体评价和主要风险>\n\n'
            '## 发现的问题\n\n'
            '- <severity>: `<file_path>:<line>` — <问题简述>\n'
            '  <详细描述为什么这是问题，以及建议如何修复>\n\n'
            '- <severity>: `<file_path>:<line>` — <问题简述>\n'
            '  <详细描述>\n'
            '```\n\n'
            '其中 `<severity>` 必须是以下之一：`critical`、`high`、`medium`、`low`。\n'
            '每条问题**必须**包含文件路径和行号。如果确实无法确定行号，写 `<file_path>:0`。\n'
            '问题描述要具体，包含"是什么问题"和"建议怎么修"。\n\n'
            '如果没有发现实质性问题，输出：\n'
            '```\n'
            '## 审查结论\n\n'
            '<整体评价>\n\n'
            '未发现明显正确性或回归问题。\n'
            '```\n'
            f'{extra_note}\n\n{compiled.markdown}\n'
        )

    @staticmethod
    def _build_prompt_en(compiled: CompiledContext, review_note: str | None = None) -> str:
        extra_note = f'\n\nAdditional note for this round:\n{review_note.strip()}\n' if review_note and review_note.strip() else ''
        return (
            '# Role\n\n'
            'You are a senior code reviewer. Your goal is to help developers catch real problems before merge.\n\n'
            '# Rules\n\n'
            '1. Only review the PR diff and directly related code. Do NOT modify files or run commands.\n'
            '2. Do NOT start services, run builds/tests, or cross-compile — unless explicitly asked.\n'
            '3. Do NOT read README, docs/, .github/ — unless the diff directly touches them.\n'
            '4. If a judgment requires runtime evidence, flag it as "unconfirmed risk" — do NOT guess.\n\n'
            '# Review Priorities (in order)\n\n'
            '1. **Correctness bugs** — logic errors, edge cases, nil/null pointers, concurrency issues\n'
            '2. **Regression risks** — breaking existing behavior, incompatible API changes\n'
            '3. **Security issues** — injection, privilege escalation, info leaks, hardcoded secrets\n'
            '4. **Dangerous assumptions** — unvalidated preconditions, missing error handling\n'
            '5. **Missing tests** — critical paths without test coverage\n\n'
            '# Severity Definitions\n\n'
            '- **critical**: Causes data loss, security breach, or production outage\n'
            '- **high**: Likely to cause runtime errors or major functional defects\n'
            '- **medium**: Potential issue that may trigger under specific conditions\n'
            '- **low**: Improvement suggestion for robustness or maintainability\n\n'
            '# Output Format\n\n'
            'Output in English. Use this exact format:\n\n'
            '```\n'
            '## Review Summary\n\n'
            '<One paragraph summarizing overall assessment and key risks>\n\n'
            '## Findings\n\n'
            '- <severity>: `<file_path>:<line>` — <brief description>\n'
            '  <Detailed explanation of why this is a problem and suggested fix>\n\n'
            '- <severity>: `<file_path>:<line>` — <brief description>\n'
            '  <Detailed explanation>\n'
            '```\n\n'
            'Where `<severity>` must be one of: `critical`, `high`, `medium`, `low`.\n'
            'Each finding MUST include file path and line number. If line is uncertain, use `<file>:0`.\n'
            'Descriptions should be specific: explain WHAT the problem is and HOW to fix it.\n\n'
            'If no material issues are found, output:\n'
            '```\n'
            '## Review Summary\n\n'
            '<Overall assessment>\n\n'
            'No material correctness or regression issues found.\n'
            '```\n'
            f'{extra_note}\n\n{compiled.markdown}\n'
        )

    def _existing_context_artifacts(self, task_id: str) -> list[dict[str, str]]:
        files = []
        for item in self.artifacts.list_files(task_id):
            if item['relative_path'].startswith('context/') and item['relative_path'] not in {'context/context.md', 'context/context.json'}:
                files.append(item)
        return files

    def _review_context(self, task: Task) -> dict[str, Any]:
        if task.source_type == 'local_diff':
            return self._local_diff_context(task)
        return self._pull_request_context(task.id)

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

    def _local_diff_context(self, task: Task) -> dict[str, Any]:
        path = self.artifacts.task_dir(task.id) / 'context' / 'local_diff.patch'
        if not path.exists():
            return {'file_patches': [], 'review_count': 0, 'comment_count': 0}
        try:
            diff_text = path.read_text()
        except Exception:
            return {'file_patches': [], 'review_count': 0, 'comment_count': 0}
        return {
            'file_patches': self._parse_unified_diff(diff_text, task.review_paths),
            'review_count': 0,
            'comment_count': 0,
        }

    @staticmethod
    def _context_budget(
        *,
        context_cfg: dict[str, Any],
        review_paths: list[str],
        file_patches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        adaptive_budget = bool(context_cfg.get('adaptive_budget', True))
        default_recent_commits = max(0, int(context_cfg.get('include_recent_commits', 8) or 8))
        default_candidate_limit = max(0, int(context_cfg.get('candidate_files_limit', 12) or 12))
        default_keywords_limit = max(0, int(context_cfg.get('keywords_limit', 12) or 12))
        default_top_level_limit = max(0, int(context_cfg.get('top_level_entries_limit', 12) or 12))
        include_top_level_cfg = context_cfg.get('include_top_level_entries')
        include_top_level = bool(include_top_level_cfg) if include_top_level_cfg is not None else False
        changed_file_count = len(review_paths or [])
        patch_count = len(file_patches or [])
        patch_chars = sum(len(item.get('patch') or '') for item in file_patches or [])

        budget = {
            'adaptive_budget': adaptive_budget,
            'changed_file_count': changed_file_count,
            'patch_count': patch_count,
            'patch_chars': patch_chars,
            'recent_commit_limit': default_recent_commits,
            'candidate_files_limit': default_candidate_limit,
            'keywords_limit': default_keywords_limit,
            'top_level_entries_limit': default_top_level_limit if include_top_level else 0,
            'include_top_level_entries': include_top_level,
        }
        if not adaptive_budget:
            return budget

        if patch_count == 0 and changed_file_count == 0:
            budget['include_top_level_entries'] = include_top_level or default_top_level_limit > 0
            budget['top_level_entries_limit'] = default_top_level_limit if budget['include_top_level_entries'] else 0
            return budget

        if patch_count == 0:
            budget['recent_commit_limit'] = min(default_recent_commits, 5)
            budget['candidate_files_limit'] = min(default_candidate_limit, 8)
            budget['keywords_limit'] = min(default_keywords_limit, 10)
            budget['include_top_level_entries'] = include_top_level or default_top_level_limit > 0
            budget['top_level_entries_limit'] = min(default_top_level_limit, 10) if budget['include_top_level_entries'] else 0
            return budget

        if changed_file_count <= 3 and patch_chars <= 8000:
            budget['recent_commit_limit'] = min(default_recent_commits, 3)
            budget['candidate_files_limit'] = min(default_candidate_limit, 4)
            budget['keywords_limit'] = min(default_keywords_limit, 8)
            budget['top_level_entries_limit'] = min(default_top_level_limit, 6) if include_top_level else 0
            return budget

        if changed_file_count <= 8 and patch_chars <= 24000:
            budget['recent_commit_limit'] = min(default_recent_commits, 4)
            budget['candidate_files_limit'] = min(default_candidate_limit, 6)
            budget['keywords_limit'] = min(default_keywords_limit, 10)
            budget['top_level_entries_limit'] = min(default_top_level_limit, 8) if include_top_level else 0
            return budget

        budget['recent_commit_limit'] = min(default_recent_commits, 6)
        budget['candidate_files_limit'] = min(default_candidate_limit, 8)
        budget['keywords_limit'] = min(default_keywords_limit, 12)
        budget['top_level_entries_limit'] = min(default_top_level_limit, 10) if include_top_level else 0
        return budget

    @staticmethod
    def _parse_unified_diff(diff_text: str, review_paths: list[str]) -> list[dict[str, Any]]:
        patches: list[dict[str, Any]] = []
        # Git diffs always emit POSIX-style paths even on Windows; normalize the
        # caller-supplied filter so backslash paths still match.
        allowed_paths = {p.replace('\\', '/') for p in (review_paths or [])}
        current_lines: list[str] = []
        current_path: str | None = None
        additions = 0
        deletions = 0

        def flush() -> None:
            nonlocal current_lines, current_path, additions, deletions
            if current_path and (not allowed_paths or current_path in allowed_paths):
                patches.append({
                    'path': current_path,
                    'status': 'modified',
                    'additions': additions,
                    'deletions': deletions,
                    'patch': ContextCompiler._truncate_patch('\n'.join(current_lines).strip()),
                })
            current_lines = []
            current_path = None
            additions = 0
            deletions = 0

        for line in diff_text.splitlines():
            if line.startswith('diff --git '):
                flush()
                continue
            if line.startswith('+++ b/'):
                current_path = line[6:].replace('\\', '/')
            elif line.startswith('+++ /dev/null'):
                current_path = None
            if current_path is not None:
                current_lines.append(line)
                if line.startswith('+') and not line.startswith('+++'):
                    additions += 1
                elif line.startswith('-') and not line.startswith('---'):
                    deletions += 1
        flush()
        return patches

    @staticmethod
    def _collect_hunk_snippets(
        *,
        repo_root: Path,
        file_patches: list[dict[str, Any]],
        file_limit: int,
        hunks_per_file: int,
        context_lines: int,
    ) -> list[dict[str, Any]]:
        if file_limit <= 0 or hunks_per_file <= 0:
            return []
        snippets: list[dict[str, Any]] = []
        files_with_snippets = 0
        for item in file_patches:
            if files_with_snippets >= file_limit:
                break
            rel_path = str(item.get('path') or '').strip()
            if not rel_path:
                continue
            file_path = repo_root / rel_path
            if not file_path.exists() or not file_path.is_file():
                continue
            if file_path.suffix.lower() not in TEXT_FILE_SUFFIXES and file_path.name not in {'Makefile', 'Dockerfile'}:
                continue
            try:
                if file_path.stat().st_size > 256 * 1024:
                    continue
                content_lines = file_path.read_text(errors='ignore').splitlines()
            except Exception:
                continue
            hunks = ContextCompiler._parse_patch_hunks(str(item.get('patch') or ''))
            if not hunks:
                continue
            file_snippet_count = 0
            for hunk in hunks:
                if file_snippet_count >= hunks_per_file:
                    break
                anchor_line = hunk['new_start']
                span = max(hunk['new_count'], 1)
                start_line = max(1, anchor_line - context_lines)
                end_line = min(len(content_lines), anchor_line + span + context_lines - 1)
                if end_line < start_line:
                    continue
                snippet_text = '\n'.join(
                    f'{line_no:>4}: {content_lines[line_no - 1]}'
                    for line_no in range(start_line, end_line + 1)
                )
                snippets.append({
                    'path': rel_path,
                    'anchor_line': anchor_line,
                    'start_line': start_line,
                    'end_line': end_line,
                    'reason': 'changed_hunk',
                    'text': snippet_text,
                })
                file_snippet_count += 1
            if file_snippet_count:
                files_with_snippets += 1
        return snippets

    @staticmethod
    def _parse_patch_hunks(patch: str) -> list[dict[str, int]]:
        hunks: list[dict[str, int]] = []
        for line in patch.splitlines():
            match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
            if not match:
                continue
            hunks.append({
                'new_start': int(match.group(1)),
                'new_count': int(match.group(2) or 1),
            })
        return hunks

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
        snippets = payload.get('related_snippets', [])
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
        ])
        entries = repo.get('top_level_entries', []) or []
        if entries:
            lines.extend(['', '### Top-Level Entries'])
            lines.extend(f'- {entry}' for entry in entries)
        if keywords:
            lines.extend(['', '### Search Keywords'])
            lines.append('- ' + ', '.join(keywords))
        if snippets:
            lines.extend(['', '### Hunk-Local Snippets'])
            for item in snippets:
                lines.append(
                    f"- {item['path']}:{item['start_line']}-{item['end_line']} (anchor={item['anchor_line']})"
                )
                lines.append('```text')
                lines.append(item['text'])
                lines.append('```')
        if candidates:
            lines.extend(['', '### Suggested Candidate Files'])
            for item in candidates:
                breakdown = f"score={item['score']} path={item.get('path_score', 0)} structure={item.get('structure_score', 0)} content={item.get('content_score', 0)}"
                lines.append(f"- {item['path']} ({breakdown})")
        commits = repo.get('recent_commits', []) or []
        if commits:
            lines.extend(['', '### Recent Commits'])
            lines.extend(f'- {commit}' for commit in commits)
        if artifacts:
            lines.extend(['', '## Existing Context Artifacts'])
            lines.extend(f'- {item["relative_path"]}' for item in artifacts)
        lines.extend([
            '',
            '## Review Guidance',
            '- Focus: correctness bugs, regression risks, dangerous assumptions, missing tests.',
            '- Start with the PR diff, only expand to related files when necessary.',
            '- Do NOT modify files or execute commands.',
            '- Do NOT start services, run builds/tests, or cross-compile.',
            '- If runtime evidence is needed, surface it as an unconfirmed risk.',
        ])
        return '\n'.join(lines).strip() + '\n'
