"""The background service: a check loop plus a Unix-socket control server."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from tunnel_keeper import config as cfg
from tunnel_keeper import paths
from tunnel_keeper.client import MAX_MESSAGE_BYTES, encode
from tunnel_keeper.service import Service
from tunnel_keeper.supervisor import Supervisor, TunnelStatus

WATCH_SECONDS = 5.0
# Permanent problems (bad config, socket taken): the unit sets RestartPreventExitStatus=78
# so systemd does not restart-loop on them. Crashes exit 1 and are restarted.
EXIT_CONFIG = 78
log = logging.getLogger("tunnel-keeper")


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30  # a client that connects and never sends must not pin a thread forever

    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_MESSAGE_BYTES)
        except OSError:
            return
        try:
            message = json.loads(raw)
        except ValueError:  # bad JSON or bad UTF-8
            reply = {"ok": False, "data": None, "error": "request is not valid JSON", "code": "bad_request"}
        else:
            reply = self.server.service.handle(message)  # type: ignore[attr-defined]
        self.wfile.write(encode(reply))


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, sock_path: Path, service: Service):
        self.sock_path = sock_path  # set first: a failed bind calls server_close()
        self.service = service
        sock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _answering(sock_path):
            self.sock_path = Path("/nonexistent")  # never unlink a live daemon's socket
            raise OSError(f"another tunnel-keeper is already listening on {sock_path}")
        sock_path.unlink(missing_ok=True)
        old_umask = os.umask(0o177)  # socket is owner-only: it can start and kill tunnels
        try:
            super().__init__(str(sock_path), _Handler)
        finally:
            os.umask(old_umask)

    def server_close(self) -> None:
        super().server_close()
        self.sock_path.unlink(missing_ok=True)


def _answering(sock_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            probe.connect(str(sock_path))
            return True
    except OSError:
        return False


class TransitionLogger:
    """Log a tunnel only when its state changes, so the journal stays readable."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def __call__(self, snapshot: tuple[TunnelStatus, ...]) -> None:
        current = {s.name: s.state for s in snapshot}
        for s in snapshot:
            if self._last.get(s.name) != s.state:
                level = logging.INFO if s.state in ("up", "stopped") else logging.WARNING
                log.log(level, "%s: %s — %s", s.name, s.state, s.detail)
        self._last = current


def run_loop(sup: Supervisor, stop: threading.Event, reload: threading.Event, on_change,
             reload_config: Callable[[], None] | None = None) -> None:
    reload_config = reload_config or (lambda: sup.apply_config(cfg.load(sup.config.path)))
    sup.heal_all()
    on_change(sup.snapshot())
    last_full = time.monotonic()
    while not stop.wait(WATCH_SECONDS):
        try:
            if reload.is_set():
                reload.clear()
                try:
                    reload_config()
                    last_full = float("-inf")  # check the new config right away
                except cfg.ConfigError as exc:
                    log.error("config reload failed, keeping the previous one: %s", exc)
            # Read the interval every tick, so a change from the panel applies at once.
            if time.monotonic() - last_full >= sup.config.interval_seconds:
                sup.heal_all()
                last_full = time.monotonic()
            else:
                sup.watch_processes()
            on_change(sup.snapshot())
        except Exception:  # noqa: BLE001 - a bad pass must never take every tunnel down
            log.exception("check pass failed; retrying next tick")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        config = cfg.load()
    except cfg.ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    log.info("loaded %d tunnel(s) from %s", len(config.tunnels), config.path)

    sup = Supervisor(config)
    service = Service(sup)
    try:
        server = ControlServer(paths.socket_path(), service)
    except OSError as exc:
        log.error("cannot listen on %s: %s", paths.socket_path(), exc)
        return EXIT_CONFIG
    threading.Thread(target=server.serve_forever, name="control", daemon=True).start()

    stop, reload = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, lambda *_: reload.set())
    try:
        run_loop(sup, stop, reload, TransitionLogger(), reload_config=service.reload)
    finally:
        server.shutdown()
        server.server_close()
        sup.shutdown()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
