from __future__ import annotations

import re
from collections import Counter

from app.core.db import Database
from app.core.models import Event, EventLevel, ReviewFinding, ReviewResult, ReviewVerdict, Run, Task, TaskStatus


SEVERITY_ORDER = ('critical', 'high', 'medium', 'low')
SEVERITY_ALIASES = {
    'critical': 'critical',
    '严重': 'critical',
    'high': 'high',
    '高': 'high',
    'medium': 'medium',
    '中': 'medium',
    'low': 'low',
    '低': 'low',
}
SEVERITY_PATTERN = re.compile(
    r'^\s*[-*]\s*\*{0,2}(critical|high|medium|low|严重|高|中|低)\*{0,2}\s*[:：\-]\s*(.+)$',
    re.IGNORECASE,
)
# Match `path/to/file.go:123` or `path/to/file.go:123-145`
PATH_LINE_PATTERN = re.compile(r'`([^`:\s]+):(\d+)(?:-\d+)?`')
# Match `path/to/file.go` — backtick-wrapped path without line number
PATH_PATTERN = re.compile(r'`([^`/\s]+/[^`:\s]+)`')
# Match bare path:line, with optional backticks and optional line range
BARE_PATH_LINE_PATTERN = re.compile(r'`?([^`\s:]+/[^`\s:]+):(\d+)(?:-\d+)?`?')
NOISE_TEXT_PREFIXES = ('[mnemo]',)
NOISY_EVENT_KINDS = {'agent.step_start', 'agent.step_finish'}

DEFAULT_LANGUAGE = 'en'
# Fallback text used when the model produced nothing we could parse. These used
# to be hardcoded Chinese, so `amc review` (which runs language='en') emitted
# Chinese on exactly the failure paths where a readable message matters most.
MESSAGES: dict[str, dict[str, str]] = {
    'en': {
        'no_result_failed': 'Review failed; no structured conclusion was produced.',
        'no_result': 'No structured review conclusion was produced.',
        'clear': 'No material correctness or regression issues found.',
        'failed': 'Review failed and produced no conclusion.',
        'no_summary': 'Review finished, but produced no structured summary.',
    },
    'zh': {
        'no_result_failed': 'Review 运行失败，未能产出结构化结论。',
        'no_result': '没有产出结构化评审结论。',
        'clear': '未发现明显正确性或回归问题。',
        'failed': 'Review 运行失败，未能给出结论。',
        'no_summary': 'Review 已结束，但没有结构化摘要。',
    },
}


def _message(key: str, language: str) -> str:
    table = MESSAGES.get((language or DEFAULT_LANGUAGE).lower(), MESSAGES[DEFAULT_LANGUAGE])
    return table.get(key, MESSAGES[DEFAULT_LANGUAGE][key])


