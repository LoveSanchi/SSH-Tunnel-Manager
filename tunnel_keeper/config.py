"""Tunnel definitions: an immutable model, validation, and atomic load/save."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from tunnel_keeper import paths

CONFIG_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 120
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 3600

AUTH_TYPES = ("agent", "key", "password_file")
CHECK_TYPES = ("port", "mysql")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Hosts and users end up as ssh arguments: a leading "-" would be read as an option.
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]][A-Za-z0-9_.:\[\]-]{0,252}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,63}$")


class ConfigError(ValueError):
    """A tunnel definition or config file is invalid."""


@dataclass(frozen=True)
class Tunnel:
    name: str
    ssh_host: str
    ssh_user: str
    local_port: int
    remote_host: str
    remote_port: int
    ssh_port: int = 22
    enabled: bool = True
    auth_type: str = "agent"
    auth_path: str = ""
    check_type: str = "port"
    check_user: str = ""
    check_password_file: str = ""
    check_database: str = ""

    @property
    def forward_spec(self) -> str:
        return f"127.0.0.1:{self.local_port}:{self.remote_host}:{self.remote_port}"

    @property
    def summary(self) -> str:
        return f":{self.local_port} → {self.remote_host}:{self.remote_port} via {self.ssh_user}@{self.ssh_host}"


@dataclass(frozen=True)
class Config:
    tunnels: tuple[Tunnel, ...] = ()
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    path: Path = field(default_factory=paths.config_file, compare=False)

    def get(self, name: str) -> Tunnel | None:
        return next((t for t in self.tunnels if t.name == name), None)

    def by_port(self, port: int) -> Tunnel | None:
        return next((t for t in self.tunnels if t.local_port == port), None)

    def with_tunnel(self, tunnel: Tunnel) -> "Config":
        """Return a copy with `tunnel` added, or replacing the one of the same name."""
        others = tuple(t for t in self.tunnels if t.name != tunnel.name)
        clash = next((t for t in others if t.local_port == tunnel.local_port), None)
        if clash:
            raise ConfigError(f"local port {tunnel.local_port} is already used by tunnel '{clash.name}'")
        validate(tunnel)
        if self.get(tunnel.name) is None:
            return dataclasses.replace(self, tunnels=self.tunnels + (tunnel,))
        return dataclasses.replace(
            self, tunnels=tuple(tunnel if t.name == tunnel.name else t for t in self.tunnels)
        )

    def replacing(self, old_name: str, tunnel: Tunnel) -> "Config":
        """Return a copy where `old_name` is replaced by `tunnel` (possibly renamed), in place."""
        if self.get(old_name) is None:
            raise ConfigError(f"no tunnel named '{old_name}'")
        others = tuple(t for t in self.tunnels if t.name != old_name)
        _require(not any(t.name == tunnel.name for t in others), f"a tunnel named '{tunnel.name}' already exists")
        clash = next((t for t in others if t.local_port == tunnel.local_port), None)
        _require(clash is None, f"local port {tunnel.local_port} is already used by tunnel '{clash.name if clash else ''}'")
        validate(tunnel)
        return dataclasses.replace(
            self, tunnels=tuple(tunnel if t.name == old_name else t for t in self.tunnels)
        )

    def with_interval(self, seconds: Any) -> "Config":
        _require(
            isinstance(seconds, int) and not isinstance(seconds, bool)
            and MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS,
            f"check interval must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds",
        )
        return dataclasses.replace(self, interval_seconds=seconds)

    def without_tunnel(self, name: str) -> "Config":
        if self.get(name) is None:
            raise ConfigError(f"no tunnel named '{name}'")
        return dataclasses.replace(self, tunnels=tuple(t for t in self.tunnels if t.name != name))


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def _port(value: Any, label: str, minimum: int = 1) -> None:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{label} must be a number")
    _require(minimum <= value <= 65535, f"{label} must be between {minimum} and 65535")


def validate(t: Tunnel) -> Tunnel:
    """Return `t` unchanged if valid, else raise ConfigError naming the bad field."""
    _require(bool(_NAME_RE.match(t.name)), "name must be 1-40 chars of a-z, 0-9, '-' or '_', starting alphanumeric")
    _require(bool(_HOST_RE.match(t.ssh_host)), f"ssh host '{t.ssh_host}' is not a valid host name")
    _require(bool(_USER_RE.match(t.ssh_user)), f"ssh user '{t.ssh_user}' is not a valid user name")
    _require(bool(_HOST_RE.match(t.remote_host)), f"remote host '{t.remote_host}' is not a valid host name")
    _port(t.ssh_port, "ssh port")
    _port(t.local_port, "local port", minimum=1024)
    _port(t.remote_port, "remote port")
    _require(t.auth_type in AUTH_TYPES, f"auth type must be one of {', '.join(AUTH_TYPES)}")
    if t.auth_type != "agent":
        _require(bool(t.auth_path), f"auth type '{t.auth_type}' needs a file path")
    _require(t.check_type in CHECK_TYPES, f"check type must be one of {', '.join(CHECK_TYPES)}")
    if t.check_type == "mysql":
        _require(bool(_USER_RE.match(t.check_user)), "a mysql check needs a valid mysql user")
    return t


def tunnel_from_dict(raw: Mapping[str, Any]) -> Tunnel:
    known = {f.name for f in dataclasses.fields(Tunnel)}
    unknown = sorted(set(raw) - known)
    _require(not unknown, f"unknown tunnel field(s): {', '.join(unknown)}")
    missing = sorted(f.name for f in dataclasses.fields(Tunnel)
                     if f.name not in raw and f.default is dataclasses.MISSING)
    _require(not missing, f"incomplete tunnel definition, missing: {', '.join(missing)}")
    for f in dataclasses.fields(Tunnel):
        if f.name in raw:
            _check_type(f.name, raw[f.name], _FIELD_TYPES[f.type])
    return validate(Tunnel(**dict(raw)))


_FIELD_TYPES = {"str": str, "int": int, "bool": bool}


def _check_type(name: str, value: Any, expected: type) -> None:
    # bool is a subclass of int: a port of `true` must not pass as 1.
    ok = isinstance(value, expected) and (expected is bool or not isinstance(value, bool))
    _require(ok, f"tunnel field '{name}' must be a {expected.__name__}")


def tunnel_to_dict(t: Tunnel) -> dict[str, Any]:
    return dataclasses.asdict(t)


def load(path: Path | None = None) -> Config:
    """Load the config; a missing file is an empty config, a malformed one is an error."""
    path = path or paths.config_file()
    if not path.exists():
        return Config(path=path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    _require(isinstance(raw, dict), f"{path}: top level must be an object")
    interval = raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    _require(
        isinstance(interval, int) and not isinstance(interval, bool) and MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS,
        f"interval_seconds must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}",
    )
    config = Config(interval_seconds=interval, path=path)
    tunnels = raw.get("tunnels", [])
    _require(isinstance(tunnels, list), f"{path}: 'tunnels' must be a list")
    for entry in tunnels:
        _require(isinstance(entry, dict), f"{path}: each tunnel must be an object")
        config = config.with_tunnel(tunnel_from_dict(entry))
    return config


def save(config: Config) -> None:
    """Write atomically with owner-only permissions (the file names credential paths)."""
    path = config.path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = {
        "version": CONFIG_VERSION,
        "interval_seconds": config.interval_seconds,
        "tunnels": [tunnel_to_dict(t) for t in config.tunnels],
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tunnels.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
