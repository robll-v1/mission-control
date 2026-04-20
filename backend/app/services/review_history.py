"""
Review History Store — Persistent review history for incremental review.

Stores review results in `.amc/reviews/` directory within the repository,
enabling:
- Incremental diff (only review new changes since last review)
- Finding dedup (detect persistent vs resolved vs new findings)
- Context inference (use commit history for auto-focus)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


@dataclass
class StoredFinding:
    """A finding stored in review history."""
    severity: str
    path: str
    line: int | None = None
    summary: str = ''
    fingerprint: str = ''  # computed for dedup

    def compute_fingerprint(self) -> str:
        """Generate fingerprint for fuzzy matching."""
        # Normalize: lowercase severity + path + first 40 chars of summary
        parts = [
            self.severity.lower(),
            self.path or '',
            self.summary[:40].lower().strip(),
        ]
        self.fingerprint = '|'.join(parts)
        return self.fingerprint


@dataclass
class ReviewRecord:
    """A single review session record."""
    review_id: str
    timestamp: float
    head_sha: str  # HEAD commit at review time
    base_ref: str  # base branch or commit
    mode: str  # 'pr' or 'local_diff'
    pr_url: str | None = None
    focus: str = ''
    verdict: str = ''  # clear, concerns, failed
    findings: list[StoredFinding] = field(default_factory=list)
    finding_count: int = 0
    duration_sec: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    commit_range: str = ''  # e.g. "abc1234..def5678"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FindingStatus:
    """Finding with resolution status."""
    finding: StoredFinding
    status: str  # 'new', 'persistent', 'resolved'
    since_round: int = 0  # which review round first found this
    code_modified: bool = False  # whether the relevant code was modified


class ReviewHistoryStore:
    """Manages review history for a repository.

    Data stored in `<repo>/.amc/reviews/`:
    - history.json: list of all review records
    - latest.json: most recent review (quick access)
    """

    STORE_DIR = '.amc/reviews'
    HISTORY_FILE = 'history.json'
    LATEST_FILE = 'latest.json'

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.store_dir = self.repo_path / self.STORE_DIR
        self.store_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_latest(self) -> ReviewRecord | None:
        """Get the most recent review record."""
        latest_file = self.store_dir / self.LATEST_FILE
        if not latest_file.exists():
            return None
        try:
            data = json.loads(latest_file.read_text(encoding='utf-8'))
            return self._dict_to_record(data)
        except (json.JSONDecodeError, KeyError):
            return None

    def get_history(self, limit: int = 20) -> list[ReviewRecord]:
        """Get review history (most recent first)."""
        history_file = self.store_dir / self.HISTORY_FILE
        if not history_file.exists():
            return []
        try:
            data = json.loads(history_file.read_text(encoding='utf-8'))
            records = [self._dict_to_record(d) for d in data]
            return records[-limit:][::-1]  # most recent first
        except (json.JSONDecodeError, KeyError):
            return []

    def get_last_reviewed_sha(self) -> str | None:
        """Get the HEAD SHA from the last review."""
        latest = self.get_latest()
        return latest.head_sha if latest else None

    def get_previous_findings(self) -> list[StoredFinding]:
        """Get findings from the most recent review."""
        latest = self.get_latest()
        return latest.findings if latest else []

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_review(self, record: ReviewRecord) -> None:
        """Save a review record to history."""
        # Compute fingerprints
        for f in record.findings:
            f.compute_fingerprint()

        record_dict = self._record_to_dict(record)

        # Update latest
        latest_file = self.store_dir / self.LATEST_FILE
        latest_file.write_text(
            json.dumps(record_dict, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )

        # Append to history
        history_file = self.store_dir / self.HISTORY_FILE
        history = []
        if history_file.exists():
            try:
                history = json.loads(history_file.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                history = []

        history.append(record_dict)

        # Keep max 50 records
        if len(history) > 50:
            history = history[-50:]

        history_file.write_text(
            json.dumps(history, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )

    # ------------------------------------------------------------------
    # Finding comparison
    # ------------------------------------------------------------------

    def compare_findings(
        self,
        new_findings: list[StoredFinding],
        previous_findings: list[StoredFinding] | None = None,
    ) -> dict[str, list[FindingStatus]]:
        """Compare new findings against previous to determine status.

        Returns dict with keys: 'new', 'persistent', 'resolved'
        """
        if previous_findings is None:
            previous_findings = self.get_previous_findings()

        # Compute fingerprints
        for f in new_findings:
            f.compute_fingerprint()
        for f in previous_findings:
            f.compute_fingerprint()

        result: dict[str, list[FindingStatus]] = {
            'new': [],
            'persistent': [],
            'resolved': [],
        }

        # Match new findings against previous
        matched_previous = set()

        for new_f in new_findings:
            match_idx = self._find_best_match(new_f, previous_findings, matched_previous)
            if match_idx is not None:
                matched_previous.add(match_idx)
                result['persistent'].append(FindingStatus(
                    finding=new_f,
                    status='persistent',
                    since_round=0,  # caller can set this
                ))
            else:
                result['new'].append(FindingStatus(
                    finding=new_f,
                    status='new',
                ))

        # Previous findings not matched in new = resolved
        for idx, prev_f in enumerate(previous_findings):
            if idx not in matched_previous:
                result['resolved'].append(FindingStatus(
                    finding=prev_f,
                    status='resolved',
                ))

        return result

    def _find_best_match(
        self,
        finding: StoredFinding,
        candidates: list[StoredFinding],
        excluded: set[int],
    ) -> int | None:
        """Find best matching finding from candidates using fuzzy matching."""
        best_score = 0.0
        best_idx = None
        threshold = 0.6  # minimum similarity to consider a match

        for idx, candidate in enumerate(candidates):
            if idx in excluded:
                continue

            score = self._similarity(finding, candidate)
            if score > best_score and score >= threshold:
                best_score = score
                best_idx = idx

        return best_idx

    def _similarity(self, a: StoredFinding, b: StoredFinding) -> float:
        """Compute similarity score between two findings (0.0 - 1.0)."""
        score = 0.0

        # Same severity = 0.2
        if a.severity.lower() == b.severity.lower():
            score += 0.2

        # Same file = 0.3
        if a.path and b.path and a.path == b.path:
            score += 0.3

        # Close line numbers = 0.2
        if a.line and b.line:
            line_diff = abs(a.line - b.line)
            if line_diff == 0:
                score += 0.2
            elif line_diff <= 5:
                score += 0.15
            elif line_diff <= 15:
                score += 0.1

        # Summary similarity = 0.3
        if a.summary and b.summary:
            ratio = SequenceMatcher(None, a.summary.lower(), b.summary.lower()).ratio()
            score += 0.3 * ratio

        return score

    # ------------------------------------------------------------------
    # Fix verification (Layer 1: code change detection)
    # ------------------------------------------------------------------

    def check_code_modified(
        self,
        findings: list[StoredFinding],
        since_sha: str,
    ) -> list[tuple[StoredFinding, bool]]:
        """Check whether the code at each finding's location was modified since a given SHA.

        Returns list of (finding, was_modified) tuples.
        Layer 1 of fix verification: determines if code was even touched.
        """
        import subprocess

        results = []
        for finding in findings:
            if not finding.path:
                results.append((finding, False))
                continue

            modified = self._is_file_region_modified(
                finding.path, finding.line, since_sha
            )
            results.append((finding, modified))

        return results

    def _is_file_region_modified(
        self, file_path: str, line: int | None, since_sha: str
    ) -> bool:
        """Check if a specific file region was modified since a commit."""
        import subprocess

        # Check if the file itself was modified
        result = subprocess.run(
            ['git', 'diff', '--name-only', f'{since_sha}..HEAD', '--', file_path],
            capture_output=True, text=True, cwd=str(self.repo_path), timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False  # file not modified

        if line is None:
            return True  # file modified, no specific line to check

        # Check if the specific line region was modified (±5 lines)
        result = subprocess.run(
            ['git', 'diff', f'{since_sha}..HEAD', '-U0', '--', file_path],
            capture_output=True, text=True, cwd=str(self.repo_path), timeout=10,
        )
        if result.returncode != 0:
            return True  # can't determine, assume modified

        # Parse diff hunks to check if our line is in a modified region
        for diff_line in result.stdout.splitlines():
            if diff_line.startswith('@@'):
                # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
                try:
                    parts = diff_line.split()
                    old_range = parts[1]  # e.g. "-42,5"
                    old_start = int(old_range.split(',')[0].lstrip('-'))
                    old_count = int(old_range.split(',')[1]) if ',' in old_range else 1
                    # Check if finding line falls within ±5 of modified region
                    if old_start - 5 <= line <= old_start + old_count + 5:
                        return True
                except (ValueError, IndexError):
                    continue

        return False

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _record_to_dict(record: ReviewRecord) -> dict:
        d = {
            'review_id': record.review_id,
            'timestamp': record.timestamp,
            'head_sha': record.head_sha,
            'base_ref': record.base_ref,
            'mode': record.mode,
            'pr_url': record.pr_url,
            'focus': record.focus,
            'verdict': record.verdict,
            'finding_count': record.finding_count,
            'duration_sec': record.duration_sec,
            'changed_files': record.changed_files,
            'commit_range': record.commit_range,
            'metadata': record.metadata,
            'findings': [
                {
                    'severity': f.severity,
                    'path': f.path,
                    'line': f.line,
                    'summary': f.summary,
                    'fingerprint': f.fingerprint,
                }
                for f in record.findings
            ],
        }
        return d

    @staticmethod
    def _dict_to_record(d: dict) -> ReviewRecord:
        findings = [
            StoredFinding(
                severity=f['severity'],
                path=f.get('path', ''),
                line=f.get('line'),
                summary=f.get('summary', ''),
                fingerprint=f.get('fingerprint', ''),
            )
            for f in d.get('findings', [])
        ]
        return ReviewRecord(
            review_id=d['review_id'],
            timestamp=d['timestamp'],
            head_sha=d['head_sha'],
            base_ref=d['base_ref'],
            mode=d['mode'],
            pr_url=d.get('pr_url'),
            focus=d.get('focus', ''),
            verdict=d.get('verdict', ''),
            findings=findings,
            finding_count=d.get('finding_count', len(findings)),
            duration_sec=d.get('duration_sec', 0.0),
            changed_files=d.get('changed_files', []),
            commit_range=d.get('commit_range', ''),
            metadata=d.get('metadata', {}),
        )
