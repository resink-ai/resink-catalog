import os
import sqlite3
import stat

from resink_catalog.home import ensure_home, init_db, load_or_create_token


def test_token_generated_once_and_persisted(tmp_path):
    ensure_home(tmp_path)
    t1 = load_or_create_token(tmp_path)
    t2 = load_or_create_token(tmp_path)
    assert t1 == t2
    assert len(t1) >= 32


def test_token_file_private(tmp_path):
    load_or_create_token(tmp_path)
    if os.name == "posix":
        mode = stat.S_IMODE((tmp_path / "token").stat().st_mode)
        assert mode == 0o600


def test_init_db_idempotent(tmp_path):
    init_db(tmp_path)
    init_db(tmp_path)
    conn = sqlite3.connect(tmp_path / "catalog.db")
    rows = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchall()
    conn.close()
    assert rows == [("5",)]


def test_a_bind_mounted_directory_we_cannot_chmod_is_accepted_if_it_is_private(tmp_path, monkeypatch):
    """Docker: /data is owned by the host user, so chmod raises PermissionError (the failure TASK-9 reproduced)."""
    import os
    from pathlib import Path

    import pytest

    from resink_catalog.home import ensure_home

    d = tmp_path / "data"
    d.mkdir()

    def deny(self, mode):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "chmod", deny)
    if os.name == "posix":
        os.chmod(d, 0o700)
        assert ensure_home(d) == d  # already private: start
        os.chmod(d, 0o755)
        with pytest.raises(PermissionError, match="accessible to others"):
            ensure_home(d)  # not ours and open to others: refuse, with advice
