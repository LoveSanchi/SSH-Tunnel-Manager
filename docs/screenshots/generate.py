"""Render README screenshots from the real GTK widgets, fed with made-up demo tunnels.

    python3 docs/screenshots/generate.py      (needs a desktop session; writes PNGs here)

Nothing talks to a real service: the client is replaced with canned demo data.
The tray dropdown itself is drawn by GNOME Shell, so menu.png re-draws the
indicator's real menu labels in a GNOME-like popup.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
_real_require = gi.require_version
gi.require_version = lambda ns, v: None if ns == "AyatanaAppIndicator3" else _real_require(ns, v)


class _FakeIndicator:
    menu = None

    def __getattr__(self, _name):
        return lambda *a: None

    def set_menu(self, menu):
        self.menu = menu


_stub = types.ModuleType("AyatanaAppIndicator3")
_stub.Indicator = types.SimpleNamespace(new=lambda *a: _FakeIndicator())
_stub.IndicatorCategory = types.SimpleNamespace(SYSTEM_SERVICES=0)
_stub.IndicatorStatus = types.SimpleNamespace(ACTIVE=1)
sys.modules["gi.repository.AyatanaAppIndicator3"] = _stub
import gi.repository  # noqa: E402

gi.repository.AyatanaAppIndicator3 = _stub

from tunnel_keeper.indicator import app, model, windows  # noqa: E402
from tunnel_keeper.indicator.dialog import TunnelDialog  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

NOW = time.time()
ROWS = [
    {"name": "orders-db", "state": "up", "detail": "SELECT 1 ok", "enabled": True, "local_port": 3390,
     "remote": "10.20.0.7:3306", "via": "deploy@bastion.example.com:22", "check_type": "mysql",
     "checked_at": NOW - 42, "failures": 0, "next_retry_at": 0, "external": False, "pid": 4242},
    {"name": "analytics-db", "state": "down", "detail": "ssh exited: Connection timed out; waiting to retry",
     "enabled": True, "local_port": 3391, "remote": "10.20.0.9:3306", "via": "deploy@bastion.example.com:22",
     "check_type": "mysql", "checked_at": NOW - 8, "failures": 2, "next_retry_at": NOW + 22,
     "external": False, "pid": None},
    {"name": "cache-redis", "state": "up", "detail": "port open", "enabled": True, "local_port": 6380,
     "remote": "10.20.1.4:6379", "via": "deploy@bastion.example.com:22", "check_type": "port",
     "checked_at": NOW - 42, "failures": 0, "next_retry_at": 0, "external": False, "pid": 4243},
    {"name": "reports-db", "state": "stopped", "detail": "stopped", "enabled": False, "local_port": 3392,
     "remote": "10.20.0.11:3306", "via": "deploy@bastion.example.com:22", "check_type": "mysql",
     "checked_at": NOW - 600, "failures": 0, "next_retry_at": 0, "external": False, "pid": None},
]
DEFINITION = {"name": "orders-db", "ssh_user": "deploy", "ssh_host": "bastion.example.com", "ssh_port": 22,
              "local_port": 3390, "remote_host": "10.20.0.7", "remote_port": 3306, "auth_type": "agent",
              "auth_path": "", "check_type": "mysql", "check_user": "readonly", "check_database": "orders",
              "check_password_file": "~/.config/tunnel-keeper/secrets/orders-db.mysql-password", "enabled": True}
LOG = """── Service events for analytics-db (journalctl --user -u tunnel-keeper) ──
2026-10-29T09:12:03+0000 laptop tunnel-keeperd[812]: INFO analytics-db: up — SELECT 1 ok
2026-10-29T11:40:17+0000 laptop tunnel-keeperd[812]: WARNING analytics-db: down — ssh exited: Connection timed out; waiting to retry
2026-10-29T11:40:32+0000 laptop tunnel-keeperd[812]: WARNING analytics-db: error — ssh exited: Connection timed out

