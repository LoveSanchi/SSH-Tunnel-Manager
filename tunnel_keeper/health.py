"""Health probes: is the local port listening, which ssh owns it, does MySQL answer."""

from __future__ import annotations

import os
import selectors
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from typing import Callable

from tunnel_keeper import paths, sshcmd
from tunnel_keeper.config import Tunnel

PORT_TIMEOUT_SECONDS = 2.0
MYSQL_TIMEOUT_SECONDS = 4


# MySQL server-side errors (1xxx: access denied, too many connections, unknown db…)
# prove the far side answered, so the tunnel itself works. 2xxx are client errors
# such as "can't connect" or "lost connection" — those do mean the path is broken.
_CLIENT_ERROR_MIN = 2000


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str
    # True when the far end answered, even with an error: restarting ssh won't help.
    reachable: bool = False


def port_open(port: int, timeout: float = PORT_TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_cmdline(pid_dir: Path) -> list[str]:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _forwards(argv: list[str], spec: str) -> bool:
    for i, arg in enumerate(argv):
        if arg == "-L" and i + 1 < len(argv) and argv[i + 1] == spec:
            return True
        if arg == f"-L{spec}":
            return True
    return False


def ssh_pids_for(spec: str, proc_root: Path = Path("/proc"), uid: int | None = None) -> list[int]:
    """PIDs of this user's `ssh` processes (ours or started elsewhere) forwarding exactly `spec`."""
    uid = os.getuid() if uid is None else uid
    pids = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = _read_cmdline(entry)
        if argv and os.path.basename(argv[0]) == "ssh" and _forwards(argv, spec):
            pids.append(int(entry.name))
    return sorted(pids)


def _read_secret(path: str) -> str:
    if not path:
        return ""
    with open(paths.expand(path), encoding="utf-8") as fh:
        return fh.read().strip()


def mysql_probe(tunnel: Tunnel) -> Probe:
    """Run `SELECT 1` through the tunnel. A listening port alone proves nothing:
    ssh keeps accepting local connections after its bastion→DB leg has died."""
    try:
        import pymysql  # optional: degrade to a port check without it
    except ImportError:
        ok = port_open(tunnel.local_port)
        return Probe(ok, "port open (pymysql missing, SELECT 1 skipped)" if ok else "port closed", reachable=ok)
    try:
        password = _read_secret(tunnel.check_password_file)
    except (OSError, ValueError) as exc:
        # A config problem, not a tunnel problem: never restart ssh over it.
        return Probe(False, f"cannot read mysql password file: {getattr(exc, 'strerror', None) or exc}",
                     reachable=True)
    try:
        conn = pymysql.connect(
            host="127.0.0.1",
            port=tunnel.local_port,
            user=tunnel.check_user,
            password=password,
            database=tunnel.check_database or None,
            connect_timeout=MYSQL_TIMEOUT_SECONDS,
            read_timeout=MYSQL_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - pymysql raises more than MySQLError
        return _failed(exc)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return Probe(True, "SELECT 1 ok", reachable=True)
    except Exception as exc:  # noqa: BLE001
        return _failed(exc)
    finally:
        conn.close()


def _failed(exc: Exception) -> Probe:
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    message = exc.args[-1] if exc.args else exc
    server_answered = code is not None and 1000 <= code < _CLIENT_ERROR_MIN
    prefix = "MySQL answered with an error" if server_answered else "SELECT 1 failed"
    return Probe(False, f"{prefix}: {message}", reachable=server_answered)


def probe(tunnel: Tunnel) -> Probe:
    if not port_open(tunnel.local_port):
        return Probe(False, f"nothing listening on local port {tunnel.local_port}")
    if tunnel.check_type == "mysql":
        return mysql_probe(tunnel)
    return tcp_probe(tunnel)


# ── tcp: can the far end be reached through the tunnel? ──────────────────────
# Connecting to the local port proves nothing: when the bastion cannot reach the target
# its connect just hangs, which looks exactly like a quiet, healthy service. So ssh is
# asked directly whether it could open remote_host:remote_port, and its answer is read.
TCP_PROBE_TIMEOUT_SECONDS = 10.0
_OPENED = ("mux_client_request_stdio_fwd: master session id", "channel_input_open_confirmation")
_REFUSED = ("open failed", "Session open refused", "forwarding request failed")
_MUX_BROKEN = ("Control socket connect", "mux_client_hello_exchange", "master hello")

Runner = Callable[[sshcmd.Command, float], "tuple[str, list[str]]"]


def watch_ssh(cmd: sshcmd.Command, timeout: float) -> tuple[str, list[str]]:
    """Run an `ssh -W` probe and classify it from stderr: "opened", "refused", "timeout"
    or "exited" (ssh quit without either answer). The probe is always killed afterwards."""
    proc = subprocess.Popen(cmd.argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env={**os.environ, **cmd.env}, start_new_session=True)
    lines: list[str] = []
    buffer = b""
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stderr, selectors.EVENT_READ)
            while (left := deadline - time.monotonic()) > 0:
                if not sel.select(timeout=left):
                    continue
                chunk = os.read(proc.stderr.fileno(), 4096)
                if not chunk:
                    return "exited", lines
                buffer += chunk
                *complete, buffer = buffer.split(b"\n")
                for raw in complete:
                    line = raw.decode("utf-8", "replace").strip()
                    lines.append(line)
                    if any(m in line for m in _OPENED):
                        return "opened", lines
                    if any(m in line for m in _REFUSED):
                        return "refused", lines
            return "timeout", lines
    finally:
        _kill_group(proc)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    for stream in (proc.stdin, proc.stderr):
        if stream:
            stream.close()


def _reason(lines: list[str]) -> str:
    for line in reversed(lines):
        if "connect failed:" in line:
            return line.split("connect failed:", 1)[1].strip()
    return "connection refused (port closed, or the bastion does not allow forwarding there)"


def tcp_probe(tunnel: Tunnel, run: Runner = watch_ssh, timeout: float = TCP_PROBE_TIMEOUT_SECONDS) -> Probe:
    target = f"{tunnel.remote_host}:{tunnel.remote_port}"
    owned = paths.control_path(tunnel.name).exists()
    cmd = sshcmd.build_mux_probe(tunnel) if owned else sshcmd.build_standalone_probe(tunnel)
    outcome, lines = run(cmd, timeout)
    if outcome == "opened":
        return Probe(True, f"far end {target} reachable", reachable=True)
    if outcome == "refused":
        # The bastion answered: the problem is beyond it, reopening ssh will not help.
        return Probe(False, f"bastion could not connect to {target}: {_reason(lines)}", reachable=True)
    if outcome == "timeout":
        return Probe(False, f"no answer from {target} within {int(timeout)}s "
                            f"(unreachable or filtered from the bastion)", reachable=True)
    last = next((ln for ln in reversed(lines) if ln and not ln.startswith("debug")), "no output")
    # ssh itself failed (dead control socket, login refused…): that is ours to fix.
    return Probe(False, f"check could not run: {last[:160]}", reachable=False)
