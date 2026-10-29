"""Decides, per tunnel, whether it is healthy and what to do when it is not.

Locking: each tunnel has its own lock, held across that tunnel's slow work (probes,
ssh start-up), so one stuck tunnel never blocks another or a status read. A short
state lock guards the shared dicts. Order is always tunnel lock → state lock.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tunnel_keeper import health, paths, process, sshcmd
from tunnel_keeper.config import Config, Tunnel
from tunnel_keeper.process import Handle

START_WAIT_SECONDS = 15.0  # > ssh ConnectTimeout, so a slow handshake is not killed mid-way
CHECK_RETRIES = 5
CHECK_RETRY_DELAY = 0.5
CONFIRM_FAILURES = 3  # failed probes in a row before a running tunnel is restarted
CONFIRM_DELAY = 1.0
BACKOFF_BASE_SECONDS = 15

UP, DOWN, STOPPED, CONFLICT, ERROR = "up", "down", "stopped", "conflict", "error"

log = logging.getLogger("tunnel-keeper")


@dataclass(frozen=True)
class TunnelStatus:
    name: str
    state: str
    detail: str = ""
    pid: int | None = None
    external: bool = False
    checked_at: float = 0.0
    failures: int = 0
    next_retry_at: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class _Runtime:
    handle: Handle | None = None
    failures: int = 0
    next_attempt_at: float = 0.0


@dataclass(frozen=True)
class Deps:
    """Everything the supervisor touches outside itself — swapped out in tests."""

    launch: Callable[[sshcmd.Command, Path], Handle] = process.launch
    build_command: Callable[[Tunnel], sshcmd.Command] = sshcmd.build
    probe: Callable[[Tunnel], health.Probe] = health.probe
    port_open: Callable[[int], bool] = health.port_open
    find_pids: Callable[[str], list[int]] = health.ssh_pids_for
    kill_pids: Callable[[list[int]], None] = process.kill_pids
    last_log_line: Callable[[Path], str] = process.last_log_line
    log_path: Callable[[str], Path] = paths.log_file
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


class Supervisor:
    def __init__(self, config: Config, deps: Deps = Deps()):
        self._deps = deps
        self._state = threading.Lock()
        self._tunnel_locks: dict[str, threading.RLock] = {}
        self._config = config
        self._runtime: dict[str, _Runtime] = {}
        self._statuses: dict[str, TunnelStatus] = {}
        self._snapshot: tuple[TunnelStatus, ...] = ()
        self._publish()

    # ── read side (lock-free: callers get immutable values) ───────────────────
    @property
    def config(self) -> Config:
        return self._config

    def snapshot(self) -> tuple[TunnelStatus, ...]:
        return self._snapshot

    def status(self, name: str) -> TunnelStatus | None:
        return next((s for s in self._snapshot if s.name == name), None)

    # ── write side ────────────────────────────────────────────────────────────
    def apply_config(self, config: Config) -> None:
        for name in list(self._runtime):
            new, old = config.get(name), self._config.get(name)
            if new is None or not new.enabled or new != old:
                with self._lock_for(name):
                    self._terminate(name)
                    with self._state:
                        self._runtime = {k: v for k, v in self._runtime.items() if k != name}
        with self._state:
            self._config = config
            self._statuses = {k: v for k, v in self._statuses.items() if config.get(k)}
            self._publish()

    def heal_all(self, force: bool = False) -> tuple[TunnelStatus, ...]:
        for tunnel in self._config.tunnels:
            self._heal_safely(tunnel.name, force)
        return self._snapshot

    def heal(self, name: str, force: bool = False) -> TunnelStatus:
        self._require(name)
        return self._heal_safely(name, force)

    def watch_processes(self) -> None:
        """Cheap, frequent pass: react at once when a tunnel we own exits."""
        for tunnel in self._config.tunnels:
            rt = self._runtime.get(tunnel.name)
            if tunnel.enabled and rt and rt.handle and rt.handle.poll() is not None:
                self._heal_safely(tunnel.name, force=False)

    def stop(self, name: str) -> TunnelStatus:
        """Stop the tunnel, including an ssh some other tool opened for the same forward."""
        tunnel = self._require(name)
        with self._lock_for(name):
            self._terminate(name)
            external = self._deps.find_pids(tunnel.forward_spec)
            if external:
                self._deps.kill_pids(external)
            return self._set(self._status(tunnel, STOPPED, "stopped"))

    def shutdown(self) -> None:
        for name in list(self._runtime):
            with self._lock_for(name):
                self._terminate(name)

    # ── internals ─────────────────────────────────────────────────────────────
    def _lock_for(self, name: str) -> threading.RLock:
        with self._state:
            lock = self._tunnel_locks.get(name)
            if lock is None:
                lock = threading.RLock()
                self._tunnel_locks = {**self._tunnel_locks, name: lock}
            return lock

    def _require(self, name: str) -> Tunnel:
        tunnel = self._config.get(name)
        if tunnel is None:
            raise KeyError(f"no tunnel named '{name}'")
        return tunnel

    def _heal_safely(self, name: str, force: bool) -> TunnelStatus:
        """Heal one tunnel; an unexpected error marks only that tunnel, never the daemon."""
        with self._lock_for(name):
            tunnel = self._config.get(name)
            if tunnel is None:  # removed while we waited for the lock
                return TunnelStatus(name, STOPPED, "removed")
            try:
                return self._set(self._heal(tunnel, force))
            except Exception as exc:  # noqa: BLE001
                log.exception("checking tunnel %s failed", name)
                return self._set(self._failed(tunnel, ERROR, f"internal error: {exc}"))

    def _publish(self) -> None:
        self._snapshot = tuple(
            self._statuses.get(t.name) or TunnelStatus(t.name, STOPPED if not t.enabled else DOWN, "not checked yet")
            for t in self._config.tunnels
        )

    def _set(self, status: TunnelStatus) -> TunnelStatus:
        with self._state:
            if self._config.get(status.name) is not None:
                self._statuses = {**self._statuses, status.name: status}
                self._publish()
        return status

    def _rt(self, name: str) -> _Runtime:
        return self._runtime.get(name, _Runtime())

    def _put_rt(self, name: str, rt: _Runtime) -> None:
        with self._state:
            self._runtime = {**self._runtime, name: rt}

    def _status(self, t: Tunnel, state: str, detail: str, **kw) -> TunnelStatus:
        rt = self._rt(t.name)
        return TunnelStatus(t.name, state, detail, checked_at=self._deps.clock(), failures=rt.failures,
                            next_retry_at=rt.next_attempt_at if state != UP else 0.0, **kw)

    def _terminate(self, name: str) -> None:
        """Stop our ssh for `name`, keeping its failure count and backoff."""
        rt = self._rt(name)
        if rt.handle:
            rt.handle.terminate()
            self._put_rt(name, dataclasses.replace(rt, handle=None))

    def _confirmed_probe(self, t: Tunnel) -> health.Probe:
        """Probe up to CONFIRM_FAILURES times: one slow SELECT 1 on a loaded database must
        not tear down a tunnel (and every connection through it)."""
        result = self._deps.probe(t)
        for _ in range(CONFIRM_FAILURES - 1):
            if result.ok or result.reachable:
                break
            self._deps.sleep(CONFIRM_DELAY)
            result = self._deps.probe(t)
        return result

    def _heal(self, t: Tunnel, force: bool) -> TunnelStatus:
        if not t.enabled:
            self._terminate(t.name)
            return self._status(t, STOPPED, "stopped")

        rt = self._rt(t.name)
        reason = ""
        if rt.handle and rt.handle.poll() is None:
            result = self._confirmed_probe(t)
            if result.ok:
                self._put_rt(t.name, dataclasses.replace(rt, failures=0, next_attempt_at=0.0))
                return self._status(t, UP, result.detail, pid=rt.handle.pid)
            if result.reachable:  # far side answered with an error: reopening won't fix it
                return self._status(t, ERROR, result.detail, pid=rt.handle.pid)
            reason = f"unhealthy ({result.detail}), restarted"
            self._terminate(t.name)
        else:
            if rt.handle:
                reason = f"ssh exited: {self._deps.last_log_line(self._deps.log_path(t.name)) or 'no output'}"
                self._put_rt(t.name, dataclasses.replace(rt, handle=None))
            external = self._deps.find_pids(t.forward_spec)
            if external:
                result = self._confirmed_probe(t)
                if result.ok or result.reachable:
                    state = UP if result.ok else ERROR
                    return self._status(t, state, f"{result.detail} (opened outside tunnel-keeper)",
                                        pid=external[0], external=True)
                self._deps.kill_pids(external)
                reason = f"external ssh unhealthy ({result.detail}), replaced"
            elif self._deps.port_open(t.local_port):
                return self._status(
                    t, CONFLICT, f"port {t.local_port} is held by another program, not this tunnel's ssh"
                )
        return self._launch(t, force, reason)

    def _launch(self, t: Tunnel, force: bool, reason: str) -> TunnelStatus:
        rt = self._rt(t.name)
        if not force and self._deps.clock() < rt.next_attempt_at:
            return self._status(t, DOWN, f"{reason or 'down'}; waiting to retry")

        handle = self._deps.launch(self._deps.build_command(t), self._deps.log_path(t.name))
        self._put_rt(t.name, dataclasses.replace(rt, handle=handle))

        if not self._wait_for_port(t, handle):
            exited = handle.poll() is not None
            self._terminate(t.name)
            why = self._deps.last_log_line(self._deps.log_path(t.name)) or "no output"
            label = "ssh exited" if exited else "port never opened"
            return self._failed(t, ERROR, f"{label}: {why}")

        result = self._deps.probe(t)
        for _ in range(CHECK_RETRIES - 1):
            if result.ok or result.reachable:
                break
            self._deps.sleep(CHECK_RETRY_DELAY)
            result = self._deps.probe(t)
        if not result.ok:
            # Keep ssh running: the forward is fine, the far side (DB or bastion→DB) is not,
            # and reopening the tunnel will not fix that. The next pass re-checks it.
            return self._failed(t, ERROR, f"tunnel open but check failed: {result.detail}", pid=handle.pid)

        self._put_rt(t.name, _Runtime(handle=handle))
        detail = result.detail if not reason else f"{result.detail} ({reason})"
        return self._status(t, UP, detail, pid=handle.pid)

    def _wait_for_port(self, t: Tunnel, handle: Handle) -> bool:
        deadline = self._deps.clock() + START_WAIT_SECONDS
        while self._deps.clock() < deadline:
            if handle.poll() is not None:
                return False
            if self._deps.port_open(t.local_port):
                return True
            self._deps.sleep(0.25)
        return False

    def _failed(self, t: Tunnel, state: str, detail: str, **kw) -> TunnelStatus:
        rt = self._rt(t.name)
        failures = rt.failures + 1
        delay = min(self._config.interval_seconds, BACKOFF_BASE_SECONDS * 2 ** (failures - 1))
        self._put_rt(t.name, dataclasses.replace(rt, failures=failures, next_attempt_at=self._deps.clock() + delay))
        return self._status(t, state, detail, **kw)
