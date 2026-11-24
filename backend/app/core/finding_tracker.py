"""First-class cross-round Finding tracker backed by SQLite.

Implements the P3 state machine contract:
- Primary key: normalized ``path + category + title`` fingerprint (per task),
  with line-aware scoring so duplicate titles in the same file never swap user
  states when reordered.
- Fallback matching across agent / language switches and git file renames:
  same file (or renamed target) + category, or same severity within ±5 lines,
  or title similarity, or shared modified diff hunk between
  ``prev_round.review_revision`` and ``curr_round.review_revision`` (marking
  ``location_changed``).
- ``Likely fixed`` verification strictly compares
  ``prev_round.review_revision..curr_round.review_revision`` (never ``HEAD``),
  skips empty/inconclusive/failed/aborted rounds, and NEVER automatically marks
  a finding ``resolved``.
- Idempotent per-round sync tracked via ``finding_sync_state`` so upgrades
  catch up prior unsynced rounds before syncing a new round and replaying a
  round never promotes ``new`` to ``persistent``.
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
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.core.db import Database
from app.core.models import (
    FindingStatus,
    ReviewFinding,
    ReviewVerdict,
    Run,
    Task,
    TrackedFinding,
)
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
_SYNCABLE_VERDICTS = {ReviewVerdict.CLEAR, ReviewVerdict.CONCERNS}


@dataclass
class DiffRevisionState:
    modified_files: set[str]
    deleted_files: set[str]
    renames: dict[str, str]  # old_path -> new_path
    hunks_by_path: dict[str, list[tuple[int, int, int, int]]]
    ranges_complete: bool


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


def _normalize_line(line: int | None) -> int | None:
    if line is None or isinstance(line, bool):
        return None
    if 1 <= line <= 2_147_483_647:
        return int(line)
    return None


def diff_modified_ranges(
    repo_path: str,
    old_rev: str | None,
    new_rev: str | None,
) -> DiffRevisionState:
    """Inspect ``git diff -M old_rev..new_rev`` in ``repo_path``.

    Tracks modified files, deleted files, renames (``old_path -> new_path``),
    and per-file ``(old_start, old_count, new_start, new_count)`` hunks keyed
    by BOTH old and new paths. Never compares against ``HEAD``.
    """
    empty = DiffRevisionState(set(), set(), {}, {}, True)
    if not repo_path or not old_rev or not new_rev or old_rev == new_rev:
        return empty

    status_res = run_text(
        ['git', '-c', 'core.quotepath=false', 'diff', '-M', '--name-status', '-z', f'{old_rev}..{new_rev}'],
        cwd=repo_path,
        timeout=15,
    )
    if status_res.returncode != 0:
        return DiffRevisionState(set(), set(), {}, {}, False)

    modified_files: set[str] = set()
    deleted_files: set[str] = set()
    renames: dict[str, str] = {}

    tokens = status_res.stdout.split('\0')
    i = 0
    while i < len(tokens):
        code = tokens[i]
        i += 1
        if not code:
            continue
        if code.startswith(('R', 'C')):
            if i + 1 >= len(tokens):
                break
            old_p = normalize_finding_path(tokens[i]) or tokens[i]
            new_p = normalize_finding_path(tokens[i + 1]) or tokens[i + 1]
            i += 2
            if code.startswith('R'):
                renames[old_p] = new_p
            # Only mark modified if content changed (similarity < 100%).
            if code != 'R100':
                modified_files.add(old_p)
                modified_files.add(new_p)
        else:
            if i >= len(tokens):
                break
            path = normalize_finding_path(tokens[i]) or tokens[i]
            i += 1
            modified_files.add(path)
            if code.startswith('D'):
                deleted_files.add(path)

    if not modified_files and not renames:
        return DiffRevisionState(modified_files, deleted_files, renames, {}, True)

    patch_res = run_text(
        ['git', '-c', 'core.quotepath=false', 'diff', '-M', f'{old_rev}..{new_rev}', '-U0'],
        cwd=repo_path,
        timeout=15,
    )
    if patch_res.returncode != 0:
        return DiffRevisionState(modified_files, deleted_files, renames, {}, False)

    hunks_by_path: dict[str, list[tuple[int, int, int, int]]] = {}
    old_path: str | None = None
    new_path: str | None = None
    for line in patch_res.stdout.splitlines():
        if line.startswith('diff --git '):
            old_path = None
            new_path = None
            continue
        if line.startswith('rename from '):
            old_path = normalize_finding_path(line[12:].strip())
            continue
        if line.startswith('rename to '):
            new_path = normalize_finding_path(line[10:].strip())
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

    return DiffRevisionState(modified_files, deleted_files, renames, hunks_by_path, True)


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


def _is_syncable_run(run: Run) -> bool:
    """Only completed rounds with a real CLEAR/CONCERNS result may reconcile findings.

    Aborted, failed, empty-output (``parse_mode == 'none'``), or inconclusive
    rounds must never downgrade prior findings to ``likely_fixed``/``unverified``.
    """
    if run.status != 'completed' or run.review_result is None:
        return False
    if run.review_result.parse_mode == 'none':
        return False
    return run.review_result.verdict in _SYNCABLE_VERDICTS


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
        existing: TrackedFinding,
        diff_state: DiffRevisionState,
    ) -> float:
        """Compute match confidence between an incoming and existing finding.

        Uses canonical ``(path, category, title)`` agreement weighted by line
        proximity so duplicate titles in the same file (e.g. line 10 and line 90)
        pair by line regardless of output order. Also handles git file renames.
        """
        inc_path = normalize_finding_path(incoming.path)
        ext_path = normalize_finding_path(existing.path)
        renamed_to = diff_state.renames.get(ext_path) if ext_path else None
        same_file = bool(inc_path and ext_path and (inc_path == ext_path or inc_path == renamed_to))

        inc_line = _normalize_line(incoming.line)
        ext_line = _normalize_line(existing.line)
        line_diff = abs(inc_line - ext_line) if (inc_line is not None and ext_line is not None) else None

        inc_cat = normalize_category(incoming.category)
        ext_cat = normalize_category(existing.category)
        same_cat = bool(inc_cat and ext_cat and inc_cat == ext_cat)
        same_sev = incoming.severity.lower() == existing.severity.lower()

        inc_base_fp = compute_fingerprint(inc_path, inc_cat, incoming.summary)
        ext_base_fp = compute_fingerprint(ext_path, ext_cat, existing.title)
        renamed_base_fp = compute_fingerprint(renamed_to, ext_cat, existing.title) if renamed_to else None

        # Tier 1: Exact canonical identity (same file or renamed file + category + title),
        # weighted by line proximity so reordering duplicate titles never swaps identities.
        if inc_base_fp == ext_base_fp or (renamed_base_fp is not None and inc_base_fp == renamed_base_fp):
            if line_diff is not None:
                return 10.0 - min(line_diff, 500) / 1000.0
            return 9.8

        if not same_file:
            return 0.0

        hunks = diff_state.hunks_by_path.get(ext_path, []) if ext_path else []
        in_same_hunk = _same_hunk_pair(ext_line, inc_line, hunks)
        title_sim = SequenceMatcher(
            None,
            normalize_title(incoming.summary),
            normalize_title(existing.title),
        ).ratio()

        # Rule A: Same file (or rename) + same explicit category + close line or same hunk or missing line
        if same_cat and (line_diff is None or line_diff <= 15 or in_same_hunk):
            proximity = 1.0 - (min(line_diff, 15) / 20.0) if line_diff is not None else 0.7
            return 5.0 + proximity + (0.5 if same_sev else 0.0) + 0.5 * title_sim

        # Rule B: Same file (or rename) + same severity + line window ±5 (cross-agent / cross-language)
        if same_sev and line_diff is not None and line_diff <= 5:
            if inc_cat and ext_cat and inc_cat != ext_cat:
                return 0.0
            return 3.0 + (1.0 - line_diff / 6.0) + 0.5 * title_sim

        # Rule C: Same file (or rename) + same modified hunk + same severity
        if in_same_hunk and same_sev:
            if inc_cat and ext_cat and inc_cat != ext_cat:
                return 0.0
            return 2.5 + 0.5 * title_sim

        # Rule D: Same file (or rename) + same severity + high title similarity (shifted by edits above)
        if same_sev and title_sim >= 0.65:
            if inc_cat and ext_cat and inc_cat != ext_cat:
                return 0.0
            proximity_bonus = (1.0 - min(line_diff, 200) / 200.0) * 0.2 if line_diff is not None else 0.0
            return 2.0 + title_sim + proximity_bonus

        return 0.0

    @staticmethod
    def sync_round(
        *,
        db: Database,
        task: Task,
        run: Run,
        all_runs: list[Run] | None = None,
    ) -> list[TrackedFinding]:
        """Idempotently reconcile a completed round's findings into SQLite ``findings``.

        If earlier completed rounds have not been synced yet (for example right
        after upgrading a pre-P3 database), replays those earlier rounds first
        in ascending ``round_index`` order before reconciling ``run``.
        """
        with db.locked():
            if not _is_syncable_run(run):
                return db.list_findings(task.id)

            runs = all_runs if all_runs is not None else db.list_runs(task.id)
            last_synced = db.get_synced_round(task.id)
            if run.round_index <= last_synced:
                return db.list_findings(task.id)

            # Catch up any earlier unsynced completed rounds first so pre-upgrade
            # Round 1 findings are never dropped when Round 2 runs after upgrade.
            earlier_unsynced = [
                item
                for item in runs
                if last_synced < item.round_index < run.round_index and _is_syncable_run(item)
            ]
            earlier_unsynced.sort(key=lambda item: item.round_index)
            for earlier in earlier_unsynced:
                FindingTracker._sync_single_round_locked(
                    db=db,
                    task=task,
                    run=earlier,
                    runs=[r for r in runs if r.round_index <= earlier.round_index],
                )

            return FindingTracker._sync_single_round_locked(
                db=db,
                task=task,
                run=run,
                runs=runs,
            )

    @staticmethod
    def _sync_single_round_locked(
        *,
        db: Database,
        task: Task,
        run: Run,
        runs: list[Run],
    ) -> list[TrackedFinding]:
        if not _is_syncable_run(run):
            return db.list_findings(task.id)
        if run.round_index <= db.get_synced_round(task.id):
            return db.list_findings(task.id)

        rev_by_round: dict[int, str] = {}
        prev_completed_rev: str | None = None
        for item in runs:
            if _is_syncable_run(item) and item.review_revision:
                rev_by_round[item.round_index] = item.review_revision
                if item.round_index < run.round_index:
                    prev_completed_rev = item.review_revision

        curr_rev = run.review_revision
        existing_list = db.list_findings(task.id)
        incoming_list = list(run.review_result.findings or [])
        now = run.ended_at or time.time()

        diff_cache: dict[tuple[str | None, str | None], DiffRevisionState] = {}

        def get_diff(old_rev: str | None) -> DiffRevisionState:
            key = (old_rev, curr_rev)
            if key not in diff_cache:
                diff_cache[key] = diff_modified_ranges(task.repo_path, old_rev, curr_rev)
            return diff_cache[key]

        # Build candidate match pairs and greedily assign highest-confidence 1-to-1 matches.
        scored_pairs: list[tuple[float, int, int]] = []
        for inc_idx, inc in enumerate(incoming_list):
            for ext_idx, ext in enumerate(existing_list):
                ext_rev = rev_by_round.get(ext.last_round) or prev_completed_rev
                diff_state = get_diff(ext_rev)
                score = FindingTracker._match_score(inc, ext, diff_state)
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
            inc_line = _normalize_line(inc.line)
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
                ext.status = FindingStatus.DISMISSED
                ext.mentioned_after_dismiss = True
                ext.reopened = False
            elif ext.user_status == FindingStatus.RESOLVED:
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
        # Disambiguate duplicate (path, category, title) deterministically by line
        # rather than array position so reordering never changes fingerprints.
        unmatched_base_counts = Counter(
            compute_fingerprint(incoming_list[i].path, incoming_list[i].category, incoming_list[i].summary)
            for i in range(len(incoming_list))
            if i not in matched_inc
        )
        for inc_idx, inc in enumerate(incoming_list):
            if inc_idx in matched_inc:
                continue
            inc_path = normalize_finding_path(inc.path)
            inc_line = _normalize_line(inc.line)
            base_fp = compute_fingerprint(inc_path, inc.category, inc.summary)
            if unmatched_base_counts[base_fp] > 1 and inc_line is not None:
                fp = hashlib.sha256(f'{base_fp}:L{inc_line}'.encode('utf-8')).hexdigest()[:16]
            else:
                fp = base_fp
            salt = 0
            while fp in used_fps:
                salt += 1
                fp = hashlib.sha256(f'{base_fp}:r{run.round_index}:{inc_line or 0}:{salt}'.encode('utf-8')).hexdigest()[:16]
            used_fps.add(fp)
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
            diff_state = get_diff(old_rev)
            code_modified = False
            ext_path = normalize_finding_path(ext.path)
            if ext_path:
                if ext_path in diff_state.deleted_files:
                    code_modified = True
                elif ext_path in diff_state.modified_files:
                    if ext.line is None or ext.line <= 0:
                        code_modified = True
                    elif not diff_state.ranges_complete:
                        code_modified = True
                    else:
                        code_modified = _line_touches_hunks(ext.line, diff_state.hunks_by_path.get(ext_path, []))

            next_system = FindingStatus.LIKELY_FIXED if code_modified else FindingStatus.UNVERIFIED
            ext.system_status = next_system
            ext.reopened = False
            ext.updated_at = now
            if ext.user_status is not None:
                ext.status = ext.user_status
            else:
                ext.status = next_system
            to_save.append(ext)

        db.save_findings(to_save, task_id=task.id, synced_round=run.round_index)
        return db.list_findings(task.id)

    @staticmethod
    def backfill_task_findings(
        *,
        db: Database,
        task: Task,
        rounds: list[Run] | None = None,
    ) -> list[TrackedFinding]:
        """Sync any unsynced completed rounds into SQLite ``findings`` (idempotent)."""
        with db.locked():
            runs = rounds if rounds is not None else db.list_runs(task.id)
            last_synced = db.get_synced_round(task.id)
            unsynced = [
                run
                for run in runs
                if run.round_index > last_synced and _is_syncable_run(run)
            ]
            if not unsynced:
                return db.list_findings(task.id)
            unsynced.sort(key=lambda r: r.round_index)
            for run in unsynced:
                FindingTracker._sync_single_round_locked(
                    db=db,
                    task=task,
                    run=run,
                    runs=[r for r in runs if r.round_index <= run.round_index],
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