── ssh output (~/.local/state/tunnel-keeper/analytics-db.log) ──
--- 2026-10-29T11:40:32 starting: ssh -N -p 22 -L 127.0.0.1:3391:10.20.0.9:3306 -o ExitOnForwardFailure=yes … deploy@bastion.example.com
ssh: connect to host bastion.example.com port 22: Connection timed out
"""


def fake_request(cmd, **_kw):
    if cmd == "status":
        return {"interval_seconds": 120, "config_path": "~/.config/tunnel-keeper/tunnels.json", "tunnels": ROWS}
    if cmd == "get":
        return DEFINITION
    return {}


app.client.request = fake_request


def spin(ms: int) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def snapshot(widget: Gtk.Widget, name: str, width: int = -1, framed: bool = True) -> None:
    """Render `widget` in an offscreen window and save it as a PNG."""
    off = Gtk.OffscreenWindow()
    if width > 0:
        widget.set_size_request(width, -1)
    if framed:  # offscreen windows skip the theme background: paint it ourselves
        frame = Gtk.EventBox()
        frame.get_style_context().add_class("frame")
        frame.add(widget)
        widget = frame
    off.add(widget)
    off.show_all()
    spin(400)
    off.get_surface().write_to_png(str(OUT / name))
    print("wrote", OUT / name)
    off.destroy()


def take(dialog: Gtk.Window, name: str, width: int = -1) -> None:
    body = dialog.get_child()
    dialog.remove(body)
    dialog.destroy()
    snapshot(body, name, width)


MENU_CSS = b"""
.popup { background: #2d2d2d; border-radius: 12px; padding: 8px 0; }
.popup label { color: #f2f2f2; padding: 6px 18px; font-size: 11pt; }
.popup .info label { color: #9a9a9a; }
.popup .hover { background: #424242; border-radius: 6px; margin: 0 6px; }
.popup separator { background: #4a4a4a; min-height: 1px; margin: 4px 0; }
.canvas { background: #1a1a1a; padding: 16px; }
.frame { background-color: @theme_bg_color; padding: 14px; }
"""


def _popup(items: list[tuple[str, str]], width: int) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.get_style_context().add_class("popup")
    box.set_size_request(width, -1)
    for kind, text in items:
        if kind == "sep":
            box.pack_start(Gtk.Separator(), False, False, 0)
            continue
        row = Gtk.Box()
        row.get_style_context().add_class(kind)
        row.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        if kind == "hover":
            row.pack_end(Gtk.Label(label="›"), False, False, 0)
        box.pack_start(row, False, False, 0)
    return box


def install_css() -> None:
    from gi.repository import Gdk

    provider = Gtk.CssProvider()
    provider.load_from_data(MENU_CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def menu_png() -> None:
    head = model.overview(ROWS).headline
    top = [("info", head), ("sep", "")]
    top += [("hover" if r["name"] == "analytics-db" else "item", model.tunnel_title(r)) for r in ROWS]
    top += [("sep", ""), ("item", "Refresh all"), ("item", "New tunnel…"), ("item", "Settings…"),
            ("sep", ""), ("item", "Hide indicator")]
    row = next(r for r in ROWS if r["name"] == "analytics-db")
    sub = [("info", line) for line in model.tunnel_details(row, NOW)] + [("sep", "")]
    sub += [("item", t) for t in ("Refresh", "Restart", "Kill", "Copy 127.0.0.1:3391", "Show log")]
    sub += [("sep", ""), ("item", "Edit…"), ("item", "Rename…"), ("item", "Remove…")]

    canvas = Gtk.Box(spacing=8)
    canvas.get_style_context().add_class("canvas")
    left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    left.pack_start(_popup(top, 300), False, False, 0)
    canvas.pack_start(left, False, False, 0)
    right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    spacer = Gtk.Box()
    spacer.set_size_request(-1, 92)
    right.pack_start(spacer, False, False, 0)
    right.pack_start(_popup(sub, 430), False, False, 0)
    canvas.pack_start(right, False, False, 0)
    snapshot(canvas, "menu.png", framed=False)


def main() -> None:
    install_css()
    menu_png()

    take(TunnelDialog(lambda *a: None), "new-tunnel.png", 480)
    take(TunnelDialog(lambda *a: None, initial=model.tunnel_to_form(DEFINITION)), "edit-tunnel.png", 480)
    take(windows.SettingsDialog(120, lambda *a: None), "settings.png", 460)

    log = windows.LogWindow("analytics-db", lambda: LOG, lambda fn: fn())
    spin(300)
    log.set_title("")
    body = log.get_child()
    body.set_size_request(900, 300)
    log.remove(body)
    log.destroy()
    snapshot(body, "log.png")


if __name__ == "__main__":
    main()
