"""The push client against the fake receiver (every exchange checked against the OpenAPI): P5, P6, P7, P13, retries,
problem documents, admission confirmation, batching, push_state as a cache."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pandas as pd
import pyarrow as pa
import pytest
from fake_cloud import ACCOUNTS, PUSH, FakeCloud, Killed, browser, problem
from push_helpers import make_pusher, rows
from pyiceberg.manifest import DataFile, DataFileContent, FileFormat
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import IdentityTransform
from pyiceberg.typedef import Record
from pyiceberg.types import LongType, NestedField

from resink_catalog import Catalog
from resink_catalog.cli import main
from resink_catalog.cloud.http import CloudError
from resink_catalog.cloud.push import Pusher, idempotency_key, push_state, unsupported_features
from resink_catalog.cloud.signin import login
from resink_catalog.credentials import FileStore

T = "shop.orders"


def added_by(catalog, snap, name=T) -> list:
    io = catalog.iceberg.load_table(tuple(name.split("."))).io
    return [
        e.data_file
        for m in snap.manifests(io)
        if m.added_snapshot_id == snap.snapshot_id
        for e in m.fetch_manifest_entry(io)
    ]


def test_a_first_push_sends_every_snapshot_and_commits(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    catalog.write_table(T, rows(100, 300), mode="append")
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.snapshots == 2 and rep.files_uploaded == 2
    view = cloud.table_view("ws_main", "shop", "orders")
    local = catalog.iceberg.load_table(("shop", "orders"))
    assert [s[0] for s in view["snapshots"]] == [s.snapshot_id for s in local.snapshots()]
    assert rep.bytes_uploaded == cloud.patch_bytes == sum(len(b) for b in cloud.blobs["ws_main"].values())
    assert all(n <= cloud.caps["chunk_bytes_max"] for _, _, n in cloud.patched)  # bounded chunks
    assert len(cloud.patched) > 2  # files span several chunks
    st = push_state(catalog, T)
    assert st[0]["last_pushed_snapshot_id"] == local.current_snapshot().snapshot_id
    assert st[0]["cloud_table_id"] == rep.cloud_table_id and st[0]["workspace_id"] == "ws_main"


def test_p5_a_repeated_push_sends_zero_bytes_and_creates_no_snapshot(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    make_pusher(catalog, device, cloud).push(T)
    before = (cloud.patch_bytes, len(cloud.table(cloud.workspaces[0]["id"], ["shop"], "orders").snapshots))
    cloud.log.clear()
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.snapshots == 0 and rep.bytes_uploaded == 0
    assert (cloud.patch_bytes, len(cloud.table("ws_main", ["shop"], "orders").snapshots)) == before
    assert cloud.calls("PATCH") == [] and cloud.calls("POST", "/pushes") == []


def test_p5_the_cloud_is_the_truth_not_push_state(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    make_pusher(catalog, device, cloud).push(T)
    import sqlite3

    with sqlite3.connect(catalog.db_path) as c:
        c.execute("DELETE FROM push_state")
    cloud.log.clear()
    assert make_pusher(catalog, device, cloud).push(T).snapshots == 0
    assert cloud.calls("PATCH") == []
    assert push_state(catalog, T)[0]["last_pushed_snapshot_id"] is not None  # the cache is rebuilt from the cloud


def test_p6_only_the_new_snapshots_added_files_are_sent(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    make_pusher(catalog, device, cloud).push(T)
    catalog.write_table(T, rows(100, 300), mode="append")
    new = catalog.iceberg.load_table(("shop", "orders")).current_snapshot()
    added = added_by(catalog, new)
    cloud.patched.clear()
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.snapshots == 1 and rep.files_uploaded == 1
    sent = {fid for fid, _, _ in cloud.patched}
    assert len(sent) == 1 and sum(n for _, _, n in cloud.patched) == added[0].file_size_in_bytes
    session = cloud.sessions[rep.sessions[0]]
    assert [s["snapshot_id"] for s in session.body["snapshots"]] == [new.snapshot_id]
    assert session.body["base_snapshot_id"] == new.parent_snapshot_id


def test_an_overwrite_sends_removed_file_ids_and_the_new_schema(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    make_pusher(catalog, device, cloud).push(T)
    catalog.write_table(T, rows(10).assign(extra=1.5))  # PyIceberg: a delete snapshot, then an append, new column
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.snapshots == 2
    view = cloud.table_view("ws_main", "shop", "orders")
    first_file = view["snapshots"][0][1][0]
    assert view["snapshots"][1][2] == [first_file]  # the delete snapshot removed the first file
    assert view["live"] == view["snapshots"][2][1]
    assert '"extra"' in view["snapshots"][2][3]  # the schema at that snapshot, field ids kept


def test_the_target_name_and_workspace_can_be_overridden(cloud, device, catalog):
    catalog.write_table(T, rows(50))
    rep = make_pusher(catalog, device, cloud).push(T, workspace="ws_two", target="sales.orders_copy")
    assert rep.target == "sales.orders_copy" and cloud.table("ws_two", ["sales"], "orders_copy") is not None


# ---------------------------------------------------------------- P7: killed mid-upload, rerun


def _table_with_files(catalog):
    catalog.write_table(T, rows(400))
    catalog.write_table(T, rows(400, 400), mode="append")


def test_p7_a_killed_push_resumes_with_only_the_missing_bytes_and_equals_an_uninterrupted_push(
    cloud, device, catalog, tmp_path
):
    _table_with_files(catalog)
    cloud.kill_after_chunks = 3
    with pytest.raises(Killed):
        make_pusher(catalog, device, cloud).push(T)
    stored_before = cloud.patch_bytes
    assert 0 < stored_before
    assert cloud.table("ws_main", ["shop"], "orders") is None  # nothing committed
    cloud.patched.clear()
    rep = make_pusher(catalog, device, cloud).push(T)  # the rerun
    total = sum(len(b) for b in cloud.blobs["ws_main"].values())
    assert rep.bytes_uploaded == total - stored_before  # only the missing bytes
    assert sum(n for _, _, n in cloud.patched) == total - stored_before
    assert len(rep.sessions) == 1 and rep.sessions[0] == "push_1"  # same session, found by its idempotency key
    assert cloud.sessions["push_1"].state == "committed"
    # the same table pushed without interruption to another fake cloud ends identical
    other = FakeCloud()
    d2 = login(
        ACCOUNTS,
        PUSH,
        store=FileStore(tmp_path / "c2"),
        transport=other.transport(),
        open_browser=browser(other),
        out=lambda s: None,
        path=tmp_path / "c2" / "device.json",
    )
    make_pusher(catalog, d2, other).push(T)
    assert other.table_view("ws_main", "shop", "orders") == cloud.table_view("ws_main", "shop", "orders")


def test_p7_a_lost_connection_mid_upload_resumes_from_the_servers_offset(cloud, device, catalog):
    """The server stored a chunk but the answer was lost: the retry gets 409 offset_mismatch and continues."""
    _table_with_files(catalog)
    inner = cloud.transport()
    state = {"n": 0}

    def flaky(req):
        resp = inner.handle_request(req)
        if req.method == "PATCH":
            state["n"] += 1
            if state["n"] == 2:
                raise httpx2.ReadError("connection reset after the server stored the chunk")
        return resp

    sleeps = []
    p = Pusher(catalog, device, transport=httpx2.MockTransport(flaky), out=lambda s: None, sleep=sleeps.append)
    rep = p.push(T)
    assert cloud.sessions[rep.sessions[0]].state == "committed" and sleeps == [1.0, 1.0]  # one backoff, one poll
    assert [c[2] for c in cloud.calls("PATCH")].count(409) == 1  # offset_mismatch, then on from the server's offset
    assert cloud.patch_bytes == sum(len(b) for b in cloud.blobs["ws_main"].values())  # no byte stored twice


@pytest.mark.skipif(sys.platform == "win32", reason="the plaintext token file is refused on Windows")
def test_p7_a_real_process_killed_mid_upload_then_rerun(tmp_path, catalog, home):
    """`resink-catalog push` in a subprocess against the fake served on 127.0.0.1, killed (SIGKILL) mid-upload."""
    cloud = FakeCloud(chunk_min=256, chunk_max=256)
    cloud.patch_delay = 0.005
    server, url = cloud.serve()
    try:
        _table_with_files(catalog)
        catalog.close()
        env = {
            **os.environ,
            "RESINK_CATALOG_CREDENTIALS_DIR": str(tmp_path / "cred"),
            "RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS": "1",
            "RESINK_ACCOUNTS_URL": url,
            "RESINK_PUSH_URL": url,
        }
        # sign in in-process into the directory the subprocess reads; its URLs come from the environment
        login(
            ACCOUNTS,
            PUSH,
            store=FileStore(tmp_path / "cred"),
            transport=cloud.transport(),
            open_browser=browser(cloud),
            out=lambda s: None,
            path=tmp_path / "cred" / "device.json",
        )
        cmd = [sys.executable, "-m", "resink_catalog.cli", "push", T, "--home", str(home)]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 60
        while cloud.patch_bytes < 3000 and time.time() < deadline and proc.poll() is None:
            time.sleep(0.005)
        proc.kill()
        proc.wait(10)
        stored = cloud.patch_bytes
        assert 0 < stored and cloud.table("ws_main", ["shop"], "orders") is None, "killed too late"
        cloud.patched.clear()
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        total = sum(len(b) for b in cloud.blobs["ws_main"].values())
        assert sum(n for _, _, n in cloud.patched) == total - stored
        assert cloud.table("ws_main", ["shop"], "orders") is not None
    finally:
        server.shutdown()


# ---------------------------------------------------------------- P13: unsupported tables, before any upload


def _assert_refused_up_front(cloud, device, catalog, name, feature):
    with pytest.raises(CloudError) as e:
        make_pusher(catalog, device, cloud).push(name)
    assert e.value.code == "unsupported_table_feature" and feature in e.value.problem["feature"]
    assert cloud.calls("POST", "/pushes") == [] and cloud.calls("PATCH") == []


def test_p13_a_partitioned_table_is_refused_before_any_upload(cloud, device, catalog):
    catalog._ensure_namespace("shop")
    t = catalog.iceberg.create_table(
        ("shop", "part"),
        schema=Schema(NestedField(1, "a", LongType(), required=False)),
        partition_spec=PartitionSpec(PartitionField(1, 1000, IdentityTransform(), "a")),
    )
    t.append(pa.table({"a": pa.array([1, 2], pa.int64())}))
    _assert_refused_up_front(cloud, device, catalog, "shop.part", "partitioned")


def _fast_append(catalog, name, data_file):
    t = catalog.iceberg.load_table(tuple(name.split(".")))
    data_file.spec_id = 0
    with t.transaction() as tx, tx.update_snapshot().fast_append() as fa:
        fa.append_data_file(data_file)


def test_p13_non_parquet_data_files_are_refused_before_any_upload(cloud, device, catalog, tmp_path):
    catalog.write_table(T, rows(10))
    orc = tmp_path / "x.orc"
    orc.write_bytes(b"ORC....")
    _fast_append(
        catalog,
        T,
        DataFile.from_args(
            content=DataFileContent.DATA,
            file_path=str(orc),
            file_format=FileFormat.ORC,
            partition=Record(),
            record_count=1,
            file_size_in_bytes=7,
        ),
    )
    _assert_refused_up_front(cloud, device, catalog, T, "non-Parquet data files")


def test_p13_delete_files_are_refused_before_any_upload(cloud, device, catalog, tmp_path):
    """A position delete file entered through PyIceberg's low-level API (PyIceberg itself writes copy-on-write; the
    real case, DuckDB's DELETE, is in test_p13_a_duckdb_delete_is_refused_before_any_upload)."""
    catalog.write_table(T, rows(10))
    pos = tmp_path / "d.parquet"
    pos.write_bytes(b"PAR1")
    _fast_append(
        catalog,
        T,
        DataFile.from_args(
            content=DataFileContent.POSITION_DELETES,
            file_path=str(pos),
            file_format=FileFormat.PARQUET,
            partition=Record(),
            record_count=1,
            file_size_in_bytes=4,
        ),
    )
    _assert_refused_up_front(cloud, device, catalog, T, "delete files")


