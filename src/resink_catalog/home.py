"""Data directory: where catalog.db and the access token live."""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

HOME_ENV = "RESINK_CATALOG_HOME"
SCHEMA_VERSION = 4


# Our own tables (design section 5). Iceberg metadata lives in PyIceberg's SQL catalog tables in the same file.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS data_roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    last_scan TEXT
);
CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER REFERENCES data_roots(id),
    path TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL,
    content_hash TEXT,
    target_table TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'overwrite',
    last_import TEXT,
    status TEXT NOT NULL,
    error TEXT,
    rows INTEGER,
    column_types TEXT
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('user', 'ai')),
    created_at TEXT NOT NULL,
    UNIQUE (table_name, tag)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('user', 'ai')),
    created_at TEXT NOT NULL
);
"""


def default_home() -> Path:
    env = os.environ.get(HOME_ENV)
    return Path(env).expanduser() if env else Path.home() / ".resink-catalog"


def ensure_home(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            home.chmod(0o700)
        except PermissionError:
            # A directory that is not ours (a Docker bind mount owned by the host user) cannot be chmod-ed. That is
            # fine when nobody else can enter it; otherwise refuse, since the token and catalog live here.
            if home.stat().st_mode & 0o077:
                raise PermissionError(
                    f"{home} is not owned by this user and is accessible to others; "
                    f"run `chmod 700 {home}` as its owner"
                ) from None
    return home


def load_or_create_token(home: Path) -> str:
    """Return the access token, generating it on first start (file mode 0600)."""
    path = home / "token"
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def init_db(home: Path) -> Path:
    """Create catalog.db with a version table. Later milestones add their own tables."""
    db_path = home / "catalog.db"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executescript(SCHEMA_SQL)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(data_sources)")}
        if "column_types" not in columns:  # a catalog created by L1 (schema version 2)
            conn.execute("ALTER TABLE data_sources ADD COLUMN column_types TEXT")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return db_path
