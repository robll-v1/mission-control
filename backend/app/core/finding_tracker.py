"""First-class cross-round Finding tracker backed by SQLite.

Implements the P3 state machine contract:
- Primary key: normalized ``path + category + title`` fingerprint (per task).
- Fallback matching across agent / language switches: same normalized file +
  category, or same severity within ±5 lines, or title similarity, or shared
  modified diff hunk between ``prev_round.review_revision`` and
  ``curr_round.review_revision`` (marking ``location_changed``).
- ``Likely fixed`` verification strictly compares
  ``prev_round.review_revision..curr_round.review_revision`` (never ``HEAD``),
  and the system NEVER automatically marks a finding ``resolved``.
- User terminal states:
  * ``dismissed`` stays ``dismissed`` if re-reported in a later round (sets
    ``mentioned_after_dismiss=True`` and updates ``last_round``).
  * ``resolved`` automatically reopens to ``persistent`` (``reopened=True``)
    if re-reported in a later round.
  * Both support Undo/Reopen back to the underlying ``system_status``.
"""
from __future__ import annotations

import hashlib
import re
import time
from difflib import SequenceMatcher

from app.core.db import Database
from app.core.models import FindingStatus, ReviewFinding, Run, Task, TrackedFinding
from app.core.proc import run_text

_NON_SLUG_RE = re.compile(r'[^a-z0-9]+')
_TITLE_PUNCT_RE = re.compile(r'[^\w\s]', re.UNICODE)
_WHITESPACE_RE = re.compile(r'\s+')
_HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')

_OPEN_STATUSES = {
    FindingStatus.NEW,
    FindingStatus.PERSISTENT,
    FindingStatus.UNVERIFIED,
    FindingStatus.LIKELY_FIXED,
}


def normalize_finding_path(path: str | None) -> str | None:
    if not path:
        return None
    cleaned = str(path).strip().strip('`').replace('\\', '/')
    while cleaned.startswith('./'):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip('/') if cleaned.startswith('/a/') or cleaned.startswith('/b/') else cleaned
    return cleaned or None


def normalize_category(category: str | None) -> str:
    if not category:
        return ''
    slug = _NON_SLUG_RE.sub('-', str(category).strip().lower()).strip('-')
    return slug[:64]


def normalize_title(title: str | None) -> str:
    if not title:
        return ''
    cleaned = _TITLE_PUNCT_RE.sub(' ', str(title).lower())
    cleaned = _WHITESPACE_RE.sub(' ', cleaned).strip()
    return cleaned[:80]


def compute_fingerprint(path: str | None, category: str | None, title: str | None) -> str:
    norm_path = normalize_finding_path(path) or ''
    norm_cat = normalize_category(category)
    norm_title = normalize_title(title)
    raw = f'{norm_path}|{norm_cat}|{norm_title}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def diff_modified_ranges(
    repo_path: str,
    old_rev: str | None,
    new_rev: str | None,
) -> tuple[set[str], dict[str, list[tuple[int, int, int, int]]], bool]:
    """Inspect ``git diff old_rev..new_rev`` in ``repo_path``.

    Returns ``(modified_paths, hunks_by_path, ranges_complete)`` where each hunk
    tuple is ``(old_start, old_count, new_start, new_count)``.
    Never compares against ``HEAD``; if either revision is missing or identical,
    returns empty sets cleanly.
    """
    if not repo_path or not old_rev or not new_rev or old_rev == new_rev:
        return set(), {}, True

    names_res = run_text(
        ['git', '-c', 'core.quotepath=false', 'diff', '--name-only', f'{old_rev}..{new_rev}'],
        cwd=repo_path,
        timeout=15,
    )
    if names_res.returncode != 0:
        return set(), {}, False
    modified_files = {
        normalize_finding_path(line) or line.strip()
        for line in names_res.stdout.splitlines()
        if line.strip()
    }
    if not modified_files:
        return set(), {}, True

    patch_res = run_text(
        ['git', '-c', 'core.quotepath=false', 'diff', '-U0', f'{old_rev}..{new_rev}'],
        cwd=repo_path,
        timeout=15,
    )
    if patch_res.returncode != 0:
        return modified_files, {}, False

    hunks_by_path: dict[str, list[tuple[int, int, int, int]]] = {}
    old_path: str | None = None
    new_path: str | None = None
    for line in patch_res.stdout.splitlines():
        if line.startswith('diff --git '):
            old_path = None
            new_path = None
            continue
        if line.startswith('--- a/'):
            old_path = normalize_finding_path(line[6:].strip())
            continue
        if line.startswith('--- /dev/null'):
            old_path = None
            continue
        if line.startswith('+++ b/'):
            new_path = normalize_finding_path(line[6:].strip())
            continue
        if line.startswith('+++ /dev/null'):
            new_path = None
            continue
        if not line.startswith('@@'):
            continue
        match = _HUNK_HEADER_RE.match(line)
        if not match:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        hunk = (old_start, old_count, new_start, new_count)
        for target in {p for p in (old_path, new_path) if p}:
            hunks_by_path.setdefault(target, []).append(hunk)

    return modified_files, hunks_by_path, True


