import dataclasses
import json
import stat

import pytest

from tunnel_keeper import config as cfg
from tunnel_keeper.config import Config, ConfigError


def test_forward_spec_and_summary(tunnel):
    assert tunnel.forward_spec == "127.0.0.1:3390:10.20.0.7:3306"
    assert "deploy@10.0.0.5" in tunnel.summary


@pytest.mark.parametrize(
    "change, message",
    [
        ({"name": "Bad Name"}, "name must be"),
        ({"ssh_host": "-oProxyCommand=evil"}, "ssh host"),
        ({"ssh_user": "-x"}, "ssh user"),
        ({"remote_host": ""}, "remote host"),
        ({"local_port": 80}, "local port must be between 1024"),
        ({"remote_port": 70000}, "remote port"),
        ({"ssh_port": True}, "ssh port must be a number"),
        ({"auth_type": "magic"}, "auth type"),
        ({"auth_type": "key"}, "needs a file path"),
        ({"check_type": "http"}, "check type"),
        ({"check_type": "mysql"}, "mysql check needs"),
    ],
)
def test_validate_rejects(tunnel, change, message):
    with pytest.raises(ConfigError, match=message):
        cfg.validate(dataclasses.replace(tunnel, **change))


def test_with_tunnel_adds_replaces_in_place_and_rejects_port_clash(tunnel):
    other = dataclasses.replace(tunnel, name="payments", local_port=3391)
    config = Config().with_tunnel(tunnel).with_tunnel(other)
    assert [t.name for t in config.tunnels] == ["orders", "payments"]

    moved = dataclasses.replace(tunnel, local_port=3320)
    replaced = config.with_tunnel(moved)
    assert [t.name for t in replaced.tunnels] == ["orders", "payments"]
    assert replaced.get("orders").local_port == 3320
    assert config.get("orders").local_port == 3390  # original untouched

    with pytest.raises(ConfigError, match="already used by tunnel 'payments'"):
        config.with_tunnel(dataclasses.replace(tunnel, name="third", local_port=3391))


def test_without_tunnel(tunnel):
    config = Config().with_tunnel(tunnel)
    assert config.without_tunnel("orders").tunnels == ()
    with pytest.raises(ConfigError, match="no tunnel named"):
        config.without_tunnel("ghost")


def test_by_port(tunnel):
    config = Config().with_tunnel(tunnel)
    assert config.by_port(3390) == tunnel
    assert config.by_port(1) is None


def test_save_load_roundtrip_with_private_permissions(tmp_path, tunnel):
    path = tmp_path / "cfg" / "tunnels.json"
    config = Config(path=path, interval_seconds=90).with_tunnel(tunnel)
    cfg.save(config)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = cfg.load(path)
    assert loaded.tunnels == (tunnel,)
    assert loaded.interval_seconds == 90


def test_load_missing_file_is_empty(tmp_path):
    assert cfg.load(tmp_path / "nope.json").tunnels == ()


@pytest.mark.parametrize(
    "body, message",
    [
        ("{not json", "cannot read"),
        ("[]", "top level must be an object"),
        ('{"interval_seconds": 5}', "interval_seconds"),
        ('{"tunnels": [1]}', "each tunnel must be an object"),
        ('{"tunnels": [{"name": "x", "bogus": 1}]}', "unknown tunnel field"),
        ('{"tunnels": [{"name": "x"}]}', "incomplete tunnel definition, missing"),
    ],
)
def test_load_rejects_bad_files(tmp_path, body, message):
    path = tmp_path / "tunnels.json"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        cfg.load(path)


def test_save_cleans_temp_file_on_failure(tmp_path, tunnel, monkeypatch):
    path = tmp_path / "cfg" / "tunnels.json"
    monkeypatch.setattr(cfg.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cfg.save(Config(path=path).with_tunnel(tunnel))
    assert list(path.parent.iterdir()) == []


def test_saved_file_is_plain_json(tmp_path, tunnel):
    path = tmp_path / "tunnels.json"
    cfg.save(Config(path=path).with_tunnel(tunnel))
    body = json.loads(path.read_text())
    assert body["version"] == 1 and body["tunnels"][0]["name"] == "orders"


@pytest.mark.parametrize("field, value, message", [
    ("ssh_host", 5, "must be a str"),
    ("local_port", "3307", "must be a int"),
    ("local_port", True, "must be a int"),
    ("enabled", "yes", "must be a bool"),
])
def test_tunnel_from_dict_checks_types(tunnel, field, value, message):
    with pytest.raises(ConfigError, match=message):
        cfg.tunnel_from_dict({**cfg.tunnel_to_dict(tunnel), field: value})


def test_load_rejects_non_list_tunnels(tmp_path):
    path = tmp_path / "t.json"
    path.write_text('{"tunnels": {}}')
    with pytest.raises(ConfigError, match="must be a list"):
        cfg.load(path)


def test_replacing_renames_in_place_and_checks_clashes(tunnel):
    other = dataclasses.replace(tunnel, name="b", local_port=3400)
    config = Config().with_tunnel(tunnel).with_tunnel(other)
    renamed = config.replacing("orders", dataclasses.replace(tunnel, name="a"))
    assert [t.name for t in renamed.tunnels] == ["a", "b"]
    with pytest.raises(ConfigError, match="already exists"):
        config.replacing("orders", dataclasses.replace(tunnel, name="b"))
    with pytest.raises(ConfigError, match="already used by tunnel 'b'"):
        config.replacing("orders", dataclasses.replace(tunnel, local_port=3400))
    with pytest.raises(ConfigError, match="no tunnel named"):
        config.replacing("ghost", tunnel)