class ReviewResultService:
    @staticmethod
    def backfill_task(*, db: Database, task: Task) -> Task:
        rounds = db.list_runs(task.id)
        events = db.list_events(task.id)
        updated = False
        for run in rounds:
            if run.review_result is None and run.status in {'completed', 'failed'}:
                run.review_result = ReviewResultService.extract_result(events=events, run=run)
                db.save_run(run)
                updated = True
        latest_round = rounds[-1] if rounds else None
        if latest_round is not None and task.status in {TaskStatus.RUNNING, TaskStatus.INGESTING, TaskStatus.VALIDATING}:
            if latest_round.status == 'completed':
                task.status = TaskStatus.WAITING_HUMAN
                task.current_stage = 'awaiting_next_round'
                updated = True
            elif latest_round.status == 'failed':
                task.status = TaskStatus.FAILED
                task.current_stage = 'review_failed'
                updated = True
        latest = ReviewResultService.latest_result(db.list_runs(task.id))
        if latest != task.latest_review_result:
            task.latest_review_result = latest
            updated = True
        if updated:
            db.save_task(task)
            return db.get_task(task.id) or task
        return task

    @staticmethod
    def extract_result(*, events: list[Event], run: Run, language: str = DEFAULT_LANGUAGE) -> ReviewResult:
        text_events = [
            event for event in events
            if event.run_id == run.id and event.kind == 'agent.text'
        ]
        text_event = ReviewResultService._pick_result_event(text_events)
        if text_event is None:
            verdict = ReviewVerdict.FAILED if run.status == 'failed' else ReviewVerdict.INCONCLUSIVE
            summary = _message(
                'no_result_failed' if verdict == ReviewVerdict.FAILED else 'no_result', language
            )
            return ReviewResult(
                verdict=verdict,
                summary=summary,
                findings=[],
                severity_counts={},
                finding_count=0,
                source_event_seq=None,
            )

        text = ReviewResultService._event_text(text_event)
        findings = ReviewResultService._parse_findings(text)
        counts = Counter(finding.severity for finding in findings)
        verdict = ReviewResultService._derive_verdict(run=run, findings=findings, text=text)
        summary = ReviewResultService._derive_summary(
            text=text, findings=findings, verdict=verdict, language=language
        )
        return ReviewResult(
            verdict=verdict,
            summary=summary,
            findings=findings,
            severity_counts={severity: counts.get(severity, 0) for severity in SEVERITY_ORDER if counts.get(severity, 0)},
            finding_count=len(findings),
            source_event_seq=text_event.seq,
        )

    @staticmethod
    def latest_result(rounds: list[Run]) -> ReviewResult | None:
        completed = [run for run in rounds if run.review_result is not None]
        if not completed:
            return None
        latest = completed[-1].review_result.model_copy(deep=True)
        latest.supersedes_round_index = completed[-1].round_index
        return latest

    @staticmethod
    def important_events(events: list[Event]) -> list[Event]:
        items: list[Event] = []
        for event in events:
            if event.kind in NOISY_EVENT_KINDS:
                continue
            if event.kind == 'agent.tool_use' and event.level == EventLevel.INFO:
                if not event.payload.get('policy_violation') and not event.payload.get('warning_reason'):
                    continue
            items.append(event)
        return items

    @staticmethod
    def _pick_result_event(events: list[Event]) -> Event | None:
        meaningful = [
            event for event in events
            if (text := ReviewResultService._event_text(event)).strip()
            and not text.strip().startswith(NOISE_TEXT_PREFIXES)
        ]
        return meaningful[-1] if meaningful else None

    @staticmethod
    def _event_text(event: Event) -> str:
        payload = event.payload
        if isinstance(payload.get('text'), str):
            return str(payload.get('text'))
        raw = payload.get('raw')
        if isinstance(raw, dict):
            part = raw.get('part')
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                return str(part.get('text'))
        return ''

    @staticmethod
    def _parse_findings(text: str) -> list[ReviewFinding]:
        findings: list[ReviewFinding] = []
        current_finding: ReviewFinding | None = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            match = SEVERITY_PATTERN.match(line)
            if match:
                if current_finding is not None:
                    findings.append(current_finding)
                raw_severity = match.group(1).lower()
                severity = SEVERITY_ALIASES.get(raw_severity, raw_severity)
                body = match.group(2).strip()
                path, line_no = ReviewResultService._extract_path_and_line(body)
                # Extract summary: text after `—` or `--` separator if present
                summary = body
                for sep in (' — ', ' -- ', ' - '):
                    if sep in body:
                        parts = body.split(sep, 1)
                        summary = parts[1].strip() if parts[1].strip() else body
                        break
                current_finding = ReviewFinding(
                    severity=severity,
                    summary=summary,
                    path=path,
                    line=line_no,
                    detail=body,
                )
                continue
            if current_finding is not None and line.strip():
                current_finding.detail = f'{current_finding.detail}\n{line.strip()}'.strip()
        if current_finding is not None:
            findings.append(current_finding)
        return findings

    @staticmethod
    def _extract_path_and_line(body: str) -> tuple[str | None, int | None]:
        match = PATH_LINE_PATTERN.search(body)
        if match:
            return match.group(1), int(match.group(2))
        match = BARE_PATH_LINE_PATTERN.search(body)
        if match:
            return match.group(1), int(match.group(2))
        match = PATH_PATTERN.search(body)
        if match:
            return match.group(1), None
        return None, None

    @staticmethod
    def _derive_verdict(*, run: Run, findings: list[ReviewFinding], text: str) -> ReviewVerdict:
        if run.status == 'failed':
            return ReviewVerdict.FAILED
        if findings:
            return ReviewVerdict.CONCERNS
        lowered = text.lower()
        if (
            'no material correctness or regression issues' in lowered
            or 'found no material issues' in lowered
            or '未发现明显正确性或回归问题' in text
            or '未发现明显问题' in text
            or '没有发现明显问题' in text
            or '未发现实质性问题' in text
        ):
            return ReviewVerdict.CLEAR
        if lowered.strip():
            return ReviewVerdict.INCONCLUSIVE
        return ReviewVerdict.INCONCLUSIVE

    @staticmethod
    def _derive_summary(
        *,
        text: str,
        findings: list[ReviewFinding],
        verdict: ReviewVerdict,
        language: str = DEFAULT_LANGUAGE,
    ) -> str:
        for paragraph in [segment.strip() for segment in text.split('\n\n') if segment.strip()]:
            if not paragraph.startswith(('- ', '* ', '#')):
                return paragraph
        if findings:
            return findings[0].summary
        if verdict == ReviewVerdict.CLEAR:
            return _message('clear', language)
        if verdict == ReviewVerdict.FAILED:
            return _message('failed', language)
        return _message('no_summary', language)

    @staticmethod
    def update_task_result(*, task: Task, rounds: list[Run]) -> Task:
        task.latest_review_result = ReviewResultService.latest_result(rounds)
        return task

    @staticmethod
    def parse_raw_text(text: str, language: str = DEFAULT_LANGUAGE) -> ReviewResult:
        """Parse raw LLM response text into a ReviewResult (no Run needed).

        Used by the direct API / inline review path.
        """
        findings = ReviewResultService._parse_findings(text)
        counts = Counter(finding.severity for finding in findings)

        # Derive verdict without a Run object
        if findings:
            verdict = ReviewVerdict.CONCERNS
        else:
            lowered = text.lower()
            if (
                'no material correctness or regression issues' in lowered
                or 'found no material issues' in lowered
                or '未发现明显正确性或回归问题' in text
                or '未发现明显问题' in text
                or '没有发现明显问题' in text
                or '未发现实质性问题' in text
            ):
                verdict = ReviewVerdict.CLEAR
            elif lowered.strip():
                verdict = ReviewVerdict.INCONCLUSIVE
            else:
                verdict = ReviewVerdict.INCONCLUSIVE

        summary = ReviewResultService._derive_summary(
            text=text, findings=findings, verdict=verdict, language=language
        )
        return ReviewResult(
            verdict=verdict,
            summary=summary,
            findings=findings,
            severity_counts={s: counts.get(s, 0) for s in SEVERITY_ORDER if counts.get(s, 0)},
            finding_count=len(findings),
            source_event_seq=None,
        )
