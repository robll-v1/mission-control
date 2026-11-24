"""Auto-detect available AI backends and their models."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class BackendInfo:
    """Information about a detected backend.

    ``models_source`` says where the model list came from. It is a fact about
    enumeration, never about usability: a list from any source only proves
    those IDs were returned by the CLI, its config or a catalog cache. Whether
    the current account may actually call a given model is NOT verified by
    detection — ``models_call_verified`` stays False until a real round has
    run that model, and ``models_note`` states the caveat. An empty list never
    means the backend is unusable, only that we could not enumerate.
    """

    SOURCE_CLI = 'cli'                # live CLI enumeration query
    SOURCE_CATALOG_CACHE = 'catalog_cache'  # full public catalog, NOT account-validated
    SOURCE_CACHE = 'cache'            # backend's own local cache file
    SOURCE_CONFIG = 'config'          # model explicitly set in backend config
    SOURCE_UNKNOWN = 'unknown'        # no reliable enumeration available

    REASONING_SOURCE_CACHE = 'cache'
    REASONING_SOURCE_CLI_HELP = 'cli_help'
    REASONING_SOURCE_CONFIG = 'config'
    REASONING_SOURCE_UNSUPPORTED = 'unsupported'
    REASONING_SOURCE_UNKNOWN = 'unknown'

    def __init__(
        self,
        name: str,
        command: str,
        version: str,
        models: list[str],
        models_source: str = SOURCE_UNKNOWN,
        models_note: str | None = None,
        models_call_verified: bool = False,
        reasoning_supported: bool = False,
        reasoning_levels: list[str] | None = None,
        reasoning_default: str | None = None,
        reasoning_source: str = REASONING_SOURCE_UNSUPPORTED,
        reasoning_note: str | None = None,
        model_reasoning: dict[str, dict[str, Any]] | None = None,
    ):
        self.name = name
        self.command = command
        self.version = version
        self.models = models
        self.models_source = models_source
        self.models_note = models_note
        self.models_call_verified = models_call_verified
        self.reasoning_supported = reasoning_supported
        self.reasoning_levels = list(reasoning_levels or [])
        self.reasoning_default = reasoning_default
        self.reasoning_source = reasoning_source
        self.reasoning_note = reasoning_note
        self.model_reasoning = dict(model_reasoning or {})

    def __repr__(self) -> str:
        return (
            f'BackendInfo({self.name}, version={self.version}, '
            f'models={len(self.models)}, source={self.models_source})'
        )


class ModelFilter:
    """Smart filtering for large model lists."""

    # Popular model families to prioritize
    PRIORITY_PATTERNS = [
        r'^gpt-6',           # GPT-6 family
        r'^gpt-5',           # GPT-5 family
        r'^gpt-4',           # GPT-4 family
        r'^claude-opus',     # Claude Opus
        r'^claude-sonnet',   # Claude Sonnet
        r'^claude-haiku',    # Claude Haiku
        r'^deepseek-v[3-9]', # DeepSeek v3+
        r'^gemini-2',        # Gemini 2.x
        r'^o1',              # OpenAI o1
        r'^o3',              # OpenAI o3
    ]

    # Patterns to exclude (test/deprecated/internal models)
    EXCLUDE_PATTERNS = [
        r'test',
        r'debug',
        r'internal',
        r'deprecated',
        r'legacy',
        r'old',
        r'-preview$',
        r'^experimental',
    ]

    @staticmethod
    def filter_models(models: list[str], max_models: int = 50) -> list[str]:
        """Filter and prioritize models for display.

        Args:
            models: Full list of models
            max_models: Maximum models to return

        Returns:
            Filtered and sorted list of models
        """
        if len(models) <= max_models:
            return models

        # Step 1: Exclude test/deprecated models
        filtered = []
        for model in models:
            model_lower = model.lower()
            if not any(re.search(pattern, model_lower) for pattern in ModelFilter.EXCLUDE_PATTERNS):
                filtered.append(model)

        # Step 2: Prioritize popular families
        priority = []
        remaining = []

        for model in filtered:
            if any(re.match(pattern, model, re.IGNORECASE) for pattern in ModelFilter.PRIORITY_PATTERNS):
                priority.append(model)
            else:
                remaining.append(model)

        # Step 3: Combine and limit
        result = priority + remaining
        return result[:max_models]

    @staticmethod
    def group_models(models: list[str]) -> dict[str, list[str]]:
        """Group models by provider/family for organized display.

        Returns:
            Dict mapping group name to list of models
        """
        groups: dict[str, list[str]] = {
            'GPT': [],
            'Claude': [],
            'DeepSeek': [],
            'Gemini': [],
            'Other': [],
        }

        for model in models:
            model_lower = model.lower()

            if 'gpt-' in model_lower or 'o1' in model_lower or 'o3' in model_lower:
                groups['GPT'].append(model)
            elif 'claude' in model_lower:
                groups['Claude'].append(model)
            elif 'deepseek' in model_lower:
                groups['DeepSeek'].append(model)
            elif 'gemini' in model_lower:
                groups['Gemini'].append(model)
            else:
                groups['Other'].append(model)

        # Remove empty groups
        return {k: v for k, v in groups.items() if v}


class BackendDetector:
    """Detect available AI coding backends on the system."""

    @staticmethod
    def _detect_safely(name: str, command: str, detector) -> BackendInfo | None:
        """Run one backend's detection; a crash must not sink the whole API.

        Unexpected failures (a cache with a shape nobody anticipated) degrade
        to an unknown-model entry carrying the reason, so one bad source can
        never take /api/config/detect down with it.
        """
        try:
            return detector()
        except Exception as exc:  # noqa: BLE001 - isolation boundary by design
            return BackendInfo(
                name, command, 'unknown', [],
                models_source=BackendInfo.SOURCE_UNKNOWN,
                models_note=f'model discovery failed unexpectedly: {exc.__class__.__name__}: {exc}',
            )

    @staticmethod
    def detect_all() -> list[BackendInfo]:
        """Detect all available backends.

        Returns:
            List of BackendInfo objects for each detected backend.
        """
        backends = []

        # OpenCode
        if shutil.which('opencode'):
            info = BackendDetector._detect_safely('opencode', 'opencode', BackendDetector._detect_opencode)
            if info:
                backends.append(info)

        # Claude Code
        if shutil.which('claude'):
            info = BackendDetector._detect_safely('claude-code', 'claude', BackendDetector._detect_claude_code)
            if info:
                backends.append(info)

        # GitHub Copilot
        if shutil.which('gh'):
            info = BackendDetector._detect_safely('copilot', 'gh', BackendDetector._detect_copilot)
            if info:
                backends.append(info)

        # Codex CLI
        if shutil.which('codex'):
            info = BackendDetector._detect_safely('codex', 'codex', BackendDetector._detect_codex)
            if info:
                backends.append(info)

        return backends

    @staticmethod
    def _run_command(cmd: list[str], timeout: int = 5) -> tuple[bool, str]:
        """Run a command and return (success, output)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode == 0, result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False, ''

    @staticmethod
    def _detect_opencode() -> BackendInfo | None:
        """Detect OpenCode installation and available models."""
        # Check if opencode exists (even if it errors on --version)
        if not shutil.which('opencode'):
            return None

        # Try to get version (might fail, but that's ok)
        success, output = BackendDetector._run_command(['opencode', '--version'])
        version = 'installed' if not success else (output.split('\n')[0] if output else 'unknown')

        # `opencode models` is the best enumeration source, but it is still
        # only enumeration: it proves which IDs the CLI exposes under the
        # current configuration, not that this account may call each one.
        # No hardcoded fallback — if we cannot enumerate, we say so instead
        # of inventing model names.
        opencode_reasoning_note = (
            'OpenCode does not expose reasoning effort levels; legacy model#variant syntax in config remains supported.'
        )
        models, note = BackendDetector._probe_opencode_models_cli()
        if models:
            return BackendInfo(
                'opencode', 'opencode', version, models,
                models_source=BackendInfo.SOURCE_CLI,
                models_note=(
                    'IDs enumerated by `opencode models`; per-model call '
                    'availability for this account is not verified.'
                ),
                reasoning_supported=False,
                reasoning_source=BackendInfo.REASONING_SOURCE_CONFIG,
                reasoning_note=opencode_reasoning_note,
            )

        # Secondary: the models-dev catalog cache in opencode.db. This lists
        # every public model, not what this account may use.
        catalog, catalog_note = BackendDetector._probe_opencode_catalog_cache()
        if catalog:
            if len(catalog) > 100:
                catalog = ModelFilter.filter_models(catalog, max_models=50)
            return BackendInfo(
                'opencode', 'opencode', version, catalog,
                models_source=BackendInfo.SOURCE_CATALOG_CACHE,
                models_note=(
                    'List from the local models-dev catalog cache; account '
                    'permissions and per-model call availability are not verified.'
                ),
                reasoning_supported=False,
                reasoning_source=BackendInfo.REASONING_SOURCE_CONFIG,
                reasoning_note=opencode_reasoning_note,
            )

        return BackendInfo(
            'opencode', 'opencode', version, [],
            models_source=BackendInfo.SOURCE_UNKNOWN,
            models_note=(
                'Could not enumerate models: `opencode models` failed'
                f' ({note or "no output"}); catalog cache unusable'
                f' ({catalog_note or "not found"}).'
                ' The backend still runs with its own default or configured model.'
            ),
            reasoning_supported=False,
            reasoning_source=BackendInfo.REASONING_SOURCE_CONFIG,
            reasoning_note=opencode_reasoning_note,
        )

    @staticmethod
    def _probe_opencode_models_cli() -> tuple[list[str], str | None]:
        """Ask the CLI directly; returns (models, failure reason)."""
        success, output = BackendDetector._run_command(['opencode', 'models'], timeout=15)
        if not success:
            return [], 'command failed or timed out'
        # Output is one or more `provider/model` IDs per line (they can nest
        # further, e.g. openrouter/anthropic/claude-sonnet); ignore header
        # noise and bare IDs without a provider namespace.
        models = []
        for line in output.splitlines():
            candidate = line.strip()
            if re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', candidate) and candidate not in models:
                models.append(candidate)
        if not models:
            return [], 'unrecognized output format'
        return models, None

    @staticmethod
    def _probe_opencode_catalog_cache() -> tuple[list[str], str | None]:
        """Read the models-dev catalog cache; returns (models, failure reason).

        This is a public catalog snapshot, not an account capability check.
        """
        # OpenCode stores the catalog in ~/.local/share/opencode/opencode.db
        import sqlite3

        db_path = Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db'
        if not db_path.exists():
            return [], 'no catalog cache file'

        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Query the models catalog from kv store
            cursor.execute(
                "SELECT value FROM kv WHERE key = 'models-dev:catalog' OR key = 'models:catalog' LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()

            if not row:
                return [], 'catalog cache is empty'

            # Parse the JSON value; validate the shape before trusting it —
            # a well-formed file can still carry a non-dict body.
            catalog_data = json.loads(row[0])
            if not isinstance(catalog_data, dict) or not isinstance(catalog_data.get('body'), str):
                return [], 'catalog cache has an unexpected structure'
            body = json.loads(catalog_data['body'])
            if not isinstance(body, dict):
                return [], 'catalog cache body is not a mapping'

            # Extract model IDs from all providers. The catalog keys models
            # per provider (bare IDs); namespace them as provider/model so a
            # saved selection stays executable by the CLI, which uses the
            # same namespace, and drop duplicates.
            models: list[str] = []
            seen: set[str] = set()
            for provider_key, provider_data in body.items():
                if not isinstance(provider_key, str) or not isinstance(provider_data, dict):
                    continue
                provider_models = provider_data.get('models')
                if not isinstance(provider_models, dict):
                    continue
                for model_id, model_info in provider_models.items():
                    if not isinstance(model_id, str) or not model_id.strip():
                        continue
                    # Only include non-deprecated models
                    if isinstance(model_info, dict) and model_info.get('status') == 'deprecated':
                        continue
                    namespaced = f'{provider_key}/{model_id}'
                    if namespaced not in seen:
                        seen.add(namespaced)
                        models.append(namespaced)

            if not models:
                return [], 'catalog cache has no models'
            return models, None

        except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError):
            # Corrupt or unreadable cache — say so rather than guess.
            return [], 'catalog cache is corrupt or unreadable'

    @staticmethod
    def _parse_claude_effort_levels(help_output: str) -> tuple[bool, list[str]]:
        """Parse `--effort <level>` options from `claude --help` output."""
        if not help_output or '--effort' not in help_output:
            return False, []
        lines = help_output.splitlines()
        block_lines: list[str] = []
        capturing = False
        for line in lines:
            if '--effort' in line:
                capturing = True
                block_lines.append(line)
                continue
            if capturing:
                if line.lstrip().startswith('-'):
                    break
                block_lines.append(line)
                if len(block_lines) >= 4:
                    break
        block = ' '.join(part.strip() for part in block_lines)
        match = re.search(r'\(([a-zA-Z0-9_.,\s-]+)\)', block)
        levels: list[str] = []
        if match:
            for item in match.group(1).split(','):
                cleaned = item.strip()
                if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,31}', cleaned) and cleaned not in levels:
                    levels.append(cleaned)
        return True, levels

    @staticmethod
    def _detect_claude_code() -> BackendInfo | None:
        """Detect Claude Code installation."""
        success, output = BackendDetector._run_command(['claude', '--version'])
        if not success:
            return None

        version = output.split('\n')[0] if output else 'unknown'

        help_ok, help_output = BackendDetector._run_command(['claude', '--help'], timeout=5)
        has_effort, effort_levels = (
            BackendDetector._parse_claude_effort_levels(help_output) if help_ok else (False, [])
        )
        if has_effort and effort_levels:
            reasoning_supported = True
            reasoning_source = BackendInfo.REASONING_SOURCE_CLI_HELP
            reasoning_note = (
                'Parsed from `claude --help` (--effort); effective support depends on the selected Claude model.'
            )
        elif has_effort:
            reasoning_supported = False
            reasoning_source = BackendInfo.REASONING_SOURCE_UNKNOWN
            reasoning_note = (
                '`claude --help` exposes --effort but did not list allowed levels; '
                'CLI default will be used unless configured explicitly.'
            )
        else:
            reasoning_supported = False
            reasoning_source = BackendInfo.REASONING_SOURCE_UNSUPPORTED
            reasoning_note = 'This Claude Code CLI version did not advertise --effort in `--help`.'

        # `claude` has no command to enumerate models (only --model), so we
        # report none instead of inventing names. The backend still runs with
        # whatever model Claude Code defaults to or is configured with.
        return BackendInfo(
            'claude-code', 'claude', version, [],
            models_source=BackendInfo.SOURCE_UNKNOWN,
            models_note=(
                'Claude Code exposes no model-listing command; it uses its own '
                'default or the model set via `claude config` / --model.'
            ),
            reasoning_supported=reasoning_supported,
            reasoning_levels=effort_levels,
            reasoning_default=None,
            reasoning_source=reasoning_source,
            reasoning_note=reasoning_note,
        )

    @staticmethod
    def _detect_copilot() -> BackendInfo | None:
        """Detect GitHub Copilot via gh CLI."""
        # First check if gh exists
        if not shutil.which('gh'):
            return None

        # Check if copilot extension is installed
        success, output = BackendDetector._run_command(['gh', 'copilot', '--help'])
        if not success:
            return None

        # Get gh version
        success, version_output = BackendDetector._run_command(['gh', '--version'])
        version = version_output.split('\n')[0] if version_output else 'unknown'

        # The Copilot CLI has no model enumeration; the service picks the
        # model. Report none rather than a guessed list.
        return BackendInfo(
            'copilot', 'gh', version, [],
            models_source=BackendInfo.SOURCE_UNKNOWN,
            models_note='Copilot CLI does not expose model selection; the service chooses the model.',
            reasoning_supported=False,
            reasoning_levels=[],
            reasoning_default=None,
            reasoning_source=BackendInfo.REASONING_SOURCE_UNSUPPORTED,
            reasoning_note='Copilot CLI does not expose a reasoning effort option.',
        )

    @staticmethod
    def _detect_codex() -> BackendInfo | None:
        """Detect Codex CLI installation."""
        success, output = BackendDetector._run_command(['codex', '--version'])
        if not success:
            return None

        version = output.split('\n')[0] if output else 'unknown'

        # `codex` has no model-listing command either; its own cache/config
        # files are the only real sources. No hardcoded fallback.
        models, source, note = BackendDetector._probe_codex_models()
        r_supported, r_levels, r_default, r_source, r_note, model_reasoning = (
            BackendDetector._probe_codex_reasoning()
        )
        return BackendInfo(
            'codex', 'codex', version, models,
            models_source=source,
            models_note=note,
            reasoning_supported=r_supported,
            reasoning_levels=r_levels,
            reasoning_default=r_default,
            reasoning_source=r_source,
            reasoning_note=r_note,
            model_reasoning=model_reasoning,
        )

    @staticmethod
    def detect_backend(name: str) -> BackendInfo | None:
        """Detect a single backend by name without probing unrelated CLIs."""
        mapping = {
            'opencode': ('opencode', BackendDetector._detect_opencode),
            'claude-code': ('claude', BackendDetector._detect_claude_code),
            'copilot': ('gh', BackendDetector._detect_copilot),
            'codex': ('codex', BackendDetector._detect_codex),
        }
        spec = mapping.get(name)
        if spec is None:
            return None
        cmd, fn = spec
        if not shutil.which(cmd):
            return None
        return BackendDetector._detect_safely(name, cmd, fn)

    @staticmethod
    def _codex_home() -> Path:
        raw = os.environ.get('CODEX_HOME', '').strip()
        if raw:
            return Path(raw).expanduser()
        return Path.home() / '.codex'

    @staticmethod
    def _probe_codex_reasoning() -> tuple[
        bool, list[str], str | None, str, str | None, dict[str, dict[str, Any]]
    ]:
        """Read per-model reasoning levels from $CODEX_HOME/models_cache.json."""
        canonical_order = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
        cache_path = BackendDetector._codex_home() / 'models_cache.json'
        if not cache_path.exists():
            return (
                False,
                [],
                None,
                BackendInfo.REASONING_SOURCE_UNKNOWN,
                'Codex model cache (~/.codex/models_cache.json) not found; '
                'reasoning levels are hidden and the CLI default will be used.',
                {},
            )
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return (
                False,
                [],
                None,
                BackendInfo.REASONING_SOURCE_UNKNOWN,
                'Codex model cache (~/.codex/models_cache.json) is unreadable; '
                'reasoning levels are hidden and the CLI default will be used.',
                {},
            )

        model_reasoning: dict[str, dict[str, Any]] = {}
        union_levels: list[str] = []
        if isinstance(data, dict) and isinstance(data.get('models'), list):
            for model in data['models']:
                if not isinstance(model, dict):
                    continue
                slug = model.get('slug')
                if not isinstance(slug, str) or not slug.strip():
                    continue
                visibility = model.get('visibility', 'list')
                if not isinstance(visibility, str) or visibility not in ('list', 'default'):
                    continue
                raw_levels = model.get('supported_reasoning_levels')
                levels: list[str] = []
                if isinstance(raw_levels, list):
                    for item in raw_levels:
                        effort = item.get('effort') if isinstance(item, dict) else item
                        if isinstance(effort, str):
                            cleaned = effort.strip()
                            if cleaned and cleaned not in levels:
                                levels.append(cleaned)
                raw_default = model.get('default_reasoning_level')
                default_level = (
                    raw_default.strip()
                    if isinstance(raw_default, str) and raw_default.strip()
                    else None
                )
                if levels:
                    model_reasoning[slug] = {'levels': levels, 'default': default_level}
                    for lvl in levels:
                        if lvl not in union_levels:
                            union_levels.append(lvl)

        if not union_levels:
            return (
                False,
                [],
                None,
                BackendInfo.REASONING_SOURCE_UNKNOWN,
                'Codex model cache did not include supported_reasoning_levels; '
                'reasoning levels are hidden and the CLI default will be used.',
                {},
            )

        ordered_levels = [lvl for lvl in canonical_order if lvl in union_levels] + [
            lvl for lvl in union_levels if lvl not in canonical_order
        ]
        # Backend-level default is None because Codex defaults vary per model
        # (callers should read model_reasoning[slug]['default']).
        return (
            True,
            ordered_levels,
            None,
            BackendInfo.REASONING_SOURCE_CACHE,
            "Read from Codex's local models_cache.json (passed via `-c model_reasoning_effort=<level>`).",
            model_reasoning,
        )

    @staticmethod
    def _probe_codex_models() -> tuple[list[str], str, str | None]:
        """Read models from Codex's own cache/config; returns (models, source, note)."""
        # First try models_cache.json (written by Codex itself). A well-formed
        # file can still carry the wrong structure (models: null, slug: null),
        # so validate shapes instead of trusting the JSON to be typed.
        codex_home = BackendDetector._codex_home()
        cache_path = codex_home / 'models_cache.json'
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                models = []
                if isinstance(data, dict) and isinstance(data.get('models'), list):
                    for model in data['models']:
                        if not isinstance(model, dict):
                            continue
                        slug = model.get('slug')
                        if not isinstance(slug, str) or not slug.strip():
                            continue
                        visibility = model.get('visibility', 'list')
                        if isinstance(visibility, str) and visibility in ('list', 'default'):
                            models.append(slug)
                if models:
                    return (
                        models, BackendInfo.SOURCE_CACHE,
                        "From Codex's own model cache; per-model call "
                        'availability is not verified.',
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # Secondary: the model explicitly set in config.toml. Parse it as
        # real TOML — line-prefix matching misread model_provider="my-proxy"
        # and model_reasoning_effort as model names and swallowed inline
        # comments. Only the top-level `model` key counts.
        config_path = codex_home / 'config.toml'
        if config_path.exists():
            try:
                import tomllib

                with open(config_path, 'rb') as f:
                    config = tomllib.load(f)
                model = config.get('model')
                if isinstance(model, str) and model.strip():
                    return (
                        [model.strip()], BackendInfo.SOURCE_CONFIG,
                        'The model configured in ~/.codex/config.toml; '
                        'call availability is not verified.',
                    )
            except (tomllib.TOMLDecodeError, OSError, ValueError):
                pass

        return (
            [], BackendInfo.SOURCE_UNKNOWN,
            'Codex exposes no model-listing command and no usable cache/config was found; '
            'the backend runs with its own default or the model set in ~/.codex/config.toml.',
        )


def main() -> None:
    """CLI tool to test backend detection."""
    print('Detecting AI backends...\n')

    backends = BackendDetector.detect_all()

    if not backends:
        print('No AI backend found.')
        print('\nPlease install one of:')
        print('  • OpenCode: https://github.com/opencode-ai/opencode')
        print('  • Claude Code: https://docs.anthropic.com/claude-code')
        print('  • GitHub Copilot: gh extension install github/gh-copilot')
        print('  • Codex CLI: https://github.com/openai/codex-cli')
        sys.exit(1)

    print(f'Found {len(backends)} backend(s):\n')

    for i, backend in enumerate(backends, 1):
        print(f'{i}. {backend.name}')
        print(f'   Command: {backend.command}')
        print(f'   Version: {backend.version}')
        if backend.models:
            print(f'   Models: {", ".join(backend.models[:3])}{"..." if len(backend.models) > 3 else ""}')
            print(f'   Source: {backend.models_source}')
            if not backend.models_call_verified:
                print('   Call availability: not verified (enumeration only)')
        elif backend.models_note:
            print(f'   Models: (not enumerated — {backend.models_note})')
        print()


if __name__ == '__main__':
    main()
