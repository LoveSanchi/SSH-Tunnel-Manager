import dataclasses

import pytest

from tests.fakes import World
from tunnel_keeper import config as cfg
from tunnel_keeper.config import Config
from tunnel_keeper.service import Service
from tunnel_keeper.supervisor import Supervisor


@pytest.fixture
def world(tunnel):
    w = World()
    w._current_port = tunnel.local_port
    return w


@pytest.fixture
def saved():
    return []


@pytest.fixture
def secrets():
    return {}


@pytest.fixture
def svc(world, tunnel, saved, secrets, tmp_path):
    sup = Supervisor(Config(path=tmp_path / "t.json").with_tunnel(tunnel), world.deps())

    def write(name, value):
        secrets[name] = value
        return f"~/s/{name}"

    return Service(sup, save=saved.append, load=lambda path: Config(path=path),
                   write_secret=write, delete_secret=lambda name: secrets.pop(name, None),
                   clock=lambda: world.now)


def ok(reply):
    assert reply["ok"], reply["error"]
    return reply["data"]


def test_bad_requests(svc):
    assert svc.handle([1])["error"].startswith("request must be")
    assert svc.handle({"cmd": "fly"})["error"] == "unknown command 'fly'"
    assert svc.handle({"cmd": "restart"})["error"] == "'name' is required"
    assert "give a tunnel" in svc.handle({"cmd": "stop"})["error"]
    reply = svc.handle({"cmd": "stop", "name": "ghost"})
    assert reply["error"] == "not managed by tunnel-keeper" and reply["code"] == "not_managed"


def test_status_describes_every_tunnel(svc):
    data = ok(svc.handle({"cmd": "status"}))
    (row,) = data["tunnels"]
    assert row["name"] == "orders" and row["remote"] == "10.20.0.7:3306"
    assert row["via"] == "deploy@10.0.0.5:2525" and data["interval_seconds"] == 120


def test_refresh_all_and_one(svc):
    assert ok(svc.handle({"cmd": "refresh"}))["tunnels"][0]["state"] == "up"
    assert ok(svc.handle({"cmd": "refresh", "name": "orders"}))["tunnels"][0]["state"] == "up"


def test_ensure_by_port_and_name(svc):
    assert ok(svc.handle({"cmd": "ensure", "port": 3390}))["state"] == "up"
    assert ok(svc.handle({"cmd": "ensure", "name": "orders"}))["state"] == "up"
    assert svc.handle({"cmd": "ensure", "port": 9999})["error"] == "not managed by tunnel-keeper"
    assert "give a tunnel" in svc.handle({"cmd": "ensure"})["error"]


def test_stop_persists_disabled_then_start(svc, saved, world):
    ok(svc.handle({"cmd": "refresh"}))
    row = ok(svc.handle({"cmd": "stop", "name": "orders"}))
    assert row["state"] == "stopped" and row["enabled"] is False
    assert saved[-1].get("orders").enabled is False
    assert "start it instead" in svc.handle({"cmd": "restart", "name": "orders"})["error"]

    row = ok(svc.handle({"cmd": "start", "name": "orders"}))
    assert row["state"] == "up" and saved[-1].get("orders").enabled is True


def test_restart(svc, world):
    ok(svc.handle({"cmd": "refresh"}))
    row = ok(svc.handle({"cmd": "restart", "name": "orders"}))
    assert row["state"] == "up" and len(world.launched) == 2
    assert svc.handle({"cmd": "restart", "name": "nope"})["error"] == "no tunnel named 'nope'"


def test_add_validates_saves_and_connects(svc, saved, tunnel, world):
    world._current_port = 3330
    new = {**cfg.tunnel_to_dict(tunnel), "name": "analytics", "local_port": 3330}
    row = ok(svc.handle({"cmd": "add", "tunnel": new}))
    assert row["state"] == "up" and saved[-1].get("analytics")

    assert "already exists" in svc.handle({"cmd": "add", "tunnel": new})["error"]
    assert ok(svc.handle({"cmd": "add", "tunnel": new, "replace": True}))["name"] == "analytics"
    assert "must be an object" in svc.handle({"cmd": "add", "tunnel": "x"})["error"]
    bad = {**new, "name": "other", "local_port": 3390}
    assert "already used by tunnel 'orders'" in svc.handle({"cmd": "add", "tunnel": bad})["error"]


