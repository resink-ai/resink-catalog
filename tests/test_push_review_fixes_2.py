"""Codex review round 2 on L5a: one test (or more) per remaining gap."""

import json
import socket
import threading
import time

import httpx2
import pytest
from fake_cloud import ACCOUNTS, PUSH, browser, problem
from push_helpers import make_pusher, rows

from resink_catalog.cloud import push as push_mod
from resink_catalog.cloud.http import Api, CloudError, RequestMaybeSent
from resink_catalog.cloud.signin import IN_FLIGHT, Device, login, logout

T = "shop.orders"


def _expire(device):
    device.info["access_expires_at"] = 0
    device.path.write_text(json.dumps(device.info))


def _token_calls(cloud):
    return len(cloud.calls("POST", "/v1/device/token"))


# ---------------------------------------------------------------- 1. an ambiguous refresh is never repeated


def _losing(cloud, mode):
    inner = cloud.transport()

    def handle(req):
        if req.url.path == "/v1/device/token":
            if mode == "connect":
                raise httpx2.ConnectError("nothing was sent")
            if mode == "503":
                return problem(503, "unavailable", "Unavailable", "busy")
            inner.handle_request(req)  # accounts rotates the token...
            raise httpx2.ReadError("...but the answer is lost")
        return inner.handle_request(req)

    return httpx2.MockTransport(handle)


@pytest.mark.parametrize("mode", ["lost", "503"])
def test_1_after_an_unclear_refresh_the_next_process_asks_for_a_new_login(cloud, device, store, mode):
    device.transport = _losing(cloud, mode)
    _expire(device)
    with pytest.raises(CloudError, match="sign-in state uncertain; run `resink-catalog login`"):
        device.access_token()
    assert IN_FLIGHT in json.loads(device.path.read_text())  # persisted, so another process sees it too
    before = _token_calls(cloud)
    later = Device.load(store=store, transport=cloud.transport(), path=device.path)  # the next invocation
    with pytest.raises(CloudError, match="sign-in state uncertain"):
        later.access_token()
    assert _token_calls(cloud) == before  # the refresh token was not sent again
    assert not cloud.devices["dev_1"].revoked
    # signing in again clears it
    d = login(
        ACCOUNTS,
        PUSH,
        store=store,
        transport=cloud.transport(),
        open_browser=browser(cloud),
        out=lambda s: None,
        path=device.path,
    )
    assert IN_FLIGHT not in json.loads(device.path.read_text())
    _expire(d)
    assert d.access_token()


def test_1_the_marker_is_written_before_the_request_is_sent(cloud, device):
    seen = {}
    inner = cloud.transport()

    def handle(req):
        if req.url.path == "/v1/device/token":
            seen["marker"] = json.loads(device.path.read_text()).get(IN_FLIGHT)
        return inner.handle_request(req)

    device.transport = httpx2.MockTransport(handle)
    _expire(device)
    device.access_token()
    assert seen["marker"]  # on disk while the request was in flight
    assert IN_FLIGHT not in json.loads(device.path.read_text())  # cleared with the new tokens


def test_1_a_refresh_that_could_not_connect_leaves_the_token_usable(cloud, device, sleeps):
    device.transport = _losing(cloud, "connect")
    _expire(device)
    with pytest.raises(CloudError, match="cannot reach"):
        device.access_token()
    assert IN_FLIGHT not in json.loads(device.path.read_text())
    device.transport = cloud.transport()
    assert device.access_token()


# ---------------------------------------------------------------- 2. logout never deletes a concurrent sign-in


def test_2_a_login_that_finishes_during_logout_keeps_its_tokens(cloud, device, store):
    inner = cloud.transport()
    revoking = threading.Event()

    def slow_revoke(req):
        if req.url.path == "/v1/device/revoke":
            revoking.set()
            time.sleep(0.5)
        return inner.handle_request(req)

    device.transport = httpx2.MockTransport(slow_revoke)
    out = threading.Thread(target=logout, args=(device,))
    out.start()
    assert revoking.wait(5)
    # another process signs in while the revoke is on the wire; its save waits for the lock
    new = login(
        ACCOUNTS,
        PUSH,
        store=store,
        transport=cloud.transport(),
        open_browser=browser(cloud),
        out=lambda s: None,
        path=device.path,
    )
    out.join(10)
    assert cloud.devices["dev_1"].revoked and not cloud.devices["dev_2"].revoked
    assert store.get("refresh_token") is not None and device.path.exists()  # the new sign-in survived
    assert json.loads(device.path.read_text())["device_id"] == new.info["device_id"] == "dev_2"


def test_2_logout_works_on_what_is_stored_now_not_on_a_stale_copy(cloud, device, store):
    other = login(
        ACCOUNTS,
        PUSH,
        store=store,
        transport=cloud.transport(),
        open_browser=browser(cloud),
        out=lambda s: None,
        path=device.path,
    )  # a newer sign-in replaced `device`'s tokens
    logout(device)
    assert cloud.devices[other.info["device_id"]].revoked
    assert store.get("refresh_token") is None and not device.path.exists()


# ---------------------------------------------------------------- 3. oscillating offsets


def test_3_offsets_moving_back_and_forth_hit_the_no_progress_limit(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    calls = {"n": 0}

    def oscillate(s, fid, req):
        calls["n"] += 1
        size = s.files[fid]["size"]
        if calls["n"] % 2:
            return httpx2.Response(
                200, json={"file_id": fid, "size_bytes": size, "received_bytes": 256, "state": "partial"}
            )
        return problem(409, "offset_mismatch", "Offset mismatch", "", received_bytes=0)

    cloud.patch = oscillate
    with pytest.raises(CloudError, match="no progress"):
        make_pusher(catalog, device, cloud).push(T)
    assert calls["n"] == 1 + push_mod.MAX_STALLS  # one real step, then MAX_STALLS answers without new progress


def test_3_one_checksum_restart_is_allowed_on_top(cloud, device, catalog):
    catalog.write_table(T, rows(300))
    cloud.faults.append(("PATCH", "/files/", problem(422, "checksum_mismatch", "Checksum mismatch", "restart")))
    assert make_pusher(catalog, device, cloud).push(T).snapshots == 1


# ---------------------------------------------------------------- 4. an absolute deadline over the whole request


def test_4_a_response_that_trickles_bytes_cannot_outlive_the_budget():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def trickle():
        conn, _ = srv.accept()
        conn.recv(65536)
        try:
            for b in b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n" + b"x" * 1000000:
                if stop.is_set():
                    return
                conn.sendall(bytes([b]))
                time.sleep(0.05)  # always within the per-read timeout
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=trickle, daemon=True).start()
    start = time.monotonic()
    try:
        with Api(f"http://127.0.0.1:{srv.getsockname()[1]}", budget=1.0) as api, pytest.raises(RequestMaybeSent):
            api.request("GET", "/push/v0/capabilities")
        assert time.monotonic() - start < 2.5
    finally:
        stop.set()
        srv.close()


def test_4_a_hanging_transport_is_abandoned_at_the_deadline():
    def hang(req):
        time.sleep(3)
        return httpx2.Response(200, json={})

    start = time.monotonic()
    with Api("https://push.example", transport=httpx2.MockTransport(hang), budget=0.5) as api:
        with pytest.raises(RequestMaybeSent, match="time limit"):
            api.request("GET", "/push/v0/capabilities")
    assert time.monotonic() - start < 1.5


# ---------------------------------------------------------------- round 3: the abandoned request is cancelled for real


def _http_workers():
    return [t for t in threading.enumerate() if t.name == "resink-catalog-http" and t.is_alive()]


def test_r3_at_the_deadline_the_connection_is_cut_and_the_worker_exits():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    closed = threading.Event()
    stop = threading.Event()

    def trickle():  # never stops on its own: only the client closing the connection ends it
        conn, _ = srv.accept()
        conn.recv(65536)
        try:
            for b in b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n" + b"x" * 1000000:
                if stop.is_set():
                    return
                conn.sendall(bytes([b]))
                time.sleep(1.5)  # each byte within the per-read timeout (2 s), so only a cut ends the read early
        except OSError:
            closed.set()  # the client's side is gone
        finally:
            conn.close()

    server = threading.Thread(target=trickle, daemon=True)
    server.start()
    try:
        api = Api(f"http://127.0.0.1:{srv.getsockname()[1]}", budget=2.0)
        first = api.client
        with pytest.raises(RequestMaybeSent):
            api.request("GET", "/push/v0/capabilities")
        deadline = time.monotonic() + 0.3  # far less than the second until the next byte would wake the read
        while _http_workers() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _http_workers() == []  # the blocked read failed and the worker returned
        assert closed.wait(3)  # the server saw the connection closed while it was still sending
        assert api.client is not first  # later calls get a fresh client
        api.close()
    finally:
        stop.set()
        srv.close()


def test_r3_later_calls_on_the_same_api_work_after_an_abandoned_one():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if len(calls) == 1:
            time.sleep(1.5)  # the fake cannot be cut; it just finishes late
        return httpx2.Response(200, json={"ok": True})

    with Api("https://push.example", transport=httpx2.MockTransport(handler), budget=0.5) as api:
        with pytest.raises(RequestMaybeSent):
            api.request("GET", "/a")
        assert api.request("GET", "/b").json() == {"ok": True}


# ---------------------------------------------------------------- round 4: cancellation during connect and TLS


class _SlowConnect:
    """httpcore's backend with a connect that takes `delay` seconds, whatever its timeout says."""

    def __init__(self, inner, delay):
        self._inner, self.delay = inner, delay

    def connect_tcp(self, *a, **kw):
        time.sleep(self.delay)
        return self._inner.connect_tcp(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _listening():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    return srv


def _wait_workers_gone(seconds):
    deadline = time.monotonic() + seconds
    while _http_workers() and time.monotonic() < deadline:
        time.sleep(0.02)
    return _http_workers() == []


def test_r4_a_connection_that_completes_after_the_deadline_is_closed_and_sends_nothing():
    srv = _listening()
    got = {}

    def serve():
        conn, _ = srv.accept()
        conn.settimeout(5)
        try:
            got["data"] = conn.recv(65536)  # b"" when the client closes without sending
        except OSError as e:
            got["error"] = e
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        api = Api(f"http://127.0.0.1:{srv.getsockname()[1]}", budget=0.5)
        api._backend._inner = _SlowConnect(api._backend._inner, 1.0)
        with pytest.raises(RequestMaybeSent):
            api.request("GET", "/push/v0/capabilities")
        assert _wait_workers_gone(1.5)  # the connect returned after the deadline, was closed, and the worker ended
        deadline = time.monotonic() + 3
        while "data" not in got and "error" not in got and time.monotonic() < deadline:
            time.sleep(0.02)
        assert got.get("data") == b""  # the request was never sent on the cancelled client
        api.close()
    finally:
        srv.close()


def test_r4_a_cut_during_the_tls_handshake_shuts_the_connection_down():
    """The server accepts and reads the ClientHello but never answers. The TCP connect takes 1 s, so the handshake's
    own timeout (the budget at the start of the attempt) would end 1 s after the deadline: only a cut that reaches
    the socket during start_tls ends it at the deadline."""
    srv = _listening()
    closed = threading.Event()

    def stall():
        conn, _ = srv.accept()
        conn.settimeout(10)
        try:
            conn.recv(65536)  # the ClientHello
            if conn.recv(65536) == b"":  # then nothing, until the client goes away
                closed.set()
        except OSError:
            closed.set()
        finally:
            conn.close()

    threading.Thread(target=stall, daemon=True).start()
    try:
        api = Api(f"https://127.0.0.1:{srv.getsockname()[1]}", budget=2.0)
        api._backend._inner = _SlowConnect(api._backend._inner, 1.0)
        with pytest.raises(RequestMaybeSent):
            api.request("GET", "/push/v0/capabilities")
        raised = time.monotonic()
        assert _wait_workers_gone(0.4)
        assert closed.wait(1.0) and time.monotonic() - raised < 0.6
        api.close()
    finally:
        srv.close()


def test_r4_a_cancelled_backend_refuses_new_connections():
    from resink_catalog.cloud.http import _CuttingBackend

    b = _CuttingBackend()
    b.cut_all()
    import httpcore2

    with pytest.raises(httpcore2.ConnectError, match="cancelled"):
        b.connect_tcp("127.0.0.1", 9)
