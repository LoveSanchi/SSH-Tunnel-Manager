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


def _ssh_argv(t: Tunnel) -> list[str]:
    argv = ["ssh", "-N", "-p", str(t.ssh_port), "-L", t.forward_spec]
    for option in SSH_OPTIONS:
        argv += ["-o", option]
    return argv


def build(t: Tunnel, sshpass: str | None = None, askpass: Path = ASKPASS) -> Command:
    """sshpass is looked up on PATH when not given; pass "" to force the askpass route."""
    argv = _ssh_argv(t)
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
