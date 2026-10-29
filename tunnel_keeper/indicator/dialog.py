"""The "New tunnel" window."""

from __future__ import annotations

import os
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from tunnel_keeper.indicator.model import FormError, form_warnings, tunnel_from_form  # noqa: E402

AUTH_CHOICES = (("agent", "ssh-agent / default keys"), ("key", "Private key file"),
                ("password_file", "Password file"))
CHECK_CHOICES = (("tcp", "Any TCP service — check the far port is reachable"),
                 ("mysql", "MySQL database — check with SELECT 1 (needs a login)"))

# submit(tunnel dict, mysql password or "", done) — must call done(error or None) on the GTK thread
Done = Callable[["str | None"], None]
Submit = Callable[[dict, str, Done], None]


class TunnelDialog(Gtk.Dialog):
    """Create a tunnel, or edit one when `initial` (a tunnel_to_form dict) is given."""

    def __init__(self, submit: Submit, initial: dict[str, str] | None = None):
        editing = initial is not None
        super().__init__(title=f"Edit {initial['name']}" if editing else "New tunnel", flags=0)
        self._initial = initial or {}
        self.set_default_size(460, -1)
        self.set_keep_above(True)
        self._submit = submit
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save and reconnect" if editing else "Create and connect", Gtk.ResponseType.OK).get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=16)
        self.get_content_area().add(grid)
        self._entries: dict[str, Gtk.Entry] = {}
        self._row = 0

        self._section(grid, "Tunnel")
        self._entry(grid, "name", "Name", "e.g. analytics-db")
        self._entry(grid, "local_port", "Local port", "e.g. 3319")
        self._entry(grid, "remote_host", "Remote host", "host as seen from the SSH server")
        self._entry(grid, "remote_port", "Remote port", "e.g. 3306")

        self._section(grid, "SSH")
        self._entry(grid, "ssh", "Login", "user@bastion-host")
        self._entry(grid, "ssh_port", "SSH port", "22")
        self._auth = self._combo(grid, "Authentication", AUTH_CHOICES, self._on_auth_changed)
        self._auth_file = Gtk.FileChooserButton(title="Choose file", action=Gtk.FileChooserAction.OPEN)
        self._auth_file.set_show_hidden(True)
        self._auth_file_label = self._attach(grid, "File", self._auth_file)

        self._section(grid, "Health check")
        self._check = self._combo(grid, "Behind it", CHECK_CHOICES, self._on_check_changed)
        self._mysql_widgets = [
            self._entry(grid, "check_user", "MySQL user", ""),
            self._entry(grid, "check_password", "MySQL password",
                        "leave blank to keep the current one" if editing else "stored owner-only in ~/.config",
                        secret=True),
            self._entry(grid, "check_database", "Database", "optional"),
        ]

        self._warning = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        self._warning.get_style_context().add_class("warning")
        grid.attach(self._warning, 0, self._row, 2, 1)
        self._row += 1
        self._entries["remote_port"].connect("changed", lambda *_: self._update_warning())

        self._error = Gtk.Label(xalign=0, wrap=True)
        self._error.get_style_context().add_class("error")
        grid.attach(self._error, 0, self._row, 2, 1)

        self._closed = False
        self.connect("destroy", lambda *_: setattr(self, "_closed", True))
        self.connect("response", self._on_response)
        self.show_all()
        self._prefill(self._initial)
        self._on_auth_changed(self._auth)
        self._on_check_changed(self._check)

    def _prefill(self, form: dict[str, str]) -> None:
        for key, entry in self._entries.items():
            if form.get(key):
                entry.set_text(form[key])
        if form.get("auth_type"):
            self._auth.set_active_id(form["auth_type"])
        if form.get("auth_path"):
            self._auth_file.set_filename(os.path.expanduser(form["auth_path"]))
        if form.get("check_type"):
            self._check.set_active_id(form["check_type"])

    # ── layout helpers ────────────────────────────────────────────────────────
    def _section(self, grid: Gtk.Grid, title: str) -> None:
        label = Gtk.Label(xalign=0)
        label.set_markup(f"<b>{title}</b>")
        grid.attach(label, 0, self._row, 2, 1)
        self._row += 1

    def _attach(self, grid: Gtk.Grid, text: str, widget: Gtk.Widget) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=1)
        widget.set_hexpand(True)
        grid.attach(label, 0, self._row, 1, 1)
        grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return label

    def _entry(self, grid: Gtk.Grid, key: str, text: str, hint: str, secret: bool = False) -> tuple:
        entry = Gtk.Entry(placeholder_text=hint, activates_default=True)
        entry.set_visibility(not secret)
        self._entries[key] = entry
        return entry, self._attach(grid, text, entry)

    def _combo(self, grid: Gtk.Grid, text: str, choices, on_change) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for value, label in choices:
            combo.append(value, label)
        combo.set_active(0)
        combo.connect("changed", on_change)
        self._attach(grid, text, combo)
        return combo

    def _on_auth_changed(self, combo: Gtk.ComboBoxText) -> None:
        visible = combo.get_active_id() != "agent"
        self._auth_file.set_visible(visible)
        self._auth_file_label.set_visible(visible)

    def _on_check_changed(self, combo: Gtk.ComboBoxText) -> None:
        visible = combo.get_active_id() == "mysql"
        for entry, label in self._mysql_widgets:
            entry.set_visible(visible)
            label.set_visible(visible)
        self._update_warning()

    def _update_warning(self) -> None:
        if not hasattr(self, "_warning"):
            return  # still building the form
        text = " ".join(form_warnings(self._form()))
        self._warning.set_text(f"⚠ {text}" if text else "")
        self._warning.set_visible(bool(text))

    # ── submit ────────────────────────────────────────────────────────────────
    def _form(self) -> dict[str, str]:
        form = {key: entry.get_text() for key, entry in self._entries.items()}
        form["auth_type"] = self._auth.get_active_id()
        form["auth_path"] = self._auth_file.get_filename() or ""
        form["check_type"] = self._check.get_active_id()
        form["check_password_file"] = self._initial.get("check_password_file", "")
        return form

    def _on_response(self, _dialog, response: int) -> None:
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        form = self._form()
        try:
            tunnel = tunnel_from_form(form)
        except FormError as exc:
            self._error.set_text(str(exc))
            self.stop_emission_by_name("response")
            return
        self.stop_emission_by_name("response")
        self._error.set_text("Connecting…")
        self.set_sensitive(False)
        self._submit(tunnel, form.get("check_password", ""), self._done)

    def _done(self, error: "str | None") -> None:
        if self._closed:  # closed while connecting; the menu shows the outcome
            return
        if error:
            self.set_sensitive(True)
            self._error.set_text(error)
            return
        self.destroy()
