import dataclasses
import os
import socket
import sys
import time
import types

import pytest

from tunnel_keeper import health, paths, sshcmd


@pytest.fixture
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    yield srv.getsockname()[1]
    srv.close()


def test_port_open(listener):
    assert health.port_open(listener)


def test_port_closed():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert not health.port_open(port, timeout=0.5)


def _proc(root, pid, argv):
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def test_ssh_pids_for_matches_only_ssh_with_exact_forward(tmp_path):
    spec = "127.0.0.1:3391:127.0.0.1:3391"
    _proc(tmp_path, 10, ["ssh", "-f", "-N", "-L", spec, "u@h"])
    _proc(tmp_path, 11, ["/usr/bin/ssh", f"-L{spec}", "u@h"])
    _proc(tmp_path, 12, ["sshpass", "-f", "x", "ssh", "-L", spec])  # sshpass itself is not ssh
    _proc(tmp_path, 13, ["ssh", "-L", "127.0.0.1:3309:127.0.0.1:3391", "u@h"])
    _proc(tmp_path, 14, ["grep", spec])
    (tmp_path / "self").mkdir()
    (tmp_path / "99").mkdir()  # vanished process: no cmdline
    assert health.ssh_pids_for(spec, tmp_path, uid=os.getuid()) == [10, 11]
    assert health.ssh_pids_for(spec, tmp_path, uid=os.getuid() + 1) == []  # other users: never


def test_probe_fails_fast_without_local_listener(tunnel):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    result = health.probe(dataclasses.replace(tunnel, local_port=port))
    assert not result.ok and not result.reachable and "nothing listening" in result.detail


def test_probe_dispatches_by_check_type(tunnel, listener, monkeypatch):
    monkeypatch.setattr(health, "tcp_probe", lambda t: health.Probe(True, "tcp"))
    monkeypatch.setattr(health, "mysql_probe", lambda t: health.Probe(True, "mysql"))
    assert health.probe(dataclasses.replace(tunnel, local_port=listener)).detail == "tcp"
    t = dataclasses.replace(tunnel, local_port=listener, check_type="mysql", check_user="ro")
    assert health.probe(t).detail == "mysql"


@pytest.mark.parametrize("outcome, lines, ok, reachable, text", [
    ("opened", [], True, True, "far end 10.20.0.7:3306 reachable"),
    ("refused", ["channel 0: open failed: connect failed: Connection refused"], False, True,
     "bastion could not connect to 10.20.0.7:3306: Connection refused"),
    ("refused", ["Stdio forwarding request failed: Session open refused by peer"], False, True, ": connection refused (port closed"),
    ("timeout", [], False, True, "no answer from 10.20.0.7:3306 within 10s"),
    ("exited", ["debug1: x", "Control socket connect(/x.ctl): Connection refused"], False, False,
     "check could not run: Control socket connect"),
    ("exited", [], False, False, "check could not run: no output"),
])
def test_tcp_probe_outcomes(tunnel, outcome, lines, ok, reachable, text):
    t = dataclasses.replace(tunnel, remote_port=3306)
    result = health.tcp_probe(t, run=lambda cmd, timeout: (outcome, lines))
    assert (result.ok, result.reachable) == (ok, reachable) and text in result.detail


def test_tcp_probe_uses_control_socket_when_present(tunnel):
    seen = []
    health.tcp_probe(tunnel, run=lambda cmd, timeout: seen.append(cmd.argv) or ("opened", []))
    ctl = paths.control_path(tunnel.name)
    ctl.parent.mkdir(parents=True)
    ctl.touch()
    health.tcp_probe(tunnel, run=lambda cmd, timeout: seen.append(cmd.argv) or ("opened", []))
    assert "-S" not in seen[0] and "-W" in seen[0]          # standalone login
    assert seen[1][:4] == ("ssh", "-v", "-S", str(ctl))     # through the tunnel's own session


def _fake_ssh(script: str) -> sshcmd.Command:
    return sshcmd.Command(argv=(sys.executable, "-c", script), env={})


