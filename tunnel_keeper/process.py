"""Starting and stopping ssh processes, and their per-tunnel log files."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

from tunnel_keeper.sshcmd import Command

TERM_GRACE_SECONDS = 3.0
MAX_LOG_BYTES = 512 * 1024


class Handle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


class ProcessHandle:
    """A launched ssh (or sshpass→ssh) process, run in its own process group so
    stopping it also stops any child it spawned."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self.pid = proc.pid

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        if self._proc.poll() is not None:
            return
        _signal_group(self.pid, signal.SIGTERM)
        try:
            self._proc.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(self.pid, signal.SIGKILL)
            self._proc.wait(timeout=TERM_GRACE_SECONDS)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _redact(argv: tuple[str, ...]) -> str:
    # sshpass -f <file> only names a file, never the secret itself, so argv is safe to log.
    return " ".join(argv)


def _rotate(log_path: Path) -> None:
    try:
        if log_path.stat().st_size > MAX_LOG_BYTES:
            log_path.replace(log_path.with_suffix(".log.1"))
    except FileNotFoundError:
        pass


def session_env() -> dict[str, str]:
    """SSH_AUTH_SOCK as the desktop session sees it *now*. The service may have started
    before GNOME imported it into the systemd user environment."""
    if os.environ.get("SSH_AUTH_SOCK"):
        return {}
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    for line in out.splitlines():
        if line.startswith("SSH_AUTH_SOCK="):
            return {"SSH_AUTH_SOCK": line.split("=", 1)[1]}
    return {}


def launch(cmd: Command, log_path: Path) -> ProcessHandle:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _rotate(log_path)
    with open(log_path, "ab") as log:
        stamp = datetime.now().isoformat(timespec="seconds")
        log.write(f"--- {stamp} starting: {_redact(cmd.argv)}\n".encode())
        log.flush()
        proc = subprocess.Popen(
            cmd.argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env={**os.environ, **session_env(), **cmd.env},
            start_new_session=True,
            close_fds=True,
        )
    return ProcessHandle(proc)


def prepare_control_socket(control_path: Path) -> None:
    """ssh will not create the socket's folder, and refuses to be a master over a stale socket."""
    control_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    control_path.unlink(missing_ok=True)


def kill_pids(pids: list[int]) -> None:
    """Stop ssh processes we did not start (e.g. opened by a repo script)."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [p for p in remaining if _alive(p)]
        if remaining:
            time.sleep(0.1)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def last_log_line(log_path: Path) -> str:
    """The last line ssh wrote in its current run — usually the reason it exited."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("---"):  # start marker of the current run: older lines are stale
            return ""
        if line:
            return line[:200]
    return ""