def test_p13_format_version_3_is_refused(catalog):
    catalog.write_table(T, rows(10))
    t = catalog.iceberg.load_table(("shop", "orders"))
    v3 = Table(
        t.name(), t.metadata.model_copy(update={"format_version": 3}), t.metadata_location, t.io, catalog.iceberg
    )
    assert unsupported_features(v3, v3.snapshots()) == ["format version 3"]  # PyIceberg 0.12 cannot write v3 tables


def test_p13_a_duckdb_delete_is_refused_before_any_upload(cloud, device, live_server):
    from test_duckdb_rest import _attach, _connect

    con = _connect()
    _attach(con, live_server, "oauth2")
    cat = Catalog(live_server.home)
    cat.write_table(T, pd.DataFrame({"id": [1, 2, 3]}))
    con.execute("DELETE FROM ice.shop.orders WHERE id = 2")
    _assert_refused_up_front(cloud, device, cat, T, "delete files")


# ---------------------------------------------------------------- retries and problem documents


def test_429_and_503_are_retried_with_backoff_honouring_retry_after(cloud, device, catalog, sleeps):
    catalog.write_table(T, rows(50))
    cloud.faults += [
        ("POST", r"/pushes$", problem(503, "unavailable", "Unavailable", "feed stale", retry_after=7)),
        ("PATCH", r"/files/", problem(429, "rate_limited", "Rate limited", "slow down", retry_after=0)),
    ]
    rep = make_pusher(catalog, device, cloud, sleeps).push(T)
    assert rep.snapshots == 1 and cloud.faults == []
    assert 7 in sleeps and 1.0 in sleeps  # Retry-After when longer, else the exponential step