def _line_touches_hunks(line: int, hunks: list[tuple[int, int, int, int]], *, window: int = 5) -> bool:
    for old_start, old_count, _new_start, _new_count in hunks:
        span_end = old_start + max(old_count, 1) - 1
        if old_start - window <= line <= span_end + window:
            return True
    return False


def _same_hunk_pair(
    old_line: int | None,
    new_line: int | None,
    hunks: list[tuple[int, int, int, int]],
    *,
    window: int = 5,
) -> bool:
    if old_line is None or new_line is None:
        return False
    for old_start, old_count, new_start, new_count in hunks:
        old_end = old_start + max(old_count, 1) - 1
        new_end = new_start + max(new_count, 1) - 1
        if (old_start - window <= old_line <= old_end + window) and (
            new_start - window <= new_line <= new_end + window
        ):
            return True
    return False


class FindingTracker:
    @staticmethod
    def counts(findings: list[TrackedFinding]) -> dict[str, int]:
        counts = {
            'all': len(findings),
            'open': 0,
            'new': 0,
            'persistent': 0,
            'unverified': 0,
            'likely_fixed': 0,
            'resolved': 0,
            'dismissed': 0,
        }
        for item in findings:
            key = item.status.value if isinstance(item.status, FindingStatus) else str(item.status)
            if key in counts:
                counts[key] += 1
            if item.status in _OPEN_STATUSES:
                counts['open'] += 1
        return counts

    @staticmethod
    def _match_score(
        incoming: ReviewFinding,
        incoming_fp: str,
        existing: TrackedFinding,
        hunks: list[tuple[int, int, int, int]],
    ) -> float:
        """Compute match confidence between an incoming and existing finding.

        Returns 0.0 if incompatible, 10.0 for exact fingerprint match, or a
        positive fallback score for cross-agent / cross-language matching.
        """
        if incoming_fp == existing.fingerprint:
            return 10.0

        inc_path = normalize_finding_path(incoming.path)
        ext_path = normalize_finding_path(existing.path)
        if not inc_path or not ext_path or inc_path != ext_path:
            return 0.0

        inc_cat = normalize_category(incoming.category)
        ext_cat = normalize_category(existing.category)
        same_cat = bool(inc_cat and ext_cat and inc_cat == ext_cat)
        same_sev = incoming.severity.lower() == existing.severity.lower()

        inc_line = incoming.line if (incoming.line is not None and incoming.line > 0) else None
        ext_line = existing.line if (existing.line is not None and existing.line > 0) else None
        line_diff = abs(inc_line - ext_line) if (inc_line is not None and ext_line is not None) else None
        in_same_hunk = _same_hunk_pair(ext_line, inc_line, hunks)

        title_sim = SequenceMatcher(
            None,
            normalize_title(incoming.summary),
            normalize_title(existing.title),
        ).ratio()

        # Rule A: Same file + same explicit category + close line or same hunk or missing line
        if same_cat and (line_diff is None or line_diff <= 15 or in_same_hunk):
            proximity = 1.0 - (min(line_diff, 15) / 20.0) if line_diff is not None else 0.7
            return 5.0 + proximity + (0.5 if same_sev else 0.0) + 0.5 * title_sim

        # Rule B: Same file + same severity + line window ±5 (cross-agent / cross-language)
        if same_sev and line_diff is not None and line_diff <= 5:
            # If both explicitly declare DIFFERENT non-empty categories, do not merge them
            if inc_cat and ext_cat and inc_cat != ext_cat:
                return 0.0
            return 3.0 + (1.0 - line_diff / 6.0) + 0.5 * title_sim

        # Rule C: Same file + same modified hunk + same severity (line shifted >5 within hunk)
        if in_same_hunk and same_sev:
            if inc_cat and ext_cat and inc_cat != ext_cat:
                return 0.0
            return 2.5 + 0.5 * title_sim

        # Rule D: Same file + same severity + high title similarity (shifted by edits above)
        if same_sev and title_sim >= 0.65:
            if inc_cat and ext_cat and inc_cat != ext_cat:
                return 0.0
            return 2.0 + title_sim

        return 0.0

    @staticmethod
    def sync_round(
        *,
        db: Database,
        task: Task,
        run: Run,
        all_runs: list[Run] | None = None,
    ) -> list[TrackedFinding]:
        """Reconcile a completed round's findings into SQLite ``findings``."""
        with db.locked():
            if run.status != 'completed' or run.review_result is None:
                return db.list_findings(task.id)

            runs = all_runs if all_runs is not None else db.list_runs(task.id)
            rev_by_round: dict[int, str] = {}
            prev_completed_rev: str | None = None
            for item in runs:
                if item.status == 'completed' and item.review_revision:
                    rev_by_round[item.round_index] = item.review_revision
                    if item.round_index < run.round_index:
                        prev_completed_rev = item.review_revision

            curr_rev = run.review_revision
            existing_list = db.list_findings(task.id)
            incoming_list = list(run.review_result.findings or [])
            now = run.ended_at or time.time()

            # Cache git diffs keyed by (old_rev, curr_rev) to avoid repeated git calls.
            diff_cache: dict[tuple[str | None, str | None], tuple[set[str], dict[str, list[tuple[int, int, int, int]]], bool]] = {}

            def get_diff(old_rev: str | None) -> tuple[set[str], dict[str, list[tuple[int, int, int, int]]], bool]:
                key = (old_rev, curr_rev)
                if key not in diff_cache:
                    diff_cache[key] = diff_modified_ranges(task.repo_path, old_rev, curr_rev)
                return diff_cache[key]

            # Compute fingerprints for incoming findings, disambiguating intra-round collisions.
            incoming_fps: list[str] = []
            seen_round_fps: set[str] = set()
            for idx, finding in enumerate(incoming_list):
                base_fp = compute_fingerprint(finding.path, finding.category, finding.summary)
                fp = base_fp
                if fp in seen_round_fps:
                    line_tag = finding.line if finding.line is not None else idx
                    fp = hashlib.sha256(f'{base_fp}:{line_tag}:{idx}'.encode('utf-8')).hexdigest()[:16]
                seen_round_fps.add(fp)
                incoming_fps.append(fp)

            # Build candidate match pairs and greedily assign highest-confidence 1-to-1 matches.
            scored_pairs: list[tuple[float, int, int]] = []
            for inc_idx, inc in enumerate(incoming_list):
                inc_path = normalize_finding_path(inc.path)
                for ext_idx, ext in enumerate(existing_list):
                    ext_rev = rev_by_round.get(ext.last_round) or prev_completed_rev
                    _, hunks_by_path, _ = get_diff(ext_rev)
                    hunks = hunks_by_path.get(inc_path, []) if inc_path else []
                    score = FindingTracker._match_score(inc, incoming_fps[inc_idx], ext, hunks)
                    if score > 0.0:
                        scored_pairs.append((score, inc_idx, ext_idx))

            scored_pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
            matched_inc: dict[int, int] = {}
            matched_ext: set[int] = set()
            for _score, inc_idx, ext_idx in scored_pairs:
                if inc_idx in matched_inc or ext_idx in matched_ext:
                    continue
                matched_inc[inc_idx] = ext_idx
                matched_ext.add(ext_idx)

            to_save: list[TrackedFinding] = []
            used_fps = {ext.fingerprint for ext in existing_list}

            # 1. Update matched existing findings.
            for inc_idx, ext_idx in matched_inc.items():
                inc = incoming_list[inc_idx]
                ext = existing_list[ext_idx]
                inc_path = normalize_finding_path(inc.path)
                inc_line = inc.line if (inc.line is not None and inc.line > 0) else None
                inc_cat = normalize_category(inc.category) or ext.category

                loc_changed = False
                prev_path = ext.previous_path
                prev_line = ext.previous_line
                if (inc_path and ext.path and inc_path != ext.path) or (
                    inc_line is not None and ext.line is not None and inc_line != ext.line
                ):
                    loc_changed = True
                    prev_path = ext.path
                    prev_line = ext.line

                ext.category = inc_cat
                ext.severity = inc.severity
                ext.path = inc_path if inc_path is not None else ext.path
                ext.line = inc_line if inc_line is not None else ext.line
                ext.title = inc.summary
                ext.detail = inc.detail or inc.summary
                ext.last_round = run.round_index
                ext.location_changed = loc_changed
                ext.previous_path = prev_path
                ext.previous_line = prev_line
                ext.system_status = FindingStatus.PERSISTENT
                ext.updated_at = now

                if ext.user_status == FindingStatus.DISMISSED:
                    # Dismissed stays dismissed; surface a reminder that round N still mentioned it.
                    ext.status = FindingStatus.DISMISSED
                    ext.mentioned_after_dismiss = True
                    ext.reopened = False
                elif ext.user_status == FindingStatus.RESOLVED:
                    # Previously marked resolved, but re-reported -> auto-reopen to persistent.
                    ext.user_status = None
                    ext.status = FindingStatus.PERSISTENT
                    ext.reopened = True
                    ext.mentioned_after_dismiss = False
                else:
                    ext.status = FindingStatus.PERSISTENT
                    ext.reopened = False
                    ext.mentioned_after_dismiss = False

                to_save.append(ext)

            # 2. Insert brand-new findings from this round.
            for inc_idx, inc in enumerate(incoming_list):
                if inc_idx in matched_inc:
                    continue
                fp = incoming_fps[inc_idx]
                if fp in used_fps:
                    fp = hashlib.sha256(f'{fp}:r{run.round_index}:{inc_idx}'.encode('utf-8')).hexdigest()[:16]
                used_fps.add(fp)
                inc_path = normalize_finding_path(inc.path)
                inc_line = inc.line if (inc.line is not None and inc.line > 0) else None
                to_save.append(
                    TrackedFinding(
                        task_id=task.id,
                        fingerprint=fp,
                        category=normalize_category(inc.category),
                        severity=inc.severity,
                        path=inc_path,
                        line=inc_line,
                        title=inc.summary,
                        detail=inc.detail or inc.summary,
                        first_round=run.round_index,
                        last_round=run.round_index,
                        status=FindingStatus.NEW,
                        system_status=FindingStatus.NEW,
                        user_status=None,
                        location_changed=False,
                        reopened=False,
                        mentioned_after_dismiss=False,
                        created_at=now,
                        updated_at=now,
                    )
                )

            # 3. Reconcile existing findings absent from this round -> likely_fixed or unverified.
            for ext_idx, ext in enumerate(existing_list):
                if ext_idx in matched_ext:
                    continue
                old_rev = rev_by_round.get(ext.last_round) or prev_completed_rev
                modified_files, hunks_by_path, ranges_complete = get_diff(old_rev)
                code_modified = False
                ext_path = normalize_finding_path(ext.path)
                if ext_path and ext_path in modified_files:
                    if ext.line is None or ext.line <= 0:
                        code_modified = True
                    elif not ranges_complete:
                        code_modified = True
                    else:
                        code_modified = _line_touches_hunks(ext.line, hunks_by_path.get(ext_path, []))

                next_system = FindingStatus.LIKELY_FIXED if code_modified else FindingStatus.UNVERIFIED
                ext.system_status = next_system
                ext.reopened = False
                ext.updated_at = now
                if ext.user_status is not None:
                    ext.status = ext.user_status
                else:
                    # System NEVER auto-resolves; only user actions can set RESOLVED.
                    ext.status = next_system
                to_save.append(ext)

            db.save_findings(to_save)
            return db.list_findings(task.id)

    @staticmethod
    def backfill_task_findings(
        *,
        db: Database,
        task: Task,
        rounds: list[Run] | None = None,
    ) -> list[TrackedFinding]:
        """Populate SQLite ``findings`` for legacy tasks that predate P3."""
        with db.locked():
            existing = db.list_findings(task.id)
            if existing:
                return existing
            runs = rounds if rounds is not None else db.list_runs(task.id)
            completed_with_results = [
                run for run in runs if run.status == 'completed' and run.review_result is not None
            ]
            if not completed_with_results:
                return []
            if not any(run.review_result.findings for run in completed_with_results if run.review_result):
                return []
            for idx, run in enumerate(completed_with_results):
                FindingTracker.sync_round(
                    db=db,
                    task=task,
                    run=run,
                    all_runs=completed_with_results[: idx + 1],
                )
            return db.list_findings(task.id)

    @staticmethod
    def set_user_status(
        *,
        db: Database,
        task_id: str,
        finding_id: str,
        action: str,
    ) -> TrackedFinding:
        """Apply a user-initiated status change (resolve / dismiss / reopen)."""
        normalized = (action or '').strip().lower()
        if normalized in {'resolve', 'resolved'}:
            target_user: FindingStatus | None = FindingStatus.RESOLVED
        elif normalized in {'dismiss', 'dismissed'}:
            target_user = FindingStatus.DISMISSED
        elif normalized in {'reopen', 'undo', 'open'}:
            target_user = None
        else:
            raise ValueError(
                f"invalid finding action: {action!r} (expected 'resolve', 'dismiss', or 'reopen')"
            )

        with db.locked():
            finding = db.get_finding(task_id, finding_id)
            if finding is None:
                raise KeyError(f'finding not found: {finding_id}')
            finding.user_status = target_user
            finding.status = target_user if target_user is not None else finding.system_status
            finding.reopened = False
            finding.mentioned_after_dismiss = False
            finding.updated_at = time.time()
            db.save_finding(finding)
            return finding
