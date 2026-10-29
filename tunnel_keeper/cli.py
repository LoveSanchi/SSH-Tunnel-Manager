"""tunnelctl — command-line control for the tunnel-keeper service."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from tunnel_keeper import __version__, client, envimport, logview
from tunnel_keeper import config as cfg
from tunnel_keeper.client import DaemonError, DaemonUnavailable

# `ensure` exit codes: scripts that used to open their own tunnel rely on these.
EXIT_OK, EXIT_UNHEALTHY, EXIT_NOT_MANAGED = 0, 1, 2
EXIT_NO_DAEMON = 3
# Under the repo's 30s hook timeout, with room left for the script's own fallback.
ENSURE_TIMEOUT_SECONDS = 20.0

MARKS = {"up": "●", "down": "○", "stopped": "◌", "conflict": "!", "error": "✕"}

Request = Callable[..., Any]


def _age(ts: float) -> str:
    if not ts:
        return "never"
    secs = int(time.time() - ts)
    return f"{secs}s ago" if secs < 120 else f"{secs // 60}m ago"


def format_rows(rows: list[dict]) -> str:
    if not rows:
        return "No tunnels yet — add one with `tunnelctl add` or the top-bar menu."
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        mark = MARKS.get(r["state"], "?")
        lines.append(
            f"{mark} {r['name']:<{width}}  {r['state']:<8} :{r['local_port']:<5} → {r['remote']:<24} "
            f"checked {_age(r['checked_at'])}"
        )
        if r.get("detail"):
            lines.append(f"  {'':<{width}}  {r['detail']}")
    return "\n".join(lines)


def cmd_status(args, request: Request) -> int:
    data = request("status")
    print(json.dumps(data, indent=2) if args.json else format_rows(data["tunnels"]))
    return EXIT_OK


def cmd_refresh(args, request: Request) -> int:
    data = request("refresh", name=args.name) if args.name else request("refresh")
    print(format_rows(data["tunnels"]))
    return EXIT_OK if all(r["state"] in ("up", "stopped") for r in data["tunnels"]) else EXIT_UNHEALTHY


def cmd_ensure(args, request: Request) -> int:
    """Used by scripts before they connect: 0 healthy, 1 managed but not healthy,
    2 not managed here (fall back to your own tunnel), 3 service not running."""
    target = {"port": args.port} if args.port else {"name": args.name}
    try:
        row = request("ensure", timeout=ENSURE_TIMEOUT_SECONDS, **target)
    except DaemonError as exc:
        if exc.code == "not_managed":
            return EXIT_NOT_MANAGED
        if exc.code in ("timeout", "error"):
            # The service is wedged or broke: let the caller fall back to its own tunnel.
            print(f"tunnelctl: {exc}", file=sys.stderr)
            return EXIT_NO_DAEMON
        raise
    if not args.quiet:
        print(format_rows([row]), file=sys.stderr)
    return EXIT_OK if row["state"] == "up" else EXIT_UNHEALTHY


def _one(cmd: str) -> Callable[..., int]:
    def run(args, request: Request) -> int:
        target = {"port": args.port} if getattr(args, "port", None) else {"name": args.name}
        row = request(cmd, **target)
        print(format_rows([row]) if "state" in row else f"removed {row.get('removed', args.name)}")
        return EXIT_OK
    return run


def cmd_rename(args, request: Request) -> int:
    print(format_rows([request("rename", name=args.name, new_name=args.new_name)]))
    return EXIT_OK


def cmd_interval(args, request: Request) -> int:
    data = request("settings", interval_seconds=args.seconds) if args.seconds else request("status")
    print(f"full check every {data['interval_seconds']}s (a dead ssh is reopened within ~5s)")
    return EXIT_OK


def cmd_logs(args, request: Request) -> int:
    sys.stdout.write(logview.report(args.name))
    return EXIT_OK


def cmd_add(args, request: Request) -> int:
    tunnel = {
        "name": args.name,
        "ssh_host": args.ssh_host,
        "ssh_port": args.ssh_port,
        "ssh_user": args.ssh_user,
        "local_port": args.local_port,
        "remote_host": args.remote_host,
        "remote_port": args.remote_port,
        "auth_type": "key" if args.key else "password_file" if args.password_file else "agent",
        "auth_path": args.key or args.password_file or "",
        "check_type": "mysql" if args.mysql_user else "tcp",
        "check_user": args.mysql_user or "",
        "check_password_file": args.mysql_password_file or "",
        "check_database": args.mysql_database or "",
    }
    _warn(cfg.tunnel_from_dict(tunnel))
    print(format_rows([request("add", tunnel=tunnel, replace=args.replace)]))
    return EXIT_OK


def _warn(tunnel: cfg.Tunnel) -> None:
    for text in cfg.warnings(tunnel):
        print(f"warning: {text}", file=sys.stderr)


def cmd_import(args, request: Request) -> int:
    source = Path(args.source).expanduser()
    env = envimport.load_env(source)
    if not env:
        print(f"no settings found in {source} (a .env file, or a folder with .env / .env.local)", file=sys.stderr)
        return EXIT_UNHEALTHY
    tunnel = envimport.tunnel_from_env(env, args.name, args.prefix, args.fallback_prefix or "",
                                       args.mysql_prefix or "")
    if args.local_port:
        tunnel = cfg.validate(dataclasses.replace(tunnel, local_port=args.local_port))
    _warn(tunnel)
    print(format_rows([request("add", tunnel=cfg.tunnel_to_dict(tunnel), replace=args.replace)]))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tunnelctl", description="Control the tunnel-keeper service.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="show every tunnel")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=cmd_status)

    s = sub.add_parser("refresh", help="check now and reopen anything that is down")
    s.add_argument("name", nargs="?")
    s.set_defaults(run=cmd_refresh)

    s = sub.add_parser("ensure", help="make sure one tunnel is healthy (for scripts)")
    target = s.add_mutually_exclusive_group(required=True)
    target.add_argument("name", nargs="?")
    target.add_argument("--port", type=int)
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(run=cmd_ensure)

    for name, help_text in (("start", "start (and keep up) a stopped tunnel"),
                            ("restart", "close and reopen a tunnel"),
                            ("remove", "stop a tunnel and delete it")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("name")
        s.set_defaults(run=_one(name))

    s = sub.add_parser("stop", help="kill a tunnel and stop watching it")
    target = s.add_mutually_exclusive_group(required=True)
    target.add_argument("name", nargs="?")
    target.add_argument("--port", type=int)
    s.set_defaults(run=_one("stop"))

    s = sub.add_parser("rename", help="rename a tunnel")
    s.add_argument("name")
    s.add_argument("new_name")
    s.set_defaults(run=cmd_rename)

    s = sub.add_parser("interval", help="show or set how often every tunnel is fully checked")
    s.add_argument("seconds", nargs="?", type=int)
    s.set_defaults(run=cmd_interval)

    s = sub.add_parser("logs", help="a tunnel's service events and ssh output")
    s.add_argument("name")
    s.set_defaults(run=cmd_logs)

    s = sub.add_parser("add", help="create a tunnel")
    s.add_argument("name")
    s.add_argument("--ssh", dest="ssh_target", required=True, metavar="USER@HOST")
    s.add_argument("--ssh-port", type=int, default=22)
    s.add_argument("--local-port", type=int, required=True)
    s.add_argument("--remote", required=True, metavar="HOST:PORT")
    auth = s.add_mutually_exclusive_group()
    auth.add_argument("--key", metavar="FILE")
    auth.add_argument("--password-file", metavar="FILE")
    s.add_argument("--mysql-user", help="it is MySQL: check with SELECT 1 as this user "
                                        "(otherwise: check the far port is reachable)")
    s.add_argument("--mysql-password-file", metavar="FILE",
                   help="file holding the password (stored as-is; not copied)")
    s.add_argument("--mysql-database")
    s.add_argument("--replace", action="store_true", help="overwrite a tunnel of the same name")
    s.set_defaults(run=cmd_add)

    s = sub.add_parser("import-env", help="create a tunnel from a project's .env settings")
    s.add_argument("source", help="a .env file, or a folder holding .env / .env.local")
    s.add_argument("--name", required=True, help="name for the new tunnel")
    s.add_argument("--prefix", required=True, help="variable prefix, e.g. DB_TUNNEL for DB_TUNNEL_HOST=…")
    s.add_argument("--fallback-prefix", help="borrow missing ssh settings from this prefix")
    s.add_argument("--mysql-prefix", help="e.g. MYSQL for MYSQL_USER/PASSWORD/DATABASE → SELECT 1 check")
    s.add_argument("--local-port", type=int, help="override the local port")
    s.add_argument("--replace", action="store_true", help="overwrite a tunnel of the same name")
    s.set_defaults(run=cmd_import)
    return p


def _split_targets(args) -> None:
    if getattr(args, "ssh_target", None):
        user, sep, host = args.ssh_target.partition("@")
        if not sep:
            raise SystemExit("tunnelctl: --ssh must look like USER@HOST")
        args.ssh_user, args.ssh_host = user, host
    if getattr(args, "remote", None):
        host, sep, port = args.remote.rpartition(":")
        if not sep or not port.isdigit():
            raise SystemExit("tunnelctl: --remote must look like HOST:PORT")
        args.remote_host, args.remote_port = host, int(port)


def main(argv: list[str] | None = None, request: Request = client.request) -> int:
    args = build_parser().parse_args(argv)
    _split_targets(args)
    try:
        return args.run(args, request)
    except DaemonUnavailable as exc:
        print(f"tunnelctl: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    except (DaemonError, argparse.ArgumentTypeError, cfg.ConfigError) as exc:
        print(f"tunnelctl: {exc}", file=sys.stderr)
        return EXIT_UNHEALTHY


if __name__ == "__main__":
    sys.exit(main())
