"""PTY-based shell command execution with color support.

NOTE: PTY support is Unix-only. On Windows, run_in_pty raises NotImplementedError.
Use the fallback in commands.py for Windows (interactive shell via subprocess).
"""

import asyncio
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Callable

# PTY support is Unix-only
UNIX_PTY_SUPPORT = sys.platform != "win32"

if UNIX_PTY_SUPPORT or TYPE_CHECKING:
    import pty
    import select


def run_in_pty(
    cmd: str, shell: str, cwd: str | None, env: dict[str, str]
) -> tuple[str, int]:
    """Run command in PTY to capture colors.

    Returns (output, returncode) tuple.

    Raises NotImplementedError on Windows (PTY not available).
    """
    if not UNIX_PTY_SUPPORT:
        raise NotImplementedError("PTY shell execution is not available on Windows")

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [shell, "-lc", cmd],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)

        output = b""
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if r:
                try:
                    data = os.read(master_fd, 4096)
                    if data:
                        output += data
                    else:
                        break
                except OSError:
                    break
            elif proc.poll() is not None:
                # Process done, drain remaining output
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.05)
                    if not r:
                        break
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            output += data
                        else:
                            break
                    except OSError:
                        break
                break

        os.close(master_fd)
        proc.wait()
        # Strip \r from PTY output (onlcr maps \n to \r\n); Rich's from_ansi
        # treats bare \r as a cursor-return and collapses lines.
        decoded = output.decode(errors="replace").replace("\r", "")
        return decoded, proc.returncode or 0
    except Exception:
        os.close(master_fd)
        raise


def _run_in_pty_with_cancel(
    cmd: str,
    shell: str,
    cwd: str | None,
    env: dict[str, str],
    check_cancelled: Callable[[], bool],
) -> tuple[str, int, bool]:
    """Run command in PTY with cancellation support.

    Args:
        cmd: Command to run
        shell: Shell to use
        cwd: Working directory
        env: Environment variables
        check_cancelled: Callable that returns True if cancelled

    Returns:
        (output, returncode, was_cancelled) tuple
    """
    if not UNIX_PTY_SUPPORT:
        raise NotImplementedError("PTY shell execution is not available on Windows")

    master_fd, slave_fd = pty.openpty()
    proc = None
    try:
        proc = subprocess.Popen(
            [shell, "-lc", cmd],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        output = b""
        was_cancelled = False

        while True:
            # Check for cancellation
            if check_cancelled():
                was_cancelled = True
                # Kill the process group
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                break

            r, _, _ = select.select([master_fd], [], [], 0.1)
            if r:
                try:
                    data = os.read(master_fd, 4096)
                    if data:
                        output += data
                    else:
                        break
                except OSError:
                    break
            elif proc.poll() is not None:
                # Process done, drain remaining output
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.05)
                    if not r:
                        break
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            output += data
                        else:
                            break
                    except OSError:
                        break
                break

        os.close(master_fd)
        master_fd = -1
        proc.wait()
        decoded = output.decode(errors="replace").replace("\r", "")
        return decoded, proc.returncode or 0, was_cancelled

    except Exception:
        if slave_fd >= 0:
            os.close(slave_fd)
        if master_fd >= 0:
            os.close(master_fd)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        raise


async def run_in_pty_cancellable(
    cmd: str,
    shell: str,
    cwd: str | None,
    env: dict[str, str],
    cancel_event: asyncio.Event,
) -> tuple[str, int, bool]:
    """Run command in PTY with async cancellation support.

    Args:
        cmd: Command to run
        shell: Shell to use
        cwd: Working directory
        env: Environment variables
        cancel_event: Event that signals cancellation when set

    Returns:
        (output, returncode, was_cancelled) tuple
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _run_in_pty_with_cancel,
        cmd,
        shell,
        cwd,
        env,
        cancel_event.is_set,
    )
