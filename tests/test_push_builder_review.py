"""builder-a's end-to-end review of L5a and the contract update (8ca93b4): table_exists up front, local_workspace_id,
cloud_table_id from table-state, session_not_open on abort, and the cloud table name."""

import json
import sqlite3

import httpx2
import pytest
from fake_cloud import CloudTable, Killed
from push_helpers import make_pusher, rows

from resink_catalog.cli import main
from resink_catalog.cloud import push as push_mod
from resink_catalog.cloud.http import CloudError
from resink_catalog.cloud.push import cloud_table_name, push_state

T = "shop.orders"


def test_a_name_collision_is_refused_before_the_table_is_scanned_or_hashed(cloud, device, catalog, monkeypatch):
    catalog.write_table(T, rows(50))
    cloud.tables[("ws_main", "shop__orders")] = CloudTable("cid", "00000000-0000-0000-0000-000000000000", False)

    def must_not_run(*a, **kw):
        raise AssertionError("the local table was scanned")

    for name in ("new_snapshots", "unsupported_features", "snapshot_delta", "Hasher"):
        monkeypatch.setattr(push_mod, name, must_not_run)
    with pytest.raises(CloudError) as e:
        make_pusher(catalog, device, cloud).push(T)
    assert e.value.code == "table_exists" and e.value.problem["resolution"][0]["action"] == "use_new_table_name"
    assert cloud.calls("POST", "/pushes") == []


def test_local_workspace_id_is_kept_in_device_json(device):
    assert json.loads(device.path.read_text())["local_workspace_id"] == "ws_local"


def test_a_no_op_push_takes_the_cloud_table_id_from_table_state(cloud, device, catalog):
    catalog.write_table(T, rows(50))
    make_pusher(catalog, device, cloud).push(T)
    with sqlite3.connect(catalog.db_path) as c:
        c.execute("DELETE FROM push_state")
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.snapshots == 0
    cloud_id = cloud.table("ws_main", ["shop"], "orders").id
    assert rep.cloud_table_id == cloud_id and push_state(catalog, T)[0]["cloud_table_id"] == cloud_id


def test_session_not_open_on_abort_counts_as_ended_and_the_push_goes_on(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    cloud.kill_after_chunks = 1
    with pytest.raises(Killed):
        make_pusher(catalog, device, cloud).push(T)
    catalog.write_table(T, rows(10, 300), mode="append")  # another body: the old session is in the way
    inner = cloud.transport()

    def ends_meanwhile(req):
        if req.method == "DELETE":  # between the client's GET and its DELETE, the old session fails
            cloud.sessions["push_1"].state = "failed"
        return inner.handle_request(req)

    from push_helpers import Sleeps

    from resink_catalog.cloud.push import Pusher

    t = Sleeps()
    rep = Pusher(
        catalog,
        device,
        transport=httpx2.MockTransport(ends_meanwhile),
        out=lambda s: None,
        sleep=t.append,
        clock=t.clock,
    ).push(T)
    assert ("DELETE", "/push/v0/pushes/push_1", 409) in cloud.log
    assert rep.sessions == ["push_2"] and rep.snapshots == 2


def test_the_cloud_table_name_follows_the_receiver():
    assert cloud_table_name("shop", "orders") == "shop__orders"
    assert cloud_table_name("main", "orders") == "orders"
    assert cloud_table_name("default", "orders") == "orders"


def test_push_prints_the_cloud_table_name(cloud, catalog, capsys, monkeypatch, store, home):
    from fake_cloud import ACCOUNTS, PUSH, browser

    catalog.write_table(T, rows(50))
    catalog.close()
    monkeypatch.setattr("resink_catalog.cli.CLOUD_TRANSPORT", cloud.transport())
    monkeypatch.setattr("resink_catalog.cloud.signin._open_store", lambda: store)
    monkeypatch.setattr("resink_catalog.cli.webbrowser.open", browser(cloud))
    monkeypatch.setenv("RESINK_ACCOUNTS_URL", ACCOUNTS)
    monkeypatch.setenv("RESINK_PUSH_URL", PUSH)
    assert main(["login"]) == 0
    capsys.readouterr()
    assert main(["push", T, "--home", str(home)]) == 0
    assert "workspace ws_main, cloud table shop__orders" in capsys.readouterr().out
    assert main(["push", T, "--home", str(home)]) == 0
    assert "up to date in workspace ws_main, cloud table shop__orders" in capsys.readouterr().out


def test_main_x_and_default_x_share_a_cloud_name_and_the_second_is_refused_clearly(cloud, device, catalog):
    catalog.write_table("main.x", rows(20))
    catalog.write_table("default.x", rows(20, 20))
    first = make_pusher(catalog, device, cloud).push("main.x")
    assert first.cloud_name == "x"
    cloud.log.clear()
    with pytest.raises(CloudError) as e:
        make_pusher(catalog, device, cloud).push("default.x")
    assert e.value.code == "history_diverged" and "the cloud table x was pushed from another local table" in str(
        e.value.problem["detail"]
    )
    assert e.value.problem["resolution"][0]["action"] == "use_new_table_name"
    assert cloud.calls("POST", "/pushes") == [] and cloud.calls("PATCH") == []
    assert make_pusher(catalog, device, cloud).push("default.x", target="default.x2").cloud_name == "x2"
