"""Tests for `amc doctor` CLI static probe command."""
from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from app import cli
from app.api import routes_config
from app.core.backend_detector import BackendDetector


def test_amc_doctor_json_outputs_valid_payload_and_exit_code(isolated_config, monkeypatch, capsys):
    fake_cap = SimpleNamespace(
        name='codex',
        version='0.169.0',
        models=['gpt-6.1-terra'],
        command='codex',
    )
    monkeypatch.setattr(BackendDetector, 'detect_all', classmethod(lambda cls: [fake_cap]))
    monkeypatch.setattr(
        routes_config,
        'check_agent',
        lambda req: routes_config.CheckAgentResponse(
            ok=True,
            backend=req.backend,
            executable='codex',
            command=['codex', '--version'],
            exit_code=0,
            message='codex 0.169.0',
            model=req.model,
            reasoning=req.reasoning,
        ),
    )

    args = argparse.Namespace(backend=None, model=None, reasoning=None, json=True)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(args)
    assert exc.value.code == 0

    data = json.loads(capsys.readouterr().out)
    assert data['ok'] is True
    assert data['checked'] == 1
    assert data['backends'][0]['backend'] == 'codex'


def test_amc_doctor_exits_nonzero_when_no_backends_detected(isolated_config, monkeypatch, capsys):
    monkeypatch.setattr(BackendDetector, 'detect_all', classmethod(lambda cls: []))
    args = argparse.Namespace(backend=None, model=None, reasoning=None, json=True)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(args)
    assert exc.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data['ok'] is False
    assert data['checked'] == 0
