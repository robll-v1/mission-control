"""Subprocess helpers that decode child output as UTF-8 on every platform.

``subprocess.run(..., text=True)`` decodes using ``locale.getpreferredencoding()``.
On a non-English Windows install that is a legacy codepage (``cp936`` for zh-CN,
``cp932`` for ja-JP, ``cp1251`` for ru-RU), and any byte the codepage cannot
represent raises ``UnicodeDecodeError`` *inside the reader thread*. The exception
never reaches the caller: ``CompletedProcess.stdout`` is simply left as ``None``,
so the failure resurfaces much later as ``AttributeError: 'NoneType' object has
no attribute 'lower'``.

Git emits UTF-8 regardless of the console codepage, so decoding its output as
UTF-8 is both correct and stable. ``errors='replace'`` keeps a stray invalid byte
from taking down a review.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence


def run_text(cmd: Sequence[str] | str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` that always captures text as UTF-8.

    Callers may override any of the defaults; only the decoding behaviour is
    opinionated.
    """
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    kwargs.setdefault('encoding', 'utf-8')
    kwargs.setdefault('errors', 'replace')
    return subprocess.run(cmd, **kwargs)  # noqa: S603


def run_git(repo_path: str | Path, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <repo_path> <args>`` with UTF-8 decoding."""
    return run_text(['git', '-C', str(repo_path), *args], **kwargs)
