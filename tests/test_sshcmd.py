import dataclasses

from tunnel_keeper import sshcmd


def test_agent_auth_uses_batch_mode(tunnel):
    cmd = sshcmd.build(tunnel)
    assert cmd.argv[:6] == ("ssh", "-N", "-p", "2525", "-L", "127.0.0.1:3390:10.20.0.7:3306")
    assert "BatchMode=yes" in cmd.argv
    assert "ExitOnForwardFailure=yes" in cmd.argv
    assert cmd.argv[-1] == "deploy@10.0.0.5"
    assert cmd.env == {}


def test_key_auth_expands_home(tunnel, monkeypatch):
    monkeypatch.setenv("HOME", "/home/me")
    cmd = sshcmd.build(dataclasses.replace(tunnel, auth_type="key", auth_path="~/.ssh/id"))
    i = cmd.argv.index("-i")
    assert cmd.argv[i + 1] == "/home/me/.ssh/id"


def test_password_file_prefers_sshpass(tunnel):
    t = dataclasses.replace(tunnel, auth_type="password_file", auth_path="/secret/pw")
    cmd = sshcmd.build(t, sshpass="/usr/bin/sshpass")
    assert cmd.argv[:3] == ("/usr/bin/sshpass", "-f", "/secret/pw")
    assert "BatchMode=yes" not in cmd.argv
    assert cmd.env == {}


def test_password_file_falls_back_to_askpass(tunnel, tmp_path):
    t = dataclasses.replace(tunnel, auth_type="password_file", auth_path="/secret/pw")
    cmd = sshcmd.build(t, sshpass="", askpass=tmp_path / "ask.sh")
    assert cmd.argv[0] == "ssh"
    assert cmd.env["SSH_ASKPASS_REQUIRE"] == "force"
    assert cmd.env["TUNNEL_KEEPER_PASSWORD_FILE"] == "/secret/pw"
    assert "/secret/pw" not in cmd.argv  # the path is passed by env, never argv
