from __future__ import annotations

from app.core.db import Database
from app.services.artifact_store import ArtifactStore


class SummaryService:
    def __init__(self, db: Database, artifacts: ArtifactStore):
        self.db = db
        self.artifacts = artifacts

    def export_summary(self, task_id: str) -> str:
        task = self.db.get_task(task_id)
        events = self.db.list_events(task_id)
        rounds = self.db.list_runs(task_id)
        checks = self.db.list_check_runs(task_id)
        if task is None:
            raise KeyError(f'task not found: {task_id}')
        lines = [
            f'# PR 评审总结：{task.title}',
            '',
            f'- 状态：{task.status}',
            f'- 阶段：{task.current_stage}',
            f'- 后端：{task.backend}',
            f'- 仓库：{task.repo_path}',
            f'- PR 链接：{task.source_url or "(none)"}',
            '',
        ]
        latest_result = task.latest_review_result
        if latest_result is not None:
            lines.extend([
                '## 最新评审结论',
                f'- 结论：{latest_result.verdict}',
                f'- 问题数：{latest_result.finding_count}',
                f'- 摘要：{latest_result.summary or "(empty)"}',
                '',
            ])
            if latest_result.severity_counts:
                lines.append('### 严重级别统计')
                for severity, count in latest_result.severity_counts.items():
                    lines.append(f'- {severity}: {count}')
                lines.append('')
            if latest_result.findings:
                lines.append('### 问题列表')
                for finding in latest_result.findings:
                    location = finding.path or '(路径缺失)'
                    if finding.line is not None:
                        location = f'{location}:{finding.line}'
                    lines.append(f'- [{finding.severity}] {location} {finding.summary}')
                lines.append('')
        lines.extend([
            '## 评审背景',
            task.description or '(empty)',
            '',
            '## 评审轮次',
        ])
        if rounds:
            lines.extend(
                (
                    f'- 第 {run.round_index} 轮：status={run.status} exit={run.exit_code} '
                    f'revision={run.review_revision or "-"} '
                    f'session={run.backend_session_id or "-"} verdict='
                    f'{run.review_result.verdict if run.review_result is not None else "inconclusive"} '
                    f'findings={run.review_result.finding_count if run.review_result is not None else 0}'
                )
                for run in rounds
            )
        else:
            lines.append('- none')
        lines.extend(['', '## 辅助检查'])
        if checks:
            lines.extend(f'- {check.name}: status={check.status} exit={check.exit_code}' for check in checks)
        else:
            lines.append('- none')
        lines.extend(['', '## 最近事件'])
        if events:
            for event in events[-20:]:
                lines.append(f'- #{event.seq} {event.kind}: {event.payload}')
        else:
            lines.append('- none')
        return self.artifacts.write_text(task_id, 'exports/summary.md', '\n'.join(lines) + '\n')
