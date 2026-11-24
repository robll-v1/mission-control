"""Platform-standard application data paths, legacy DB migration, and data-dir lock (P5).

Default storage layout (overridable via ``AMC_DATA_DIR``):
  - macOS:   ``~/Library/Application Support/amc``
  - Linux:   ``${XDG_DATA_HOME:-~/.local/share}/amc``
  - Windows: ``%APPDATA%\\amc``

Global user config remains at ``~/.config/amc/config.yaml`` (honoring
``XDG_CONFIG_HOME`` and ``%APPDATA%\\amc\\config.yaml`` on Windows) via
``app.core.config._global_config_path()``.
"""
from __future__ import annotations

import errno
import logging
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from app.core.models import Task

logger = logging.getLogger(__name__)

_SQLITE_MAGIC = b'SQLite format 3\x00'
_REQUIRED_AMC_TABLES = frozenset({'tasks', 'runs', 'events'})
_REQUIRED_TASK_COLUMNS = frozenset({'id', 'data'})
_REQUIRED_RUN_COLUMNS = frozenset({'id', 'task_id', 'data'})
_REQUIRED_EVENT_COLUMNS = frozenset({'id', 'task_id', 'seq', 'data'})


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


class DataDirLock:
    """Cross-platform non-blocking OS file lock preventing two backends from sharing one data dir."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.lock_path = self.data_dir / 'amc.lock'
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Attempt to acquire an exclusive non-blocking lock. Returns False if already held."""
        if self._fd is not None:
            return True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == 'win32':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            # Reserve byte 0 for Windows mandatory 1-byte lock (`msvcrt.locking`);
            # store the holder PID starting at byte 1 so other processes can read
            # it without hitting Windows byte-range PermissionError.
            os.write(fd, b'\n' + f'{os.getpid()}\n'.encode('ascii'))
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if sys.platform == 'win32':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def holder_pid(self) -> int | None:
        fd: int | None = None
        try:
            fd = os.open(str(self.lock_path), os.O_RDONLY)
            os.lseek(fd, 1, os.SEEK_SET)
            raw = os.read(fd, 64)
            text = raw.decode('ascii', errors='ignore').strip()
            return int(text) if text.isdigit() else None
        except OSError:
            return None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def probe_data_dir_lock(data_dir: Path | str) -> tuple[bool, int | None]:
    """Return ``(is_locked, holder_pid)`` without holding the lock after returning."""
    lock = DataDirLock(data_dir)
    if lock.acquire():
        lock.release()
        return False, None
    return True, lock.holder_pid()


def _is_amc_checkout_root(root: Path) -> bool:
    """Return True when ``root`` looks like a Mission Control source checkout."""
    pyproject = root / 'pyproject.toml'
    try:
        lst = pyproject.lstat()
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
            return False
        text = pyproject.read_text(encoding='utf-8', errors='ignore')[:4096]
    except OSError:
        return False
    return 'agent-mission-control' in text


def _has_symlink_component(path: Path, *, stop_at: Path | None = None) -> bool:
    """Return True if ``path`` or any inspected parent up to ``stop_at`` is a symlink."""
    current = path
    boundary = stop_at.resolve() if stop_at is not None else None
    while True:
        try:
            lst = current.lstat()
        except OSError:
            return True
        if stat.S_ISLNK(lst.st_mode):
            return True
        if boundary is not None and current.resolve() == boundary:
            return False
        if current.parent == current:
            return False
        current = current.parent


