"""Adapter registry — maps backend names to RunnerAdapter implementations."""

from __future__ import annotations

from app.adapters.base import RunnerAdapter
from app.adapters.opencode_adapter import OpenCodeAdapter


_REGISTRY: dict[str, type[RunnerAdapter]] = {
    'opencode': OpenCodeAdapter,
}

AVAILABLE_BACKENDS: list[str] = []


def _rebuild_list() -> None:
    global AVAILABLE_BACKENDS
    AVAILABLE_BACKENDS = sorted(_REGISTRY.keys())


_rebuild_list()


def register(name: str, cls: type[RunnerAdapter]) -> None:
    _REGISTRY[name] = cls
    _rebuild_list()


def get_adapter(
    name: str,
    *,
    model: str | None = None,
    variant: str | None = None,
    reasoning: str | None = None,
) -> RunnerAdapter:
    """Instantiate an adapter by backend name.

    ``reasoning`` is the preferred parameter; ``variant`` remains a
    backward-compatible alias. Raises KeyError if name is not registered.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f'Unknown backend: {name!r}. Available: {AVAILABLE_BACKENDS}')
    import inspect
    sig = inspect.signature(cls.__init__)
    eff_reasoning = reasoning if reasoning is not None else variant
    kwargs: dict = {}
    if 'model' in sig.parameters:
        kwargs['model'] = model
    if 'reasoning' in sig.parameters:
        kwargs['reasoning'] = eff_reasoning
    if 'variant' in sig.parameters:
        kwargs['variant'] = variant if variant is not None else eff_reasoning
    return cls(**kwargs)


def _auto_register() -> None:
    """Import all adapter modules to trigger registration."""
    try:
        from app.adapters.claude_code_adapter import ClaudeCodeAdapter
        _REGISTRY['claude-code'] = ClaudeCodeAdapter
    except ImportError:
        pass
    try:
        from app.adapters.copilot_adapter import CopilotAdapter
        _REGISTRY['copilot'] = CopilotAdapter
    except ImportError:
        pass
    try:
        from app.adapters.codex_adapter import CodexAdapter
        _REGISTRY['codex'] = CodexAdapter
    except ImportError:
        pass
    _rebuild_list()


_auto_register()
