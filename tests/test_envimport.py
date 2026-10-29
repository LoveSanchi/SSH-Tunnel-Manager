import stat

import pytest

from tunnel_keeper import envimport, secretstore
from tunnel_keeper.config import ConfigError

ENV = {
    "DB_TUNNEL_BASTION_HOST": "bastion.example.com",
    "DB_TUNNEL_BASTION_PORT": "2222",
    "DB_TUNNEL_BASTION_USER": "deploy",
    "DB_TUNNEL_PASSWORD_FILE": "~/.secrets/bastion.txt",
    "DB_TUNNEL_REMOTE_HOST": "10.20.0.7",
    "DB_TUNNEL_REMOTE_PORT": "3306",
    "DB_TUNNEL_LOCAL_PORT": "3390",
    "MYSQL_USER": "ro",
    "MYSQL_PASSWORD": "pw1",
    "MYSQL_DATABASE": "orders",
    "AUDIT_TUNNEL_HOST": "db-host.example.com",
    "AUDIT_TUNNEL_REMOTE_PORT": "3391",
    "AUDIT_MYSQL_PORT": "3391",
    "AUDIT_MYSQL_USER": "audit",
    "AUDIT_MYSQL_PASSWORD": "pw2",
}


def fake_writer(store):
    def write(name, value):
        store[name] = value
        return f"~/secret/{name}"
    return write


def test_parse_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\n\nA=1\nexport B="two"\nC=\'3\'\nD=x # kept\nNOEQ\nA=dup\n')
    assert envimport.parse_env_file(f) == {"A": "1", "B": "two", "C": "3", "D": "x # kept"}


def test_load_env_folder_local_wins_and_blank_ignored(tmp_path):
    (tmp_path / ".env").write_text("A=env\nB=env\n")
    (tmp_path / ".env.local").write_text("A=local\nB=\n")
    assert envimport.load_env(tmp_path) == {"A": "local", "B": "env"}
    assert envimport.load_env(tmp_path / ".env") == {"A": "env", "B": "env"}


def test_tunnel_with_mysql_check():
    store = {}
    t = envimport.tunnel_from_env(ENV, "orders-db", "DB_TUNNEL", mysql_prefix="MYSQL", write=fake_writer(store))
    assert t.forward_spec == "127.0.0.1:3390:10.20.0.7:3306"
    assert (t.ssh_user, t.ssh_host, t.ssh_port) == ("deploy", "bastion.example.com", 2222)
    assert t.auth_type == "password_file" and t.auth_path == "~/.secrets/bastion.txt"
    assert (t.check_type, t.check_user, t.check_database) == ("mysql", "ro", "orders")
    assert t.check_password_file == "~/secret/orders-db" and store == {"orders-db": "pw1"}


def test_fallback_prefix_and_mysql_port():
    store = {}
    t = envimport.tunnel_from_env(ENV, "audit-db", "AUDIT_TUNNEL", fallback_prefix="DB_TUNNEL",
                                  mysql_prefix="AUDIT_MYSQL", write=fake_writer(store))
    assert t.forward_spec == "127.0.0.1:3391:127.0.0.1:3391"
    assert (t.ssh_host, t.ssh_user, t.ssh_port) == ("db-host.example.com", "deploy", 2222)
    assert t.auth_path == "~/.secrets/bastion.txt" and store == {"audit-db": "pw2"}


def test_key_auth_and_port_check():
    env = {k: v for k, v in ENV.items() if k != "DB_TUNNEL_PASSWORD_FILE"}
    env["DB_TUNNEL_SSH_KEY"] = "~/.ssh/id"
    t = envimport.tunnel_from_env(env, "orders-db", "DB_TUNNEL", write=fake_writer({}))
    assert t.auth_type == "key" and t.check_type == "tcp"


def test_agent_auth_when_nothing_given():
    env = {k: v for k, v in ENV.items() if k != "DB_TUNNEL_PASSWORD_FILE"}
    assert envimport.tunnel_from_env(env, "orders-db", "DB_TUNNEL", write=fake_writer({})).auth_type == "agent"


@pytest.mark.parametrize("drop, change, message", [
    ("DB_TUNNEL_REMOTE_PORT", {}, "REMOTE_PORT is not set"),
    ("DB_TUNNEL_LOCAL_PORT", {}, "LOCAL_PORT is not set"),
    (None, {"DB_TUNNEL_BASTION_PORT": "abc"}, "must be a number"),
    ("DB_TUNNEL_BASTION_HOST", {}, "ssh host"),
])
def test_import_errors_write_no_secret(drop, change, message):
    env = {**{k: v for k, v in ENV.items() if k != drop}, **change}
    store = {}
    with pytest.raises(ConfigError, match=message):
        envimport.tunnel_from_env(env, "orders-db", "DB_TUNNEL", mysql_prefix="MYSQL", write=fake_writer(store))
    assert store == {}


def test_write_secret_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = secretstore.write_secret("orders", "hunter2")
    assert path == "~/.config/tunnel-keeper/secrets/orders.mysql-password"
    real = tmp_path / ".config/tunnel-keeper/secrets/orders.mysql-password"
    assert real.read_text() == "hunter2\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert stat.S_IMODE(real.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(real.parent.parent.stat().st_mode) == 0o700


def test_secret_replace_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    secretstore.write_secret("r", "one")
    secretstore.write_secret("r", "two")
    secrets = tmp_path / ".config/tunnel-keeper/secrets"
    assert (secrets / "r.mysql-password").read_text() == "two\n"
    assert [p.name for p in secrets.iterdir()] == ["r.mysql-password"]  # no temp files left
    secretstore.delete_secret("r")
    secretstore.delete_secret("r")  # idempotent
    assert list(secrets.iterdir()) == []


def test_rename_secret_and_is_managed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    path = secretstore.write_secret("a", "pw")
    assert secretstore.is_managed(path, "a") and not secretstore.is_managed(path, "b")
    assert not secretstore.is_managed("", "a")
    new = secretstore.rename_secret("a", "b")
    assert secretstore.is_managed(new, "b")
    secrets = tmp_path / ".config/tunnel-keeper/secrets"
    assert [p.name for p in secrets.iterdir()] == ["b.mysql-password"]
    assert (secrets / "b.mysql-password").read_text() == "pw\n"
