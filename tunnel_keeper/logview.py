"""Everything we know about one tunnel's history: its ssh log and the service's events."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from tunnel_keeper import paths

TAIL_BYTES = 64 * 1024
JOURNAL_LINES = 2000

Runner = Callable[[list[str]], str]

NO_SSH_LOG = (
    "No ssh log yet. tunnel-keeper writes one for every ssh it starts itself;\n"
    "this tunnel is still the one another tool opened (shown as \"opened outside\n"
    "tunnel-keeper\"), or it has not been started yet. Restart it from the menu to\n"
    "have tunnel-keeper open — and log — its own."
)


def tail_file(path: Path, max_bytes: int = TAIL_BYTES) -> str | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    if size > max_bytes:
        data = data.split("\n", 1)[-1]  # drop the partial first line
    return data


def _journal(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(could not read the journal: {exc})"


def service_events(name: str, run: Runner = _journal, lines: int = JOURNAL_LINES) -> list[str]:
    """The service's journal lines about `name` (state changes, edits, starts/stops)."""
    out = run(["journalctl", "--user", "-u", "tunnel-keeper", "--no-pager", "-o", "short-iso", "-n", str(lines)])
    markers = (f" {name}: ", f"tunnel {name} ", f"checking tunnel {name} ")
    return [line for line in out.splitlines() if any(m in line for m in markers)]


def report(name: str, run: Runner = _journal) -> str:
    """A plain-text report for the log window and `tunnelctl logs`."""
    events = service_events(name, run)
    ssh_log = tail_file(paths.log_file(name))
    parts = [
        f"── Service events for {name} (journalctl --user -u tunnel-keeper) ──",
        "\n".join(events) if events else "(none yet)",
        "",
        f"── ssh output ({paths.log_file(name)}) ──",
        ssh_log.rstrip() if ssh_log and ssh_log.strip() else NO_SSH_LOG,
    ]
    return "\n".join(parts) + "\n"
