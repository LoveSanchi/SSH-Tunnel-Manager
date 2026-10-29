"""Talk to the daemon over its Unix socket: one JSON line in, one JSON line out.

Every response uses the same envelope: {"ok": bool, "data": ..., "error": str|null}.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from tunnel_keeper import paths

MAX_MESSAGE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 90.0


class DaemonUnavailable(RuntimeError):
    """The background service is not running (or its socket is unreachable)."""


class DaemonError(RuntimeError):
    """The daemon refused or failed a request; the message is user-facing."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def read_line(sock: socket.socket) -> bytes:
    chunks, size = [], 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk:
            break
        if size > MAX_MESSAGE_BYTES:
            raise DaemonError("message too large")
    return b"".join(chunks).split(b"\n", 1)[0]


def request(cmd: str, sock_path: Path | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS, **args: Any) -> Any:
    """Send one command and return its `data`, or raise DaemonError / DaemonUnavailable."""
    sock_path = sock_path or paths.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall(encode({"cmd": cmd, **args}))
            raw = read_line(sock)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonUnavailable(
            "tunnel-keeper service is not running — start it with: systemctl --user start tunnel-keeper"
        ) from exc
    except socket.timeout as exc:
        raise DaemonError(f"no reply from tunnel-keeper within {int(timeout)}s", "timeout") from exc
    except OSError as exc:
        raise DaemonUnavailable(f"cannot reach tunnel-keeper at {sock_path}: {exc.strerror or exc}") from exc
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DaemonError("malformed reply from tunnel-keeper") from exc
    if not reply.get("ok"):
        raise DaemonError(reply.get("error") or "request failed", reply.get("code") or "error")
    return reply.get("data")
