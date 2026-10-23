"""API routes for configuration management."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.backend_detector import BackendDetector, ModelFilter
from app.core.config import (
    DEFAULT_CONFIG,
    ConfigError,
    global_config_lock,
    load_global_config,
    save_global_config,
)

router = APIRouter(prefix='/api/config', tags=['config'])


class BackendResponse(BaseModel):
    """Response model for backend detection."""
    name: str
    version: str
    models: list[str]
    command: str
    model_count: int  # Total models before filtering
    grouped_models: dict[str, list[str]] | None = None  # Optional grouped view


class DetectResponse(BaseModel):
    """Response for backend detection endpoint."""
    backends: list[BackendResponse]


class SaveConfigRequest(BaseModel):
    """Request model for saving configuration."""
    backend: str
    model: str
    variant: str | None = None


class SaveConfigResponse(BaseModel):
    """Response for save config endpoint."""
    status: str
    message: str
    config_path: str | None = None


@router.get('/detect', response_model=DetectResponse)
def detect_backends():
    """Detect all available AI backends on the system.

    Returns:
        List of detected backends with their versions and available models.
        For backends with many models, they are filtered and grouped.
    """
    try:
        backends = BackendDetector.detect_all()

        return DetectResponse(
            backends=[
                BackendResponse(
                    name=b.name,
                    version=b.version,
                    models=b.models,
                    command=b.command,
                    model_count=len(b.models),
                    grouped_models=ModelFilter.group_models(b.models) if len(b.models) > 20 else None,
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
        Current configuration or default config if none exists.
    """
    try:
        config = load_global_config()
        defaults = DEFAULT_CONFIG['backend']
        section = config.get('backend', {}) if isinstance(config, dict) else {}
        if not isinstance(section, dict):
            section = {}
        backend = str(section.get('default') or defaults['default'])
        selected = section.get(backend, {})
        if not isinstance(selected, dict):
            selected = {}
        return {'config': {
            'backend': backend,
            'model': str(selected.get('model') or '').strip() or None,
            'variant': str(selected.get('variant') or '').strip() or None,
        }}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to load config: {str(e)}')


@router.post('/save', response_model=SaveConfigResponse)
def save_config(request: SaveConfigRequest):
    """Save user-selected configuration to global config file.

    Args:
        request: Configuration with backend, model, and optional variant.

    Returns:
        Success status and config file path.
    """
    try:
        if not request.backend.strip():
            raise HTTPException(status_code=400, detail='backend is required')
        try:
            with global_config_lock:
                # strict load: a corrupt file must abort the save, not be
                # silently treated as empty and overwritten.
                config = load_global_config(strict=True)
                backend_config = config.setdefault('backend', {})
                if not isinstance(backend_config, dict):
                    backend_config = {}
                    config['backend'] = backend_config
                backend_config['default'] = request.backend
                selected = backend_config.setdefault(request.backend, {})
                if not isinstance(selected, dict):
                    selected = {}
                    backend_config[request.backend] = selected
                selected['model'] = request.model
                if request.variant is not None:
                    selected['variant'] = request.variant
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
            message=f'Configuration saved successfully',
            config_path=config_path,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to save config: {str(e)}')
