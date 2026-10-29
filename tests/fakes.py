"""A scripted world for Supervisor tests: fake ssh processes, ports and probes."""

from __future__ import annotations

from pathlib import Path

from tunnel_keeper import health
from tunnel_keeper.supervisor import Deps


class FakeHandle:
    def __init__(self, pid: int, world: "World", port: int):
        self.pid = pid
        self.world = world
        self.port = port
        self.code: int | None = None

    def poll(self):
        return self.code

    def terminate(self):
        if self.code is None:
            self.code = -15
            self.world.open_ports.discard(self.port)
            self.world.terminated.append(self.pid)

    def die(self, code=255):
        self.code = code
        self.world.open_ports.discard(self.port)


class World:
    def __init__(self):
        self.now = 1000.0
        self.open_ports: set[int] = set()
        self.healthy = True
        self.launch_opens_port = True
        self.launch_exits = False
        self.external: dict[str, list[int]] = {}
        self.killed: list[int] = []
        self.terminated: list[int] = []
        self.launched: list[FakeHandle] = []
        self.log_line = "ssh: Connection refused"
        self._pid = 500

    def deps(self) -> Deps:
        return Deps(
            launch=self._launch,
            build_command=lambda t: ("cmd", t.name),
            probe=lambda t: health.Probe(self.healthy and t.local_port in self.open_ports,
                                         "SELECT 1 ok" if self.healthy else "SELECT 1 failed"),
            port_open=lambda port: port in self.open_ports,
            find_pids=lambda spec: list(self.external.get(spec, [])),
            kill_pids=self._kill,
            last_log_line=lambda path: self.log_line,
            log_path=lambda name: Path(f"/tmp/{name}.log"),
            clock=lambda: self.now,
            sleep=self._sleep,
        )

    def _launch(self, cmd, log_path):
        self._pid += 1
        port = self._current_port
        handle = FakeHandle(self._pid, self, port)
        if self.launch_exits:
            handle.code = 255
        elif self.launch_opens_port:
            self.open_ports.add(port)
        self.launched.append(handle)
        return handle

    def _kill(self, pids):
        self.killed.extend(pids)
        for spec, ext in list(self.external.items()):
            if set(ext) & set(pids):
                self.external.pop(spec)
                self.open_ports.discard(int(spec.split(":")[1]))

    def _sleep(self, seconds):
        self.now += seconds
