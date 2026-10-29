import pytest

from tunnel_keeper.indicator import model

ROW = {"name": "orders", "state": "up", "detail": "SELECT 1 ok", "enabled": True, "local_port": 3390,
       "remote": "h:3306", "via": "me@b:22", "check_type": "mysql", "checked_at": 100}


def rows(*states):
    return [{**ROW, "name": f"t{i}", "state": s, "enabled": s != "stopped"} for i, s in enumerate(states)]


@pytest.mark.parametrize("data, icon, label, headline", [
    (None, model.ICON_OFF, "", "Tunnel service is not running"),
    ([], model.ICON_OFF, "", "No tunnels yet"),
    (rows("stopped"), model.ICON_OFF, "", "No tunnels running"),
    (rows("up"), model.ICON_OK, "", "1 of 1 tunnel up"),
    (rows("up", "up", "stopped"), model.ICON_OK, "", "2 of 2 tunnels up"),
    (rows("up", "error"), model.ICON_WARN, "1/2", "1 of 2 tunnels up"),
    (rows("down", "conflict"), model.ICON_ERROR, "0/2", "0 of 2 tunnels up"),
])
def test_overview(data, icon, label, headline):
    assert model.overview(data) == model.Overview(icon, label, headline)


def test_titles_and_details():
    assert model.tunnel_title(ROW) == "🟢  orders   :3390"
    lines = model.tunnel_details(ROW, now=130)
    assert lines[0] == "Up — SELECT 1 ok"
    assert lines[3] == "checked 30s ago · SELECT 1 check"
    assert model.tunnel_details({**ROW, "check_type": "tcp", "checked_at": 0})[3] == "not checked yet · far-end reachability check"


def test_age_minutes():
    assert model.age(0) == "not checked yet"
    assert model.age(100, now=400) == "checked 5m ago"


def test_structure_ignores_text_that_is_updated_in_place():
    assert model.structure(None) is None
    assert model.structure([ROW]) == model.structure([{**ROW, "checked_at": 999, "state": "error", "detail": "x"}])
    assert model.structure([ROW]) != model.structure([{**ROW, "enabled": False}])
    assert model.structure([ROW], now=130) != model.structure(
        [{**ROW, "state": "down", "next_retry_at": 200}], now=130)  # extra retry line


def test_tunnel_to_form_roundtrip():
    stored = {"name": "a", "ssh_user": "me", "ssh_host": "b", "ssh_port": 2525, "local_port": 3320,
              "remote_host": "h", "remote_port": 3306, "auth_type": "password_file", "auth_path": "~/pw",
              "check_type": "mysql", "check_user": "ro", "check_database": "db",
              "check_password_file": "~/.config/tunnel-keeper/secrets/a.mysql-password", "enabled": True}
    form = model.tunnel_to_form(stored)
    assert form["ssh"] == "me@b" and form["ssh_port"] == "2525"
    back = model.tunnel_from_form(form)
    assert back == {k: stored[k] for k in back}


FORM = {"name": "analytics", "ssh": "me@bastion", "ssh_port": "", "local_port": "3320",
        "remote_host": "10.0.0.1", "remote_port": "3306", "auth_type": "key", "auth_path": "/k",
        "check_type": "mysql", "check_user": "ro", "check_database": ""}


def test_form_to_tunnel():
    t = model.tunnel_from_form(FORM)
    assert (t["ssh_user"], t["ssh_host"], t["ssh_port"], t["local_port"]) == ("me", "bastion", 22, 3320)
    assert t["check_password_file"] == ""
    agent = model.tunnel_from_form({**FORM, "auth_type": "agent"})
    assert agent["auth_path"] == ""


@pytest.mark.parametrize("change, message", [
    ({"ssh": "bastion"}, "user@host"),
    ({"local_port": "abc"}, "Local port must be a number"),
    ({"name": "Bad!"}, "name must be"),
])
def test_form_errors(change, message):
    with pytest.raises(model.FormError, match=message):
        model.tunnel_from_form({**FORM, **change})


def test_retry_line_only_when_not_up():
    down = {**ROW, "state": "down", "next_retry_at": 160, "failures": 2}
    assert model.tunnel_details(down, now=130)[-1] == "next retry in 30s · 2 failed tries"
    assert len(model.tunnel_details({**ROW, "next_retry_at": 160}, now=130)) == 4


def test_form_warnings_for_mysql_ports():
    assert "usually MySQL" in model.form_warnings({"remote_port": "3306", "check_type": "tcp"})[0]
    assert model.form_warnings({"remote_port": "3306", "check_type": "mysql"}) == []
    assert model.form_warnings({"remote_port": "6379", "check_type": "tcp"}) == []
    assert model.form_warnings({"remote_port": "", "check_type": "tcp"}) == []
