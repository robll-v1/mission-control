from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    'backend': {
        'default': 'opencode',
        'opencode': {'model': '', 'variant': ''},
    },
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


def _global_config_path() -> Path:
    """~/.config/amc/config.yaml (XDG-compatible)."""
    xdg = os.environ.get('XDG_CONFIG_HOME', '')
    if xdg:
        return Path(xdg) / 'amc' / 'config.yaml'
    return Path.home() / '.config' / 'amc' / 'config.yaml'


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_global_config() -> dict[str, Any]:
    """Load global user config from ~/.config/amc/config.yaml."""
    path = _global_config_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


def load_repo_config(repo_path: str) -> dict[str, Any]:
    """Load merged config: DEFAULT < global < project .amc.yaml."""
    # Layer 1: defaults
    merged = dict(DEFAULT_CONFIG)

    # Layer 2: global user config
    global_cfg = load_global_config()
    if global_cfg:
        merged = _deep_merge(merged, global_cfg)

    # Layer 3: project-level .amc.yaml
    config_path = Path(repo_path) / '.amc.yaml'
    if config_path.exists():
        try:
            project_cfg = yaml.safe_load(config_path.read_text()) or {}
            merged = _deep_merge(merged, project_cfg)
        except Exception:
            pass

    return merged

