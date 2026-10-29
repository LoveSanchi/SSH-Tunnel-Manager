"""Top-bar indicator: shows every tunnel's health and controls the service."""

from __future__ import annotations

import os

# The menu is drawn by GNOME Shell, not by us, so under native Wayland GTK gets no
# input serial and clipboard writes are dropped. XWayland keeps "Copy address" working.
os.environ.setdefault("GDK_BACKEND", "x11")

import fcntl  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")  # GTK 4 is installed too; never let Gdk 4 load first
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from tunnel_keeper import client, logview, paths  # noqa: E402
from tunnel_keeper.indicator import model, windows  # noqa: E402
from tunnel_keeper.indicator.dialog import TunnelDialog  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent / "icons"
POLL_SECONDS = 3
STATUS_TIMEOUT = 2.0


class Indicator:
    def __init__(self) -> None:
        self._ind = AppIndicator.Indicator.new(
            "tunnel-keeper", model.ICON_OFF, AppIndicator.IndicatorCategory.SYSTEM_SERVICES
        )
        self._ind.set_icon_theme_path(str(ICON_DIR))
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title("Tunnels")
        self._rows: list[dict] | None = None
        self._interval = 0
        self._busy: str = ""
        self._structure: Any = object()
        self._polling = False
        self._header: Gtk.MenuItem | None = None
        self._titles: dict[str, Gtk.MenuItem] = {}
        self._details: dict[str, list[Gtk.MenuItem]] = {}
        self._render()
        self._poll()
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll)

    # ── polling (socket I/O off the GTK thread; results applied on it) ────────
    def _poll(self) -> bool:
        if not self._polling:
            self._polling = True
            threading.Thread(target=self._fetch, daemon=True).start()
        return True  # keep the GLib timer running

    def _fetch(self) -> None:
        try:
            data = client.request("status", timeout=STATUS_TIMEOUT)
        except (client.DaemonUnavailable, client.DaemonError):
            data = None
        GLib.idle_add(self._apply, data)

    def _apply(self, data: dict | None) -> bool:
        self._polling = False
        self._rows = data["tunnels"] if data else None
        self._interval = data["interval_seconds"] if data else 0
        self._render()
        return False

    def _render(self) -> None:
        view = model.overview(self._rows)
        self._ind.set_icon_full(view.icon, view.headline)
        self._ind.set_label(view.label, "0/0")
        structure = (model.structure(self._rows), self._busy)
        if structure != self._structure:
            self._structure = structure
            self._ind.set_menu(self._build_menu())
        self._update_texts(view.headline)

    def _update_texts(self, headline: str) -> None:
        """Refresh every label in place: states and "checked Ns ago" stay current."""
        if self._header:
            self._header.set_label(f"⏳ {self._busy}…" if self._busy else headline)
        for row in self._rows or []:
            title = self._titles.get(row["name"])
            if title:
                title.set_label(model.tunnel_title(row))
            for item, text in zip(self._details.get(row["name"], []), model.tunnel_details(row)):
                item.set_label(text)

    # ── background work ───────────────────────────────────────────────────────
    def _run(self, label: str, fn: Callable[[], Any], on_error: Callable[[str], None] | None = None) -> None:
        """Run a slow daemon call off the GTK thread; update the menu when it finishes."""
        self._busy = label
        self._render()

        def worker() -> None:
            error = None
            try:
                fn()
            except (client.DaemonUnavailable, client.DaemonError, OSError) as exc:
                error = str(exc)
            GLib.idle_add(self._finished, error, on_error)

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self, error: str | None, on_error: Callable[[str], None] | None) -> bool:
        self._busy = ""
        if error:
            (on_error or windows.error)(error)
        self._poll()
        return False

    def _action(self, label: str, cmd: str, **args: Any) -> Callable[..., None]:
        return lambda *_: self._run(label, lambda: client.request(cmd, **args))

    def _submitter(self, label: str, request: Callable[[], Any]) -> Callable[[Callable], None]:
        """For dialogs: run `request`, then tell the dialog (done) on the GTK thread."""
        def go(done: Callable[[str | None], None]) -> None:
            def work() -> None:
                request()
                GLib.idle_add(done, None)
            self._run(label, work, on_error=done)
        return go

    # ── menu ──────────────────────────────────────────────────────────────────
    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        self._titles, self._details = {}, {}
        self._header = _label(menu, "")
        menu.append(Gtk.SeparatorMenuItem())
        if self._rows is None:
            _item(menu, "Start tunnel service", self._start_service)
        else:
            for row in self._rows:
                top = Gtk.MenuItem(label=model.tunnel_title(row))
                top.set_submenu(self._tunnel_menu(row))
                menu.append(top)
                self._titles[row["name"]] = top
            if self._rows:
                menu.append(Gtk.SeparatorMenuItem())
            _item(menu, "Refresh all", self._action("Refreshing", "refresh"), sensitive=not self._busy)
            _item(menu, "New tunnel…", self._new_tunnel)
            _item(menu, "Settings…", self._settings)
        menu.append(Gtk.SeparatorMenuItem())
        _item(menu, "Hide indicator", lambda *_: Gtk.main_quit())
        menu.show_all()
        return menu

    def _tunnel_menu(self, row: dict) -> Gtk.Menu:
        name, idle = row["name"], not self._busy
        sub = Gtk.Menu()
        self._details[name] = [_label(sub, line) for line in model.tunnel_details(row)]
        sub.append(Gtk.SeparatorMenuItem())
        if row["enabled"]:
            _item(sub, "Refresh", self._action(f"Checking {name}", "refresh", name=name), sensitive=idle)
            _item(sub, "Restart", self._action(f"Restarting {name}", "restart", name=name), sensitive=idle)
            _item(sub, "Kill", self._action(f"Stopping {name}", "stop", name=name), sensitive=idle)
        else:
            _item(sub, "Start", self._action(f"Starting {name}", "start", name=name), sensitive=idle)
        _item(sub, f"Copy 127.0.0.1:{row['local_port']}", lambda *_: _copy(f"127.0.0.1:{row['local_port']}"))
        _item(sub, "Show log", lambda *_: self._show_log(name))
        sub.append(Gtk.SeparatorMenuItem())
        _item(sub, "Edit…", lambda *_: self._edit(name), sensitive=idle)
        _item(sub, "Duplicate…", lambda *_: self._duplicate(name), sensitive=idle)
        _item(sub, "Rename…", lambda *_: self._rename(name), sensitive=idle)
        _item(sub, "Remove…", lambda *_: self._remove(name), sensitive=idle)
        return sub

    # ── menu handlers ─────────────────────────────────────────────────────────
    def _start_service(self, *_: Any) -> None:
        self._run("Starting service", lambda: subprocess.run(
            ["systemctl", "--user", "start", "tunnel-keeper.service"], check=True, timeout=30))

    def _remove(self, name: str) -> None:
        if windows.confirm(f"Remove tunnel '{name}'?", "It is closed now and will not be reopened."):
            self._run(f"Removing {name}", lambda: client.request("remove", name=name))

    def _show_log(self, name: str) -> None:
        windows.LogWindow(name, lambda: logview.report(name),
                          lambda fn: threading.Thread(target=fn, daemon=True).start()).present()

    def _settings(self, *_: Any) -> None:
        def submit(interval: int, done: Callable[[str | None], None]) -> None:
            self._submitter("Saving settings", lambda: client.request("settings", interval_seconds=interval))(done)

        windows.SettingsDialog(self._interval or 120, submit).present()

    def _rename(self, name: str) -> None:
        def submit(new_name: str, done: Callable[[str | None], None]) -> None:
            self._submitter(f"Renaming {name}", lambda: client.request("rename", name=name, new_name=new_name))(done)

        windows.RenameDialog(name, submit).present()

    def _new_tunnel(self, *_: Any) -> None:
        def submit(tunnel: dict, password: str, done: Callable[[str | None], None]) -> None:
            # The service stores the password itself, and only once the tunnel is accepted.
            extra = {"check_password": password} if tunnel["check_type"] == "mysql" and password else {}
            self._submitter(f"Connecting {tunnel['name']}",
                            lambda: client.request("add", tunnel=tunnel, **extra))(done)

        TunnelDialog(submit).present()

    def _edit(self, name: str) -> None:
        def submit(tunnel: dict, password: str, done: Callable[[str | None], None]) -> None:
            extra = {"check_password": password} if tunnel["check_type"] == "mysql" and password else {}
            self._submitter(f"Saving {name}",
                            lambda: client.request("update", name=name, tunnel=tunnel, **extra))(done)

        def fetched(definition: dict) -> bool:
            TunnelDialog(submit, initial=model.tunnel_to_form(definition)).present()
            return False

        self._run(f"Loading {name}",
                  lambda: GLib.idle_add(fetched, client.request("get", name=name)))


    def _duplicate(self, name: str) -> None:
        """A new tunnel pre-filled from `name`; its stored MySQL password is copied unless
        a new one is typed."""
        def submit(tunnel: dict, password: str, done: Callable[[str | None], None]) -> None:
            extra = {"check_password": password} if tunnel["check_type"] == "mysql" and password else {}
            self._submitter(f"Connecting {tunnel['name']}",
                            lambda: client.request("add", tunnel=tunnel, copy_secret_from=name, **extra))(done)

        rows = self._rows or []
        names, ports = {r["name"] for r in rows}, {int(r["local_port"]) for r in rows}

        def fetched(definition: dict) -> bool:
            TunnelDialog(submit, initial=model.duplicate_form(definition, names, ports), copy_of=name).present()
            return False

        self._run(f"Loading {name}", lambda: GLib.idle_add(fetched, client.request("get", name=name)))


# ── small GTK helpers ─────────────────────────────────────────────────────────
def _label(menu: Gtk.Menu, text: str) -> Gtk.MenuItem:
    item = Gtk.MenuItem(label=text)
    item.set_sensitive(False)
    menu.append(item)
    return item


def _item(menu: Gtk.Menu, text: str, handler: Callable, sensitive: bool = True) -> None:
    item = Gtk.MenuItem(label=text)
    item.connect("activate", handler)
    item.set_sensitive(sensitive)
    menu.append(item)


def _copy(text: str) -> None:
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)


def _single_instance() -> object | None:
    """Hold a lock for our lifetime; None if another indicator already runs."""
    lock_path = paths.socket_path().with_name("tunnel-keeper-indicator.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    lock = _single_instance()
    if lock is None:
        print("tunnel-keeper-indicator is already running", file=sys.stderr)
        return 0
    Indicator()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