def test_remove(svc, saved):
    assert ok(svc.handle({"cmd": "remove", "name": "orders"})) == {"removed": "orders"}
    assert saved[-1].tunnels == ()
    assert "no tunnel" in svc.handle({"cmd": "remove", "name": "orders"})["error"]


def test_reload(svc):
    assert ok(svc.handle({"cmd": "reload"}))["tunnels"] == []


def test_internal_errors_are_contained(svc, monkeypatch):
    monkeypatch.setattr(svc, "_handlers", {"status": lambda m: 1 / 0})
    reply = svc.handle({"cmd": "status"})
    assert not reply["ok"] and "internal error" in reply["error"]


def test_ensure_trusts_a_fresh_up_and_rechecks_a_stale_one(svc, world):
    ok(svc.handle({"cmd": "refresh"}))
    probes_before = len(world.launched)
    world.healthy = False  # would fail if re-probed
    assert ok(svc.handle({"cmd": "ensure", "port": 3390}))["state"] == "up"
    world.now += 31
    assert ok(svc.handle({"cmd": "ensure", "port": 3390}))["state"] != "up"
    assert len(world.launched) > probes_before


def test_ensure_ignores_backoff(svc, world):
    world.launch_exits = True
    ok(svc.handle({"cmd": "refresh"}))
    world.launch_exits = False
    assert ok(svc.handle({"cmd": "ensure", "port": 3390}))["state"] == "up"


def test_add_stores_password_only_when_accepted(svc, secrets, saved, tunnel, world):
    world._current_port = 3331
    new = {**cfg.tunnel_to_dict(tunnel), "name": "db2", "local_port": 3331,
           "check_type": "mysql", "check_user": "ro"}
    clash = {**new, "local_port": 3390}
    assert "already used" in svc.handle({"cmd": "add", "tunnel": clash, "check_password": "x"})["error"]
    assert secrets == {}
    dup = {**cfg.tunnel_to_dict(tunnel), "check_type": "mysql", "check_user": "ro"}
    assert "already exists" in svc.handle({"cmd": "add", "tunnel": dup, "check_password": "typo"})["error"]
    assert secrets == {}
    assert "must be a string" in svc.handle({"cmd": "add", "tunnel": new, "check_password": 5})["error"]

    ok(svc.handle({"cmd": "add", "tunnel": new, "check_password": "pw"}))
    assert secrets == {"db2": "pw"}
    assert saved[-1].get("db2").check_password_file == "~/s/db2"

    ok(svc.handle({"cmd": "remove", "name": "db2"}))
    assert secrets == {}


def test_get_returns_definition_without_secret_values(svc):
    data = ok(svc.handle({"cmd": "get", "name": "orders"}))
    assert data["local_port"] == 3390 and "check_password" not in data
    assert svc.handle({"cmd": "get", "name": "nope"})["error"] == "no tunnel named 'nope'"


def test_update_edits_in_place_and_reconnects(svc, saved, tunnel, world):
    ok(svc.handle({"cmd": "refresh"}))
    world._current_port = 3340
    changed = {**cfg.tunnel_to_dict(tunnel), "local_port": 3340, "remote_port": 3307}
    row = ok(svc.handle({"cmd": "update", "name": "orders", "tunnel": changed}))
    assert row["state"] == "up" and row["local_port"] == 3340
    assert saved[-1].get("orders").remote_port == 3307
    assert world.terminated  # the old forward was closed first


def test_update_keeps_enabled_flag_and_noop(svc, saved, tunnel):
    ok(svc.handle({"cmd": "stop", "name": "orders"}))
    saves = len(saved)
    same = {**cfg.tunnel_to_dict(tunnel), "enabled": True}
    row = ok(svc.handle({"cmd": "update", "name": "orders", "tunnel": same}))
    assert row["enabled"] is False and len(saved) == saves  # nothing changed, nothing written


