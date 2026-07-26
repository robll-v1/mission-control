"""Regression tests for the findings parser and its language handling."""
from __future__ import annotations

from app.core.models import ReviewVerdict, Run
from app.services.review_result_service import ReviewResultService


SAMPLE = """## Review Summary

The change introduces a SQL injection and leaks a connection.

## Findings

- critical: `app/accounts.py:11` — SQL injection in get_user
  The username is interpolated straight into the query.
- high: `app/accounts.py:27` — Off-by-one in top_payouts
  range(count + 1) walks one past the end.
- **medium**: `app/accounts.py:15` — Connection is never closed
"""


def test_parses_severity_path_and_line():
    result = ReviewResultService.parse_raw_text(SAMPLE)
    assert result.verdict == ReviewVerdict.CONCERNS
    assert result.finding_count == 3
    assert [f.severity for f in result.findings] == ['critical', 'high', 'medium']
    assert [f.path for f in result.findings] == ['app/accounts.py'] * 3
    assert [f.line for f in result.findings] == [11, 27, 15]
    assert result.severity_counts == {'critical': 1, 'high': 1, 'medium': 1}


def test_markdown_bold_severity_markers_are_parsed():
    result = ReviewResultService.parse_raw_text('- **high**: `a/b.py:3` — boom')
    assert result.finding_count == 1
    assert result.findings[0].severity == 'high'


def test_chinese_severity_aliases_map_to_canonical_values():
    result = ReviewResultService.parse_raw_text('- 严重: `a/b.py:3` — 出错了')
    assert result.findings[0].severity == 'critical'


def test_clear_verdict_when_model_reports_no_issues():
    result = ReviewResultService.parse_raw_text(
        '## Review Summary\n\nLooks good.\n\nNo material correctness or regression issues found.'
    )
    assert result.verdict == ReviewVerdict.CLEAR
    assert result.finding_count == 0


def test_detail_lines_attach_to_the_preceding_finding():
    result = ReviewResultService.parse_raw_text(SAMPLE)
    assert 'interpolated straight into the query' in result.findings[0].detail


# --- language handling -----------------------------------------------------

def test_english_fallback_summary_is_english():
    """language='en' must not emit Chinese on the no-summary path."""
    result = ReviewResultService.parse_raw_text('', language='en')
    assert result.summary.isascii(), result.summary


def test_chinese_fallback_summary_is_available():
    result = ReviewResultService.parse_raw_text('', language='zh')
    assert not result.summary.isascii()


def test_unknown_language_falls_back_to_english():
    result = ReviewResultService.parse_raw_text('', language='fr')
    assert result.summary.isascii()


def test_extract_result_without_text_events_uses_the_requested_language():
    run = Run(task_id='t', backend='opencode', status='failed', started_at=0.0)
    result = ReviewResultService.extract_result(events=[], run=run, language='en')
    assert result.verdict == ReviewVerdict.FAILED
    assert result.summary.isascii(), result.summary
