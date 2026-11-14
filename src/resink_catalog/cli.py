"""Command line: `resink-catalog serve`."""

from __future__ import annotations

import argparse
import json
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

DEFAULT_PORT = 8765
HOST = "127.0.0.1"  # fixed on purpose: the catalog never listens on another interface


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(128)
    return sock


def serve(home: Path, port: int, open_browser: bool) -> None:  # noqa: PLR0913
    ensure_home(home)
    init_db(home)
    token = load_or_create_token(home)
    sock = _bind(port)
    actual_port = sock.getsockname()[1]
    url = f"http://{HOST}:{actual_port}/?token={token}"
    # First line of output is the URL; tests and scripts rely on it.
    print(f"resink-catalog {__version__} listening on {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    config = uvicorn.Config(create_app(token, home), log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resink-catalog")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="start the local catalog on 127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    p.add_argument("--home", type=Path, default=None, help="data directory")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    for name, help_ in (
        ("import", "import a file, or apply the proposal for a folder added with add-folder"),
        ("add-folder", "find data files in a folder and propose tables; imports nothing"),
        ("scan", "re-check all sources and folders for changes"),
    ):
        q = sub.add_parser(name, help=help_)
        q.add_argument("--home", type=Path, default=None, help="data directory")
        q.add_argument("--json", action="store_true", help="print machine-readable output")
        if name == "import":
            q.add_argument("path", type=Path, help="a CSV or Parquet file, or a folder added with add-folder")
            q.add_argument("--table", help="target table, 'name' or 'namespace.name' (single file only)")
        elif name == "add-folder":
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
            if args.path.is_dir():
                results = cat.import_folder(args.path)
            else:
                results = [cat.import_file(args.path, args.table)]
            _print(
                args,
                [r.as_dict() for r in results],
                [f"{r.outcome:9} {r.table:30} {r.path}" + (f"  ({r.error})" if r.error else "") for r in results],
            )
            return 1 if any(r.outcome == "error" for r in results) else 0
        elif args.command == "scan":
            report = cat.scan(apply=args.apply)
            _print(args, report, [f"{k}: {len(v)}" for k, v in report.items()])
    except ImportErrorForUser as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        serve(args.home or default_home(), args.port, not args.no_browser)
        return 0
    return run_command(args)


if __name__ == "__main__":
    sys.exit(main())