def _is_nonempty_regular_sqlite_file(path: Path, *, trusted_root: Path | None = None) -> bool:
    """Verify ``path`` is a regular non-symlink SQLite file (and its parents under ``trusted_root`` are not symlinks)."""
    try:
        if trusted_root is not None:
            if _has_symlink_component(path, stop_at=trusted_root):
                return False
        else:
            lst = path.lstat()
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                return False
            parent_lst = path.parent.lstat()
            if stat.S_ISLNK(parent_lst.st_mode):
                return False
        lst = path.lstat()
        if not stat.S_ISREG(lst.st_mode) or lst.st_size < len(_SQLITE_MAGIC):
            return False
        with path.open('rb') as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    """Open ``path`` strictly read-only so probing/importing never mutates or checkpoints it."""
    uri = f'{path.resolve().as_uri()}?mode=ro'
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _legacy_db_has_valid_amc_data(path: Path, *, trusted_root: Path | None = None) -> bool:
    """Return True iff ``path`` is a genuine AMC SQLite DB where EVERY Task row is valid."""
    if not _is_nonempty_regular_sqlite_file(path, trusted_root=trusted_root):
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly_sqlite(path)
        conn.row_factory = sqlite3.Row
        tables = {
            row['name']
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not _REQUIRED_AMC_TABLES.issubset(tables):
            return False
        cols = {row['name'] for row in conn.execute('PRAGMA table_info(tasks)').fetchall()}
        if not _REQUIRED_TASK_COLUMNS.issubset(cols):
            return False
        run_cols = {row['name'] for row in conn.execute('PRAGMA table_info(runs)').fetchall()}
        if not _REQUIRED_RUN_COLUMNS.issubset(run_cols):
            return False
        event_cols = {row['name'] for row in conn.execute('PRAGMA table_info(events)').fetchall()}
        if not _REQUIRED_EVENT_COLUMNS.issubset(event_cols):
            return False
        rows = conn.execute('SELECT data FROM tasks').fetchall()
        if not rows:
            return False
        for row in rows:
            if not isinstance(row['data'], str):
                return False
            Task.model_validate_json(row['data'])
        return True
    except (sqlite3.Error, ValueError):
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def find_legacy_db_paths(*, extra_roots: Iterable[Path] = ()) -> list[Path]:
    """Locate existing legacy ``data/amc.db`` ONLY inside explicitly trusted AMC checkout roots.

    Never scans untrusted caller CWD automatically (prevents a malicious or
    unrelated repo's ``./data/amc.db`` from being imported into the user's app
    data directory).
    """
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in extra_roots:
        if not _is_amc_checkout_root(root):
            continue
        raw_cand = root / 'data' / 'amc.db'
        if not _is_nonempty_regular_sqlite_file(raw_cand, trusted_root=root):
            continue
        try:
            cand = raw_cand.resolve()
        except OSError:
            continue
        if cand in seen:
            continue
        seen.add(cand)
        candidates.append(cand)
    return candidates


def _publish_no_overwrite(temp_path: Path, target: Path) -> bool:
    """Atomically publish ``temp_path`` to ``target`` ONLY if ``target`` does not exist.

    Uses kernel-atomic ``os.link()`` (create-if-absent on POSIX and Windows NTFS).
    If the underlying filesystem does not support hard links, refuses to perform a
    racy ``os.replace()`` that could clobber a concurrently created database.
    """
    try:
        os.link(temp_path, target)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno in {
            errno.EPERM,
            errno.EXDEV,
            errno.ENOTSUP,
            getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP),
            errno.EINVAL,
        }:
            logger.warning(
                'Skipping automatic legacy DB import into %s: filesystem does not support atomic non-overwriting link (%s).',
                target,
                exc,
            )
            return False
        raise


def maybe_import_legacy_db(
    target_db: Path | None = None,
    *,
    candidates: Iterable[Path] | None = None,
    extra_roots: Iterable[Path] = (),
) -> Path | None:
    """Atomically import a legacy ``./data/amc.db`` on first run into the app data dir.

    Safety guarantees:
    - Never overwrites an existing ``target_db`` (kernel-atomic ``os.link`` publication).
    - Never follows symlinks or imports non-AMC databases (verifies schema & Task JSON).
    - Opens legacy source strictly read-only (``mode=ro``) so the source DB is untouched.
    - Uses a unique temporary file (safe across threads and processes) and SQLite's
      online ``backup()`` API to capture live WAL data consistently.
    """
    if os.environ.get('AMC_NO_LEGACY_IMPORT', '').strip().lower() in {'1', 'true', 'yes'}:
        return None
    if target_db is None and os.environ.get('AMC_DB_PATH', '').strip():
        return None

    target = (target_db if target_db is not None else get_db_path()).resolve()
    try:
        if target.exists():
            return None
    except OSError:
        return None

    search_list = (
        list(candidates)
        if candidates is not None
        else find_legacy_db_paths(extra_roots=extra_roots)
    )

    for raw_cand in search_list:
        cand_path = Path(raw_cand)
        if not _legacy_db_has_valid_amc_data(cand_path):
            continue
        try:
            cand = cand_path.resolve()
        except OSError:
            continue
        if cand == target:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f'.{target.name}.',
            suffix='.import.tmp',
        )
        os.close(tmp_fd)
        temp_path = Path(tmp_name)
        src_conn: sqlite3.Connection | None = None
        dst_conn: sqlite3.Connection | None = None
        try:
            src_conn = _open_readonly_sqlite(cand)
            dst_conn = sqlite3.connect(str(temp_path), timeout=5.0)
            src_conn.backup(dst_conn)
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None

            if not _publish_no_overwrite(temp_path, target):
                return None
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
