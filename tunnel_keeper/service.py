"""Command handlers behind the daemon socket. The daemon is the only writer of the
config file, so a CLI and the top-bar indicator can never race each other."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Any, Callable

from tunnel_keeper import config as cfg
from tunnel_keeper import secretstore
from tunnel_keeper.config import Config, ConfigError
from tunnel_keeper.supervisor import Supervisor, TunnelStatus

log = logging.getLogger("tunnel-keeper")


ENSURE_FRESH_SECONDS = 30  # `ensure` trusts an "up" verdict this recent without re-probing

NOT_MANAGED = "not_managed"


class RequestError(ValueError):
    """A bad request; the message goes back to the caller verbatim."""

    def __init__(self, message: str, code: str = "bad_request"):
        super().__init__(message)
        self.code = code


class Service:
    def __init__(
        self,
        supervisor: Supervisor,
        save: Callable[[Config], None] = cfg.save,
        load: Callable[..., Config] = cfg.load,
        write_secret: Callable[[str, str], str] = secretstore.write_secret,
        delete_secret: Callable[[str], None] = secretstore.delete_secret,
        rename_secret: Callable[[str, str], str] = secretstore.rename_secret,
        is_managed_secret: Callable[[str, str], bool] = secretstore.is_managed,
        read_secret: Callable[[str], str] = secretstore.read_secret,
        clock: Callable[[], float] = time.time,
    ):
        self._sup = supervisor
        self._save = save
        self._load = load
        self._write_secret = write_secret
        self._delete_secret = delete_secret
        self._rename_secret = rename_secret
        self._is_managed_secret = is_managed_secret
        self._read_secret = read_secret
        self._clock = clock
        self._config_lock = threading.Lock()
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "status": self._status,
            "refresh": self._refresh,
            "ensure": self._ensure,
            "start": self._start,
            "stop": self._stop,
            "restart": self._restart,
            "add": self._add,
            "remove": self._remove,
            "reload": self._reload,
            "get": self._get,
            "update": self._update,
            "rename": self._rename,
            "settings": self._settings,
        }

    def handle(self, message: Any) -> dict[str, Any]:
        """Dispatch one request; always returns the {ok, data, error} envelope."""
        if not isinstance(message, dict) or not isinstance(message.get("cmd"), str):
            return _fail("request must be an object with a 'cmd' string")
        handler = self._handlers.get(message["cmd"])
        if handler is None:
            return _fail(f"unknown command '{message['cmd']}'")
        try:
            return {"ok": True, "data": handler(message), "error": None}
        except RequestError as exc:
            return _fail(str(exc), exc.code)
        except ConfigError as exc:
            return _fail(str(exc))
        except KeyError as exc:  # str(KeyError) adds quotes; use the raw message
            return _fail(str(exc.args[0]) if exc.args else "not found")
        except Exception:  # noqa: BLE001 - never let one request kill the daemon
            log.exception("request %r failed", message.get("cmd"))
            return _fail("internal error — see: journalctl --user -u tunnel-keeper")

    # ── views ─────────────────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        config = self._sup.config
        statuses = {s.name: s for s in self._sup.snapshot()}
        return {
            "interval_seconds": config.interval_seconds,
            "config_path": str(config.path),
            "tunnels": [_row(t, statuses.get(t.name)) for t in config.tunnels],
        }

    # ── handlers ──────────────────────────────────────────────────────────────
    def _status(self, _msg: dict) -> dict:
        return self.describe()

    def _refresh(self, msg: dict) -> dict:
        name = msg.get("name")
        if name:
            self._sup.heal(_name(msg), force=True)
        else:
            self._sup.heal_all(force=True)
        return self.describe()

    def _ensure(self, msg: dict) -> dict:
        """Called before every DB use by repo scripts and hooks, so keep it fast: a recent
        "up" is trusted; anything else is checked now, ignoring backoff."""
        tunnel = self._lookup(msg)
        current = self._sup.status(tunnel.name)
        if current and current.state == "up" and self._clock() - current.checked_at < ENSURE_FRESH_SECONDS:
            return _row(tunnel, current)
        return _row(tunnel, self._sup.heal(tunnel.name, force=tunnel.enabled))

    def _start(self, msg: dict) -> dict:
        tunnel = self._set_enabled(_name(msg), True)
        return _row(tunnel, self._sup.heal(tunnel.name, force=True))

    def _stop(self, msg: dict) -> dict:
        tunnel = self._set_enabled(self._lookup(msg).name, False)
        return _row(tunnel, self._sup.stop(tunnel.name))

    def _restart(self, msg: dict) -> dict:
        name = _name(msg)
        tunnel = self._sup.config.get(name)
        if tunnel is None:
            raise RequestError(f"no tunnel named '{name}'")
        if not tunnel.enabled:
            raise RequestError(f"'{name}' is stopped — start it instead")
        self._sup.stop(name)
        return _row(tunnel, self._sup.heal(name, force=True))

    def _add(self, msg: dict) -> dict:
        raw = msg.get("tunnel")
        if not isinstance(raw, dict):
            raise RequestError("'tunnel' must be an object")
        password = _password(msg)
        tunnel = cfg.tunnel_from_dict(raw)
        if not password:
            password = self._copied_password(msg, tunnel)
        with self._config_lock:
            config = self._sup.config
            if config.get(tunnel.name) and not msg.get("replace"):
                raise RequestError(f"a tunnel named '{tunnel.name}' already exists")
            updated = config.with_tunnel(tunnel)  # validates (port clash…) before any write
            if password:
                # Only now, once the request is known good, may the stored password change.
                tunnel = dataclasses.replace(tunnel, check_password_file=self._write_secret(tunnel.name, password))
                updated = config.with_tunnel(tunnel)
            self._commit(updated)
        log.info("tunnel %s saved: %s", tunnel.name, tunnel.summary)
        return _row(tunnel, self._sup.heal(tunnel.name, force=True))

    def _copied_password(self, msg: dict, tunnel: cfg.Tunnel) -> str | None:
        """A duplicate ("copy_secret_from": the original) gets its own copy of the original's
        stored MySQL password, so removing or renaming the original never breaks it."""
        source = msg.get("copy_secret_from")
        if not isinstance(source, str) or not source:
            return None
        if tunnel.check_type != "mysql" or not self._is_managed_secret(tunnel.check_password_file, source):
            return None
        try:
            return self._read_secret(source)
        except OSError as exc:
            raise RequestError(f"could not read the MySQL password of '{source}': type it in the form") from exc

    def _get(self, msg: dict) -> dict:
        """The full definition, for the Edit form. Holds file paths, never secret values."""
        return cfg.tunnel_to_dict(self._existing(_name(msg)))

    def _update(self, msg: dict) -> dict:
        """Replace a tunnel's definition; a different `name` in it renames the tunnel."""
        old_name = _name(msg)
        raw = msg.get("tunnel")
        if not isinstance(raw, dict):
            raise RequestError("'tunnel' must be an object")
        password = _password(msg)
        old = self._existing(old_name)
        new = cfg.tunnel_from_dict({**raw, "enabled": old.enabled})
        return self._replace(old, new, password)

    def _rename(self, msg: dict) -> dict:
        old = self._existing(_name(msg))
        new_name = msg.get("new_name")
        if not isinstance(new_name, str) or not new_name:
            raise RequestError("'new_name' is required")
        return self._replace(old, dataclasses.replace(old, name=new_name), None)

    def _replace(self, old: cfg.Tunnel, new: cfg.Tunnel, password: str | None) -> dict:
        with self._config_lock:
            config = self._sup.config
            config.replacing(old.name, new)  # validate before touching anything on disk
            if password:
                new = dataclasses.replace(new, check_password_file=self._write_secret(new.name, password))
            elif new.name != old.name and self._is_managed_secret(new.check_password_file, old.name):
                new = dataclasses.replace(new, check_password_file=self._rename_secret(old.name, new.name))
            if new == old:
                return _row(new, self._sup.status(new.name))
            # Close the old forward (ours or adopted) before the definition changes under it.
            self._sup.stop(old.name)
            self._commit(config.replacing(old.name, new))
            if new.name != old.name:
                self._delete_secret(old.name)
        log.info("tunnel %s updated%s: %s", old.name,
                 f" (renamed to {new.name})" if new.name != old.name else "", new.summary)
        if not new.enabled:
            return _row(new, self._sup.stop(new.name))
        return _row(new, self._sup.heal(new.name, force=True))

    def _settings(self, msg: dict) -> dict:
        if "interval_seconds" in msg:
            with self._config_lock:
                self._commit(self._sup.config.with_interval(msg["interval_seconds"]))
            log.info("check interval set to %ss", msg["interval_seconds"])
        return self.describe()

    def _remove(self, msg: dict) -> dict:
        name = _name(msg)
        with self._config_lock:
            config = self._sup.config
            if config.get(name) is None:
                raise RequestError(f"no tunnel named '{name}'")
            self._sup.stop(name)
            self._commit(config.without_tunnel(name))
            self._delete_secret(name)
        log.info("tunnel %s removed", name)
        return {"removed": name}

    def _reload(self, _msg: dict) -> dict:
        self.reload()
        self._sup.heal_all()
        return self.describe()

    def reload(self) -> None:
        """Re-read the config file (also used for SIGHUP), serialised with edits."""
        with self._config_lock:
            self._sup.apply_config(self._load(self._sup.config.path))

    # ── helpers ───────────────────────────────────────────────────────────────
    def _commit(self, config: Config) -> None:
        self._save(config)
        self._sup.apply_config(config)

    def _set_enabled(self, name: str, enabled: bool) -> cfg.Tunnel:
        with self._config_lock:
            tunnel = self._sup.config.get(name)
            if tunnel is None:
                raise RequestError(f"no tunnel named '{name}'")
            updated = dataclasses.replace(tunnel, enabled=enabled)
            if updated != tunnel:
                self._commit(self._sup.config.with_tunnel(updated))
            log.info("tunnel %s %s", name, "started" if enabled else "stopped")
            return updated

    def _existing(self, name: str) -> cfg.Tunnel:
        tunnel = self._sup.config.get(name)
        if tunnel is None:
            raise RequestError(f"no tunnel named '{name}'")
        return tunnel

    def _lookup(self, msg: dict) -> cfg.Tunnel:
        config = self._sup.config
        if msg.get("name"):
            tunnel = config.get(_name(msg))
        elif isinstance(msg.get("port"), int):
            tunnel = config.by_port(msg["port"])
        else:
            raise RequestError("give a tunnel 'name' or a local 'port'")
        if tunnel is None:
            raise RequestError("not managed by tunnel-keeper", NOT_MANAGED)
        return tunnel


def _password(msg: dict) -> str | None:
    password = msg.get("check_password")
    if password is not None and not isinstance(password, str):
        raise RequestError("'check_password' must be a string")
    return password


def _name(msg: dict) -> str:
    name = msg.get("name")
    if not isinstance(name, str) or not name:
        raise RequestError("'name' is required")
    return name


def _row(t: cfg.Tunnel, status: TunnelStatus | None) -> dict[str, Any]:
    status = status or TunnelStatus(t.name, "down", "not checked yet")
    return {
        **status.to_dict(),
        "enabled": t.enabled,
        "local_port": t.local_port,
        "remote": f"{t.remote_host}:{t.remote_port}",
        "via": f"{t.ssh_user}@{t.ssh_host}:{t.ssh_port}",
        "check_type": t.check_type,
    }


def _fail(error: str, code: str = "error") -> dict[str, Any]:
    return {"ok": False, "data": None, "error": error, "code": code}
