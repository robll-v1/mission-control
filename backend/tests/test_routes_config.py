from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI

from app.api import routes_config
from app.api.routes_config import SaveConfigRequest
from app.core.config import load_global_config


def test_save_preserves_existing_global_config_and_omitted_variant(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    path = tmp_path / 'xdg' / 'amc' / 'config.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(
        'unknown_extension:\n  keep: true\n'
        'execution:\n  idle_timeout_sec: 900\n'
        'backend:\n  default: opencode\n'
        '  codex:\n    model: old\n    variant: high\n',
        encoding='utf-8',
    )

    result = routes_config.save_config(SaveConfigRequest(backend='codex', model='new'))
    saved = load_global_config()

    assert result.status == 'success'
    assert saved['unknown_extension'] == {'keep': True}
    assert saved['execution']['idle_timeout_sec'] == 900
    assert saved['backend']['default'] == 'codex'
    assert saved['backend']['codex'] == {'model': 'new', 'variant': 'high'}


def test_current_config_exposes_only_selected_backend_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'xdg' / 'amc' / 'config.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(
        'backend:\n  default: codex\n  codex:\n    model: m\n    variant: high\n    api_key: secret\n'
        'direct_api:\n  api_key: unrelated-secret\n', encoding='utf-8'
    )

    result = routes_config.get_current_config()
    assert result == {'config': {'backend': 'codex', 'model': 'm', 'variant': 'high'}}


def test_detect_does_not_block_event_loop(monkeypatch):
    async def run_check():
        app = FastAPI()
        app.include_router(routes_config.router)

        def slow_detection():
            time.sleep(0.2)
            return []

        monkeypatch.setattr(routes_config.BackendDetector, 'detect_all', slow_detection)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            started = time.perf_counter()
            request = asyncio.create_task(client.get('/api/config/detect'))
            await asyncio.sleep(0.04)
            elapsed = time.perf_counter() - started
            assert not request.done()
            assert elapsed < 0.15
            response = await request
        assert response.status_code == 200
        assert response.json() == {'backends': []}

    asyncio.run(run_check())
