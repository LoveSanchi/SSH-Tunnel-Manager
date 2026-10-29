from pathlib import Path

from tunnel_keeper import paths


def test_xdg_locations(tmp_path):
    assert paths.config_file() == tmp_path / "config" / "tunnel-keeper" / "tunnels.json"
    assert paths.secrets_dir() == tmp_path / "config" / "tunnel-keeper" / "secrets"
    assert paths.log_file("a") == tmp_path / "state" / "tunnel-keeper" / "a.log"
    assert paths.socket_path() == tmp_path / "run" / "tunnel-keeper.sock"


def test_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert paths.config_dir() == tmp_path / ".config" / "tunnel-keeper"
    assert paths.socket_path() == tmp_path / "state" / "tunnel-keeper" / "tunnel-keeper.sock"
    monkeypatch.setenv("TUNNEL_KEEPER_SOCKET", "/x/y.sock")
    assert paths.socket_path() == Path("/x/y.sock")
    assert paths.expand("") == ""