@pytest.mark.parametrize("script, outcome", [
    ("import sys,time; print('debug1: mux_client_request_stdio_fwd: master session id: 2', file=sys.stderr, flush=True); time.sleep(30)", "opened"),
    ("import sys,time; print('debug2: channel_input_open_confirmation: channel 0: callback start', file=sys.stderr, flush=True); time.sleep(30)", "opened"),
    ("import sys,time; print('channel 0: open failed: connect failed: No route to host', file=sys.stderr, flush=True); time.sleep(30)", "refused"),
    ("import time; time.sleep(30)", "timeout"),
    ("import sys; print('Permission denied (password).', file=sys.stderr)", "exited"),
])
def test_watch_ssh_classifies_and_always_kills(script, outcome):
    start = time.monotonic()
    result, lines = health.watch_ssh(_fake_ssh(script), timeout=1.0)
    assert result == outcome
    assert time.monotonic() - start < 5  # never waits for the 30s sleep


class _FakeCursor:
    def __init__(self, fail):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        if self.fail:
            raise _FakePyMySQL.MySQLError(2013, "Lost connection")

    def fetchone(self):
        return (1,)


class _FakePyMySQL(types.ModuleType):
    class MySQLError(Exception):
        pass

    def __init__(self, connect_error=None, query_fails=False):
        super().__init__("pymysql")
        self.connect_error = connect_error
        self.query_fails = query_fails
        self.calls = []
        self.closed = False

    def connect(self, **kw):
        self.calls.append(kw)
        if self.connect_error:
            raise self.MySQLError(1045, self.connect_error)
        mod = self

        class Conn:
            def cursor(self):
                return _FakeCursor(mod.query_fails)

            def close(self):
                mod.closed = True

        return Conn()


@pytest.fixture
def mysql_tunnel(tunnel, tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    return dataclasses.replace(tunnel, check_type="mysql", check_user="ro", check_password_file=str(pw),
                               check_database="orders")


def test_mysql_probe_ok(monkeypatch, mysql_tunnel):
    fake = _FakePyMySQL()
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    assert health.mysql_probe(mysql_tunnel) == health.Probe(True, "SELECT 1 ok", True)
    assert fake.calls[0]["password"] == "s3cret"
    assert fake.calls[0]["port"] == 3390 and fake.calls[0]["host"] == "127.0.0.1"
    assert fake.closed


def test_mysql_probe_connect_failure(monkeypatch, mysql_tunnel):
    monkeypatch.setitem(sys.modules, "pymysql", _FakePyMySQL(connect_error="Access denied"))
    result = health.mysql_probe(mysql_tunnel)
    assert not result.ok and "Access denied" in result.detail
    assert result.reachable  # 1045 came from the server: the tunnel itself works


def test_mysql_probe_query_failure_closes(monkeypatch, mysql_tunnel):
    fake = _FakePyMySQL(query_fails=True)
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    result = health.mysql_probe(mysql_tunnel)
    assert not result.ok and "Lost connection" in result.detail
    assert not result.reachable  # 2013 is a client-side transport error
    assert fake.closed


def test_mysql_probe_unexpected_exception(monkeypatch, mysql_tunnel):
    fake = _FakePyMySQL()
    fake.connect = lambda **kw: (_ for _ in ()).throw(RuntimeError("weird"))
    monkeypatch.setitem(sys.modules, "pymysql", fake)
    result = health.mysql_probe(mysql_tunnel)
    assert not result.ok and not result.reachable and "weird" in result.detail


def test_mysql_probe_binary_password_file(monkeypatch, mysql_tunnel, tmp_path):
    monkeypatch.setitem(sys.modules, "pymysql", _FakePyMySQL())
    bad = tmp_path / "bin"
    bad.write_bytes(b"\xff\xfe")
    result = health.mysql_probe(dataclasses.replace(mysql_tunnel, check_password_file=str(bad)))
    assert not result.ok and result.reachable  # config problem: never restart ssh over it


def test_mysql_probe_unreadable_password(monkeypatch, mysql_tunnel):
    monkeypatch.setitem(sys.modules, "pymysql", _FakePyMySQL())
    result = health.mysql_probe(dataclasses.replace(mysql_tunnel, check_password_file="/nope/pw"))
    assert not result.ok and "password file" in result.detail


def test_mysql_probe_without_pymysql_degrades_to_port(monkeypatch, mysql_tunnel, listener):
    monkeypatch.setitem(sys.modules, "pymysql", None)
    result = health.mysql_probe(dataclasses.replace(mysql_tunnel, local_port=listener))
    assert result.ok and "pymysql missing" in result.detail
