"""Import a tunnel from a project's .env files, so a repo's existing settings
(e.g. DB_TUNNEL_HOST=…, DB_TUNNEL_REMOTE_PORT=…) need not be typed in again.

For a prefix P the importer reads (first non-empty wins):

    ssh host     P_SSH_HOST, P_BASTION_HOST, P_HOST
    ssh port     P_SSH_PORT, P_BASTION_PORT              (default 22)
    ssh user     P_SSH_USER, P_BASTION_USER
    auth         P_SSH_KEY (key file) or P_PASSWORD_FILE  (else ssh-agent)
    remote       P_REMOTE_HOST (default 127.0.0.1), P_REMOTE_PORT
    local port   P_LOCAL_PORT, then M_PORT

ssh settings fall back to a second prefix when one tunnel borrows another's login.
With a MySQL prefix M, M_USER / M_PASSWORD / M_DATABASE switch on a SELECT 1 check.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Mapping

from tunnel_keeper.config import ConfigError, Tunnel, validate
from tunnel_keeper.secretstore import write_secret

SecretWriter = Callable[[str, str], str]


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; '#' lines skipped; one layer of surrounding quotes stripped.
    Inline comments are NOT stripped (dotenv keeps them in the value too)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def load_env(path: Path) -> dict[str, str]:
    """A single env file, or a folder's .env overlaid by its .env.local."""
    if path.is_file():
        return {k: v for k, v in parse_env_file(path).items() if v != ""}
    merged: dict[str, str] = {}
    for name in (".env", ".env.local"):
        candidate = path / name
        if candidate.exists():
            merged = {**merged, **{k: v for k, v in parse_env_file(candidate).items() if v != ""}}
    return merged


def _first(env: Mapping[str, str], keys: list[str], default: str = "") -> str:
    return next((env[k] for k in keys if env.get(k)), default)


def _int(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{label} must be a number, got '{value}'") from exc


def _keys(prefixes: list[str], *suffixes: str) -> list[str]:
    return [f"{p}_{s}" for p in prefixes if p for s in suffixes]


def tunnel_from_env(
    env: Mapping[str, str],
    name: str,
    prefix: str,
    fallback_prefix: str = "",
    mysql_prefix: str = "",
    write: SecretWriter = write_secret,
) -> Tunnel:
    ssh = [prefix, fallback_prefix]
    key = _first(env, _keys(ssh, "SSH_KEY"))
    password_file = _first(env, _keys(ssh, "PASSWORD_FILE"))
    auth_type, auth_path = ("key", key) if key else ("password_file", password_file) if password_file else ("agent", "")

    remote_port = _first(env, _keys([prefix], "REMOTE_PORT"))
    if not remote_port:
        raise ConfigError(f"{prefix}_REMOTE_PORT is not set")
    local_port = _first(env, _keys([prefix], "LOCAL_PORT") + _keys([mysql_prefix], "PORT"))
    if not local_port:
        raise ConfigError(f"{prefix}_LOCAL_PORT is not set (pass --local-port)")

    check: dict[str, str] = {"check_type": "tcp"}
    user = env.get(f"{mysql_prefix}_USER", "") if mysql_prefix else ""
    if user:
        check = {"check_type": "mysql", "check_user": user,
                 "check_database": env.get(f"{mysql_prefix}_DATABASE", "")}

    tunnel = validate(Tunnel(
        name=name,
        ssh_host=_first(env, _keys(ssh, "SSH_HOST", "BASTION_HOST", "HOST")),
        ssh_port=_int(_first(env, _keys(ssh, "SSH_PORT", "BASTION_PORT"), "22"), f"{prefix}_SSH_PORT"),
        ssh_user=_first(env, _keys(ssh, "SSH_USER", "BASTION_USER")),
        auth_type=auth_type,
        auth_path=auth_path,
        local_port=_int(local_port, f"{prefix}_LOCAL_PORT"),
        remote_host=_first(env, _keys([prefix], "REMOTE_HOST"), "127.0.0.1"),
        remote_port=_int(remote_port, f"{prefix}_REMOTE_PORT"),
        **check,
    ))
    password = env.get(f"{mysql_prefix}_PASSWORD", "") if user else ""
    if not password:
        return tunnel
    # Only a valid definition may write the password file.
    return dataclasses.replace(tunnel, check_password_file=write(name, password))
