"""Platform-standard application data paths and legacy DB migration (P5).

Default storage layout (overridable via ``AMC_DATA_DIR``):
  - macOS:   ``~/Library/Application Support/amc``
  - Linux:   ``${XDG_DATA_HOME:-~/.local/share}/amc``
  - Windows: ``%APPDATA%\\amc``

Global user config remains at ``~/.config/amc/config.yaml`` (honoring
``XDG_CONFIG_HOME`` and ``%APPDATA%\\amc\\config.yaml`` on Windows) via
``app.core.config._global_config_path()``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_SQLITE_MAGIC = b'SQLite format 3\x00'


def get_data_dir() -> Path:
    """Return the platform-appropriate application data directory for AMC."""
    override = os.environ.get('AMC_DATA_DIR', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'amc'
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA', '').strip()
        if appdata:
            return Path(appdata) / 'amc'
        return Path.home() / 'AppData' / 'Roaming' / 'amc'
    xdg_data = os.environ.get('XDG_DATA_HOME', '').strip()
    if xdg_data:
        return Path(xdg_data).expanduser() / 'amc'
    return Path.home() / '.local' / 'share' / 'amc'


def get_db_path() -> Path:
    """Return the SQLite database file path (honors ``AMC_DB_PATH``)."""
    override = os.environ.get('AMC_DB_PATH', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_data_dir() / 'amc.db'


def get_runtime_dir() -> Path:
    """Return the runtime artifacts directory (honors ``AMC_RUNTIME_DIR``)."""
    override = os.environ.get('AMC_RUNTIME_DIR', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_data_dir() / 'runtime'


def get_worktrees_dir() -> Path:
    """Return a short path for isolated git worktrees (Windows 260-char safe)."""
    override = os.environ.get('AMC_WORKTREES_DIR', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_data_dir() / 'wt'


def get_packaged_run_dir() -> Path:
    """Return the runtime PID/log/ports directory for packaged (non-checkout) installs."""
    return get_data_dir() / 'run'


def get_bundled_ui_dir() -> Path:
    """Return the directory containing built frontend assets bundled in the Python package."""
    return Path(__file__).resolve().parent.parent / 'static' / 'ui'


def has_bundled_ui() -> bool:
    """Return True when the built React frontend bundle is present inside the package."""
    return (get_bundled_ui_dir() / 'index.html').is_file()


def _is_nonempty_sqlite_file(path: Path) -> bool:
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size < len(_SQLITE_MAGIC):
            return False
        with path.open('rb') as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _legacy_db_has_data(path: Path) -> bool:
    """Return True when ``path`` is a readable AMC SQLite DB containing at least one task."""
    if not _is_nonempty_sqlite_file(path):
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if row is None:
            return False
        has_row = conn.execute('SELECT 1 FROM tasks LIMIT 1').fetchone()
        return has_row is not None
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def find_legacy_db_paths(*, extra_roots: Iterable[Path] = ()) -> list[Path]:
    """Locate existing legacy ``./data/amc.db`` databases from CWD or source checkouts."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    roots = [Path.cwd(), *extra_roots]
    for root in roots:
        try:
            cand = (root / 'data' / 'amc.db').resolve()
        except OSError:
            continue
        if cand in seen:
            continue
        seen.add(cand)
        if _is_nonempty_sqlite_file(cand):
            candidates.append(cand)
    return candidates


def maybe_import_legacy_db(
    target_db: Path | None = None,
    *,
    candidates: Iterable[Path] | None = None,
    extra_roots: Iterable[Path] = (),
) -> Path | None:
    """Atomically import a legacy ``./data/amc.db`` on first run into the app data dir.

    Safety guarantees:
    - Never overwrites an existing non-empty ``target_db``.
    - Never modifies or deletes the legacy source database (kept as backup).
    - Skips when ``AMC_DB_PATH`` explicitly overrides the DB path (unless
      ``target_db`` is explicitly passed) or ``AMC_NO_LEGACY_IMPORT=1`` is set.
    - Uses SQLite's online ``backup()`` API into a temp file + ``os.replace()``
      so WAL/journal changes are captured cleanly and partial copies cannot occur.
    """
    if os.environ.get('AMC_NO_LEGACY_IMPORT', '').strip().lower() in {'1', 'true', 'yes'}:
        return None
    if target_db is None and os.environ.get('AMC_DB_PATH', '').strip():
        return None

    target = (target_db if target_db is not None else get_db_path()).resolve()
    try:
        if target.exists() and target.stat().st_size > 0:
            return None
    except OSError:
        return None

    search_list = (
        list(candidates)
        if candidates is not None
        else find_legacy_db_paths(extra_roots=extra_roots)
    )

    for raw_cand in search_list:
        try:
            cand = Path(raw_cand).resolve()
        except OSError:
            continue
        if cand == target:
            continue
        if not _legacy_db_has_data(cand):
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.parent / f'.{target.name}.{os.getpid()}.import.tmp'
        src_conn: sqlite3.Connection | None = None
        dst_conn: sqlite3.Connection | None = None
        try:
            src_conn = sqlite3.connect(str(cand), timeout=5.0)
            dst_conn = sqlite3.connect(str(temp_path), timeout=5.0)
            src_conn.backup(dst_conn)
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None

            # Re-check immediately before publishing so a concurrent startup cannot be clobbered.
            if target.exists() and target.stat().st_size > 0:
                return None
            os.replace(temp_path, target)
            logger.info('Imported legacy database from %s to %s', cand, target)
            return cand
        except (OSError, sqlite3.Error) as exc:
            logger.warning('Failed to import legacy database from %s: %s', cand, exc)
            return None
        finally:
            if dst_conn is not None:
                try:
                    dst_conn.close()
                except sqlite3.Error:
                    pass
            if src_conn is not None:
                try:
                    src_conn.close()
                except sqlite3.Error:
                    pass
            try:
                temp_path.unlink()
            except OSError:
                pass
    return None
