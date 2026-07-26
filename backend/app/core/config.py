from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    'backend': {
        'default': 'opencode',
        'opencode': {'model': '', 'variant': ''},
    },
    'context': {
        'adaptive_budget': True,
        'hunk_snippets_enabled': True,
        'include_recent_commits': 8,
        'candidate_files_limit': 12,
        'keywords_limit': 12,
        'top_level_entries_limit': 12,
        'include_top_level_entries': False,
        'hunk_snippet_file_limit': 3,
        'hunk_snippet_hunks_per_file': 1,
        'hunk_snippet_context_lines': 8,
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
    """Locate the user-level config file in a platform-appropriate place.

    * ``XDG_CONFIG_HOME`` / ``~/.config/amc/config.yaml`` on macOS & Linux.
    * ``%APPDATA%\\amc\\config.yaml`` on Windows (with a fall-back to the
      Linux-style location so legacy installs keep working).
    """
    xdg = os.environ.get('XDG_CONFIG_HOME', '')
    if xdg:
        return Path(xdg) / 'amc' / 'config.yaml'
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA')
        if appdata:
            primary = Path(appdata) / 'amc' / 'config.yaml'
            legacy = Path.home() / '.config' / 'amc' / 'config.yaml'
            if primary.exists() or not legacy.exists():
                return primary
            return legacy
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


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Read a YAML config file as UTF-8.

    Config files are written as UTF-8 (``amc init`` emits UTF-8, and users paste
    non-ASCII comments into them), but ``Path.read_text()`` without an explicit
    encoding decodes using the locale codepage. On a zh-CN/ja-JP Windows install
    that raises ``UnicodeDecodeError`` and the config is silently ignored, so the
    user gets default behaviour with no clue why. Decode explicitly, and warn
    rather than swallow so a broken config is visible.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        print(f'[amc] warning: ignoring unreadable config {path}: {exc}', file=sys.stderr)
        return {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        print(
            f'[amc] warning: ignoring config {path}: expected a mapping, got {type(loaded).__name__}',
            file=sys.stderr,
        )
        return {}
    return loaded


def load_global_config() -> dict[str, Any]:
    """Load global user config from the platform config path."""
    path = _global_config_path()
    if not path.exists():
        return {}
    return _load_yaml_file(path)


def load_repo_config(repo_path: str) -> dict[str, Any]:
    """Load merged config: DEFAULT < global < project .amc.yaml."""
    # Layer 1: defaults
    merged = copy.deepcopy(DEFAULT_CONFIG)

    # Layer 2: global user config
    global_cfg = load_global_config()
    if global_cfg:
        merged = _deep_merge(merged, global_cfg)

    # Layer 3: project-level .amc.yaml
    config_path = Path(repo_path) / '.amc.yaml'
    if config_path.exists():
        project_cfg = _load_yaml_file(config_path)
        if project_cfg:
            merged = _deep_merge(merged, project_cfg)

    return merged
