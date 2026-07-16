"""Run ffmpeg/ffprobe subprocesses with a hard timeout.

Without a timeout a malformed or oversized input can wedge an encode and hang
the worker forever. `run()` bounds every call and turns an expiry into a clear
error; the child process is killed on timeout by subprocess itself.
"""

from __future__ import annotations

import subprocess

from ..log import get_logger

log = get_logger(__name__)


def run(
    cmd: list[str], *, timeout: float | None, cwd: str | None = None
) -> subprocess.CompletedProcess:
    """Run `cmd` capturing output, bounded by `timeout` seconds. Raises
    RuntimeError on timeout (the child is killed); otherwise returns the
    CompletedProcess so the caller can inspect returncode/stderr."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        log.error("%s timed out after %ss", cmd[0], timeout)
        raise RuntimeError(
            f"{cmd[0]} timed out after {timeout}s — the input may be too large "
            f"or malformed."
        ) from exc
