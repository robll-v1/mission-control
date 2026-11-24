from __future__ import annotations

import copy
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    """A config file exists but cannot be parsed."""


DEFAULT_CONFIG: dict[str, Any] = {
    'backend': {
        'default': 'opencode',
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


def _drop_unset_values(mapping: dict) -> dict:
    """Copy ``mapping`` without blank strings or nulls, at any depth."""
    cleaned: dict = {}
    for key, value in mapping.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cleaned[key] = _drop_unset_values(value) if isinstance(value, dict) else value
    return cleaned


def _without_unset_backend_values(layer: dict[str, Any]) -> dict[str, Any]:
    """Treat blank values under ``backend`` as "not set" for this layer.

    ``model: ''`` and ``variant: ''`` mean "let the agent decide", exactly
    like leaving the key out, and a blank ``default`` means no preference.
    Merged as plain values they used to override the layer below, so a
    project ``.amc.yaml`` written with ``model: ''`` (this repo's own, the
    README example, ``amc init`` with no model) silently discarded the model
    saved in the global config or on the Web model selection page.

    Only ``backend`` is treated this way: elsewhere a blank can be a real
    override, e.g. ``validation.checks.build.command: ''`` in a project turns
    off a check the global config enables.
    """
    backend = layer.get('backend')
    if not isinstance(backend, dict):
        return layer
    return {**layer, 'backend': _drop_unset_values(backend)}


def _load_yaml_file(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Read a YAML config file as UTF-8.

    Config files are written as UTF-8 (``amc init`` emits UTF-8, and users paste
    non-ASCII comments into them), but ``Path.read_text()`` without an explicit
    encoding decodes using the locale codepage. On a zh-CN/ja-JP Windows install
    that raises ``UnicodeDecodeError`` and the config is silently ignored, so the
    user gets default behaviour with no clue why. Decode explicitly, and warn
    rather than swallow so a broken config is visible.

    ``strict=True`` raises :class:`ConfigError` instead of warning, for callers
    about to rewrite the file — silently treating a parseable-but-recoverable
    file as empty and saving over it destroys the user's content.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        if strict:
            raise ConfigError(f'{path}: {exc}') from exc
        print(f'[amc] warning: ignoring unreadable config {path}: {exc}', file=sys.stderr)
        return {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        if strict:
            raise ConfigError(f'{path}: expected a mapping, got {type(loaded).__name__}')
        print(
            f'[amc] warning: ignoring config {path}: expected a mapping, got {type(loaded).__name__}',
            file=sys.stderr,
        )
        return {}
    return loaded


def load_global_config(*, strict: bool = False) -> dict[str, Any]:
    """Load global user config from the platform config path."""
    path = _global_config_path()
    if not path.exists():
        return {}
    return _load_yaml_file(path, strict=strict)


def load_repo_config(repo_path: str) -> dict[str, Any]:
    """Load merged config: DEFAULT < global < project .amc.yaml."""
    # Layer 1: defaults
    merged = copy.deepcopy(DEFAULT_CONFIG)

    # Layer 2: global user config
    global_cfg = load_global_config()
    if global_cfg:
        merged = _deep_merge(merged, _without_unset_backend_values(global_cfg))

    # Layer 3: project-level .amc.yaml
    config_path = Path(repo_path) / '.amc.yaml'
    if config_path.exists():
        project_cfg = _load_yaml_file(config_path)
        if project_cfg:
            merged = _deep_merge(merged, _without_unset_backend_values(project_cfg))

    return merged


def _clean_opt(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _extract_backend_section(cfg: dict[str, Any]) -> dict[str, Any]:
    cleaned = _without_unset_backend_values(cfg)
    section = cleaned.get('backend', {})
    return section if isinstance(section, dict) else {}


def _extract_agent_entry(section: dict[str, Any], agent: str) -> dict[str, Any]:
    entry = section.get(agent, {})
    return entry if isinstance(entry, dict) else {}


def _extract_reasoning(entry: dict[str, Any]) -> str | None:
    """Read reasoning effort from an agent config mapping, honoring `variant` alias."""
    return _clean_opt(entry.get('reasoning')) or _clean_opt(entry.get('variant'))


class _UnsetType:
    pass


UNSET = _UnsetType()


class SettingsResolver:
    """Single source of truth for resolving effective per-round RunSettings.

    Precedence per field (highest to lowest):
      round override > env (AMC_MODEL / AMC_REASONING) > review default > repo (.amc.yaml) > global > default

    Cross-agent isolation:
      When a higher layer selects a different agent (e.g. round switches from
      task's `codex` to `claude-code`), model and reasoning from the previous
      agent's review-level defaults are NEVER inherited. Config files are
      always looked up under `backend.<selected_agent>`.

    Explicit reset:
      Passing `round_model=None` or `round_reasoning=None` (as opposed to
      `UNSET`) explicitly forces CLI/model default (`None`) for this round,
      overriding env/review/repo/global values.
    """

    @staticmethod
    def resolve(
        repo_path: str | None = None,
        *,
        # Explicit round / CLI overrides (UNSET = not provided; None = explicit reset to default)
        round_agent: str | None | _UnsetType = UNSET,
        round_model: str | None | _UnsetType = UNSET,
        round_reasoning: str | None | _UnsetType = UNSET,
        round_language: str | None | _UnsetType = UNSET,
        round_focus: str | None = None,
        round_note: str | None = None,
        # Review (task)-level defaults
        review_agent: str | None = None,
        review_model: str | None = None,
        review_reasoning: str | None = None,
        review_language: str | None = None,
        review_focus: str = '',
        # Reserved optional speed tier
        speed: str | None = None,
    ):
        from app.core.context_compiler import DEFAULT_REVIEW_LANGUAGE, normalize_review_language
        from app.core.models import RunSettings

        default_agent = str(DEFAULT_CONFIG['backend']['default'])
        global_section = _extract_backend_section(load_global_config())
        repo_section: dict[str, Any] = {}
        if repo_path:
            config_path = Path(repo_path) / '.amc.yaml'
            if config_path.exists():
                repo_section = _extract_backend_section(_load_yaml_file(config_path))

        global_default_agent = _clean_opt(global_section.get('default'))
        repo_default_agent = _clean_opt(repo_section.get('default'))

        # 1. Resolve agent
        cleaned_round_agent = None if isinstance(round_agent, _UnsetType) else _clean_opt(round_agent)
        cleaned_review_agent = _clean_opt(review_agent)
        if cleaned_round_agent:
            selected_agent = cleaned_round_agent
            agent_source = 'round'
        elif cleaned_review_agent:
            selected_agent = cleaned_review_agent
            agent_source = 'review'
        elif repo_default_agent:
            selected_agent = repo_default_agent
            agent_source = 'repo'
        elif global_default_agent:
            selected_agent = global_default_agent
            agent_source = 'global'
        else:
            selected_agent = default_agent
            agent_source = 'default'

        # Cross-agent guard: review-level model/reasoning only apply if the
        # selected agent still matches the review's agent.
        review_matches_agent = (
            cleaned_review_agent is not None and cleaned_review_agent == selected_agent
        )
        effective_review_model = _clean_opt(review_model) if review_matches_agent else None
        effective_review_reasoning = _clean_opt(review_reasoning) if review_matches_agent else None

        repo_agent_cfg = _extract_agent_entry(repo_section, selected_agent)
        global_agent_cfg = _extract_agent_entry(global_section, selected_agent)
        env_model = _clean_opt(os.environ.get('AMC_MODEL', ''))
        env_reasoning = _clean_opt(os.environ.get('AMC_REASONING', ''))

        # 2. Resolve model
        if not isinstance(round_model, _UnsetType):
            resolved_model = _clean_opt(round_model)
            model_source = 'round'
        elif env_model:
            resolved_model = env_model
            model_source = 'env'
        elif effective_review_model:
            resolved_model = effective_review_model
            model_source = 'review'
        elif _clean_opt(repo_agent_cfg.get('model')):
            resolved_model = _clean_opt(repo_agent_cfg.get('model'))
            model_source = 'repo'
        elif _clean_opt(global_agent_cfg.get('model')):
            resolved_model = _clean_opt(global_agent_cfg.get('model'))
            model_source = 'global'
        else:
            resolved_model = None
            model_source = 'default'

        # 3. Resolve reasoning (with `variant` backward-compat alias)
        if not isinstance(round_reasoning, _UnsetType):
            resolved_reasoning = _clean_opt(round_reasoning)
            reasoning_source = 'round'
        elif env_reasoning:
            resolved_reasoning = env_reasoning
            reasoning_source = 'env'
        elif effective_review_reasoning:
            resolved_reasoning = effective_review_reasoning
            reasoning_source = 'review'
        elif _extract_reasoning(repo_agent_cfg):
            resolved_reasoning = _extract_reasoning(repo_agent_cfg)
            reasoning_source = 'repo'
        elif _extract_reasoning(global_agent_cfg):
            resolved_reasoning = _extract_reasoning(global_agent_cfg)
            reasoning_source = 'global'
        else:
            resolved_reasoning = None
            reasoning_source = 'default'

        # 4. Resolve language
        cleaned_round_lang = None if isinstance(round_language, _UnsetType) else _clean_opt(round_language)
        cleaned_review_lang = _clean_opt(review_language)
        if cleaned_round_lang:
            resolved_language = normalize_review_language(cleaned_round_lang)
            language_source = 'round'
        elif cleaned_review_lang:
            resolved_language = normalize_review_language(cleaned_review_lang)
            language_source = 'review'
        else:
            resolved_language = DEFAULT_REVIEW_LANGUAGE
            language_source = 'default'

        resolved_focus = (round_focus if round_focus is not None else review_focus).strip()
        resolved_note = (round_note or '').strip()

        return RunSettings(
            agent=selected_agent,
            model=resolved_model,
            reasoning=resolved_reasoning,
            speed=_clean_opt(speed),
            language=resolved_language,
            focus=resolved_focus,
            note=resolved_note,
            sources={
                'agent': agent_source,
                'model': model_source,
                'reasoning': reasoning_source,
                'language': language_source,
            },
        )


def resolve_run_settings(
    repo_path: str | None = None,
    **kwargs: Any,
):
    """Convenience wrapper around :meth:`SettingsResolver.resolve`."""
    return SettingsResolver.resolve(repo_path, **kwargs)


def resolve_backend_config(
    repo_path: str,
    *,
    backend: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    variant: str | None = None,
) -> dict[str, str | None]:
    """Resolve the selected backend and its model/variant from merged configuration.

    Kept as a thin backward-compatible view over :class:`SettingsResolver`.
    """
    eff_reasoning = reasoning if reasoning is not None else variant
    resolved = SettingsResolver.resolve(
        repo_path,
        round_agent=backend if backend is not None else UNSET,
        round_model=model if model is not None else UNSET,
        round_reasoning=eff_reasoning if eff_reasoning is not None else UNSET,
    )
    return {
        'backend': resolved.agent,
        'model': resolved.model,
        'variant': resolved.reasoning,
    }


def save_global_config(config: dict[str, Any]) -> None:
    """Atomically save the full global config mapping."""
    path = _global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        with temp_path.open('w', encoding='utf-8') as handle:
            yaml.safe_dump(config, handle, default_flow_style=False, allow_unicode=True, sort_keys=False)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


# Serialises read-modify-write cycles on the global config within this process:
# the backend serves /api/config/save from concurrent request threads, and the
# atomic replace inside save_global_config only prevents torn files, not the
# lost update where two savers both load the same base and the last write wins.
global_config_lock = threading.Lock()
