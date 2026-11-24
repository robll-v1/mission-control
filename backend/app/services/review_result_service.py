from __future__ import annotations

import json
import re
from collections import Counter

from app.core.context_compiler import normalize_review_language
from app.core.db import Database
from app.core.finding_tracker import FindingTracker, normalize_category, normalize_finding_path
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
JSON_FENCE_OPEN_PATTERN = re.compile(
    r'^[ \t]*```(?:json|amc-review-json)[ \t]*$',
    re.MULTILINE | re.IGNORECASE,
)
JSON_FENCE_PATTERN = re.compile(
    r'^[ \t]*```(?:json|amc-review-json)[ \t]*\n(.*?)\n?[ \t]*```[ \t]*$',
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
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


def _message(key: str, language: str | None) -> str:
    canonical = normalize_review_language(language)
    table = MESSAGES.get(canonical, MESSAGES[DEFAULT_LANGUAGE])
    return table.get(key, MESSAGES[DEFAULT_LANGUAGE][key])


class ReviewResultService:
    @staticmethod
    def _run_language(events: list[Event], run_id: str) -> str:
        for event in events:
            if event.run_id == run_id and event.kind == 'review.round_started':
                lang = event.payload.get('language')
                if isinstance(lang, str) and lang.strip():
                    return normalize_review_language(lang)
        return DEFAULT_LANGUAGE

    @staticmethod
    def backfill_task(*, db: Database, task: Task) -> Task:
        with db.locked():
            task = db.get_task(task.id) or task
            rounds = db.list_runs(task.id)
            events = db.list_events(task.id)
            updated = False
            for run in rounds:
                if run.review_result is None and run.status in {'completed', 'failed'}:
                    run_lang = ReviewResultService._run_language(events, run.id)
                    run.review_result = ReviewResultService.extract_result(
                        events=events, run=run, language=run_lang
                    )
                    db.save_run(run)
                    updated = True
            latest_round = rounds[-1] if rounds else None
            # Only RUNNING can be stale here: the latest Run of a RUNNING task
            # is the round in flight. INGESTING (the next round's preflight and
            # prepare) and VALIDATING legitimately sit on a finished Run, and
            # repainting them hid a live round together with its abort button.
            if latest_round is not None and task.status == TaskStatus.RUNNING:
                if latest_round.status == 'completed':
                    task.status = TaskStatus.WAITING_HUMAN
                    task.current_stage = 'awaiting_next_round'
                    updated = True
                elif latest_round.status == 'failed':
                    task.status = TaskStatus.FAILED
                    task.current_stage = 'review_failed'
                    updated = True
            latest = ReviewResultService.latest_result(db.list_runs(task.id))
            if latest is not None and latest != task.latest_review_result:
                task.latest_review_result = latest
                updated = True
            if updated:
                db.save_task(task)
                task = db.get_task(task.id) or task
            FindingTracker.backfill_task_findings(db=db, task=task, rounds=db.list_runs(task.id))
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
                parse_mode='none',
                source_event_seq=None,
            )

        text = ReviewResultService._event_text(text_event)
        return ReviewResultService._parse_structured_result(
            text=text,
            run=run,
            language=language,
            source_event_seq=text_event.seq,
        )

    @staticmethod
    def _normalize_newlines(text: str) -> str:
        return text.replace('\r\n', '\n').replace('\r', '\n')

    @staticmethod
    def _parse_structured_result(
        *,
        text: str,
        run: Run | None,
        language: str,
        source_event_seq: int | None,
    ) -> ReviewResult:
        normalized = ReviewResultService._normalize_newlines(text)
        json_parsed, is_raw_json = ReviewResultService._parse_json_payload(normalized)
        prose_text = '' if is_raw_json else ReviewResultService._strip_json_fences(normalized)

        if json_parsed is not None:
            json_verdict, json_summary, findings = json_parsed
            parse_mode = 'json'
        else:
            json_verdict = None
            json_summary = ''
            findings = ReviewResultService._parse_findings(prose_text)
            parse_mode = 'markdown'

        counts = Counter(finding.severity for finding in findings)
        verdict = ReviewResultService._derive_verdict(
            run=run,
            findings=findings,
            text=prose_text,
            json_verdict=json_verdict,
        )
        if json_summary:
            summary = json_summary
        else:
            summary = ReviewResultService._derive_summary(
                text=prose_text, findings=findings, verdict=verdict, language=language
            )
        return ReviewResult(
            verdict=verdict,
            summary=summary,
            findings=findings,
            severity_counts={severity: counts.get(severity, 0) for severity in SEVERITY_ORDER if counts.get(severity, 0)},
            finding_count=len(findings),
            parse_mode=parse_mode,
            source_event_seq=source_event_seq,
        )

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        cleaned = JSON_FENCE_PATTERN.sub('', ReviewResultService._normalize_newlines(text))
        # Also strip any truncated/unclosed trailing ```json fence on its own line
        # so partial JSON never pollutes the fallback summary.
        open_match = JSON_FENCE_OPEN_PATTERN.search(cleaned)
        if open_match is not None:
            cleaned = cleaned[:open_match.start()]
        return cleaned.strip()

    @staticmethod
    def _parse_json_payload(
        text: str,
    ) -> tuple[tuple[str | None, str, list[ReviewFinding]] | None, bool]:
        """Try JSON-first structured extraction.

        Inspects either a standalone raw JSON object (if the entire reply is JSON)
        or ONLY the final fenced JSON block. Rejects an earlier closed fence if
        an unclosed/truncated trailing ``json`` fence or Markdown findings follow
        it, so illustrative prose examples can never swallow real findings.
        Returns ``(parsed_or_none, was_raw_standalone_json)``.
        """
        text = ReviewResultService._normalize_newlines(text)
        stripped = text.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                data = json.loads(stripped)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None, False
            parsed = ReviewResultService._validate_json_object(data)
            return parsed, parsed is not None

        matches = list(JSON_FENCE_PATTERN.finditer(text))
        if not matches:
            return None, False
        last_match = matches[-1]
        trailing = text[last_match.end():]
        # If an unclosed standalone ```json fence or Markdown findings appear after
        # the last closed fence, that fence was an earlier example, not final output.
        if JSON_FENCE_OPEN_PATTERN.search(trailing):
            return None, False
        if any(SEVERITY_PATTERN.match(line.rstrip()) for line in trailing.splitlines()):
            return None, False
        raw_json = last_match.group(1).strip()
        if not raw_json:
            return None, False
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None, False
        return ReviewResultService._validate_json_object(data), False

    @staticmethod
    def _validate_json_object(data: object) -> tuple[str | None, str, list[ReviewFinding]] | None:
        if not isinstance(data, dict):
            return None
        # Require an explicit "findings" list so illustrative JSON snippets like
        # {"verdict": "clear"} in prose can never swallow real Markdown findings.
        if 'findings' not in data or not isinstance(data['findings'], list):
            return None

        raw_verdict = data.get('verdict')
        verdict_str: str | None = None
        if isinstance(raw_verdict, str):
            norm_v = raw_verdict.strip().lower()
            if norm_v in {'clear', 'concerns', 'failed', 'inconclusive'}:
                verdict_str = norm_v

        raw_findings = data['findings']
        findings: list[ReviewFinding] = []
        for item in raw_findings:
            if not isinstance(item, dict):
                return None
            raw_sev = str(item.get('severity') or '').strip().lower()
            severity = SEVERITY_ALIASES.get(raw_sev)
            if severity is None:
                return None
            title = str(item.get('title') or item.get('summary') or '').strip()
            detail = str(item.get('detail') or item.get('description') or '').strip()
            if not title and detail:
                title = detail.splitlines()[0].strip()
            if not title:
                return None
            path = normalize_finding_path(item.get('path') or item.get('file'))
            raw_line = item.get('line')
            line_no: int | None = None
            if isinstance(raw_line, bool):
                line_no = None
            elif isinstance(raw_line, int):
                line_no = raw_line if 1 <= raw_line <= 2_147_483_647 else None
            elif isinstance(raw_line, str):
                cleaned_line = raw_line.strip()
                if cleaned_line.isascii() and cleaned_line.isdigit():
                    stripped_zeros = cleaned_line.lstrip('0')
                    if 1 <= len(stripped_zeros) <= 10:
                        val = int(stripped_zeros)
                        line_no = val if 1 <= val <= 2_147_483_647 else None
            category = normalize_category(item.get('category') or item.get('rule_category'))
            findings.append(
                ReviewFinding(
                    severity=severity,
                    summary=title,
                    path=path,
                    line=line_no,
                    detail=detail or title,
                    category=category,
                )
            )

        summary = str(data.get('summary') or '').strip()
        return verdict_str, summary, findings

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
            # Codex labels interim progress text as commentary. A round that
            # stops after one ("I'll read the patch first") has no result.
            and event.payload.get('phase') != 'commentary'
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
    def _derive_verdict(
        *,
        run: Run | None,
        findings: list[ReviewFinding],
        text: str,
        json_verdict: str | None = None,
    ) -> ReviewVerdict:
        if run is not None and run.status == 'aborted':
            return ReviewVerdict.INCONCLUSIVE
        if run is not None and run.status == 'failed':
            return ReviewVerdict.FAILED
        if findings:
            return ReviewVerdict.CONCERNS
        if json_verdict == 'clear':
            return ReviewVerdict.CLEAR
        if json_verdict == 'concerns':
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
            if not paragraph.startswith(('- ', '* ', '#', '```')):
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
        if not text.strip():
            return ReviewResult(
                verdict=ReviewVerdict.INCONCLUSIVE,
                summary=_message('no_summary', language),
                findings=[],
                severity_counts={},
                finding_count=0,
                parse_mode='none',
                source_event_seq=None,
            )
        return ReviewResultService._parse_structured_result(
            text=text,
            run=None,
            language=language,
            source_event_seq=None,
        )
