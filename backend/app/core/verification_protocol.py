"""Pure protocol layer for cross-model Cold Falsification.

Zero database, subprocess, FastAPI, or MCP dependencies:
- ``build_falsification_prompt`` constructs an adversarial, cold-context prompt
  containing ONLY structured candidate claims (never primary-reviewer reasoning
  or brand identity) to suppress agreeableness/sycophancy bias.
- ``parse_falsification_output`` parses structured JSON verdicts defensively,
  rejects hallucinated finding IDs, and fills omitted findings honestly as
  ``uncertain`` rather than guessing.
- ``summarize_attestations`` tallies latest attestation stances across findings.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.models import AttestationStance, TrackedFinding

_FENCED_JSON_RE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL | re.IGNORECASE)
_MAX_FIELD_CHARS = 1200


@dataclass(frozen=True)
class ParsedAttestation:
    finding_id: str
    stance: AttestationStance
    rationale: str = ''
    evidence: str = ''
    trigger_path: str = ''
    omitted: bool = False


def _clean_text(value: Any, *, limit: int = _MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + '...'
    return text


def build_falsification_prompt(
    findings: Sequence[TrackedFinding],
    *,
    language: str = 'en',
) -> str:
    """Build an adversarial cold-falsification prompt for candidate findings.

    Intentionally excludes the primary reviewer's identity, summary, and raw
    chain-of-thought so the verifier evaluates each claim solely against the
    code in the read-only worktree.
    """
    candidates = [
        {
            'finding_id': f.id,
            'severity': f.severity,
            'category': f.category,
            'path': f.path,
            'line': f.line,
            'title': f.title,
            'detail': f.detail,
        }
        for f in findings
    ]
    candidates_json = json.dumps(candidates, indent=2, ensure_ascii=False)

    if language == 'zh-CN':
        lang_rule = 'Write `rationale`, `evidence`, and `trigger_path` in Simplified Chinese (keep file paths, symbols, and JSON keys/enums in English).'
    else:
        lang_rule = 'Write `rationale`, `evidence`, and `trigger_path` in concise English.'

    return (
        'You are an independent adversarial code verifier operating in a read-only repository worktree.\n'
        'Your job is COLD FALSIFICATION of the candidate findings below.\n\n'
        'RULES:\n'
        '1. Assume EVERY candidate finding may be a false positive until you verify it directly in the codebase.\n'
        '2. Open and inspect the cited file, surrounding guards, callers, types, and tests in this worktree.\n'
        '3. Try your hardest to DISPROVE each claim first:\n'
        '   - Mark `"disputed"` if the code is actually safe, unreachable, already guarded upstream, or the claim misreads the code. Cite the exact counter-evidence (`file:line` or symbol) in `evidence`.\n'
        '   - Mark `"confirmed"` ONLY if you cannot disprove it AND you can state a concrete `trigger_path` showing how the bug occurs.\n'
        '   - Mark `"uncertain"` if the repository lacks sufficient context to confirm or disprove it.\n'
        '4. Do NOT modify any files or run destructive commands.\n'
        '5. You MUST return one entry for EVERY `finding_id` listed below. Never invent new `finding_id` values.\n'
        f'6. {lang_rule}\n\n'
        'CANDIDATE FINDINGS:\n'
        f'```json\n{candidates_json}\n```\n\n'
        'Return ONLY a single fenced JSON block matching this exact schema:\n'
        '```json\n'
        '{\n'
        '  "verifications": [\n'
        '    {\n'
        '      "finding_id": "<exact finding_id from above>",\n'
        '      "stance": "confirmed | disputed | uncertain",\n'
        '      "rationale": "<concise explanation of why it holds or is a false positive>",\n'
        '      "evidence": "<file:line or symbol inspected>",\n'
        '      "trigger_path": "<concrete trigger path when confirmed; empty string otherwise>"\n'
        '    }\n'
        '  ]\n'
        '}\n'
        '```\n'
    )


def _extract_verifications_object(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or '').strip()
    if not text:
        return None

    # 1. Prefer the last fenced JSON block that contains a `verifications` list.
    matches = _FENCED_JSON_RE.findall(text)
    for block in reversed(matches):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get('verifications'), list):
            return obj

    # 2. Try parsing the entire text as raw JSON.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get('verifications'), list):
            return obj
    except json.JSONDecodeError:
        pass

    # 3. Scan backwards for a balanced JSON object containing `"verifications"`.
    decoder = json.JSONDecoder()
    idx = text.rfind('"verifications"')
    while idx != -1:
        brace = text.rfind('{', 0, idx)
        while brace != -1:
            try:
                obj, _end = decoder.raw_decode(text, brace)
                if isinstance(obj, dict) and isinstance(obj.get('verifications'), list):
                    return obj
            except json.JSONDecodeError:
                pass
            brace = text.rfind('{', 0, brace)
        idx = text.rfind('"verifications"', 0, idx)
    return None


def _parse_stance(raw: Any) -> AttestationStance:
    if not isinstance(raw, str):
        return AttestationStance.UNCERTAIN
    cleaned = raw.strip().lower()
    try:
        return AttestationStance(cleaned)
    except ValueError:
        return AttestationStance.UNCERTAIN


def parse_falsification_output(
    raw_text: str,
    *,
    ordered_finding_ids: Sequence[str],
) -> tuple[list[ParsedAttestation], str]:
    """Parse falsification output defensively and guarantee one entry per input finding_id."""
    valid_ids = set(ordered_finding_ids)
    obj = _extract_verifications_object(raw_text)
    if obj is None:
        return (
            [
                ParsedAttestation(
                    finding_id=fid,
                    stance=AttestationStance.UNCERTAIN,
                    rationale='Verifier output did not contain a valid structured verifications JSON payload.',
                    omitted=True,
                )
                for fid in ordered_finding_ids
            ],
            'fallback_uncertain',
        )

    parsed_by_id: dict[str, ParsedAttestation] = {}
    for item in obj.get('verifications', []):
        if not isinstance(item, dict):
            continue
        fid = str(item.get('finding_id') or '').strip()
        if not fid or fid not in valid_ids:
            continue
        stance = _parse_stance(item.get('stance'))
        rationale = _clean_text(item.get('rationale'))
        evidence = _clean_text(item.get('evidence'), limit=400)
        trigger_path = _clean_text(item.get('trigger_path'), limit=600) if stance == AttestationStance.CONFIRMED else ''
        parsed_by_id[fid] = ParsedAttestation(
            finding_id=fid,
            stance=stance,
            rationale=rationale,
            evidence=evidence,
            trigger_path=trigger_path,
            omitted=False,
        )

    ordered: list[ParsedAttestation] = []
    for fid in ordered_finding_ids:
        existing = parsed_by_id.get(fid)
        if existing is not None:
            ordered.append(existing)
        else:
            ordered.append(
                ParsedAttestation(
                    finding_id=fid,
                    stance=AttestationStance.UNCERTAIN,
                    rationale='Verifier omitted this finding from its structured response.',
                    omitted=True,
                )
            )
    return ordered, 'json'


def summarize_attestations(findings: Sequence[TrackedFinding]) -> dict[str, int]:
    """Summarize the latest attestation stance across a list of tracked findings."""
    counts = {
        'confirmed': 0,
        'disputed': 0,
        'uncertain': 0,
        'unattested': 0,
        'cross_model_verified': 0,
    }
    for finding in findings:
        atts = getattr(finding, 'attestations', None) or []
        if not atts:
            counts['unattested'] += 1
            continue
        latest = atts[-1]
        stance_key = latest.stance.value if isinstance(latest.stance, AttestationStance) else str(latest.stance)
        if stance_key in counts:
            counts[stance_key] += 1
        else:
            counts['uncertain'] += 1
        if getattr(latest, 'cross_model', False):
            counts['cross_model_verified'] += 1
    return counts
