"""What the top-bar indicator shows — pure functions, no GTK, so they are testable."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from tunnel_keeper import config as cfg

ICON_OK = "tunnel-keeper-ok"
ICON_WARN = "tunnel-keeper-warn"
ICON_ERROR = "tunnel-keeper-error"
ICON_OFF = "tunnel-keeper-off"

DOTS = {"up": "🟢", "down": "🔴", "error": "🔴", "conflict": "🟠", "stopped": "⚪"}


@dataclass(frozen=True)
class Overview:
    icon: str
    label: str
    headline: str


def overview(rows: list[dict] | None) -> Overview:
    """Icon + short label for the top bar. `rows=None` means the service is unreachable."""
    if rows is None:
        return Overview(ICON_OFF, "", "Tunnel service is not running")
    watched = [r for r in rows if r.get("enabled")]
    if not watched:
        return Overview(ICON_OFF, "", "No tunnels running" if rows else "No tunnels yet")
    up = sum(1 for r in watched if r["state"] == "up")
    headline = f"{up} of {len(watched)} tunnel{'s' if len(watched) != 1 else ''} up"
    if up == len(watched):
        return Overview(ICON_OK, "", headline)
    if up == 0:
        return Overview(ICON_ERROR, f"{up}/{len(watched)}", headline)
    return Overview(ICON_WARN, f"{up}/{len(watched)}", headline)


def age(ts: float, now: float | None = None) -> str:
    if not ts:
        return "not checked yet"
    secs = int((now if now is not None else time.time()) - ts)
    if secs < 60:
        return f"checked {secs}s ago"
    return f"checked {secs // 60}m ago"


def tunnel_title(row: dict) -> str:
    return f"{DOTS.get(row['state'], '⚫')}  {row['name']}   :{row['local_port']}"


def tunnel_details(row: dict, now: float | None = None) -> list[str]:
    lines = [f"{row['state'].capitalize()} — {row.get('detail') or 'no detail'}"]
    lines.append(f"127.0.0.1:{row['local_port']} → {row['remote']}")
    lines.append(f"via {row['via']}")
    lines.append(f"{age(row.get('checked_at', 0), now)} · {CHECK_LABELS.get(row['check_type'], row['check_type'])}")
    retry_at = row.get("next_retry_at") or 0
    if retry_at and row["state"] != "up":
        current = now if now is not None else time.time()
        lines.append(f"next retry in {max(0, int(retry_at - current))}s · {row.get('failures', 0)} failed tries")
    return lines


CHECK_LABELS = {"tcp": "far-end reachability check", "mysql": "SELECT 1 check"}


def form_warnings(form: dict[str, str]) -> list[str]:
    """Non-blocking advice for the form, e.g. a MySQL port with a plain reachability check."""
    port = form.get("remote_port", "").strip()
    if form.get("check_type", "tcp") != "tcp" or not port.isdigit():
        return []
    probe = cfg.Tunnel(name="x", ssh_host="h", ssh_user="u", local_port=1024, remote_host="h",
                       remote_port=int(port), check_type="tcp")
    return cfg.warnings(probe)


def structure(rows: list[dict] | None, now: float | None = None) -> Any:
    """Changes only when the menu needs new items. Text (states, "checked Ns ago")
    is updated in place on every poll, so it never goes stale while the menu exists."""
    if rows is None:
        return None
    return tuple((r["name"], r["enabled"], len(tunnel_details(r, now))) for r in rows)


def tunnel_to_form(t: dict[str, Any]) -> dict[str, str]:
    """Pre-fill the Edit form from a stored tunnel definition (no secret values)."""
    return {
        "name": t["name"],
        "ssh": f"{t['ssh_user']}@{t['ssh_host']}",
        "ssh_port": str(t["ssh_port"]),
        "local_port": str(t["local_port"]),
        "remote_host": t["remote_host"],
        "remote_port": str(t["remote_port"]),
        "auth_type": t["auth_type"],
        "auth_path": t.get("auth_path", ""),
        "check_type": t["check_type"],
        "check_user": t.get("check_user", ""),
        "check_database": t.get("check_database", ""),
        "check_password_file": t.get("check_password_file", ""),
    }


def duplicate_form(t: dict[str, Any], names: set[str], ports: set[int]) -> dict[str, str]:
    """The Duplicate form: everything copied, with a name and local port no tunnel uses yet
    (two tunnels can't listen on the same port). Change what differs, then create."""
    form = tunnel_to_form(t)
    base = f"{t['name']}-copy"
    name, n = base, 2
    while name in names:
        name, n = f"{base}-{n}", n + 1
    port = int(t["local_port"]) + 1
    while port in ports:
        port += 1
    return {**form, "name": name, "local_port": str(port) if port <= 65535 else ""}


class FormError(ValueError):
    pass


def _port(text: str, label: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise FormError(f"{label} must be a number")
    return int(text)


def tunnel_from_form(form: dict[str, str]) -> dict[str, Any]:
    """Turn the New-tunnel dialog's text fields into a validated tunnel dict.

    The MySQL password itself is not part of the dict; the caller stores it and
    sets `check_password_file`."""
    target = form.get("ssh", "").strip()
    user, sep, host = target.partition("@")
    if not sep or not user or not host:
        raise FormError("SSH login must look like user@host")
    auth_type = form.get("auth_type", "agent")
    tunnel = {
        "name": form.get("name", "").strip(),
        "ssh_user": user,
        "ssh_host": host,
        "ssh_port": _port(form.get("ssh_port", "22") or "22", "SSH port"),
        "local_port": _port(form.get("local_port", ""), "Local port"),
        "remote_host": form.get("remote_host", "").strip(),
        "remote_port": _port(form.get("remote_port", ""), "Remote port"),
        "auth_type": auth_type,
        "auth_path": form.get("auth_path", "").strip() if auth_type != "agent" else "",
        "check_type": form.get("check_type", "tcp"),
        "check_user": form.get("check_user", "").strip(),
        "check_database": form.get("check_database", "").strip(),
        "check_password_file": form.get("check_password_file", "") if form.get("check_type") == "mysql" else "",
    }
    try:
        cfg.tunnel_from_dict(tunnel)
    except cfg.ConfigError as exc:
        raise FormError(str(exc)) from exc
    return tunnel
