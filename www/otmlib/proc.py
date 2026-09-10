"""Cancellable subprocess runner (mkgmap, splitter, osmium)."""

from __future__ import annotations

import contextvars
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

cancel_event: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "cancel_event",
    default=None,
)


HEARTBEAT_S = 30.0


class BuildCancelled(Exception):
    """Map build was cancelled by the user."""


def _cmd_product(cmd: list[str]) -> Path | None:
    """The file a long command is writing, if the invocation names one.

    External tools (tilemaker, osmium) put the real work there; the captured log stays small
    while they run, so a heartbeat that measures the log looks frozen.
    """
    for flag in ("--output", "-o"):
        if flag in cmd:
            index = cmd.index(flag)
            if index + 1 < len(cmd):
                return Path(cmd[index + 1])
    return None


def _human_size(path: Path) -> str:
    """Size of a file on disk, as a sign of life."""
    try:
        size = path.stat().st_size
    except OSError:
        return "n/a"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


def check_cancelled() -> None:
    event = cancel_event.get()
    if event is not None and event.is_set():
        raise BuildCancelled("cancelled")


def worker_count(items: int, env_var: str, *, cap: int = 0) -> int:
    """Workers for a batch of *items*, overridable via *env_var*.

    Leaves two cores for the OS and for the writer threads osmium spawns.
    """
    override = os.environ.get(env_var)
    if override:
        try:
            return max(1, min(items, int(override)))
        except ValueError:
            log.warning("%s=%r is not a number, ignoring", env_var, override)
    jobs = max(1, (os.cpu_count() or 2) - 2)
    if cap:
        jobs = min(jobs, cap)
    return max(1, min(items, jobs))


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        proc.wait(timeout=3)


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    check_cancelled()
    log.info("$ %s", " ".join(cmd))
    err_file = tempfile.NamedTemporaryFile(prefix="otm-cmd-", suffix=".log", delete=False)
    err_path = Path(err_file.name)
    kwargs: dict = {
        "cwd": cwd,
        "stdout": err_file,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        started = time.monotonic()
        next_beat = started + HEARTBEAT_S
        while proc.poll() is None:
            event = cancel_event.get()
            if event is not None and event.is_set():
                _terminate(proc)
                raise BuildCancelled("cancelled")
            now = time.monotonic()
            if now >= next_beat:
                next_beat = now + HEARTBEAT_S
                product = _cmd_product(cmd)
                watching = product if product is not None and product.exists() else err_path
                log.info(
                    "… %s still running (%.0fs, %s %s)",
                    Path(cmd[0]).name,
                    now - started,
                    "written" if watching != err_path else "log",
                    _human_size(watching),
                )
            time.sleep(0.2)
    except BuildCancelled:
        raise
    except Exception:
        if proc is not None:
            _terminate(proc)
        raise
    finally:
        err_file.close()

    output = ""
    try:
        output = err_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    err_path.unlink(missing_ok=True)
    if proc is None:
        raise RuntimeError(f"Failed to start: {cmd}")
    if proc.returncode:
        if output.strip():
            log.error("%s", output[-8000:])
        tail = "\n".join(output.strip().splitlines()[-15:])
        detail = f"\n{tail}" if tail else ""
        raise RuntimeError(
            f"Command failed with exit {proc.returncode}: {Path(cmd[0]).name}{detail}"
        )
