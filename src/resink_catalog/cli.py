"""Command line: `resink-catalog serve`."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .catalog import Catalog, ImportErrorForUser
from .home import default_home, ensure_home, init_db, load_or_create_token
from .lock import CatalogInUse, CatalogLock

DEFAULT_PORT = 8765
HOST = "127.0.0.1"  # the address in every link; the catalog listens only here...
CONTAINER_ENV = "RESINK_CATALOG_IN_CONTAINER"  # ...except inside the Docker image, see listen_host()


def listen_host() -> str:
    """127.0.0.1, always, except inside the Docker image (which sets RESINK_CATALOG_IN_CONTAINER=1): there the process
    must listen on the container's interface, and the user keeps it private by publishing the port on the host's
    127.0.0.1 only (`-p 127.0.0.1:8765:8765`). The token and the Host check apply either way."""
    return "0.0.0.0" if os.environ.get(CONTAINER_ENV) == "1" else HOST  # noqa: S104


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((listen_host(), port))
    sock.listen(128)
    return sock


def _hold_catalog(home: Path) -> CatalogLock:
    """The lock `serve` keeps for its whole life. At start it first tries for the exclusive lock: when nobody else has
    the catalog open, it finishes an interrupted restore, removes leftovers of interrupted imports, and only then
    downgrades to shared (without a gap)."""
    from .maintenance import remove_orphans
    from .restore_journal import recover

    try:
        hold = CatalogLock(home, exclusive=True)
    except CatalogInUse:
        try:
            shared = CatalogLock(home)  # others have it open: no cleanup now
            recover(home)  # but an interrupted restore is still finished or undone before anything opens catalog.db
            return shared
        except CatalogInUse:
            raise SystemExit("error: the catalog is being restored; try again when it is done") from None
    try:
        recover(home)
        init_db(home)
        with Catalog(home, lock=False) as cat:
            n = len(remove_orphans(cat))
        if n:
            print(f"removed {n} leftover file(s) of interrupted imports", file=sys.stderr, flush=True)
    except Exception as e:  # housekeeping must never stop the server from starting
        print(f"warning: startup cleanup skipped: {e}", file=sys.stderr, flush=True)
    hold.downgrade()
    return hold


def serve(home: Path, port: int, open_browser: bool) -> None:  # noqa: PLR0913
    ensure_home(home)
    hold = _hold_catalog(home)  # held for the life of the server, even while idle, so `restore` refuses
    init_db(home)
    token = load_or_create_token(home)
    sock = _bind(port)
    actual_port = sock.getsockname()[1]
    url = f"http://{HOST}:{actual_port}/#token={token}"  # a fragment is never sent to any server
    # First line of output is the URL; tests and scripts rely on it.
    print(f"resink-catalog {__version__} listening on {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    config = uvicorn.Config(create_app(token, home), log_level="warning")
    try:
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        hold.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resink-catalog")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="start the local catalog on 127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    p.add_argument("--home", type=Path, default=None, help="data directory")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    m = sub.add_parser("mcp", help="MCP server on stdio, for Claude Code, Codex and other MCP clients")
    m.add_argument("--home", type=Path, default=None, help="data directory")
    k = sub.add_parser("agent-kit", help="install skill files for Claude Code and Codex")
    ks = k.add_subparsers(dest="kit_command", required=True)
    ki = ks.add_parser("install", help="copy the skills to where the tools look for them")
    ki.add_argument(
        "--claude", nargs="?", const="", metavar="DIR", help="install for Claude Code (default ~/.claude/skills)"
    )
    ki.add_argument("--codex", nargs="?", const="", metavar="DIR", help="for Codex (default ~/.codex/prompts)")
    ks.add_parser("config", help="print the MCP configuration for both tools")
    b = sub.add_parser("backup", help="copy the catalog and every file it references to a new directory, consistently")
    b.add_argument("dest", type=Path, help="a new or empty directory outside the catalog's own")
    b.add_argument("--home", type=Path, default=None, help="data directory")
    r = sub.add_parser("restore", help="restore a backup made by `backup` (to the location it was made from)")
    r.add_argument("source", type=Path, help="the backup directory")
    r.add_argument("--home", type=Path, default=None, help="data directory")
    r.add_argument("--replace", action="store_true", help="move an existing catalog aside instead of refusing")
    c = sub.add_parser("cleanup", help="remove files left behind by interrupted imports (older than --min-age-hours)")
    c.add_argument("--home", type=Path, default=None, help="data directory")
    c.add_argument("--min-age-hours", type=float, default=24, help="leave younger files alone (default 24)")
    c.add_argument("--dry-run", action="store_true", help="only list the files")
    li = sub.add_parser("login", help="sign this computer in to Resink cloud (opens the browser)")
    li.add_argument("--accounts-url", help="accounts service (default $RESINK_ACCOUNTS_URL, else production)")
    li.add_argument("--push-url", help="push receiver (default $RESINK_PUSH_URL, else production)")
    li.add_argument("--workspace", help="target cloud workspace id (default: the one chosen in the browser)")
    li.add_argument("--no-browser", action="store_true", help="only print the sign-in URL")
    lo = sub.add_parser("logout", help="revoke this computer's sign-in and delete the local tokens")
    lo.add_argument("--local", action="store_true", help="only delete the local tokens (when accounts is unreachable)")
    pu = sub.add_parser("push", help="send a table's new snapshots to Resink cloud")
    pu.add_argument("table", help="local table, 'name' or 'namespace.name'")
    pu.add_argument("--home", type=Path, default=None, help="data directory")
    pu.add_argument("--workspace", help="target cloud workspace id (default: chosen at sign-in)")
    pu.add_argument("--to", metavar="NAMESPACE.NAME", help="cloud table name (default: the local one)")
    pu.add_argument("--yes", action="store_true", help="accept extra storage cost without asking")
    for name, help_ in (
        ("import", "import a file, or apply the proposal for a folder added with add-folder"),
        ("add-folder", "find data files in a folder and propose tables; imports nothing"),
        ("scan", "re-check all sources and folders for changes"),
        ("preview", "show the inferred column types and sample rows of a file, before importing"),
    ):
        q = sub.add_parser(name, help=help_)
        q.add_argument("--home", type=Path, default=None, help="data directory")
        q.add_argument("--json", action="store_true", help="print machine-readable output")
        if name == "import":
            q.add_argument("path", type=Path, help="a CSV or Parquet file, or a folder added with add-folder")
            q.add_argument("--table", help="target table, 'name' or 'namespace.name' (single file only)")
            q.add_argument(
                "--type",
                action="append",
                default=[],
                metavar="COLUMN=TYPE",
                help="override an inferred column type (one file): string, long, double, boolean, date, timestamp",
            )
        elif name in ("add-folder", "preview"):
            q.add_argument("path", type=Path)
        else:
            q.add_argument("--apply", action="store_true", help="re-import the sources that changed")
    return parser


def _print(args, payload, lines) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in lines:
            print(line)


def _parse_types(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        col, sep, typ = pair.rpartition("=")
        if not sep or not col:
            raise ImportErrorForUser(f"--type expects COLUMN=TYPE, got {pair!r}")
        out[col] = typ
    return out


def _preview_lines(view: dict) -> list[str]:
    lines = [
        f"{c['name']:24} {c['type']:10}" + (f" (inferred {c['inferred']})" if c["type"] != c["inferred"] else "")
        for c in view["columns"]
    ]
    if view["error"]:
        lines.append(f"error: {view['error']}")
    lines += [str(r) for r in view["rows"][:5]]
    return lines


def run_command(args) -> int:
    cat = Catalog(args.home or default_home())
    try:
        if args.command == "add-folder":
            found = cat.add_folder(args.path)
            _print(
                args,
                found,
                [f"{s['status']:9} {s['mode']:9} {s['target_table']:30} {s['path']}" for s in found]
                + [f"{len(found)} files proposed. Nothing imported yet: run `resink-catalog import {args.path}`."],
            )
        elif args.command == "import":
            types = _parse_types(args.type)
            if args.path.is_dir():
                if types:
                    raise ImportErrorForUser("--type applies to one file, not to a folder")
                results = cat.import_folder(args.path)
            else:
                results = [cat.import_file(args.path, args.table, types or None)]
            _print(
                args,
                [r.as_dict() for r in results],
                [f"{r.outcome:9} {r.table:30} {r.path}" + (f"  ({r.error})" if r.error else "") for r in results],
            )
            return 1 if any(r.outcome == "error" for r in results) else 0
        elif args.command == "preview":
            p = args.path.expanduser().resolve()
            view = cat.preview(cat.register_file(p))
            _print(args, view, _preview_lines(view))
            return 1 if view["error"] else 0
        elif args.command == "scan":
            report = cat.scan(apply=args.apply)
            _print(args, report, [f"{k}: {len(v)}" for k, v in report.items()])
    except ImportErrorForUser as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


CLOUD_TRANSPORT = None  # tests put a fake cloud (an httpx transport) here; None is the network


def _confirm_cost(admission: dict) -> bool:
    cost = admission.get("estimated_extra_monthly_cost") or {}
    print(
        f"This push exceeds your organization's included storage by {admission.get('over_included_bytes', '?')} bytes; "
        f"estimated extra cost {cost.get('amount', '?')} {cost.get('currency', '')} per month.",
        file=sys.stderr,
    )
    if not sys.stdin.isatty():
        return False
    return input("Continue? [y/N] ").strip().lower() in ("y", "yes")


def cloud_command(args) -> int:
    from .cloud.http import CloudError
    from .cloud.signin import Device, login, logout

    def err(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        if args.command == "login":
            d = login(
                args.accounts_url,
                args.push_url,
                workspace=args.workspace,
                transport=CLOUD_TRANSPORT,
                open_browser=None if args.no_browser else webbrowser.open,
                out=err,
            )
            names = {w["id"]: w["name"] for w in d.info["workspaces"]}
            ws = d.info["target_workspace_id"]
            print(f"signed in as device {d.info['device_id']}; pushes go to workspace {names.get(ws, ws) or 'none'}")
            return 0
        device = Device.load(transport=CLOUD_TRANSPORT)
        if args.command == "logout":
            logout(device, local_only=args.local)
            print("signed out: " + ("local tokens deleted" if args.local else "device revoked, local tokens deleted"))
            return 0
        from .cloud.push import Pusher

        with Catalog(args.home or default_home()) as cat:
            pusher = Pusher(
                cat, device, transport=CLOUD_TRANSPORT, out=err, confirm=(lambda a: True) if args.yes else _confirm_cost
            )
            try:
                rep = pusher.push(args.table, workspace=args.workspace, target=args.to)
            finally:
                pusher.close()
        where = f"workspace {rep.workspace_id}, cloud table {rep.cloud_name}"
        if rep.snapshots == 0:
            print(f"{rep.table} is up to date in {where}; nothing sent")
        else:
            print(
                f"pushed {rep.table} to {where}: {rep.snapshots} snapshot(s), "
                f"{rep.files_uploaded} file(s), {rep.bytes_uploaded} bytes uploaded"
            )
        return 0
    except CloudError as e:
        for line in e.lines():
            err(line)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("login", "logout", "push"):
        return cloud_command(args)
    if args.command == "agent-kit":
        from . import agent_kit

        if args.kit_command == "config":
            print(agent_kit.MCP_CONFIG, end="")
            return 0
        if args.claude is None and args.codex is None:
            print("error: choose --claude and/or --codex", file=sys.stderr)
            return 2
        written = []
        if args.claude is not None:
            written += agent_kit.install_claude(Path(args.claude) if args.claude else None)
        if args.codex is not None:
            written += agent_kit.install_codex(Path(args.codex) if args.codex else None)
        for p in written:
            print(p)
        return 0
    if args.command == "mcp":
        from .mcp_server import build_server

        cat = Catalog(args.home or default_home())
        build_server(lambda: cat).run("stdio")  # stdout carries the protocol: nothing else may print to it
        return 0
    if args.command in ("backup", "restore"):
        from .backup import BackupError, backup, restore

        try:
            if args.command == "backup":
                m = backup(args.home or default_home(), args.dest)
                print(f"backed up {len(m['files'])} files and catalog.db to {args.dest}")
            else:
                m = restore(args.source, args.home or default_home(), replace=args.replace)
                print(f"restored {len(m['files'])} files and catalog.db to {args.home or default_home()}")
        except BackupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    if args.command == "cleanup":
        from .maintenance import CleanupRefused, cleanup

        try:
            files = cleanup(args.home or default_home(), args.min_age_hours * 3600, args.dry_run)
        except CleanupRefused as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for f in files:
            print(f"{'would remove' if args.dry_run else 'removed'} {f}")
        print(f"{len(files)} file(s)")
        return 0
    if args.command == "serve":
        serve(args.home or default_home(), args.port, not args.no_browser)
        return 0
    return run_command(args)


if __name__ == "__main__":
    sys.exit(main())
