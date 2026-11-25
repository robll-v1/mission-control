"""Safe local editor launching and bounded repository file snippet reader (P6).

Security invariants (per docs/design/redesign-spec-v1.md §2.3 #10):
1. Target paths are resolved via ``Path.resolve()`` and must remain strictly
   inside the task's resolved ``repo_path`` root (blocks ``..`` traversal and
   symlinks escaping the repository).
2. Only regular files are opened or read; snippet reads use ``O_NOFOLLOW`` +
   ``fstat`` + strict ``_MAX_SNIPPET_FILE_BYTES + 1`` bounded reads to avoid
   symlink-swap TOCTOU and unbounded memory allocation.
3. Editor commands are restricted to an explicit allowlist of native binaries
   (on Windows, ``.cmd``/``.bat`` shell wrappers are explicitly rejected so
   untrusted repo filenames can never be parsed by ``cmd.exe``) and invoked
   with ``shell=False`` and argument lists only.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

_GOTO_EDITORS = ('cursor', 'code', 'windsurf')
_COLON_LINE_EDITORS = ('zed', 'subl')
_MACOS_FALLBACK_EDITOR = 'open'
ALLOWED_EDITORS: tuple[str, ...] = (*_GOTO_EDITORS, *_COLON_LINE_EDITORS, _MACOS_FALLBACK_EDITOR)

# Native Windows executable names for allowlisted editors (never `.cmd`/`.bat` shims).
_WINDOWS_NATIVE_EXES: dict[str, tuple[str, ...]] = {
    'code': ('Code.exe',),
    'cursor': ('Cursor.exe',),
    'windsurf': ('Windsurf.exe',),
    'zed': ('Zed.exe',),
    'subl': ('subl.exe', 'sublime_text.exe'),
}

_MAX_SNIPPET_FILE_BYTES = 512 * 1024  # 512 KiB guard against huge/binary files


class PathAccessError(ValueError):
    """Raised when a requested file path escapes the repository or is invalid."""


class EditorNotAllowedError(ValueError):
    """Raised when a requested editor is not in the explicit allowlist."""


class EditorUnavailableError(RuntimeError):
    """Raised when no allowlisted native editor executable is available."""


def resolve_repo_file(repo_path: str, file_path: str) -> tuple[Path, str]:
    """Resolve ``file_path`` strictly inside ``repo_path`` and verify it is a regular file.

    Returns ``(resolved_target, repo_relative_posix_path)``.
    """
    raw_repo = (repo_path or '').strip()
    raw_file = (file_path or '').strip()
    if not raw_repo:
        raise PathAccessError('repository path is empty')
    if not raw_file or '\x00' in raw_file:
        raise PathAccessError('file path is invalid')

    repo_root = Path(raw_repo).expanduser().resolve()
    if not repo_root.is_dir():
        raise PathAccessError('repository directory does not exist')

    req = Path(raw_file)
    candidate = req if req.is_absolute() else (repo_root / req)
    # Check containment BEFORE strict existence lookup so traversal attempts
    # always fail with PathAccessError (400) without probing outside paths.
    try:
        candidate.resolve(strict=False).relative_to(repo_root)
    except ValueError as exc:
        raise PathAccessError('file path escapes repository root') from exc

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(f'file not found: {raw_file}') from exc

    try:
        rel = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise PathAccessError('file path escapes repository root') from exc

    try:
        st = resolved.stat()
    except OSError as exc:
        raise FileNotFoundError(f'file not accessible: {raw_file}') from exc
    if not stat.S_ISREG(st.st_mode):
        raise PathAccessError('target path is not a regular file')

    return resolved, rel.as_posix()


def _is_trusted_native_binary(binary: str, *, platform: str | None = None) -> bool:
    """Ensure the resolved launcher is an absolute native executable (never a batch/shell wrapper on Windows)."""
    plat = platform or sys.platform
    if not binary or '\x00' in binary:
        return False
    if plat == 'win32':
        p = PureWindowsPath(binary)
        # Reject .cmd, .bat, .ps1, .vbs, etc. which delegate through cmd.exe/shell.
        return p.is_absolute() and p.suffix.lower() == '.exe'
    return PurePosixPath(binary).is_absolute()


def _locate_native_editor_binary(key: str, *, platform: str | None = None) -> str | None:
    """Locate a native executable on PATH (or well-known Windows install roots) for an allowlisted key."""
    plat = platform or sys.platform
    if plat == 'win32':
        for exe_name in _WINDOWS_NATIVE_EXES.get(key, ()):
            found = shutil.which(exe_name)
            if found and _is_trusted_native_binary(found, platform=plat):
                return found
        # Also check standard per-user / system install directories for native .exe
        local_app = os.environ.get('LOCALAPPDATA', '').strip()
        prog_files = os.environ.get('ProgramFiles', '').strip()
        win_candidates: dict[str, list[Path]] = {
            'code': [
                *(Path(base) / 'Programs' / 'Microsoft VS Code' / 'Code.exe' for base in (local_app,) if base),
                *(Path(base) / 'Microsoft VS Code' / 'Code.exe' for base in (prog_files,) if base),
            ],
            'cursor': [
                *(Path(base) / 'Programs' / 'cursor' / 'Cursor.exe' for base in (local_app,) if base),
            ],
            'windsurf': [
                *(Path(base) / 'Programs' / 'Windsurf' / 'Windsurf.exe' for base in (local_app,) if base),
            ],
            'zed': [
                *(Path(base) / 'Programs' / 'Zed' / 'Zed.exe' for base in (local_app,) if base),
            ],
            'subl': [
                *(Path(base) / 'Sublime Text' / 'subl.exe' for base in (prog_files,) if base),
                *(Path(base) / 'Sublime Text' / 'sublime_text.exe' for base in (prog_files,) if base),
            ],
        }
        for cand in win_candidates.get(key, []):
            if cand.is_file() and _is_trusted_native_binary(str(cand), platform=plat):
                return str(cand)
        return None

    found = shutil.which(key)
    if found and _is_trusted_native_binary(found, platform=plat):
        return found
    if plat == 'darwin' and key == _MACOS_FALLBACK_EDITOR and Path('/usr/bin/open').is_file():
        return '/usr/bin/open'
    return None


def _resolve_editor_binary(editor: str | None) -> tuple[str, str]:
    """Return ``(editor_key, native_executable_path)`` strictly from the allowlist."""
    if editor is not None:
        key = editor.strip().lower()
        if not key:
            editor = None
        else:
            allowed = set(_GOTO_EDITORS) | set(_COLON_LINE_EDITORS)
            if sys.platform == 'darwin':
                allowed.add(_MACOS_FALLBACK_EDITOR)
            if key not in allowed:
                raise EditorNotAllowedError(
                    f'editor {editor!r} is not allowed; permitted editors: {", ".join(sorted(allowed))}'
                )
            binary = _locate_native_editor_binary(key)
            if not binary:
                raise EditorUnavailableError(f'native editor executable for {key!r} is not installed')
            return key, binary

    # Auto-detect first installed allowlisted native editor
    for key in (*_GOTO_EDITORS, *_COLON_LINE_EDITORS):
        binary = _locate_native_editor_binary(key)
        if binary:
            return key, binary
    if sys.platform == 'darwin':
        binary = _locate_native_editor_binary(_MACOS_FALLBACK_EDITOR)
        if binary:
            return _MACOS_FALLBACK_EDITOR, binary
    raise EditorUnavailableError(
        'No supported native editor found (checked: cursor, code, windsurf, zed, subl).'
    )


def build_editor_argv(
    editor_key: str,
    binary: str,
    target: Path,
    line: int | None = None,
    *,
    platform: str | None = None,
) -> list[str]:
    """Build a safe argv list (never a shell string) for an allowlisted native editor.

    ``target`` is used as-is without re-resolving symlinks so the caller's already
    validated path is passed directly.
    """
    if not _is_trusted_native_binary(binary, platform=platform):
        raise EditorNotAllowedError(f'editor binary {binary!r} is not a trusted native executable')
    abs_target = str(target)
    valid_line = int(line) if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None
    if editor_key in _GOTO_EDITORS:
        spec = f'{abs_target}:{valid_line}' if valid_line else abs_target
        return [binary, '--goto', spec]
    if editor_key in _COLON_LINE_EDITORS:
        spec = f'{abs_target}:{valid_line}' if valid_line else abs_target
        return [binary, spec]
    if editor_key == _MACOS_FALLBACK_EDITOR:
        # Use `-t` so macOS opens files in the default text editor rather than
        # executing `.command`/script bundles or launching arbitrary handlers.
        return [binary, '-t', abs_target]
    raise EditorNotAllowedError(f'editor {editor_key!r} is not allowed')


def _spawn_editor(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=(sys.platform != 'win32'),
    )


def open_in_editor(
    repo_path: str,
    file_path: str,
    *,
    line: int | None = None,
    editor: str | None = None,
) -> dict[str, object]:
    """Validate path containment + native editor allowlist, then spawn cleanly."""
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1 or line > 10_000_000):
        raise ValueError('line must be a positive integer')

    resolved, rel_path = resolve_repo_file(repo_path, file_path)
    editor_key, binary = _resolve_editor_binary(editor)
    argv = build_editor_argv(editor_key, binary, resolved, line=line)
    _spawn_editor(argv)
    return {
        'ok': True,
        'editor': editor_key,
        'path': rel_path,
        'line': line,
    }


def _read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    """Open ``path`` without following a final symlink, verify regular file via fstat, and read <= max_bytes+1."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EACCES, errno.EPERM):
            raise PathAccessError('target path cannot be opened safely') from exc
        raise FileNotFoundError(f'file not accessible: {path.name}') from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PathAccessError('target path is not a regular file')
        if st.st_size > max_bytes:
            raise ValueError('file is too large for inline preview')
        with os.fdopen(fd, 'rb') as handle:
            fd = -1  # ownership transferred to handle
            data = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    if len(data) > max_bytes:
        raise ValueError('file is too large for inline preview')
    return data


def read_file_snippet(
    repo_path: str,
    file_path: str,
    *,
    line: int,
    context: int = 2,
) -> dict[str, object]:
    """Read a small bounded line window around ``line`` from a file inside ``repo_path``."""
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ValueError('line must be a positive integer')
    safe_ctx = max(0, min(int(context), 10))
    resolved, rel_path = resolve_repo_file(repo_path, file_path)

    raw = _read_bounded_regular_file(resolved, _MAX_SNIPPET_FILE_BYTES)
    if b'\x00' in raw[:8192]:
        raise ValueError('binary file cannot be previewed')

    text = raw.decode('utf-8', errors='replace')
    all_lines = text.splitlines()
    if not all_lines:
        return {'path': rel_path, 'target_line': line, 'lines': []}

    start = max(1, line - safe_ctx)
    end = min(len(all_lines), line + safe_ctx)
    if start > len(all_lines):
        start = max(1, len(all_lines) - safe_ctx)
        end = len(all_lines)

    snippet_lines = [
        {
            'number': idx,
            'text': all_lines[idx - 1],
            'highlighted': idx == line,
        }
        for idx in range(start, end + 1)
    ]
    return {
        'path': rel_path,
        'target_line': line,
        'lines': snippet_lines,
    }
