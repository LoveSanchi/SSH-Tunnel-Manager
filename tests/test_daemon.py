import json
import logging
import socket
import threading

import pytest

from tests.fakes import World
from tunnel_keeper import client, daemon
from tunnel_keeper import config as cfg
from tunnel_keeper.config import Config
from tunnel_keeper.service import Service
from tunnel_keeper.supervisor import Supervisor, TunnelStatus


@pytest.fixture
def server(tmp_path, tunnel):
    world = World()
    world._current_port = tunnel.local_port
    sup = Supervisor(Config(path=tmp_path / "t.json").with_tunnel(tunnel), world.deps())
    sock = tmp_path / "run" / "tk.sock"
    srv = daemon.ControlServer(sock, Service(sup, save=lambda c: None))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield sock
    srv.shutdown()
    srv.server_close()
    assert not sock.exists()


def test_socket_is_owner_only(server):
    assert server.stat().st_mode & 0o077 == 0


def test_request_roundtrip(server):
    data = client.request("refresh", sock_path=server)
    assert data["tunnels"][0]["state"] == "up"


def test_error_envelope_becomes_daemon_error(server):
    with pytest.raises(client.DaemonError, match="not managed") as info:
        client.request("stop", sock_path=server, name="x")
    assert info.value.code == "not_managed"


def test_invalid_json_request(server):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(server))
        s.sendall(b"not json\n")
        reply = json.loads(client.read_line(s))
    assert reply["ok"] is False and reply["error"] == "request is not valid JSON"


def test_non_utf8_request(server):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(server))
        s.sendall(b"\xff\xfe\n")
        assert json.loads(client.read_line(s))["error"] == "request is not valid JSON"


def test_daemon_unavailable(tmp_path):
    with pytest.raises(client.DaemonUnavailable, match="systemctl --user start"):
        client.request("status", sock_path=tmp_path / "missing.sock")


def test_malformed_reply(tmp_path):
    path = tmp_path / "bad.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen()

    def serve():
        conn, _ = srv.accept()
        conn.recv(100)
        conn.sendall(b"garbage\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(client.DaemonError, match="malformed"):
        client.request("status", sock_path=path)
    srv.close()


def test_transition_logger_logs_only_changes(caplog):
    caplog.set_level(logging.INFO, logger="tunnel-keeper")
    log = daemon.TransitionLogger()
    log((TunnelStatus("a", "up", "ok"),))
    log((TunnelStatus("a", "up", "ok again"),))
    log((TunnelStatus("a", "error", "boom"),))
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["a: up — ok", "a: error — boom"]
    assert caplog.records[1].levelno == logging.WARNING


def test_run_loop_full_checks_watch_and_reload(tmp_path, tunnel, monkeypatch):
    monkeypatch.setattr(daemon, "WATCH_SECONDS", 0.01)
    world = World()
    world._current_port = tunnel.local_port
    path = tmp_path / "t.json"
    cfg.save(Config(path=path, interval_seconds=30).with_tunnel(tunnel))
    sup = Supervisor(cfg.load(path), world.deps())
    stop, reload = threading.Event(), threading.Event()
    seen = []

    def on_change(snapshot):
        seen.append(snapshot)
        if len(seen) == 2:
            path.write_text("{broken")
            reload.set()
        if len(seen) == 3:
            cfg.save(Config(path=path, interval_seconds=30))
            reload.set()
        if len(seen) >= 5:
            stop.set()

    daemon.run_loop(sup, stop, reload, on_change)
    assert seen[0][0].state == "up"
    assert sup.config.tunnels == ()  # the bad file was ignored, the good one applied


def test_main_serves_status_then_stops(tmp_path, monkeypatch, tunnel):
    sock = tmp_path / "run" / "main.sock"
    monkeypatch.setenv("TUNNEL_KEEPER_SOCKET", str(sock))
    seen = {}

    def fake_loop(sup, stop, reload, on_change, reload_config=None):
        seen["status"] = client.request("status", sock_path=sock)

    monkeypatch.setattr(daemon, "run_loop", fake_loop)
    assert daemon.main() == 0
    assert seen["status"]["tunnels"] == []
    assert not sock.exists()


def test_main_refuses_broken_config(tmp_path, monkeypatch):
    path = tmp_path / "config" / "tunnel-keeper" / "tunnels.json"
    path.parent.mkdir(parents=True)
    path.write_text("{oops")
    assert daemon.main() == daemon.EXIT_CONFIG


def test_main_reports_unbindable_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL_KEEPER_SOCKET", str(tmp_path / ("x" * 120) / "s.sock"))
    assert daemon.main() == daemon.EXIT_CONFIG


def test_client_reports_unreachable_socket(tmp_path):
    with pytest.raises(client.DaemonUnavailable, match="cannot reach"):
        client.request("status", sock_path=tmp_path / ("x" * 120) / "s.sock")


def test_second_daemon_does_not_steal_the_socket(server):
    with pytest.raises(OSError, match="already listening"):
        daemon.ControlServer(server, None)
    assert client.request("status", sock_path=server)["tunnels"]


def test_run_loop_survives_a_crashing_pass(tunnel, monkeypatch, caplog):
    monkeypatch.setattr(daemon, "WATCH_SECONDS", 0.01)
    world = World()
    world._current_port = tunnel.local_port
    sup = Supervisor(Config(interval_seconds=30).with_tunnel(tunnel), world.deps())
    stop, reload = threading.Event(), threading.Event()
    calls = []

    def on_change(snapshot):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        if len(calls) >= 4:
            stop.set()

    daemon.run_loop(sup, stop, reload, on_change)
    assert len(calls) >= 4 and "check pass failed" in caplog.text
