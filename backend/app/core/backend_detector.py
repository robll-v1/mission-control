"""Auto-detect available AI backends and their models."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class BackendInfo:
    """Information about a detected backend.

    ``models_source`` says where the model list came from so callers can tell
    an account-validated list apart from a local cache, and ``models_note``
    explains why the list is empty when it is. An empty list never means the
    backend is unusable — it only means we could not enumerate its models.
    """

    SOURCE_CLI = 'cli'                # live CLI query, reflects current login
    SOURCE_CATALOG_CACHE = 'catalog_cache'  # full public catalog, NOT account-validated
    SOURCE_CACHE = 'cache'            # backend's own local cache file
    SOURCE_CONFIG = 'config'          # model explicitly set in backend config
    SOURCE_UNKNOWN = 'unknown'        # no reliable enumeration available

    def __init__(
        self,
        name: str,
        command: str,
        version: str,
        models: list[str],
        models_source: str = SOURCE_UNKNOWN,
        models_note: str | None = None,
    ):
        self.name = name
        self.command = command
        self.version = version
        self.models = models
        self.models_source = models_source
        self.models_note = models_note

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
    def detect_all() -> list[BackendInfo]:
        """Detect all available backends.

        Returns:
            List of BackendInfo objects for each detected backend.
        """
        backends = []

        # OpenCode
        if shutil.which('opencode'):
            info = BackendDetector._detect_opencode()
            if info:
                backends.append(info)

        # Claude Code
        if shutil.which('claude'):
            info = BackendDetector._detect_claude_code()
            if info:
                backends.append(info)

        # GitHub Copilot
        if shutil.which('gh'):
            info = BackendDetector._detect_copilot()
            if info:
                backends.append(info)

        # Codex CLI
        if shutil.which('codex'):
            info = BackendDetector._detect_codex()
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

        # `opencode models` is the authoritative source: it lists what the
        # current login can actually use. No hardcoded fallback — if we cannot
        # enumerate, we say so instead of inventing model names.
        models, note = BackendDetector._probe_opencode_models_cli()
        if models:
            return BackendInfo(
                'opencode', 'opencode', version, models,
                models_source=BackendInfo.SOURCE_CLI, models_note=None,
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
                    'List from the local models-dev catalog cache; '
                    'not validated against the current account.'
                ),
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
        )

    @staticmethod
    def _probe_opencode_models_cli() -> tuple[list[str], str | None]:
        """Ask the CLI directly; returns (models, failure reason)."""
        success, output = BackendDetector._run_command(['opencode', 'models'], timeout=15)
        if not success:
            return [], 'command failed or timed out'
        # Output is one `provider/model` per line; ignore any header noise.
        models = [
            line.strip()
            for line in output.splitlines()
            if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', line.strip())
        ]
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

            # Parse the JSON value
            catalog_data = json.loads(row[0])
            body = json.loads(catalog_data['body'])

            # Extract model IDs from all providers
            models = []
            for provider_key, provider_data in body.items():
                if isinstance(provider_data, dict) and 'models' in provider_data:
                    for model_id, model_info in provider_data['models'].items():
                        # Only include non-deprecated models
                        if model_info.get('status') != 'deprecated':
                            models.append(model_id)

            if not models:
                return [], 'catalog cache has no models'
            return models, None

        except (sqlite3.Error, json.JSONDecodeError, KeyError):
            # Corrupt or unreadable cache — say so rather than guess.
            return [], 'catalog cache is corrupt or unreadable'

    @staticmethod
    def _detect_claude_code() -> BackendInfo | None:
        """Detect Claude Code installation."""
        success, output = BackendDetector._run_command(['claude', '--version'])
        if not success:
            return None

        version = output.split('\n')[0] if output else 'unknown'

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
        return BackendInfo('codex', 'codex', version, models, models_source=source, models_note=note)

    @staticmethod
    def _probe_codex_models() -> tuple[list[str], str, str | None]:
        """Read models from Codex's own cache/config; returns (models, source, note)."""
        # First try models_cache.json (written by Codex itself)
        cache_path = Path.home() / '.codex' / 'models_cache.json'
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    data = json.load(f)
                    if 'models' in data:
                        # Extract model slugs from cache
                        models = []
                        for model in data['models']:
                            if isinstance(model, dict) and 'slug' in model:
                                # Filter out models marked as hidden
                                visibility = model.get('visibility', 'list')
                                if visibility in ('list', 'default'):
                                    models.append(model['slug'])
                        if models:
                            return models, BackendInfo.SOURCE_CACHE, None
            except (json.JSONDecodeError, OSError):
                pass

        # Secondary: the model explicitly set in config.toml
        config_path = Path.home() / '.codex' / 'config.toml'
        if config_path.exists():
            try:
                content = config_path.read_text()
                # Simple TOML parsing for the configured model
                for line in content.split('\n'):
                    line = line.strip()
                    if line.startswith('model') and '=' in line and not line.startswith('['):
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            model = parts[1].strip().strip('"').strip("'")
                            if model:
                                return [model], BackendInfo.SOURCE_CONFIG, None
            except (OSError, UnicodeDecodeError):
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
        elif backend.models_note:
            print(f'   Models: (not enumerated — {backend.models_note})')
        print()


if __name__ == '__main__':
    main()
