"""Small windows opened from the top-bar menu: log viewer, settings, rename, alerts."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from tunnel_keeper.config import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS  # noqa: E402

Done = Callable[["str | None"], None]


class LogWindow(Gtk.Window):
    """Shows a tunnel's service events and ssh output. `load` runs off the GTK thread."""

    def __init__(self, name: str, load: Callable[[], str], run_in_background: Callable[[Callable], None]):
        super().__init__(title=f"Log — {name}")
        self.set_default_size(820, 480)
        self._load = load
        self._background = run_in_background

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.add(box)
        bar = Gtk.Box(spacing=6)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.reload())
        bar.pack_end(refresh, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self._view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self._view)
        box.pack_start(scroller, True, True, 0)
        self.show_all()
        self.reload()

    def reload(self) -> None:
        self._view.get_buffer().set_text("Loading…")
        self._background(lambda: GLib.idle_add(self._show, self._load()))

    def _show(self, text: str) -> bool:
        buffer = self._view.get_buffer()
        buffer.set_text(text)
        self._view.scroll_to_iter(buffer.get_end_iter(), 0.0, False, 0.0, 1.0)
        return False


class _FormDialog(Gtk.Dialog):
    """A one-row form whose submit runs in the background and reports back via done()."""

    def __init__(self, title: str, ok_label: str, label: str, widget: Gtk.Widget, hint: str = ""):
        super().__init__(title=title, flags=0)
        self.set_keep_above(True)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button(ok_label, Gtk.ResponseType.OK).get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=16)
        grid.attach(Gtk.Label(label=label, xalign=1), 0, 0, 1, 1)
        widget.set_hexpand(True)
        grid.attach(widget, 1, 0, 1, 1)
        if hint:
            note = Gtk.Label(label=hint, xalign=0, wrap=True)
            note.get_style_context().add_class("dim-label")
            grid.attach(note, 0, 1, 2, 1)
        self._error = Gtk.Label(xalign=0, wrap=True)
        grid.attach(self._error, 0, 2, 2, 1)
        self.get_content_area().add(grid)
        self._closed = False
        self.connect("destroy", lambda *_: setattr(self, "_closed", True))
        self.connect("response", self._on_response)
        self.show_all()

    def value(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def submit(self, value, done: Done) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _on_response(self, _dialog, response: int) -> None:
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        self.stop_emission_by_name("response")
        self._error.set_text("Saving…")
        self.set_sensitive(False)
        self.submit(self.value(), self._done)

    def _done(self, error: "str | None") -> None:
        if self._closed:
            return
        if error:
            self.set_sensitive(True)
            self._error.set_text(error)
            return
        self.destroy()


class SettingsDialog(_FormDialog):
    def __init__(self, interval: int, submit: Callable[[int, Done], None]):
        self._spin = Gtk.SpinButton.new_with_range(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, 10)
        self._spin.set_value(interval)
        self._submit = submit
        super().__init__("Tunnel settings", "Save", "Check every (seconds)", self._spin,
                         "How often every tunnel gets a full health check (SELECT 1 for MySQL). "
                         "A tunnel whose ssh dies is reopened within ~5s regardless.")

    def value(self) -> int:
        return int(self._spin.get_value())

    def submit(self, value: int, done: Done) -> None:
        self._submit(value, done)


class RenameDialog(_FormDialog):
    def __init__(self, name: str, submit: Callable[[str, Done], None]):
        self._entry = Gtk.Entry(text=name, activates_default=True)
        self._submit = submit
        super().__init__(f"Rename {name}", "Rename", "New name", self._entry,
                         "a-z, 0-9, '-' and '_'. The tunnel is briefly reopened under its new name.")

    def value(self) -> str:
        return self._entry.get_text().strip()

    def submit(self, value: str, done: Done) -> None:
        self._submit(value, done)


def confirm(title: str, body: str) -> bool:
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
    dialog.format_secondary_text(body)
    dialog.set_keep_above(True)
    ok = dialog.run() == Gtk.ResponseType.OK
    dialog.destroy()
    return ok


def error(message: str) -> None:
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                               text="Tunnel action failed")
    dialog.format_secondary_text(message)
    dialog.set_keep_above(True)
    dialog.get_message_area().foreach(
        lambda w: w.set_ellipsize(Pango.EllipsizeMode.NONE) if isinstance(w, Gtk.Label) else None)
    dialog.run()
    dialog.destroy()
