"""API routes for configuration management."""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.adapters import get_adapter
from app.core.backend_detector import BackendDetector, ModelFilter
from app.core.config import (
    _MAX_CONFIG_BYTES,
    DEFAULT_CONFIG,
    UNSET,
    ConfigError,
    SettingsResolver,
    global_config_lock,
    load_global_config,
    save_global_config,
)
from app.core.proc import run_text

router = APIRouter(prefix='/api/config', tags=['config'])


class ModelReasoningResponse(BaseModel):
    levels: list[str] = Field(default_factory=list)
    default: str | None = None


class BackendResponse(BaseModel):
    """Response model for backend detection."""
    name: str
    version: str
    models: list[str]
    command: str
    model_count: int  # Total models before filtering
    grouped_models: dict[str, list[str]] | None = None  # Optional grouped view
    # Where the model list came from ('cli' / 'catalog_cache' / 'cache' /
    # 'config' / 'unknown') and why it is empty when it is. Additive fields:
    # existing clients keep working and just ignore them.
    models_source: str = 'unknown'
    models_note: str | None = None
    # Enumeration never proves usability: detection makes no model calls, so
    # this stays false until a real review round has run that model.
    models_call_verified: bool = False
    # P2 additive reasoning capabilities:
    reasoning_supported: bool = False
    reasoning_levels: list[str] = Field(default_factory=list)
    reasoning_default: str | None = None
    reasoning_source: str = 'unsupported'
    reasoning_note: str | None = None
    model_reasoning: dict[str, ModelReasoningResponse] = Field(default_factory=dict)


class DetectResponse(BaseModel):
    """Response for backend detection endpoint."""
    backends: list[BackendResponse]


class SaveConfigRequest(BaseModel):
    """Request model for saving configuration.

    Uses ``model_fields_set`` so callers can distinguish omitting a field
    (keep existing saved value) from explicitly passing ``null`` / ``""``
    (reset to CLI / Model default).
    """
    backend: str
    model: str | None = None
    reasoning: str | None = None
    variant: str | None = None


class SaveConfigResponse(BaseModel):
    """Response for save config endpoint."""
    status: str
    message: str
    config_path: str | None = None


class CheckAgentRequest(BaseModel):
    """Request model for static CLI agent check (no LLM calls)."""
    backend: str
    model: str | None = None
    reasoning: str | None = None


class CheckAgentResponse(BaseModel):
    ok: bool
    backend: str
    executable: str
    command: list[str]
    exit_code: int | None = None
    message: str
    model: str | None = None
    reasoning: str | None = None


@router.get('/detect', response_model=DetectResponse)
def detect_backends():
    """Detect all available AI backends on the system.

    Returns:
        List of detected backends with their versions, available models, and
        reasoning capabilities.
    """
    try:
        backends = BackendDetector.detect_all()

        return DetectResponse(
            backends=[
                BackendResponse(
                    name=b.name,
                    version=b.version,
                    models=b.models,
                    models_source=b.models_source,
                    models_note=b.models_note,
                    models_call_verified=b.models_call_verified,
                    command=b.command,
                    model_count=len(b.models),
                    grouped_models=ModelFilter.group_models(b.models) if len(b.models) > 20 else None,
                    reasoning_supported=b.reasoning_supported,
                    reasoning_levels=b.reasoning_levels,
                    reasoning_default=b.reasoning_default,
                    reasoning_source=b.reasoning_source,
                    reasoning_note=b.reasoning_note,
                    model_reasoning={
                        slug: ModelReasoningResponse(
                            levels=list(info.get('levels') or []),
                            default=info.get('default'),
                        )
                        for slug, info in b.model_reasoning.items()
                    },
                )
                for b in backends
            ]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to detect backends: {str(e)}')


@router.get('/current')
def get_current_config():
    """Get the current global configuration.

    Returns:
        Current configuration (plus per-backend saved models/reasoning) or
        default config if none exists.
    """
    try:
        config = load_global_config()
        defaults = DEFAULT_CONFIG['backend']
        section = config.get('backend', {}) if isinstance(config, dict) else {}
        if not isinstance(section, dict):
            section = {}
        raw_default = section.get('default')
        backend = (
            raw_default.strip()
            if isinstance(raw_default, str) and raw_default.strip()
            else defaults['default']
        )
        selected = section.get(backend, {})
        if not isinstance(selected, dict):
            selected = {}

        def _clean_str(val: Any) -> str | None:
            if not isinstance(val, str):
                return None
            return val.strip() or None

        def _entry_view(entry: Any) -> dict[str, str | None]:
            if not isinstance(entry, dict):
                return {'model': None, 'reasoning': None, 'variant': None}
            model_val = _clean_str(entry.get('model'))
            reasoning_val = _clean_str(entry.get('reasoning')) or _clean_str(entry.get('variant'))
            return {
                'model': model_val,
                'reasoning': reasoning_val,
                'variant': reasoning_val,
            }

        per_backend = {
            key: _entry_view(val)
            for key, val in section.items()
            if key != 'default' and isinstance(val, dict)
        }
        selected_view = _entry_view(selected)
        return {'config': {
            'backend': backend,
            'model': selected_view['model'],
            'reasoning': selected_view['reasoning'],
            'variant': selected_view['variant'],
            'per_backend': per_backend,
        }}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to load config: {str(e)}')


def _is_git_repo_dir(resolved_root: Path) -> bool:
    git_entry = resolved_root / '.git'
    if git_entry.is_dir():
        return (git_entry / 'HEAD').is_file()
    if git_entry.is_file():
        try:
            first_line = git_entry.read_text(encoding='utf-8', errors='replace').splitlines()[0].strip()
        except (OSError, IndexError):
            return False
        if not first_line.startswith('gitdir:'):
            return False
        raw_target = first_line[len('gitdir:'):].strip()
        if not raw_target or '\x00' in raw_target:
            return False
        try:
            target = Path(raw_target)
            if not target.is_absolute():
                target = (resolved_root / target).resolve()
            return target.is_dir() and (target / 'HEAD').is_file()
        except (OSError, RuntimeError, ValueError):
            return False
    return False


def _validate_effective_repo_path(repo_path: str | None) -> str | None:
    if repo_path is None or not repo_path.strip():
        return None
    root = Path(repo_path.strip())
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=400, detail='repo_path must be an existing directory')
    resolved_root = root.resolve()
    if not _is_git_repo_dir(resolved_root):
        raise HTTPException(status_code=400, detail='repo_path must be a git repository')
    cfg_path = resolved_root / '.amc.yaml'
    if cfg_path.exists() or cfg_path.is_symlink():
        try:
            resolved_cfg = cfg_path.resolve(strict=True)
            st = resolved_cfg.stat()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f'unreadable .amc.yaml: {exc}') from exc
        if not resolved_cfg.is_relative_to(resolved_root) or not stat.S_ISREG(st.st_mode):
            raise HTTPException(status_code=400, detail='.amc.yaml must be a regular file inside repo_path')
        if st.st_size > _MAX_CONFIG_BYTES:
            raise HTTPException(status_code=400, detail='.amc.yaml exceeds maximum allowed size')
    return str(resolved_root)


