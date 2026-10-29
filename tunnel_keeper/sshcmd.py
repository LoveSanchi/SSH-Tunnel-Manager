"""Build the ssh command line (and environment) that opens one tunnel."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from tunnel_keeper import paths
from tunnel_keeper.config import Tunnel

ASKPASS = Path(__file__).resolve().parent / "askpass.sh"

SSH_OPTIONS = (
    "ExitOnForwardFailure=yes",
    "ServerAliveInterval=30",
    "ServerAliveCountMax=3",
    "StrictHostKeyChecking=accept-new",
    "ConnectTimeout=10",
    "LogLevel=ERROR",  # hide login banners so the log tail is the real error
)


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    env: Mapping[str, str]


def _with_auth(t: Tunnel, argv: list[str], sshpass: str | None, askpass: Path) -> Command:
    """Append authentication options and the destination to an ssh argv."""
    argv = list(argv)
    destination = f"{t.ssh_user}@{t.ssh_host}"

    if t.auth_type == "password_file":
        secret = paths.expand(t.auth_path)
        # Skip agent keys: with several loaded, ssh can hit "Too many authentication
        # failures" before it ever tries the password.
        argv += ["-o", "NumberOfPasswordPrompts=1", "-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=password,keyboard-interactive", destination]
        sshpass = shutil.which("sshpass") if sshpass is None else sshpass
        if sshpass:
            return Command(argv=(sshpass, "-f", secret, *argv), env={})
        return Command(
            argv=tuple(argv),
            env={
                "SSH_ASKPASS": str(askpass),
                "SSH_ASKPASS_REQUIRE": "force",
                "TUNNEL_KEEPER_PASSWORD_FILE": secret,
            },
        )

    # Never let ssh block on an interactive prompt nobody will see.
    argv += ["-o", "BatchMode=yes"]
    if t.auth_type == "key":
        argv += ["-i", paths.expand(t.auth_path), "-o", "IdentitiesOnly=yes"]
    return Command(argv=(*argv, destination), env={})


def build(t: Tunnel, sshpass: str | None = None, askpass: Path = ASKPASS) -> Command:
    """The long-running tunnel. It is also a ControlMaster, so the far-end check can open
    a connection through this very session without logging in again.
    sshpass is looked up on PATH when not given; pass "" to force the askpass route."""
    argv = ["ssh", "-N", "-p", str(t.ssh_port), "-L", t.forward_spec]
    for option in SSH_OPTIONS:
        argv += ["-o", option]
    argv += ["-o", "ControlMaster=yes", "-o", f"ControlPath={paths.control_path(t.name)}",
             "-o", "ControlPersist=no"]
    return _with_auth(t, argv, sshpass, askpass)


def build_mux_probe(t: Tunnel) -> Command:
    """Ask the running tunnel's ssh to open remote_host:remote_port, through its own session."""
    return Command(
        argv=("ssh", "-v", "-S", str(paths.control_path(t.name)), "-o", "BatchMode=yes",
              "-W", f"{t.remote_host}:{t.remote_port}", "tunnel-keeper-probe"),
        env={},
    )


def build_standalone_probe(t: Tunnel, sshpass: str | None = None, askpass: Path = ASKPASS) -> Command:
    """Same question over a fresh login — for a tunnel some other tool opened (no control socket)."""
    argv = ["ssh", "-vv", "-p", str(t.ssh_port), "-W", f"{t.remote_host}:{t.remote_port}",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    return _with_auth(t, argv, sshpass, askpass)