def test_retries_stop_after_five_minutes_in_total(cloud, device, catalog, sleeps):
    catalog.write_table(T, rows(50))
    cloud.faults += [("GET", r"/capabilities", problem(503, "unavailable", "Unavailable", "down", retry_after=30))] * 50
    with pytest.raises(CloudError) as e:
        make_pusher(catalog, device, cloud, sleeps).push(T)
    assert e.value.status == 503 and sum(sleeps) <= 300 and sum(sleeps) > 240


def test_other_4xx_are_never_retried_and_the_problem_is_printed_verbatim(cloud, device, catalog, capsys, monkeypatch):
    catalog.write_table(T, rows(50))
    catalog.close()
    paused = problem(
        423,
        "workspace_paused",
        "Workspace paused",
        "Workspace ws_main is paused for billing.",
        pause_reason="billing",
        resolution=[
            {"action": "buy_credits", "url": "https://console.example/billing", "detail": "Add credits."},
            {"action": "contact_admin"},
        ],
    )
    cloud.faults.append(("POST", r"/pushes$", paused))
    monkeypatch.setattr("resink_catalog.cli.CLOUD_TRANSPORT", cloud.transport())
    monkeypatch.setattr("resink_catalog.cloud.signin._open_store", lambda: device.store)
    assert main(["push", T, "--home", str(catalog.home)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "error: Workspace paused (workspace_paused)",
        "Workspace ws_main is paused for billing.",
        "resolution: buy_credits Add credits. https://console.example/billing",
    ]
    assert len(cloud.calls("POST", r"/pushes$")) == 1


# ---------------------------------------------------------------- sessions, admission, batching, history


def test_an_over_included_push_needs_confirmation(cloud, device, catalog):
    catalog.write_table(T, rows(50))
    cloud.over_included = True
    with pytest.raises(CloudError, match="cancelled"):
        make_pusher(catalog, device, cloud).push(T)
    assert cloud.calls("PATCH") == [] and cloud.sessions["push_1"].state == "aborted"
    rep = make_pusher(
        catalog, device, cloud, confirm=lambda a: a["estimated_extra_monthly_cost"]["amount"] == "0.02"
    ).push(T)
    assert rep.snapshots == 1  # the commit carried confirm_over_included (else 409 confirmation_required)


def test_snapshots_are_split_into_sessions_by_the_advertised_limit(device, catalog, store, tmp_path):
    cloud = FakeCloud(max_snapshots=1)
    d = login(ACCOUNTS, PUSH, store=store, transport=cloud.transport(), open_browser=browser(cloud), out=lambda s: None)
    for i in range(3):
        catalog.write_table(T, rows(20, i * 20), mode="append")
    rep = make_pusher(catalog, d, cloud).push(T)
    assert rep.snapshots == 3 and len(rep.sessions) == 3


def test_history_diverged_is_refused_locally(cloud, device, catalog):
    catalog.write_table(T, rows(50))
    make_pusher(catalog, device, cloud).push(T)
    cloud.table("ws_main", ["shop"], "orders").snapshots[-1]["source"] = 12345  # not in the local history
    with pytest.raises(CloudError) as e:
        make_pusher(catalog, device, cloud).push(T)
    assert e.value.code == "history_diverged" and e.value.problem["resolution"][0]["action"] == "use_new_table_name"


def test_an_unfinished_session_of_an_older_local_state_is_aborted_and_replaced(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    cloud.kill_after_chunks = 1
    with pytest.raises(Killed):
        make_pusher(catalog, device, cloud).push(T)
    catalog.write_table(T, rows(10, 300), mode="append")  # the local table moved on: another body, another key
    rep = make_pusher(catalog, device, cloud).push(T)
    assert cloud.sessions["push_1"].state == "aborted" and rep.sessions == ["push_2"] and rep.snapshots == 2


def test_an_ended_session_moves_the_idempotency_chain_on(cloud, device, catalog):
    catalog.write_table(T, rows(50))
    cloud.kill_after_chunks = 1
    with pytest.raises(Killed):
        make_pusher(catalog, device, cloud).push(T)
    cloud.sessions["push_1"].state = "expired"
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.sessions == ["push_2"]
    assert idempotency_key("d", {"a": 1}) == idempotency_key("d", {"a": 1}) != idempotency_key("d", {"a": 1}, "push_1")


# ---------------------------------------------------------------- the CLI end to end


def test_cli_login_push_logout(cloud, catalog, capsys, monkeypatch, store, home):
    catalog.write_table(T, rows(100))
    catalog.close()
    monkeypatch.setattr("resink_catalog.cli.CLOUD_TRANSPORT", cloud.transport())
    monkeypatch.setattr("resink_catalog.cloud.signin._open_store", lambda: store)
    monkeypatch.setattr("resink_catalog.cli.webbrowser.open", browser(cloud))
    monkeypatch.setenv("RESINK_ACCOUNTS_URL", ACCOUNTS)
    monkeypatch.setenv("RESINK_PUSH_URL", PUSH)
    assert main(["login"]) == 0
    assert "signed in as device dev_1; pushes go to workspace Main" in capsys.readouterr().out
    assert main(["push", "orders", "--home", str(home)]) == 1  # main.orders does not exist
    assert "no such table: main.orders" in capsys.readouterr().err
    assert main(["push", T, "--home", str(home)]) == 0
    assert "1 snapshot(s)" in capsys.readouterr().out
    assert main(["push", T, "--home", str(home)]) == 0
    assert "up to date" in capsys.readouterr().out
    assert main(["logout"]) == 0
    assert cloud.devices["dev_1"].revoked and store.get("refresh_token") is None
    assert main(["push", T, "--home", str(home)]) == 1
    assert "not signed in" in capsys.readouterr().err
    assert cloud.unchecked == []


def test_cloud_urls_come_from_flags_env_or_sign_in(monkeypatch):
    from resink_catalog.cloud.signin import cloud_urls

    monkeypatch.delenv("RESINK_ACCOUNTS_URL", raising=False)
    monkeypatch.delenv("RESINK_PUSH_URL", raising=False)
    assert cloud_urls(saved={"accounts_url": "http://a", "push_url": "http://p"}) == ("http://a", "http://p")
    monkeypatch.setenv("RESINK_PUSH_URL", "http://env-p/")
    assert cloud_urls(saved={"push_url": "http://p"})[1] == "http://env-p"
    assert cloud_urls(push_url="http://flag")[1] == "http://flag"


def test_no_production_host_in_the_tests():
    here = Path(__file__).parent
    files = ["test_push_client.py", "test_push_login.py", "fake_cloud.py", "conftest.py", "push_helpers.py"]
    text = "".join((here / f).read_text() for f in files)
    assert re.findall(r"https?://[\w.-]*resink\.ai", text) == []