def test_update_disabled_tunnel_stays_stopped(svc, saved, tunnel):
    ok(svc.handle({"cmd": "stop", "name": "orders"}))
    changed = {**cfg.tunnel_to_dict(tunnel), "remote_port": 3307}
    row = ok(svc.handle({"cmd": "update", "name": "orders", "tunnel": changed}))
    assert row["state"] == "stopped" and saved[-1].get("orders").remote_port == 3307


def test_update_errors(svc, tunnel):
    assert "must be an object" in svc.handle({"cmd": "update", "name": "orders", "tunnel": 1})["error"]
    bad = {**cfg.tunnel_to_dict(tunnel), "local_port": 80}
    assert "local port" in svc.handle({"cmd": "update", "name": "orders", "tunnel": bad})["error"]


def test_rename_moves_managed_secret(svc, saved, secrets, tunnel):
    moved = []
    svc._is_managed_secret = lambda path, name: path == f"~/s/{name}"
    svc._rename_secret = lambda old, new: moved.append((old, new)) or f"~/s/{new}"
    mysql = {**cfg.tunnel_to_dict(tunnel), "check_type": "mysql", "check_user": "ro"}
    ok(svc.handle({"cmd": "update", "name": "orders", "tunnel": mysql, "check_password": "pw"}))
    assert secrets == {"orders": "pw"}

    row = ok(svc.handle({"cmd": "rename", "name": "orders", "new_name": "primary"}))
    assert row["name"] == "primary" and row["state"] == "up"
    assert moved == [("orders", "primary")]
    assert saved[-1].get("primary").check_password_file == "~/s/primary"
    assert saved[-1].get("orders") is None
    assert "'new_name' is required" in svc.handle({"cmd": "rename", "name": "primary"})["error"]


def test_rename_rejects_existing_name(svc, tunnel, world):
    world._current_port = 3350
    ok(svc.handle({"cmd": "add", "tunnel": {**cfg.tunnel_to_dict(tunnel), "name": "b", "local_port": 3350}}))
    assert "already exists" in svc.handle({"cmd": "rename", "name": "orders", "new_name": "b"})["error"]


def test_settings_interval(svc, saved):
    assert ok(svc.handle({"cmd": "settings", "interval_seconds": 60}))["interval_seconds"] == 60
    assert saved[-1].interval_seconds == 60
    assert "between 30 and 3600" in svc.handle({"cmd": "settings", "interval_seconds": 5})["error"]
    assert "between" in svc.handle({"cmd": "settings", "interval_seconds": True})["error"]
    assert ok(svc.handle({"cmd": "settings"}))["interval_seconds"] == 60


def test_add_duplicate_copies_the_originals_password(world, tunnel, saved, tmp_path):
    from tunnel_keeper.supervisor import Supervisor
    secrets = {"orders": "pw"}

    def write(name, value):
        secrets[name] = value
        return f"~/s/{name}"

    def read(name):
        if name not in secrets:
            raise OSError("gone")
        return secrets[name]

    sup = Supervisor(Config(path=tmp_path / "t.json").with_tunnel(tunnel), world.deps())
    svc = Service(sup, save=saved.append, load=lambda path: Config(path=path), write_secret=write,
                  delete_secret=lambda name: secrets.pop(name, None), read_secret=read,
                  is_managed_secret=lambda path, name: path == f"~/s/{name}", clock=lambda: world.now)
    world._current_port = 3331
    copy = {**cfg.tunnel_to_dict(tunnel), "name": "orders-copy", "local_port": 3331, "check_type": "mysql",
            "check_user": "ro", "check_password_file": "~/s/orders"}
    ok(svc.handle({"cmd": "add", "tunnel": copy, "copy_secret_from": "orders"}))
    assert secrets["orders-copy"] == "pw" and saved[-1].get("orders-copy").check_password_file == "~/s/orders-copy"
    secrets.pop("orders")
    again = {**copy, "name": "orders-copy-2", "local_port": 3332}
    assert "type it in the form" in svc.handle({"cmd": "add", "tunnel": again, "copy_secret_from": "orders"})["error"]
