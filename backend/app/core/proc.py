"""Subprocess helpers: UTF-8 text capture and cross-platform process-tree teardown.

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

Process-tree lifecycle helpers (``spawn_managed_process``,
``mark_termination_requested``, ``wait_for_process_exit``,
``terminate_process_tree``) coordinate *all* reaping and termination for a
``Popen`` through a single per-process ownership state:
- Once ``mark_termination_requested(proc)`` is called (atomically with abort
  acceptance) or ``terminate_process_tree(proc)`` begins, normal waits refuse
  to reap ``proc`` and wait for teardown to finish killing the whole tree and
  reaping the leader. This closes the race where a fast-exiting parent whose
  descendant did not inherit stdout/stderr gets reaped before teardown runs.
- On Unix, group liveness is inspected via ``/proc`` (Linux) or ``ps`` (macOS /
  BSD) with a shared remaining time budget while the leader remains unreaped
  (pinned in the kernel), so the full group is killed even on macOS Python
  3.11/3.12 where ``os.waitid`` is absent. ``ECHILD`` is distinguished from
  ``waitid`` unavailability so a process reaped externally is never signalled
  by PGID.
- On Windows, ``spawn_managed_process`` launches the process with
  ``CREATE_SUSPENDED``, assigns it to a ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
  Job Object *before* its first instruction runs, and then resumes its primary
  thread — guaranteeing descendants cannot escape even if a wrapper exits
  immediately.
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence


@dataclass
class _ProcessState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    termination_requested: bool = False
    terminating: bool = False
    reaped: bool = False
    job_handle: int | None = None


_STATES_GUARD = threading.Lock()
_STATES: weakref.WeakKeyDictionary[subprocess.Popen[Any], _ProcessState] = weakref.WeakKeyDictionary()


def _state_for_proc(proc: subprocess.Popen[Any]) -> _ProcessState:
    with _STATES_GUARD:
        state = _STATES.get(proc)
        if state is None:
            state = _ProcessState()
            _STATES[proc] = state
        return state


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


# ---------------------------------------------------------------------------
# Windows Job Object + CREATE_SUSPENDED helpers
# ---------------------------------------------------------------------------

_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _win32_kernel32() -> Any:
    """Return kernel32 with ``use_last_error=True`` so ``ctypes.get_last_error()`` is accurate."""
    return ctypes.WinDLL('kernel32', use_last_error=True)  # type: ignore[attr-defined]


def _create_and_assign_windows_job(proc: subprocess.Popen[Any]) -> tuple[int | None, str | None]:
    """Attach a kill-on-close Job Object to ``proc`` on Windows.

    Returns ``(job_handle, error_reason)``.
    """
    if sys.platform != 'win32':
        return None, 'not win32'
    handle = getattr(proc, '_handle', None)
    if not handle:
        return None, 'subprocess has no Windows process handle'
    try:
        kernel32 = _win32_kernel32()
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, f'CreateJobObjectW failed (winerror={ctypes.get_last_error()})'

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ('ReadOperationCount', ctypes.c_uint64),
                ('WriteOperationCount', ctypes.c_uint64),
                ('OtherOperationCount', ctypes.c_uint64),
                ('ReadTransferCount', ctypes.c_uint64),
                ('WriteTransferCount', ctypes.c_uint64),
                ('OtherTransferCount', ctypes.c_uint64),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', ctypes.c_uint32),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', ctypes.c_uint32),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', ctypes.c_uint32),
                ('SchedulingClass', ctypes.c_uint32),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ('IoInfo', _IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok_set = kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok_set:
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return None, f'SetInformationJobObject failed (winerror={err})'
        ok_assign = kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(handle)))
        if not ok_assign:
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            return None, f'AssignProcessToJobObject failed (winerror={err})'
        return int(job), None
    except Exception as exc:
        return None, str(exc)


def _resume_windows_process(proc: subprocess.Popen[Any]) -> bool:
    """Resume a process spawned with ``CREATE_SUSPENDED`` via documented Win32 APIs."""
    if sys.platform != 'win32':
        return True
    try:
        kernel32 = _win32_kernel32()
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.ResumeThread.restype = ctypes.c_uint32
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        class _THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', ctypes.c_uint32),
                ('cntUsage', ctypes.c_uint32),
                ('th32ThreadID', ctypes.c_uint32),
                ('th32OwnerProcessID', ctypes.c_uint32),
                ('tpBasePri', ctypes.c_long),
                ('tpDeltaPri', ctypes.c_long),
                ('dwFlags', ctypes.c_uint32),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snap or snap == _INVALID_HANDLE_VALUE:
            return False
        resumed = False
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(_THREADENTRY32)
            if kernel32.Thread32First(snap, ctypes.byref(entry)):
                while True:
                    if entry.th32OwnerProcessID == proc.pid:
                        th = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                        if th:
                            try:
                                prev = kernel32.ResumeThread(th)
                                if prev != 0xFFFFFFFF:
                                    resumed = True
                            finally:
                                kernel32.CloseHandle(th)
                    if not kernel32.Thread32Next(snap, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return resumed
    except Exception:
        return False


def _terminate_windows_job(job_handle: int) -> bool:
    try:
        kernel32 = _win32_kernel32()
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return bool(kernel32.TerminateJobObject(ctypes.c_void_p(job_handle), 1))
    except Exception:
        return False


def _close_windows_job(job_handle: int | None) -> None:
    if not job_handle:
        return
    try:
        kernel32 = _win32_kernel32()
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(ctypes.c_void_p(job_handle))
    except Exception:
        pass


def spawn_managed_process(cmd: Sequence[str], **popen_kwargs: Any) -> subprocess.Popen[Any]:
    """Spawn a subprocess isolated in its own process group (Unix) or Job Object (Windows).

    On Windows, the process is created suspended, bound to a kill-on-close Job
    Object *before* its first instruction executes, and then resumed. If
    resuming fails, the suspended process is killed before raising.
    """
    kwargs = dict(popen_kwargs)
    if sys.platform != 'win32':
        kwargs.setdefault('start_new_session', True)
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
        _state_for_proc(proc)
        return proc

    base_flags = int(kwargs.get('creationflags', 0))
    create_new_group = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
    kwargs['creationflags'] = base_flags | create_new_group | _CREATE_SUSPENDED
    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
    state = _state_for_proc(proc)
    job, job_err = _create_and_assign_windows_job(proc)
    if job is None:
        # Fail closed: never resume a process whose Job Object failed to bind,
        # otherwise a fast-exiting wrapper could leak unmonitored descendants.
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        with state.lock:
            state.reaped = proc.returncode is not None
            state.done.set()
        raise RuntimeError(f'failed to bind Windows Job Object for process {proc.pid}: {job_err}')
    with state.lock:
        state.job_handle = job
    if not _resume_windows_process(proc):
        _terminate_windows_job(job)
        _close_windows_job(job)
        with state.lock:
            state.job_handle = None
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        with state.lock:
            state.reaped = proc.returncode is not None
            state.done.set()
        raise RuntimeError(f'failed to resume suspended Windows child process {proc.pid}')
    return proc


def register_managed_process(proc: subprocess.Popen[Any]) -> None:
    """Register an already-spawned ``Popen`` for coordinated reaping/teardown."""
    state = _state_for_proc(proc)
    with state.lock:
        if state.job_handle is None and sys.platform == 'win32':
            job, _err = _create_and_assign_windows_job(proc)
            state.job_handle = job


def mark_termination_requested(proc: subprocess.Popen[Any]) -> bool:
    """Atomically claim teardown ownership so normal waits cannot reap ``proc`` first.

    Returns True if teardown is active or may proceed, False only if ``proc``
    was already reaped with no active teardown in flight. Never publishes
    ``state.done`` while ``state.terminating`` is True.
    """
    state = _state_for_proc(proc)
    with state.lock:
        if state.terminating:
            state.termination_requested = True
            return True
        if state.reaped or proc.returncode is not None:
            state.reaped = True
            state.done.set()
            return False
        state.termination_requested = True
        return True


# ---------------------------------------------------------------------------
# Unix process-group inspection helpers (never reap the leader)
# ---------------------------------------------------------------------------

LeaderProbeState = Literal['running', 'exited', 'reaped', 'unsupported']


def _probe_leader_without_reap(pid: int) -> LeaderProbeState:
    """Probe child ``pid`` without reaping it, distinguishing ECHILD from unsupported."""
    if not (
        hasattr(os, 'waitid')
        and hasattr(os, 'P_PID')
        and hasattr(os, 'WEXITED')
        and hasattr(os, 'WNOWAIT')
        and hasattr(os, 'WNOHANG')
    ):
        return 'unsupported'
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        return 'exited' if info is not None else 'running'
    except ChildProcessError:
        # ECHILD: ``pid`` is no longer our unreaped child (already reaped
        # externally). Callers must NOT treat the PID/PGID as still owned.
        return 'reaped'
    except OSError:
        return 'unsupported'


def _pgroup_has_live_members(pgid: int, *, timeout: float = 1.0) -> tuple[bool | None, bool]:
    """Check whether ANY non-zombie process (leader or descendant) is alive in ``pgid``.

    Returns ``(result, timed_out)`` where ``result`` is:
        True  - at least one non-zombie member is alive in ``pgid``.
        False - no non-zombie members remain in ``pgid``.
        None  - unable to inspect process table (sandbox or timeout).
    """
    proc_root = Path('/proc')
    if proc_root.is_dir():
        try:
            for entry in os.scandir(proc_root):
                if not entry.name.isdigit():
                    continue
                try:
                    stat_text = (proc_root / entry.name / 'stat').read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                rparen = stat_text.rfind(')')
                if rparen == -1:
                    continue
                fields = stat_text[rparen + 2:].split()
                if len(fields) < 3:
                    continue
                state = fields[0]
                try:
                    member_pgid = int(fields[2])
                except ValueError:
                    continue
                if member_pgid == pgid and state != 'Z':
                    return True, False
            return False, False
        except OSError:
            pass

    effective_timeout = max(0.05, min(timeout, 1.5))
    try:
        result = subprocess.run(
            ['ps', '-axo', 'pid=,pgid=,stat='],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        return None, True
    except OSError:
        return None, False
    if result.returncode != 0:
        return None, False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            member_pgid = int(parts[1])
        except ValueError:
            continue
        stat = parts[2]
        if member_pgid == pgid and not stat.startswith('Z'):
            return True, False
    return False, False


# ---------------------------------------------------------------------------
# Coordinated wait & termination
# ---------------------------------------------------------------------------

def wait_for_process_exit(proc: subprocess.Popen[Any], *, timeout: float | None = None) -> int:
    """Wait for ``proc`` to exit and reap it under the shared ownership state.

    If ``mark_termination_requested(proc)`` or ``terminate_process_tree(proc)``
    has claimed teardown ownership, this yields to teardown and waits on
    ``state.done`` instead of racing to reap ``proc`` while group signals are
    still pending or in flight.
    """
    state = _state_for_proc(proc)
    deadline = None if timeout is None else (time.monotonic() + max(timeout, 0.0))
    while True:
        job_to_close: int | None = None
        exit_code: int | None = None
        with state.lock:
            owned_by_teardown = state.terminating or state.termination_requested
            if not owned_by_teardown:
                rc = proc.returncode if proc.returncode is not None else proc.poll()
                if rc is not None:
                    state.reaped = True
                    job_to_close = state.job_handle
                    state.job_handle = None
                    state.done.set()
                    exit_code = rc
        if job_to_close is not None:
            _close_windows_job(job_to_close)
        if exit_code is not None:
            return exit_code

        remaining = None if deadline is None else (deadline - time.monotonic())
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(cmd=getattr(proc, 'args', '<proc>'), timeout=timeout or 0.0)

        if owned_by_teardown:
            wait_step = remaining if remaining is not None else 0.1
            state.done.wait(timeout=min(wait_step, 0.1))
        else:
            sleep_for = 0.02 if remaining is None else min(0.02, remaining)
            time.sleep(sleep_for)


def _terminate_windows_tree(proc: subprocess.Popen[Any], state: _ProcessState, *, grace_sec: float) -> None:
    """Terminate a process tree on Windows without losing the tree root."""
    pid = proc.pid
    # 1. Gentle tree termination via taskkill /T (never call proc.terminate()
    # on failure, because killing the wrapper parent alone could orphan
    # children before TerminateJobObject or taskkill /F /T can act).
    try:
        subprocess.run(
            ['taskkill', '/T', '/PID', str(pid)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

    deadline = time.monotonic() + max(grace_sec, 0.0)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.02)

    # 2. Force-kill the entire tree: Job Object first (kills descendants even
    # if the leader wrapper already exited), then taskkill /F /T as fallback.
    with state.lock:
        job = state.job_handle
        state.job_handle = None

    job_killed = False
    if job is not None:
        job_killed = _terminate_windows_job(job)
        _close_windows_job(job)

    if not job_killed:
        try:
            res = subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
            )
            taskkill_forced = res.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            taskkill_forced = False
        if not taskkill_forced and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def terminate_process_tree(
    proc: subprocess.Popen[Any],
    *,
    grace_sec: float = 2.0,
    join_timeout_sec: float | None = None,
) -> None:
    """Terminate ``proc`` and its entire process group/tree safely.

    Shares ownership with ``mark_termination_requested`` and
    ``wait_for_process_exit`` so no other thread can reap ``proc`` before
    teardown completes. Concurrent callers wait for the active teardown and
    raise ``TimeoutError`` if ``join_timeout_sec`` elapses while teardown is
    still running.
    """
    state = _state_for_proc(proc)
    with state.lock:
        if state.terminating:
            # Another caller is actively tearing down this process (including
            # Job Object cleanup after proc.returncode is already set); wait
            # for it to finish instead of returning early or publishing done.
            wait_for_other = True
        elif state.reaped or proc.returncode is not None:
            state.reaped = True
            state.done.set()
            return
        else:
            state.termination_requested = True
            state.terminating = True
            state.done.clear()
            wait_for_other = False

    if wait_for_other:
        wait_limit = (max(grace_sec, 0.0) + 15.0) if join_timeout_sec is None else max(join_timeout_sec, 0.0)
        if not state.done.wait(timeout=wait_limit):
            raise TimeoutError(
                f'timed out after {wait_limit:.2f}s waiting for active process-tree teardown of PID {proc.pid}'
            )
        return

    try:
        if sys.platform == 'win32':
            _terminate_windows_tree(proc, state, grace_sec=grace_sec)
            return

        # Verify ownership right before signalling PGID: if external code reaped
        # ``proc`` behind our back, ECHILD tells us the PID is no longer pinned.
        if _probe_leader_without_reap(proc.pid) == 'reaped':
            return

        pgid = proc.pid
        used_pg = True
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            return
        except OSError:
            used_pg = False
            try:
                proc.terminate()
            except OSError:
                return

        deadline = time.monotonic() + max(grace_sec, 0.0)
        needs_sigkill = True
        ps_stalled = False

        if used_pg:
            sandbox_settle_deadline: float | None = None
            while True:
                now = time.monotonic()
                remaining = deadline - now
                live, timed_out = (
                    (None, True)
                    if ps_stalled
                    else _pgroup_has_live_members(pgid, timeout=max(0.05, remaining))
                )
                if timed_out:
                    ps_stalled = True
                if live is False:
                    needs_sigkill = False
                    break
                if live is None:
                    leader_state = _probe_leader_without_reap(proc.pid)
                    if leader_state == 'reaped':
                        # External reap lost PGID ownership; never SIGKILL PGID.
                        needs_sigkill = False
                        break
                    now = time.monotonic()
                    if leader_state == 'exited':
                        if sandbox_settle_deadline is None:
                            sandbox_settle_deadline = min(deadline, now + 0.15)
                        if now >= sandbox_settle_deadline:
                            break
                    elif leader_state == 'unsupported':
                        if sandbox_settle_deadline is None:
                            sandbox_settle_deadline = min(deadline, now + 0.35)
                        if now >= sandbox_settle_deadline:
                            break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        else:
            try:
                proc.wait(timeout=max(grace_sec, 0.0))
                needs_sigkill = False
            except subprocess.TimeoutExpired:
                needs_sigkill = True

        if needs_sigkill:
            if used_pg:
                # Re-verify PGID ownership before SIGKILL: never signal a group
                # whose leader was externally reaped.
                if proc.returncode is None and _probe_leader_without_reap(proc.pid) != 'reaped':
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    if not ps_stalled:
                        kill_deadline = time.monotonic() + 2.0
                        while time.monotonic() < kill_deadline:
                            rem = kill_deadline - time.monotonic()
                            live, timed_out = _pgroup_has_live_members(pgid, timeout=max(0.05, rem))
                            if live is False or live is None or timed_out:
                                break
                            time.sleep(0.02)
            else:
                try:
                    proc.kill()
                except OSError:
                    pass

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    finally:
        with state.lock:
            if proc.returncode is not None:
                state.reaped = True
            job = state.job_handle
            state.job_handle = None
        if job is not None:
            _close_windows_job(job)
        with state.lock:
            state.termination_requested = False
            state.terminating = False
            state.done.set()
