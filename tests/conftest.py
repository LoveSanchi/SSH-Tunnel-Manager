import pytest

from tunnel_keeper.config import Tunnel


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Never touch the real ~/.config or runtime dir from a test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("TUNNEL_KEEPER_SOCKET", raising=False)
    (tmp_path / "run").mkdir()


@pytest.fixture
def tunnel():
    return Tunnel(
        name="orders",
        ssh_host="10.0.0.5",
        ssh_user="deploy",
        ssh_port=2525,
        local_port=3390,
        remote_host="10.20.0.7",
        remote_port=3306,
    )
