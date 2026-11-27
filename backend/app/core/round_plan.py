"""Pure decoupling contracts for round planning, disposition, and continuation.

Breaks the two MIT Axiomatic Design feedback cycles in CLI/SDK review:
- Cycle 1 <-> 5 (baseline selection <-> finding tracking): ``RoundPlan``
  freezes the candidate base, chosen base, reason, target revision, and
  reviewer selection before dispatch so planning, context compilation, and
  reconciliation consume one explicit immutable record.
- Cycle 4 <-> 6 (round verdict <-> loop continuation): ``RoundDisposition``
  separates single-round ``raw_verdict`` from cross-round ``aggregate_verdict``,
  and ``evaluate_continuation`` applies an explicit stopping policy AFTER
  reconciliation instead of breaking prematurely on raw ``clear``.

Zero database, subprocess, HTTP, or MCP dependencies.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.finding_tracker import (
    DiffRevisionState,
    _line_touches_hunks,
    normalize_finding_path,
)
from app.core.models import ReviewResult, ReviewVerdict
from app.services.review_history import (
    FindingStatus,
    ReviewHistoryStore,
    StoredFinding,
)


@dataclass(frozen=True)
class RoundPlan:
    """Immutable pre-dispatch specification for one review round."""
    candidate_base: str | None
    selected_base: str | None
    base_reason: str
    target_revision: str | None
    changed_files: tuple[str, ...]
    selected_backend: str
    selection_reason: str
    selection_note: str | None = None
    incremental_active: bool = False
    prior_baseline_sha: str | None = None
    prior_finding_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'candidate_base': self.candidate_base,
            'selected_base': self.selected_base,
            'base_reason': self.base_reason,
            'target_revision': self.target_revision,
            'changed_files': list(self.changed_files),
            'selected_backend': self.selected_backend,
            'selection_reason': self.selection_reason,
            'selection_note': self.selection_note,
            'incremental_active': self.incremental_active,
            'prior_baseline_sha': self.prior_baseline_sha,
            'prior_finding_count': self.prior_finding_count,
        }


@dataclass(frozen=True)
class RoundDisposition:
    """Reconciled cross-round disposition kept distinct from raw round verdict."""
    raw_verdict: ReviewVerdict
    aggregate_verdict: ReviewVerdict
    passed: bool
    new_findings: tuple[FindingStatus, ...]
    persistent_findings: tuple[FindingStatus, ...]
    unverified_findings: tuple[StoredFinding, ...]
    likely_fixed_findings: tuple[StoredFinding, ...]
    all_statuses: tuple[FindingStatus, ...]
    severity_counts: dict[str, int]
    summary_note: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {
            'raw_verdict': self.raw_verdict.value,
            'aggregate_verdict': self.aggregate_verdict.value,
            'passed': self.passed,
            'counts': {
                'new': len(self.new_findings),
                'persistent': len(self.persistent_findings),
                'unverified': len(self.unverified_findings),
                'likely_fixed': len(self.likely_fixed_findings),
            },
            'severity_counts': dict(self.severity_counts),
            'summary_note': self.summary_note,
        }


@dataclass(frozen=True)
class ContinuationDecision:
    """Explicit loop stopping decision evaluated after reconciliation."""
    should_continue: bool
    stop_reason: str
    policy: str
    rounds_executed: int
    max_rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            'should_continue': self.should_continue,
            'stop_reason': self.stop_reason,
            'policy': self.policy,
            'rounds_executed': self.rounds_executed,
            'max_rounds': self.max_rounds,
        }


def _stored_finding_touched_by_diff(
    finding: StoredFinding,
    diff_state: DiffRevisionState | None,
) -> bool:
    """Return True if ``diff_state`` proves code around ``finding`` changed."""
    if diff_state is None or not diff_state.ranges_complete:
        return False
    norm_path = normalize_finding_path(finding.path)
    if not norm_path:
        return False
    if norm_path in diff_state.deleted_files:
        return True
    target_path = diff_state.renames.get(norm_path, norm_path)
    if norm_path not in diff_state.modified_files and target_path not in diff_state.modified_files:
        return False
    if finding.line is None or finding.line <= 0:
        return True
    hunks = diff_state.hunks_by_path.get(norm_path) or diff_state.hunks_by_path.get(target_path) or []
    return _line_touches_hunks(finding.line, hunks, window=5)


def compute_round_disposition(
    raw_result: ReviewResult,
    *,
    is_incremental: bool,
    previous_findings: Sequence[StoredFinding],
    diff_state: DiffRevisionState | None = None,
) -> RoundDisposition:
    """Reconcile a round's raw result against prior open findings without disk I/O.

    Preserves the strict honesty invariant:
    - Omitted prior findings whose code did NOT change (or when diff evidence is
      unavailable) remain ``unverified`` and lift a raw ``CLEAR`` to ``CONCERNS``.
    - Omitted prior findings whose code WAS modified between the prior baseline
      and the current snapshot become ``likely_fixed`` (never auto-``resolved``).
    """
    raw_verdict = raw_result.verdict
    severity_counts = dict(raw_result.severity_counts)
    new_list: list[FindingStatus] = []
    persistent_list: list[FindingStatus] = []
    unverified_list: list[StoredFinding] = []
    likely_fixed_list: list[StoredFinding] = []
    all_statuses: list[FindingStatus] = []

    if is_incremental and previous_findings:
        new_stored = [
            StoredFinding(
                severity=f.severity,
                path=f.path or '',
                line=f.line,
                summary=f.summary,
            )
            for f in raw_result.findings
        ]
        store = object.__new__(ReviewHistoryStore)
        comparison = store.compare_findings(new_stored, previous_findings=list(previous_findings))
        new_list = list(comparison['new'])
        persistent_list = list(comparison['persistent'])

        for fs in comparison['unverified']:
            if _stored_finding_touched_by_diff(fs.finding, diff_state):
                likely_fixed_list.append(fs.finding)
                all_statuses.append(FindingStatus(finding=fs.finding, status='likely_fixed'))
            else:
                unverified_list.append(fs.finding)
                all_statuses.append(fs)
        all_statuses = new_list + persistent_list + all_statuses

    for finding in unverified_list:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1

    aggregate_verdict = raw_verdict
    passed = raw_verdict == ReviewVerdict.CLEAR
    notes: list[str] = []

    if unverified_list:
        if raw_verdict == ReviewVerdict.CLEAR:
            aggregate_verdict = ReviewVerdict.CONCERNS
            passed = False
        notes.append(
            f'{len(unverified_list)} earlier finding(s) were not covered by this incremental review and remain unverified.'
        )

    if likely_fixed_list:
        notes.append(
            f'{len(likely_fixed_list)} earlier finding(s) appear likely fixed after code changes (verify before marking resolved).'
        )

    return RoundDisposition(
        raw_verdict=raw_verdict,
        aggregate_verdict=aggregate_verdict,
        passed=passed,
        new_findings=tuple(new_list),
        persistent_findings=tuple(persistent_list),
        unverified_findings=tuple(unverified_list),
        likely_fixed_findings=tuple(likely_fixed_list),
        all_statuses=tuple(all_statuses),
        severity_counts=severity_counts,
        summary_note='\n\n'.join(notes),
    )


def evaluate_continuation(
    *,
    disposition: RoundDisposition,
    rounds_executed: int,
    max_rounds: int,
    has_error: bool = False,
    policy: str = 'stop_on_aggregate_clear',
) -> ContinuationDecision:
    """Decide whether an automated multi-round loop should launch another round."""
    valid_policy = policy if policy in ('stop_on_aggregate_clear', 'stop_on_raw_clear') else 'stop_on_aggregate_clear'

    if has_error or disposition.raw_verdict in (ReviewVerdict.FAILED, ReviewVerdict.INCONCLUSIVE):
        return ContinuationDecision(
            should_continue=False,
            stop_reason='terminal_error',
            policy=valid_policy,
            rounds_executed=rounds_executed,
            max_rounds=max_rounds,
        )

    if disposition.aggregate_verdict == ReviewVerdict.CLEAR and disposition.passed:
        return ContinuationDecision(
            should_continue=False,
            stop_reason='aggregate_clear',
            policy=valid_policy,
            rounds_executed=rounds_executed,
            max_rounds=max_rounds,
        )

    if valid_policy == 'stop_on_raw_clear' and disposition.raw_verdict == ReviewVerdict.CLEAR:
        return ContinuationDecision(
            should_continue=False,
            stop_reason='raw_clear_retained_unverified',
            policy=valid_policy,
            rounds_executed=rounds_executed,
            max_rounds=max_rounds,
        )

    if rounds_executed >= max_rounds:
        return ContinuationDecision(
            should_continue=False,
            stop_reason='max_rounds_reached',
            policy=valid_policy,
            rounds_executed=rounds_executed,
            max_rounds=max_rounds,
        )

    return ContinuationDecision(
        should_continue=True,
        stop_reason='continue_next_round',
        policy=valid_policy,
        rounds_executed=rounds_executed,
        max_rounds=max_rounds,
    )
