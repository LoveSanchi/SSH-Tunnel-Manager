"""Filesystem locations, following the XDG base-directory spec."""

import os
from pathlib import Path

APP = "tunnel-keeper"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def config_file() -> Path:
    return config_dir() / "tunnels.json"


def secrets_dir() -> Path:
    return config_dir() / "secrets"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def log_file(tunnel_name: str) -> Path:
    return state_dir() / f"{tunnel_name}.log"


def socket_path() -> Path:
    override = os.environ.get("TUNNEL_KEEPER_SOCKET", "").strip()
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    base = Path(runtime) if runtime else state_dir()
    return base / f"{APP}.sock"


def expand(path: str) -> str:
    return os.path.expanduser(path) if path else path
