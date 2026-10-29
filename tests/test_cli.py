import json

import pytest

from tunnel_keeper import cli
from tunnel_keeper.client import DaemonError, DaemonUnavailable

ROW = {"name": "orders", "state": "up", "detail": "SELECT 1 ok", "local_port": 3390,
       "remote": "h:3306", "checked_at": 0, "enabled": True}


class Recorder:
    def __init__(self, reply=None, error=None):
        self.calls = []
        self.reply = reply
        self.error = error

    def __call__(self, cmd, **kw):
        kw.pop("timeout", None)
        self.calls.append((cmd, kw))
        if self.error:
            raise self.error
        return self.reply


def test_status_table_and_json(capsys):
    assert cli.main(["status"], Recorder({"tunnels": [ROW]})) == 0
    out = capsys.readouterr().out
    assert "● orders" in out and "SELECT 1 ok" in out
    assert cli.main(["status", "--json"], Recorder({"tunnels": []})) == 0
    assert json.loads(capsys.readouterr().out) == {"tunnels": []}


def test_empty_status(capsys):
    cli.main(["status"], Recorder({"tunnels": []}))
    assert "No tunnels yet" in capsys.readouterr().out


def test_refresh_exit_code_reflects_health(capsys):
    assert cli.main(["refresh"], Recorder({"tunnels": [ROW]})) == 0
    bad = {**ROW, "state": "error"}
    rec = Recorder({"tunnels": [bad]})
    assert cli.main(["refresh", "orders"], rec) == 1
    assert rec.calls == [("refresh", {"name": "orders"})]


@pytest.mark.parametrize("reply, error, code", [
    (ROW, None, 0),
    ({**ROW, "state": "error"}, None, 1),
    (None, DaemonError("not managed by tunnel-keeper", "not_managed"), 2),
    (None, DaemonUnavailable("down"), 3),
    (None, DaemonError("no reply", "timeout"), 3),
    (None, DaemonError("internal error", "error"), 3),
    (None, DaemonError("'name' is required", "bad_request"), 1),
])
def test_ensure_exit_codes(reply, error, code, capsys):
    assert cli.main(["ensure", "--port", "3390", "-q"], Recorder(reply, error)) == code


def test_ensure_by_name_prints_to_stderr(capsys):
    rec = Recorder(ROW)
    cli.main(["ensure", "orders"], rec)
    assert rec.calls == [("ensure", {"name": "orders"})]
    assert "orders" in capsys.readouterr().err


def test_stop_by_port():
    rec = Recorder(ROW)
    assert cli.main(["stop", "--port", "3390"], rec) == 0
    assert rec.calls == [("stop", {"port": 3390})]


def test_single_commands(capsys):
    for cmd in ("start", "stop", "restart"):
        rec = Recorder(ROW)
        assert cli.main([cmd, "orders"], rec) == 0
        assert rec.calls == [(cmd, {"name": "orders"})]
    assert cli.main(["remove", "orders"], Recorder({"removed": "orders"})) == 0
    assert "removed orders" in capsys.readouterr().out


def test_add_builds_tunnel():
    rec = Recorder(ROW)
    code = cli.main(["add", "x", "--ssh", "me@bastion", "--ssh-port", "2525", "--local-port", "3320",
                     "--remote", "10.0.0.1:3306", "--password-file", "~/pw", "--mysql-user", "ro"], rec)
    assert code == 0
    cmd, kw = rec.calls[0]
    t = kw["tunnel"]
    assert cmd == "add" and kw["replace"] is False
    assert (t["ssh_user"], t["ssh_host"], t["remote_host"], t["remote_port"]) == ("me", "bastion", "10.0.0.1", 3306)
    assert t["auth_type"] == "password_file" and t["check_type"] == "mysql"


def test_add_warns_for_mysql_port_with_tcp_check(capsys):
    cli.main(["add", "x", "--ssh", "me@b", "--local-port", "3320", "--remote", "10.0.0.1:3306"], Recorder(ROW))
    assert "usually MySQL" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["add", "x", "--ssh", "bastion", "--local-port", "1", "--remote", "h:1"],
    ["add", "x", "--ssh", "a@b", "--local-port", "1", "--remote", "h"],
])
def test_add_rejects_bad_targets(args):
    with pytest.raises(SystemExit):
        cli.main(args, Recorder(ROW))


def test_import_env(tmp_path, capsys):
    (tmp_path / ".env.local").write_text(
        "DB_TUNNEL_BASTION_HOST=bastion.example.com\nDB_TUNNEL_BASTION_USER=deploy\n"
        "DB_TUNNEL_REMOTE_HOST=10.20.0.7\nDB_TUNNEL_REMOTE_PORT=3306\nDB_TUNNEL_LOCAL_PORT=3307\n"
    )
    rec = Recorder(ROW)
    code = cli.main(["import-env", str(tmp_path), "--name", "orders-db", "--prefix", "DB_TUNNEL",
                     "--local-port", "3390"], rec)
    assert code == 0
    (cmd, kw), = rec.calls
    assert cmd == "add" and kw["tunnel"]["local_port"] == 3390 and kw["replace"] is False


def test_import_env_missing_files(tmp_path, capsys):
    assert cli.main(["import-env", str(tmp_path), "--name", "x", "--prefix", "P"], Recorder(ROW)) == 1


def test_import_env_incomplete_settings(tmp_path, capsys):
    (tmp_path / ".env").write_text("P_HOST=h\n")
    assert cli.main(["import-env", str(tmp_path), "--name", "x", "--prefix", "P"], Recorder(ROW)) == 1
    assert "P_REMOTE_PORT is not set" in capsys.readouterr().err


def test_age():
    assert cli._age(0) == "never"


def test_rename_interval_logs(capsys, monkeypatch):
    rec = Recorder(ROW)
    assert cli.main(["rename", "orders", "primary"], rec) == 0
    assert rec.calls == [("rename", {"name": "orders", "new_name": "primary"})]
    rec = Recorder({"interval_seconds": 60})
    assert cli.main(["interval", "60"], rec) == 0 and rec.calls == [("settings", {"interval_seconds": 60})]
    assert cli.main(["interval"], Recorder({"interval_seconds": 60})) == 0
    assert "every 60s" in capsys.readouterr().out
    monkeypatch.setattr(cli.logview, "report", lambda name: f"report for {name}\n")
    assert cli.main(["logs", "orders"], Recorder()) == 0
    assert capsys.readouterr().out == "report for orders\n"
