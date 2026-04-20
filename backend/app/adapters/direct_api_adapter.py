"""
Direct LLM API Adapter — calls OpenAI-compatible endpoints via HTTP.

Bypasses agent CLI subprocess entirely. Used by MCP server to avoid
nested subprocess issues (e.g., opencode-inside-opencode).

Supports two wire formats:
- "responses": OpenAI Responses API (streaming SSE) — used by Codex CLI
- "chat": OpenAI Chat Completions API (streaming SSE)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import httpx


@dataclass
class DirectAPIConfig:
    """Configuration for direct LLM API access."""
    base_url: str
    api_key: str
    model: str
    wire_api: str = 'responses'  # 'responses' or 'chat'
    timeout: float = 300.0  # 5 minutes per request
    max_tokens: int = 16384
    temperature: float = 0.1

    @classmethod
    def from_amc_config(cls, cfg: dict) -> 'DirectAPIConfig':
        """Load from amc config dict (backend.direct_api section)."""
        direct = cfg.get('backend', {}).get('direct_api', {})
        return cls(
            base_url=direct.get('base_url', ''),
            api_key=direct.get('api_key', ''),
            model=direct.get('model', ''),
            wire_api=direct.get('wire_api', 'responses'),
            timeout=float(direct.get('timeout', 300)),
            max_tokens=int(direct.get('max_tokens', 16384)),
            temperature=float(direct.get('temperature', 0.1)),
        )

    @classmethod
    def from_codex_config(cls, path: str | None = None) -> 'DirectAPIConfig | None':
        """Auto-detect API settings from user's Codex config.toml."""
        if path is None:
            path = os.path.expanduser('~/.codex/config.toml')
        if not os.path.exists(path):
            return None
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                return None
        try:
            with open(path, 'rb') as f:
                cfg = tomllib.load(f)
            # Read model provider config
            provider_name = cfg.get('model_provider', 'OpenAI')
            providers = cfg.get('model_providers', {})
            provider_cfg = providers.get(provider_name, {})
            base_url = provider_cfg.get('base_url', '')
            wire_api = provider_cfg.get('wire_api', 'responses')
            model = cfg.get('model', '')
            # API key from auth.json
            api_key = ''
            auth_path = os.path.expanduser('~/.codex/auth.json')
            if os.path.exists(auth_path):
                with open(auth_path) as f:
                    auth = json.load(f)
                api_key = auth.get('OPENAI_API_KEY', '')
            if base_url and api_key:
                return cls(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    wire_api=wire_api,
                )
        except (OSError, ValueError, KeyError):
            pass
        return None

    @classmethod
    def from_opencode_config(cls, path: str | None = None) -> 'DirectAPIConfig | None':
        """Auto-detect API settings from user's opencode.json."""
        if path is None:
            path = os.path.expanduser('~/.config/opencode/opencode.json')
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                oc = json.load(f)
            providers = oc.get('provider', {})
            openai_cfg = providers.get('openai', {}).get('options', {})
            if openai_cfg.get('baseURL') and openai_cfg.get('apiKey'):
                return cls(
                    base_url=openai_cfg['baseURL'],
                    api_key=openai_cfg['apiKey'],
                    model='',
                    wire_api='chat',
                )
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def is_valid(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


class DirectAPIAdapter:
    """Calls LLM API directly via HTTP. Supports Responses API and Chat Completions."""

    def __init__(self, config: DirectAPIConfig):
        self.config = config

    def call_llm(self, prompt: str, *, system: str = '') -> str:
        """Synchronous LLM API call. Returns the assistant's response text."""
        if self.config.wire_api == 'responses':
            return self._call_responses_api(prompt, system=system)
        else:
            return self._call_chat_api(prompt, system=system)

    async def async_call_llm(self, prompt: str, *, system: str = '') -> str:
        """Async LLM API call. Returns the assistant's response text."""
        if self.config.wire_api == 'responses':
            return await self._async_call_responses_api(prompt, system=system)
        else:
            return await self._async_call_chat_api(prompt, system=system)

    # ── Responses API (streaming SSE) ─────────────────────────────────

    def _call_responses_api(self, prompt: str, *, system: str = '') -> str:
        """Call OpenAI Responses API with streaming."""
        url = f"{self.config.base_url.rstrip('/')}/responses"
        payload = self._build_responses_payload(prompt, system=system)
        headers = self._build_headers()

        with httpx.Client(timeout=self.config.timeout) as client:
            with client.stream('POST', url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                return self._collect_responses_stream(resp.iter_lines())

    async def _async_call_responses_api(self, prompt: str, *, system: str = '') -> str:
        """Async call to OpenAI Responses API with streaming."""
        url = f"{self.config.base_url.rstrip('/')}/responses"
        payload = self._build_responses_payload(prompt, system=system)
        headers = self._build_headers()

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            async with client.stream('POST', url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                return await self._async_collect_responses_stream(resp.aiter_lines())

    def _build_responses_payload(self, prompt: str, *, system: str = '') -> dict:
        input_msgs = []
        if system:
            input_msgs.append({'role': 'developer', 'content': system})
        input_msgs.append({'role': 'user', 'content': prompt})
        return {
            'model': self.config.model,
            'input': input_msgs,
            'stream': True,
            'max_output_tokens': self.config.max_tokens,
            'temperature': self.config.temperature,
        }

    @staticmethod
    def _collect_responses_stream(lines) -> str:
        """Collect text from Responses API SSE stream."""
        full_text = ''
        for line in lines:
            if not line.strip() or not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str == '[DONE]':
                break
            try:
                evt = json.loads(data_str)
                if evt.get('type') == 'response.output_text.delta':
                    full_text += evt.get('delta', '')
            except json.JSONDecodeError:
                pass
        return full_text

    @staticmethod
    async def _async_collect_responses_stream(lines) -> str:
        """Async collect text from Responses API SSE stream."""
        full_text = ''
        async for line in lines:
            if not line.strip() or not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str == '[DONE]':
                break
            try:
                evt = json.loads(data_str)
                if evt.get('type') == 'response.output_text.delta':
                    full_text += evt.get('delta', '')
            except json.JSONDecodeError:
                pass
        return full_text

    # ── Chat Completions API (streaming SSE) ──────────────────────────

    def _call_chat_api(self, prompt: str, *, system: str = '') -> str:
        """Call OpenAI Chat Completions API with streaming."""
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        payload = self._build_chat_payload(prompt, system=system)
        headers = self._build_headers()

        with httpx.Client(timeout=self.config.timeout) as client:
            with client.stream('POST', url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                return self._collect_chat_stream(resp.iter_lines())

    async def _async_call_chat_api(self, prompt: str, *, system: str = '') -> str:
        """Async call to Chat Completions API with streaming."""
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        payload = self._build_chat_payload(prompt, system=system)
        headers = self._build_headers()

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            async with client.stream('POST', url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                return await self._async_collect_chat_stream(resp.aiter_lines())

    def _build_chat_payload(self, prompt: str, *, system: str = '') -> dict:
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        return {
            'model': self.config.model,
            'messages': messages,
            'stream': True,
            'max_tokens': self.config.max_tokens,
            'temperature': self.config.temperature,
        }

    @staticmethod
    def _collect_chat_stream(lines) -> str:
        """Collect text from Chat Completions SSE stream."""
        full_text = ''
        for line in lines:
            if not line.strip() or not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str == '[DONE]':
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk.get('choices', [{}])[0].get('delta', {})
                content = delta.get('content', '')
                if content:
                    full_text += content
            except (json.JSONDecodeError, IndexError):
                pass
        return full_text

    @staticmethod
    async def _async_collect_chat_stream(lines) -> str:
        """Async collect text from Chat Completions SSE stream."""
        full_text = ''
        async for line in lines:
            if not line.strip() or not line.startswith('data: '):
                continue
            data_str = line[6:]
            if data_str == '[DONE]':
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk.get('choices', [{}])[0].get('delta', {})
                content = delta.get('content', '')
                if content:
                    full_text += content
            except (json.JSONDecodeError, IndexError):
                pass
        return full_text

    # ── Shared helpers ────────────────────────────────────────────────

    def _build_headers(self) -> dict:
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.config.api_key}',
        }


def resolve_direct_api_config(amc_cfg: dict) -> DirectAPIConfig:
    """Resolve DirectAPIConfig from amc config + codex/opencode fallback.

    Priority:
    1. Explicit backend.direct_api section in amc config
    2. Auto-detect from ~/.codex/config.toml (Codex CLI — preferred)
    3. Auto-detect from opencode.json
    4. Environment variables (AMC_API_BASE_URL, AMC_API_KEY, AMC_MODEL)
    """
    # Try explicit config
    cfg = DirectAPIConfig.from_amc_config(amc_cfg)
    if cfg.is_valid():
        return cfg

    # Try Codex config (preferred — has correct wire_api and model)
    codex_cfg = DirectAPIConfig.from_codex_config()
    if codex_cfg and codex_cfg.is_valid():
        return codex_cfg

    # Try opencode.json fallback
    oc_cfg = DirectAPIConfig.from_opencode_config()

    # Merge: use amc model if available, detected credentials as fallback
    backend_cfg = amc_cfg.get('backend', {})
    default_backend = backend_cfg.get('default', 'opencode')
    model = (
        backend_cfg.get('direct_api', {}).get('model')
        or backend_cfg.get('model')
        or backend_cfg.get(default_backend, {}).get('model')
        or (codex_cfg.model if codex_cfg else '')
        or os.environ.get('AMC_MODEL', '')
    )

    base_url = (
        cfg.base_url
        or (codex_cfg.base_url if codex_cfg else '')
        or (oc_cfg.base_url if oc_cfg else '')
        or os.environ.get('AMC_API_BASE_URL', '')
    )
    api_key = (
        cfg.api_key
        or (codex_cfg.api_key if codex_cfg else '')
        or (oc_cfg.api_key if oc_cfg else '')
        or os.environ.get('AMC_API_KEY', '')
    )
    wire_api = (
        cfg.wire_api
        if cfg.base_url  # only use amc wire_api if base_url was explicitly set
        else (codex_cfg.wire_api if codex_cfg else 'responses')
    )

    return DirectAPIConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        wire_api=wire_api,
    )
