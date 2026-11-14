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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    if args.command == "serve":
        serve(args.home or default_home(), args.port, not args.no_browser)
        return 0
    return run_command(args)


if __name__ == "__main__":
    sys.exit(main())
