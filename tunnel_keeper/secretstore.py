"""Owner-only storage for the MySQL passwords used by `SELECT 1` health checks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from tunnel_keeper import paths


def write_secret(name: str, value: str) -> str:
    """Store `value` for tunnel `name` with mode 0600 and return its path (as ~/… when under $HOME)."""
    directory = paths.secrets_dir()
    paths.config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    target = _path(name)
    # Write a fresh 0600 file and rename it over the old one: never a moment where the
    # secret sits in a file with looser permissions, never a half-written password.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    home = str(Path.home())
    text = str(target)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def read_secret(name: str) -> str:
    """The stored password of tunnel `name` (raises OSError when there is none)."""
    with open(_path(name), encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def delete_secret(name: str) -> None:
    _path(name).unlink(missing_ok=True)


def _path(name: str) -> Path:
    return paths.secrets_dir() / f"{name}.mysql-password"


def is_managed(path: str, name: str) -> bool:
    """True when `path` is the file this module keeps for tunnel `name`."""
    return bool(path) and Path(paths.expand(path)) == _path(name)


def rename_secret(old: str, new: str) -> str:
    """Move tunnel `old`'s stored password to `new`; returns the new path (as ~/…)."""
    written = write_secret(new, read_secret(old))
    _path(old).unlink(missing_ok=True)
    return written
