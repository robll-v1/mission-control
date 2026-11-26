"""Tests for the bundled skills.sh Mission Control Agent Skill."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_DIR = REPO_ROOT / 'skills' / 'mission-control'
SKILL_MD = SKILL_DIR / 'SKILL.md'
HELPER_PY = SKILL_DIR / 'scripts' / 'run_review.py'


def _load_helper():
    spec = importlib.util.spec_from_file_location('amc_skill_run_review', HELPER_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_md_conforms_to_agent_skills_spec():
    text = SKILL_MD.read_text(encoding='utf-8')
    parts = text.split('---', 2)
    assert len(parts) == 3, 'SKILL.md must begin with YAML frontmatter'
    meta = yaml.safe_load(parts[1])
    body = parts[2].strip()

    assert meta['name'] == SKILL_DIR.name == 'mission-control'
    assert re.fullmatch(r'[a-z0-9-]+', meta['name'])
    assert len(meta['name']) <= 64
    assert 20 <= len(meta['description']) <= 1024
    assert len(body.splitlines()) <= 500


def test_skill_helper_prefers_cross_agent_and_resolves_amc_or_uvx(tmp_path):
    helper = _load_helper()

    # Cross-agent selection avoids the host agent when a second CLI is installed
    backend, reason = helper.choose_reviewer(exclude='claude-code', installed=['codex', 'claude-code'])
    assert backend == 'codex'
    assert 'cross_agent' in reason

    backend2, reason2 = helper.choose_reviewer(exclude='codex', installed=['codex', 'claude-code'])
    assert backend2 == 'claude-code'
    assert 'cross_agent' in reason2

    # Falls back honestly if only the same agent is installed
    backend3, reason3 = helper.choose_reviewer(exclude='codex', installed=['codex'])
    assert backend3 == 'codex'
    assert reason3 == 'fallback_same_agent'

    # Explicit backend always wins
    backend4, reason4 = helper.choose_reviewer(requested='opencode', exclude='opencode', installed=['codex', 'opencode'])
    assert backend4 == 'opencode'
    assert reason4 == 'explicit'

    # Command resolution prefers amc, falls back to uvx, then pipx
    assert helper.resolve_amc_command(lambda n: '/usr/local/bin/amc' if n == 'amc' else None) == ['/usr/local/bin/amc']
    assert helper.resolve_amc_command(lambda n: '/opt/bin/uvx' if n == 'uvx' else None) == [
        '/opt/bin/uvx',
        '--from',
        helper.AMC_GIT_SPEC,
        'amc',
    ]

    argv = helper.build_review_argv(
        ['/usr/local/bin/amc'],
        repo=str(tmp_path),
        backend='codex',
        reasoning='high',
        focus='security',
    )
    assert '--backend' in argv and 'codex' in argv
    assert '--reasoning' in argv and 'high' in argv
    assert '--format' in argv and 'json' in argv
    assert '--exit-code' in argv
