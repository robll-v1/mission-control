from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    'context': {
        'include_recent_commits': 8,
        'candidate_files_limit': 12,
    },
    'execution': {
        'idle_timeout_sec': 180,
    },
    'worktree': {
        'auto_cleanup': True,
        'prune_on_start': True,
    },
    'validation': {
        'default_mode': 'standard',
        'checks': {},
    },
}


def load_repo_config(repo_path: str) -> dict[str, Any]:
    config_path = Path(repo_path) / '.amc.yaml'
    if not config_path.exists():
        return DEFAULT_CONFIG
    loaded = yaml.safe_load(config_path.read_text()) or {}
    merged = dict(DEFAULT_CONFIG)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged
