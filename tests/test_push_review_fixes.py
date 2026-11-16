"""Codex review round 1 on L5a: one test (or more) per finding."""

import socket
import threading
import time

import httpx2
import pytest
from fake_cloud import ACCOUNTS, PUSH, Killed, browser, problem
from push_helpers import Sleeps, make_pusher, rows

from resink_catalog.cloud import push as push_mod
from resink_catalog.cloud import signin as signin_mod
from resink_catalog.cloud.http import Api, CloudError
from resink_catalog.cloud.push import file_path, push_path
from resink_catalog.cloud.signin import CallbackListener, Device, LoginError, login

T = "shop.orders"


def _expire(device):
    device.info["access_expires_at"] = 0
    signin_mod._write_private(device.path, device.info)


# ---------------------------------------------------------------- 1. token exchanges are never resent


def _lose_token_response(cloud, kind="read"):
    """Forwards the request to the fake (so accounts processes it), then loses the answer."""
    inner = cloud.transport()
    seen = {"token": 0}

    def handle(req):
        if req.url.path == "/v1/device/token":
            seen["token"] += 1
            if kind == "connect" and seen["token"] == 1:
                raise httpx2.ConnectError("nothing was sent")
            resp = inner.handle_request(req)
            if kind == "read":
                raise httpx2.ReadError("connection reset before the answer arrived")
            return resp
        return inner.handle_request(req)

    return httpx2.MockTransport(handle), seen


def test_1_a_lost_refresh_answer_is_not_resent_so_the_device_is_not_revoked(cloud, device, store):
    device.transport, seen = _lose_token_response(cloud)
    _expire(device)
    with pytest.raises(CloudError, match="lost the connection"):
        device.access_token()
    assert seen["token"] == 1  # one attempt only: a resent refresh token would count as reuse
    assert not cloud.devices["dev_1"].revoked
    assert store.get("refresh_token") is not None  # nothing deleted on a transport error


def test_1_a_503_from_the_token_endpoint_is_not_retried(cloud, device):
    """(Not through the contract-checking fake: the YAML declares no 503 for /v1/device/token.)"""
    calls = []

    def busy(req):
        calls.append(req.url.path)
        return problem(503, "unavailable", "Unavailable", "busy")

    device.transport = httpx2.MockTransport(busy)
    _expire(device)
    with pytest.raises(CloudError):
        device.access_token()
    assert calls == ["/v1/device/token"]


def test_1_a_connect_error_before_sending_is_retried(cloud, device, sleeps):
    device.transport, seen = _lose_token_response(cloud, kind="connect")
    _expire(device)
    assert device.access_token()
    assert seen["token"] == 2 and sleeps == [1.0]


def test_1_a_lost_code_exchange_answer_is_not_resent(cloud, store, tmp_path):
    transport, seen = _lose_token_response(cloud)
    with pytest.raises(CloudError, match="lost the connection"):
        login(ACCOUNTS, PUSH, store=store, transport=transport, open_browser=browser(cloud), out=lambda s: None)
    assert seen["token"] == 1


# ---------------------------------------------------------------- 2. concurrent refreshes are serialised


def test_2_two_processes_refreshing_at_once_rotate_once_and_share_the_result(cloud, device, store):
    _expire(device)
    inner = cloud.transport()

    def slow(req):  # make the two refreshes overlap
        if req.url.path == "/v1/device/token":
            time.sleep(0.3)
        return inner.handle_request(req)

    a = Device.load(store=store, transport=httpx2.MockTransport(slow), path=device.path)
    b = Device.load(store=store, transport=httpx2.MockTransport(slow), path=device.path)
    tokens, errors = [], []

    def run(d):
        try:
            tokens.append(d.access_token())
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(d,)) for d in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert len(cloud.calls("POST", "/v1/device/token")) == 2  # sign-in + exactly one rotation
    assert tokens[0] == tokens[1] == store.get("access_token")
    assert not cloud.devices["dev_1"].revoked


def test_2_a_stale_process_adopts_the_new_token_instead_of_rotating_or_deleting(cloud, device, store):
    other = Device.load(store=store, transport=cloud.transport(), path=device.path)
    stale = store.get("access_token")
    _expire(other)
    other.access_token()  # another process rotates
    saved = (store.get("access_token"), store.get("refresh_token"))
    device.refresh(stale)  # this process saw `stale` refused by the receiver
    assert (store.get("access_token"), store.get("refresh_token")) == saved
    assert len(cloud.calls("POST", "/v1/device/token")) == 2 and not cloud.devices["dev_1"].revoked


# ---------------------------------------------------------------- 3. https only, except loopback


@pytest.mark.parametrize("url", ["http://accounts.example", "ftp://x", "http://10.0.0.1:8080", "accounts.example"])
def test_3_login_refuses_a_non_https_address_before_the_browser_or_any_credential(url, store):
    opened = []
    with pytest.raises(CloudError, match="must be https"):
        login(url, "https://push.example", store=store, open_browser=opened.append, out=lambda s: None)
    with pytest.raises(CloudError, match="must be https"):
        login("https://accounts.example", url, store=store, open_browser=opened.append, out=lambda s: None)
    assert opened == [] and store.get("refresh_token") is None


def test_3_loopback_http_is_allowed_and_env_overrides_are_checked_too(cloud, device, store, monkeypatch):
    for url in ("http://127.0.0.1:8080", "http://localhost:1", "http://[::1]:2"):
        Api(url).close()
    monkeypatch.setenv("RESINK_PUSH_URL", "http://push.example")
    with pytest.raises(CloudError, match="must be https"):
        Device.load(store=store, path=device.path)


# ---------------------------------------------------------------- 4. server values in URL paths


@pytest.mark.parametrize("bad", ["../../v1/device/revoke", "a/b", "..", "", "x?y=1", "a%2Fb", 7, None])
def test_4_an_invalid_push_id_is_refused(bad):
    with pytest.raises(CloudError, match="invalid push_id"):
        push_path(bad)


def test_4_file_ids_must_be_the_contracts_format():
    assert file_path("push_1", "a" * 64) == f"/push/v0/pushes/push_1/files/{'a' * 64}"
    for bad in ("A" * 64, "a" * 63, "../" + "a" * 61):
        with pytest.raises(CloudError, match="invalid file_id"):
            file_path("push_1", bad)


def test_4_a_session_with_a_traversal_push_id_is_never_used_in_a_path(cloud, device, catalog):
    catalog.write_table(T, rows(20))
    session = {
        "push_id": "../../../v1/device/revoke",
        "state": "open",
        "table": {"namespace": ["shop"], "name": "orders"},
        "expires_at": "2030-01-01T00:00:00Z",
        "admission": {
            "outcome": "accepted",
            "incoming_bytes": 1,
            "org": {"ceiling_bytes": 1, "committed_bytes": 0, "staged_bytes": 0, "reserved_bytes": 0},
            "workspace": {"ceiling_bytes": 1, "committed_bytes": 0, "staged_bytes": 0, "reserved_bytes": 0},
        },
        "files": [],
    }
    cloud.faults.append(("POST", r"/pushes$", httpx2.Response(201, json=session)))
    with pytest.raises(CloudError, match="invalid push_id"):
        make_pusher(catalog, device, cloud).push(T)
    assert cloud.calls("POST", "revoke") == [] and not cloud.devices["dev_1"].revoked


# ---------------------------------------------------------------- 5. expired local history


def test_5_a_first_push_after_snapshot_expiry_is_refused_before_any_session(cloud, device, catalog):
    for i in range(3):
        catalog.write_table(T, rows(20, i * 20), mode="append")
    t = catalog.iceberg.load_table(("shop", "orders"))
    oldest, root = t.snapshots()[0].snapshot_id, t.snapshots()[1].snapshot_id
    t.maintenance.expire_snapshots().by_id(oldest).commit()
    with pytest.raises(CloudError) as e:
        make_pusher(catalog, device, cloud).push(T)
    assert e.value.problem["title"] == "Incomplete local history" and str(root) in e.value.problem["detail"]
    assert cloud.calls("POST", "/pushes") == [] and cloud.calls("PATCH") == []


def test_5_expiry_of_snapshots_the_cloud_already_has_is_fine(cloud, device, catalog):
    for i in range(2):
        catalog.write_table(T, rows(20, i * 20), mode="append")
    make_pusher(catalog, device, cloud).push(T)
    catalog.write_table(T, rows(20, 40), mode="append")
    t = catalog.iceberg.load_table(("shop", "orders"))
    t.maintenance.expire_snapshots().by_id(t.snapshots()[0].snapshot_id).commit()
    assert make_pusher(catalog, device, cloud).push(T).snapshots == 1


# ---------------------------------------------------------------- 6. the idempotency chain keeps advancing


def test_6_many_ended_sessions_do_not_block_recovery(cloud, device, catalog, monkeypatch):
    monkeypatch.setattr(push_mod, "SESSION_ATTEMPTS", 3)
    catalog.write_table(T, rows(20))
    cloud.end_new_sessions = 4  # more than one run may walk
    with pytest.raises(CloudError, match="run push again"):
        make_pusher(catalog, device, cloud).push(T)
    assert len(cloud.calls("POST", r"/pushes$")) == 3
    cloud.log.clear()
    rep = make_pusher(catalog, device, cloud).push(T)  # starts after the last ended session, not from the start
    assert rep.snapshots == 1 and rep.sessions == ["push_5"]
    assert len(cloud.calls("POST", r"/pushes$")) == 2  # push_4 (ended), then push_5


def test_6_a_rerun_still_finds_the_same_live_session(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    cloud.end_new_sessions = 1
    cloud.kill_after_chunks = 2
    with pytest.raises(Killed):
        make_pusher(catalog, device, cloud).push(T)
    rep = make_pusher(catalog, device, cloud).push(T)
    assert rep.sessions == ["push_2"] and len(cloud.sessions) == 2


# ---------------------------------------------------------------- 7. upload offsets are checked


def _status(fid, received, size, state):
    return httpx2.Response(200, json={"file_id": fid, "size_bytes": size, "received_bytes": received, "state": state})


@pytest.mark.parametrize(
    ("received", "state", "match"),
    [(10**9, "partial", "bytes received"), (-1, "partial", "bytes received"), (1, "complete", "as 'complete'")],
)
def test_7_impossible_offsets_from_the_receiver_are_refused(cloud, device, catalog, received, state, match):
    catalog.write_table(T, rows(300))
    real = cloud.patch

    def lying(s, fid, req):
        real(s, fid, req)
        return _status(fid, received, s.files[fid]["size"], state)

    cloud.patch = lying
    with pytest.raises(CloudError, match=match):
        make_pusher(catalog, device, cloud).push(T)


def test_7_an_upload_without_progress_stops(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    cloud.patch = lambda s, fid, req: problem(409, "offset_mismatch", "Offset mismatch", "", received_bytes=0)
    with pytest.raises(CloudError, match="no progress"):
        make_pusher(catalog, device, cloud).push(T)
    assert len(cloud.calls("PATCH")) == push_mod.MAX_STALLS


# ---------------------------------------------------------------- 8. the loopback listener's deadline


def test_8_a_peer_that_trickles_bytes_cannot_hold_the_login_past_its_deadline():
    listener = CallbackListener()
    stop = threading.Event()

    def trickle():
        with socket.create_connection(("127.0.0.1", listener.port)) as c:
            for ch in b"GET /callback?state=x HTTP/1.1\r\n":
                if stop.is_set():
                    return
                try:
                    c.sendall(bytes([ch]))
                except OSError:
                    return
                time.sleep(0.2)

    threading.Thread(target=trickle, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(LoginError, match="timed out"):
        listener.wait("s" * 32, timeout=1.0)
    stop.set()
    assert time.monotonic() - start < 2.5


def test_8_one_slow_connection_is_cut_and_the_real_callback_still_arrives(cloud, store, monkeypatch):
    monkeypatch.setattr(signin_mod, "CONNECTION_SECONDS", 0.5)
    real_browser = browser(cloud)

    def slow_then_real(url):
        port = int(url.split("127.0.0.1%3A")[1].split("%2F")[0])
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"GET /cal")  # never finishes the request
        real_browser(url)

    d = login(ACCOUNTS, PUSH, store=store, transport=cloud.transport(), open_browser=slow_then_real, out=lambda s: None)
    assert d.info["device_id"] == "dev_1"


# ---------------------------------------------------------------- 9. the retry budget counts request time


def test_9_the_budget_includes_time_spent_in_requests_and_caps_each_timeout():
    t = Sleeps()
    timeouts = []

    def slow_503(req):
        timeouts.append(req.extensions["timeout"]["read"])
        t.offset += 280  # each request takes 280 s
        return problem(503, "unavailable", "Unavailable", "down", retry_after=0)

    with Api("https://push.example", transport=httpx2.MockTransport(slow_503), sleep=t.append, clock=t.clock) as api:
        r = api.request("GET", "/push/v0/capabilities")
    assert r.status_code == 503
    assert len(timeouts) == 2  # 280 s, a 1 s backoff, then only 19 s were left: the second request gets that
    assert timeouts[0] == 60 and timeouts[1] < 20
    assert list(t) == [1.0]  # no backoff once the deadline has passed


def test_9_a_long_retry_after_is_not_waited_past_the_deadline():
    t = Sleeps()
    with Api(
        "https://push.example",
        transport=httpx2.MockTransport(lambda r: problem(429, "rate_limited", "Slow down", "", retry_after=400)),
        sleep=t.append,
        clock=t.clock,
    ) as api:
        assert api.request("GET", "/push/v0/capabilities").status_code == 429
    assert list(t) == []