@router.get('/effective')
def get_effective_settings(
    repo_path: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
):
    """Preview effective settings and the layer each value came from."""
    safe_repo = _validate_effective_repo_path(repo_path)
    try:
        resolved = SettingsResolver.resolve(
            safe_repo,
            round_agent=backend if backend is not None else UNSET,
            round_model=model if model is not None else UNSET,
            round_reasoning=reasoning if reasoning is not None else UNSET,
        )
        return {
            'settings': resolved.model_dump(),
            'backend': resolved.agent,
            'model': resolved.model,
            'reasoning': resolved.reasoning,
            'sources': resolved.sources,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to resolve effective settings: {str(e)}')


@router.post('/check-agent', response_model=CheckAgentResponse)
def check_agent(request: CheckAgentRequest):
    """Run a strictly non-LLM static CLI probe and in-process setting validation."""
    backend_name = request.backend.strip()
    if not backend_name:
        raise HTTPException(status_code=400, detail='backend is required')
    model_val = (request.model or '').strip() or None
    reasoning_val = (request.reasoning or '').strip() or None
    try:
        adapter = get_adapter(backend_name, model=model_val, reasoning=reasoning_val)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    executable = adapter.executable_name()
    cmd = adapter.doctor_command()
    effective_reasoning = adapter.reasoning if adapter.supports_reasoning else None
    if not shutil.which(executable):
        return CheckAgentResponse(
            ok=False,
            backend=backend_name,
            executable=executable,
            command=cmd,
            exit_code=None,
            message=f'Executable `{executable}` was not found on PATH.',
            model=adapter.model,
            reasoning=effective_reasoning,
        )

    try:
        result = run_text(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        return CheckAgentResponse(
            ok=False,
            backend=backend_name,
            executable=executable,
            command=cmd,
            exit_code=None,
            message=f'Static probe timed out after 15s: {" ".join(cmd)}',
            model=adapter.model,
            reasoning=effective_reasoning,
        )
    except OSError as exc:
        return CheckAgentResponse(
            ok=False,
            backend=backend_name,
            executable=executable,
            command=cmd,
            exit_code=None,
            message=f'Failed to launch `{executable}`: {exc}',
            model=adapter.model,
            reasoning=effective_reasoning,
        )

    stdout = (result.stdout or '').strip()
    stderr = (result.stderr or '').strip()
    detail = (stdout.splitlines()[0] if stdout else '') or (stderr.splitlines()[0] if stderr else '')
    if result.returncode != 0:
        msg = stderr or stdout or f'Exited with code {result.returncode}'
        return CheckAgentResponse(
            ok=False,
            backend=backend_name,
            executable=executable,
            command=cmd,
            exit_code=result.returncode,
            message=msg,
            model=adapter.model,
            reasoning=effective_reasoning,
        )

    # Validate requested reasoning against locally known CLI/cache capabilities
    # because `--version` short-circuits before CLI flags validate effort names.
    if effective_reasoning is not None:
        detected = BackendDetector.detect_backend(backend_name)
        if detected is not None:
            if not detected.reasoning_supported and detected.reasoning_source == 'unsupported':
                return CheckAgentResponse(
                    ok=False,
                    backend=backend_name,
                    executable=executable,
                    command=cmd,
                    exit_code=result.returncode,
                    message=(
                        f'CLI check passed ({detail or backend_name}), but reasoning effort '
                        f'{effective_reasoning!r} is not supported by this {backend_name} installation. '
                        f'{detected.reasoning_note or ""}'
                    ).strip(),
                    model=adapter.model,
                    reasoning=effective_reasoning,
                )
            allowed: list[str] = []
            if adapter.model and adapter.model in detected.model_reasoning:
                allowed = list(detected.model_reasoning[adapter.model].get('levels') or [])
            elif detected.reasoning_levels:
                allowed = list(detected.reasoning_levels)
            if allowed and effective_reasoning not in allowed:
                target = f'{backend_name} / {adapter.model}' if adapter.model else backend_name
                return CheckAgentResponse(
                    ok=False,
                    backend=backend_name,
                    executable=executable,
                    command=cmd,
                    exit_code=result.returncode,
                    message=(
                        f'CLI check passed ({detail or backend_name}), but reasoning level '
                        f'{effective_reasoning!r} is not in detected levels for {target}: '
                        f'{", ".join(allowed)}.'
                    ),
                    model=adapter.model,
                    reasoning=effective_reasoning,
                )

    msg = detail or f'{backend_name} static check passed.'
    if reasoning_val and not adapter.supports_reasoning:
        msg = f'{msg} (note: {backend_name} ignores reasoning effort and uses its CLI default)'
    return CheckAgentResponse(
        ok=True,
        backend=backend_name,
        executable=executable,
        command=cmd,
        exit_code=result.returncode,
        message=msg,
        model=adapter.model,
        reasoning=effective_reasoning,
    )


@router.post('/save', response_model=SaveConfigResponse)
def save_config(request: SaveConfigRequest):
    """Save user-selected configuration to global config file.

    Distinguishes omitted fields (keep existing value) from explicit ``null`` /
    empty string (clear saved value so CLI/model default takes effect).
    """
    try:
        backend_name = request.backend.strip()
        if not backend_name:
            raise HTTPException(status_code=400, detail='backend is required')
        fields = request.model_fields_set
        try:
            with global_config_lock:
                # strict load: a corrupt file must abort the save, not be
                # silently treated as empty and overwritten.
                config = load_global_config(strict=True)
                backend_config = config.setdefault('backend', {})
                if not isinstance(backend_config, dict):
                    backend_config = {}
                    config['backend'] = backend_config
                backend_config['default'] = backend_name
                selected = backend_config.setdefault(backend_name, {})
                if not isinstance(selected, dict):
                    selected = {}
                    backend_config[backend_name] = selected

                if 'model' in fields:
                    cleaned_model = (request.model or '').strip()
                    if cleaned_model:
                        selected['model'] = cleaned_model
                    else:
                        selected.pop('model', None)

                if 'reasoning' in fields:
                    cleaned_reasoning = (request.reasoning or '').strip()
                    if cleaned_reasoning:
                        selected['reasoning'] = cleaned_reasoning
                        # Keep variant in sync or clear stale variant so no split-brain.
                        selected.pop('variant', None)
                    else:
                        selected.pop('reasoning', None)
                        selected.pop('variant', None)
                elif 'variant' in fields and request.variant is not None:
                    # Legacy alias compatibility: `variant: null` preserves the
                    # existing saved value (only `reasoning: null` performs an
                    # explicit reset).
                    cleaned_variant = request.variant.strip()
                    if cleaned_variant:
                        selected['variant'] = cleaned_variant
                        selected['reasoning'] = cleaned_variant
                    else:
                        selected.pop('variant', None)
                        selected.pop('reasoning', None)

                # Save inside the same lock: the atomic replace below prevents
                # torn files, but only serialising the whole read-modify-write
                # prevents one concurrent save from erasing another's backend.
                save_global_config(config)
        except ConfigError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    'Global config file is unreadable and was NOT overwritten. '
                    f'Repair or remove it first, then save again. ({exc})'
                ),
            ) from exc

        from app.core.config import _global_config_path
        config_path = str(_global_config_path())

        return SaveConfigResponse(
            status='success',
            message='Configuration saved successfully',
            config_path=config_path,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to save config: {str(e)}')
