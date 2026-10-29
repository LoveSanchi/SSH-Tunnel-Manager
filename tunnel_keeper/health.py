"""Health probes: is the local port listening, which ssh owns it, does MySQL answer."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from tunnel_keeper import paths
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
    if tunnel.check_type == "mysql":
        return mysql_probe(tunnel)
    ok = port_open(tunnel.local_port)
    return Probe(ok, "port open" if ok else "port closed", reachable=ok)
