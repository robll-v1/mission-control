#!/usr/bin/env python3
"""Verify a built agent-mission-control wheel in an isolated no-Node environment.

Checks performed:
1. Wheel archive contains bundled React UI (`app/static/ui/index.html` + assets)
   and excludes test suites / node_modules.
2. Fresh venv outside the repository installs the wheel cleanly.
3. With `node` and `npm` stripped from PATH and isolated `AMC_DATA_DIR` /
   `XDG_CONFIG_HOME`, `amc --help`, `amc doctor`, `amc start --no-browser`,
   `/api/health`, `/` (bundled HTML), `/api/config/storage` (`bundled_ui: true`),
   `amc status`, and `amc stop` all succeed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def _path_without_node(raw_path: str) -> str:
    kept: list[str] = []
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        p = Path(entry)
        if (p / 'node').exists() or (p / 'node.exe').exists() or (p / 'npm').exists():
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def check_wheel_contents(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path, 'r') as zf:
        names = zf.namelist()
    assert 'app/static/ui/index.html' in names, 'Wheel is missing bundled app/static/ui/index.html'
    assets = [n for n in names if n.startswith('app/static/ui/assets/') and (n.endswith('.js') or n.endswith('.css'))]
    assert len(assets) >= 2, f'Wheel is missing bundled JS/CSS assets: {assets}'
    bad = [n for n in names if n.startswith('tests/') or '/tests/' in n or 'node_modules/' in n]
    assert not bad, f'Wheel leaked forbidden files: {bad[:5]}'
    print(f'[smoke] Wheel contents OK: {len(names)} files, bundled assets: {assets}')


def run_clean_venv_smoke(wheel_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix='amc-wheel-smoke-') as tmp:
        root = Path(tmp)
        venv_dir = root / 'venv'
        data_dir = root / 'data'
        xdg_dir = root / 'xdg'
        work_dir = root / 'outside_cwd'
        data_dir.mkdir()
        xdg_dir.mkdir()
        work_dir.mkdir()

        uv_bin = shutil.which('uv')
        if uv_bin:
            subprocess.run([uv_bin, 'venv', str(venv_dir), '--python', sys.executable], check=True)
            venv_py = venv_dir / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            subprocess.run([uv_bin, 'pip', 'install', '--python', str(venv_py), str(wheel_path)], check=True)
        else:
            subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True)
            venv_py = venv_dir / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            subprocess.run([str(venv_py), '-m', 'pip', 'install', '--quiet', str(wheel_path)], check=True)

        amc_bin = venv_dir / ('Scripts/amc.exe' if sys.platform == 'win32' else 'bin/amc')
        assert amc_bin.is_file(), f'amc entry point not found at {amc_bin}'
        alias_bin = venv_dir / (
            'Scripts/agent-mission-control.exe' if sys.platform == 'win32' else 'bin/agent-mission-control'
        )
        assert alias_bin.is_file(), f'agent-mission-control entry point not found at {alias_bin}'

        env = {
            **os.environ,
            'PATH': str(amc_bin.parent) + os.pathsep + _path_without_node(os.environ.get('PATH', '')),
            'AMC_DATA_DIR': str(data_dir),
            'AMC_NO_LEGACY_IMPORT': '1',
            'XDG_CONFIG_HOME': str(xdg_dir),
        }
        env.pop('AMC_MODEL', None)
        env.pop('AMC_REASONING', None)
        env.pop('PYTHONPATH', None)

        # Verify Node is genuinely absent from the test PATH
        assert shutil.which('node', path=env['PATH']) is None, 'node should be excluded from smoke PATH'

        # 1. CLI help, package-name alias & doctor
        subprocess.run([str(amc_bin), '--help'], cwd=str(work_dir), env=env, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([str(alias_bin), '--help'], cwd=str(work_dir), env=env, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([str(amc_bin), 'review', '--help'], cwd=str(work_dir), env=env, check=True, stdout=subprocess.DEVNULL)
        doctor = subprocess.run([str(amc_bin), 'doctor', '--json'], cwd=str(work_dir), env=env, capture_output=True, text=True)
        # Doctor exits 0 if at least one CLI is present, non-zero if none; either way it must emit valid JSON
        parsed_doc = json.loads(doctor.stdout)
        assert isinstance(parsed_doc, dict), f'Unexpected doctor JSON: {doctor.stdout[:200]}'
        print(f'[smoke] amc --help & amc doctor OK (exit={doctor.returncode})')

        # 2. Packaged single-server start (foreground supervisor) -> health -> bundled UI -> status -> stop
        port = _pick_free_port()
        start_log = root / 'amc-start.log'
        with start_log.open('w', encoding='utf-8') as log_f:
            start_proc = subprocess.Popen(
                [str(amc_bin), 'start', '--port', str(port), '--no-browser'],
                cwd=str(work_dir),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            try:
                health_url = f'http://127.0.0.1:{port}/api/health'
                for _ in range(100):
                    if start_proc.poll() is not None:
                        log_f.flush()
                        raise AssertionError(
                            f'amc start exited early (code={start_proc.returncode}):\n'
                            f'{start_log.read_text(encoding="utf-8", errors="replace")}'
                        )
                    try:
                        with urllib.request.urlopen(health_url, timeout=2) as resp:
                            if resp.status == 200 and json.loads(resp.read()) == {'ok': True}:
                                break
                    except Exception:
                        time.sleep(0.2)
                else:
                    log_f.flush()
                    raise AssertionError(
                        f'Backend health check did not pass on port {port}:\n'
                        f'{start_log.read_text(encoding="utf-8", errors="replace")}'
                    )

                with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as resp:
                    html = resp.read().decode('utf-8', errors='replace')
                    assert resp.status == 200
                    assert '<div id="root"></div>' in html, 'Root HTML did not serve bundled React index.html'
                    assert '/assets/' in html, 'Root HTML missing bundled /assets/ reference'

                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/config/storage', timeout=5) as resp:
                    storage = json.loads(resp.read())
                    assert storage.get('bundled_ui') is True, f'Expected bundled_ui=True, got {storage}'

                status_res = subprocess.run(
                    [str(amc_bin), 'status'],
                    cwd=str(work_dir),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                assert '[OK]' in status_res.stdout and 'backend is running' in status_res.stdout, (
                    f'Unexpected amc status output: {status_res.stdout}'
                )
                print(f'[smoke] Packaged amc start / bundled UI / status OK on 127.0.0.1:{port} (no Node.js)')
            finally:
                stop_res = subprocess.run(
                    [str(amc_bin), 'stop'],
                    cwd=str(work_dir),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                try:
                    start_proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    start_proc.terminate()
                    start_proc.wait(timeout=5)
                assert stop_res.returncode == 0, f'amc stop failed:\n{stop_res.stdout}\n{stop_res.stderr}'
        print('[smoke] amc stop OK — wheel verified end-to-end!')


def main() -> int:
    parser = argparse.ArgumentParser(description='Smoke-test a built agent-mission-control wheel without Node.js.')
    parser.add_argument('wheel', type=Path, help='Path to the built .whl file')
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file():
        raise SystemExit(f'Wheel file not found: {wheel}')
    check_wheel_contents(wheel)
    run_clean_venv_smoke(wheel)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
