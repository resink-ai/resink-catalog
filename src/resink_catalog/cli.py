"""Command line: `resink-catalog serve`."""

from __future__ import annotations

import argparse
import socket
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .home import default_home, ensure_home, init_db, load_or_create_token

DEFAULT_PORT = 8765
HOST = "127.0.0.1"  # fixed on purpose: the catalog never listens on another interface


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    sock.listen(128)
    return sock


def serve(home: Path, port: int, open_browser: bool) -> None:
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
    config = uvicorn.Config(create_app(token), log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resink-catalog")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="start the local catalog on 127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    p.add_argument("--home", type=Path, default=None, help="data directory")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        serve(args.home or default_home(), args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
