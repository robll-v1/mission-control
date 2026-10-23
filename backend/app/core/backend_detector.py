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
    """Information about a detected backend."""

    def __init__(self, name: str, command: str, version: str, models: list[str]):
        self.name = name
        self.command = command
        self.version = version
        self.models = models

    def __repr__(self) -> str:
        return f'BackendInfo({self.name}, version={self.version}, models={len(self.models)})'


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

        # Try to read models from OpenCode database
        models = BackendDetector._probe_opencode_models()

        # Apply smart filtering if we got a huge list
        if len(models) > 100:
            models = ModelFilter.filter_models(models, max_models=50)
        elif not models:
            # Comprehensive model list including latest GPT-6 and Claude 5.5 models
            models = [
                # Claude 5.5 family
                'claude-opus-5.5',
                'claude-sonnet-5.5',
                'claude-haiku-5.5',
                'claude-fable-5.1',
                # GPT-6 family (latest)
                'gpt-6',
                'gpt-6-turbo',
                'gpt-6-mini',
                # GPT-5 family
                'gpt-5',
                'gpt-5-turbo',
                # GPT-4 family
                'gpt-4',
                'gpt-4-turbo',
                'gpt-4o',
                'gpt-4o-mini',
                # DeepSeek
                'deepseek-v3',
                'deepseek-r1',
                # Gemini
                'gemini-2.5-pro',
                'gemini-2.5-flash',
            ]

        return BackendInfo('opencode', 'opencode', version, models)

    @staticmethod
    def _probe_opencode_models() -> list[str]:
        """Try to read available models from OpenCode database."""
        # OpenCode stores models in ~/.local/share/opencode/opencode.db
        import sqlite3

        db_path = Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db'
        if not db_path.exists():
            return []

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
                return []

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

            return models

        except (sqlite3.Error, json.JSONDecodeError, KeyError):
            # Return empty on error - fallback handled by caller
            return []

    @staticmethod
    def _detect_claude_code() -> BackendInfo | None:
        """Detect Claude Code installation."""
        success, output = BackendDetector._run_command(['claude', '--version'])
        if not success:
            return None

        version = output.split('\n')[0] if output else 'unknown'

        # Claude Code model list - current as of 2026
        models = [
            'claude-opus-5.5',
            'claude-sonnet-5.5',
            'claude-haiku-5.5',
            'claude-fable-5.1',
        ]

        return BackendInfo('claude-code', 'claude', version, models)

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

        # Copilot uses GPT models - updated to latest
        models = [
            'gpt-6',
            'gpt-6-turbo',
            'gpt-5',
            'gpt-5-turbo',
            'gpt-4',
            'gpt-4-turbo',
            'gpt-4o',
            'gpt-4o-mini',
            'gpt-3.5-turbo',
        ]

        return BackendInfo('copilot', 'gh', version, models)

    @staticmethod
    def _detect_codex() -> BackendInfo | None:
        """Detect Codex CLI installation."""
        success, output = BackendDetector._run_command(['codex', '--version'])
        if not success:
            return None

        version = output.split('\n')[0] if output else 'unknown'

        # Try to read models from Codex config
        models = BackendDetector._probe_codex_models()
        if not models:
            # Comprehensive GPT model list
            models = [
                'gpt-6',
                'gpt-6-turbo',
                'gpt-6-mini',
                'gpt-5',
                'gpt-5-turbo',
                'gpt-4',
                'gpt-4-turbo',
                'gpt-4o',
                'gpt-4o-mini',
                'gpt-3.5-turbo',
            ]

        return BackendInfo('codex', 'codex', version, models)

    @staticmethod
    def _probe_codex_models() -> list[str]:
        """Try to read available models from Codex models cache."""
        # First try models_cache.json (most accurate)
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
                            return models
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback: try config.toml
        config_path = Path.home() / '.codex' / 'config.toml'
        if not config_path.exists():
            return []

        try:
            content = config_path.read_text()
            # Simple TOML parsing for model list
            models = []
            in_models_section = False
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('[') and 'model' in line.lower():
                    in_models_section = True
                elif line.startswith('['):
                    in_models_section = False
                elif in_models_section and '=' in line:
                    # Extract model name from lines like: model = "gpt-4"
                    if 'model' in line.lower():
                        parts = line.split('=')
                        if len(parts) == 2:
                            model = parts[1].strip().strip('"').strip("'")
                            if model and model not in models:
                                models.append(model)
            return models
        except (OSError, UnicodeDecodeError):
            return []


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
        print()


if __name__ == '__main__':
    main()
