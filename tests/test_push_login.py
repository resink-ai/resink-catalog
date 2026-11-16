"""P2 and token handling: `login` (PKCE S256, loopback on 127.0.0.1, state, one callback, 5-minute limit), refresh
rotation, logout, and where the tokens live (never catalog.db). Against the fake accounts in fake_cloud.py."""

import json
import os
import stat
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fake_cloud import ACCOUNTS, PUSH, browser
from push_helpers import Sleeps, make_pusher, rows

from resink_catalog.cloud.http import Api, CloudError
from resink_catalog.cloud.signin import CallbackListener, Device, LoginError, login, logout, pkce


def _login(cloud, store, **kw):
    return login(ACCOUNTS, PUSH, store=store, transport=cloud.transport(), out=lambda s: None, **kw)


def test_p2_login_happy_path_leaves_a_device_token(cloud, store, cred_dir):
    seen = {}
    d = _login(cloud, store, open_browser=browser(cloud, record=seen))
    q = {k: v[0] for k, v in parse_qs(urlsplit(seen["authorize_url"]).query).items()}
    assert q["client_id"] == "resink-catalog" and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and 43 <= len(q["code_challenge"]) <= 128
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith("/callback")
    assert 16 <= len(q["state"]) <= 128
    seen["thread"].join(5)
    assert seen["callback_status"] == 200
    assert store.get("access_token") in cloud.access and store.get("refresh_token") in cloud.refresh
    info = json.loads((cred_dir / "device.json").read_text())
    assert info["device_id"] == d.info["device_id"] == "dev_1"
    assert info["target_workspace_id"] == "ws_main"  # the workspace marked default (chosen in the browser)
    assert (info["accounts_url"], info["push_url"]) == (ACCOUNTS, PUSH)
    assert "access_token" not in json.dumps(info) and store.get("access_token") not in json.dumps(info)


def test_p2_the_challenge_is_s256_of_the_verifier():
    import base64
    import hashlib

    v, c = pkce()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_p2_a_wrong_state_is_rejected_and_nothing_is_exchanged(cloud, store):
    with pytest.raises(LoginError, match="state does not match"):
        _login(cloud, store, open_browser=browser(cloud, tamper_state=True))
    assert cloud.calls("POST", "/v1/device/token") == []
    assert store.get("access_token") is None and store.get("refresh_token") is None


def test_p2_a_non_loopback_redirect_is_rejected_by_accounts(cloud):
    _, challenge = pkce()
    cloud.check_requests = False  # these requests break the contract's redirect_uri pattern on purpose
    with httpx2.Client(transport=cloud.transport()) as c:
        for uri in (
            "http://evil.example/callback",
            "http://localhost:5000/callback",
            "https://127.0.0.1:5000/callback",
        ):
            params = {
                "client_id": "resink-catalog",
                "redirect_uri": uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s" * 20,
            }
            r = c.get(f"{ACCOUNTS}/v1/device/authorize", params=params, follow_redirects=False)
            assert r.status_code == 400, uri


def test_p2_the_listener_binds_loopback_only():
    listener = CallbackListener()
    try:
        assert listener.server.server_address[0] == "127.0.0.1"
        assert listener.redirect_uri == f"http://127.0.0.1:{listener.port}/callback"
    finally:
        listener.close()


def test_p2_a_reused_code_is_rejected(cloud, store, tmp_path):
    seen = {}
    _login(cloud, store, open_browser=browser(cloud, record=seen))
    # the same code presented again (by an attacker who saw the redirect, or a replayed callback)
    with pytest.raises(LoginError, match="code already used"):
        _login(cloud, store, open_browser=browser(cloud, replay_code=seen["code"]), path=tmp_path / "other.json")
    assert len(cloud.devices) == 1


def test_p2_exactly_one_callback_is_accepted(cloud, store):
    seen = {}
    _login(cloud, store, open_browser=browser(cloud, record=seen))
    seen["thread"].join(5)
    with pytest.raises(httpx2.HTTPError):  # the listener is closed after the first callback
        httpx2.get(seen["callback"], timeout=2)


def test_login_gives_up_after_the_timeout(cloud, store):
    with pytest.raises(LoginError, match="timed out"):
        _login(cloud, store, open_browser=lambda url: None, timeout=0.3)


def test_a_denied_sign_in_is_reported(cloud, store):
    with pytest.raises(LoginError, match="access_denied"):
        _login(cloud, store, open_browser=browser(cloud, error="access_denied"))


def test_login_workspace_flag_overrides_the_default(cloud, store):
    d = _login(cloud, store, open_browser=browser(cloud), workspace="ws_two")
    assert d.default_workspace() == "ws_two"


# ---------------------------------------------------------------- refresh, logout


def test_refresh_rotates_and_a_reused_refresh_token_means_log_in_again(cloud, device, store):
    first_refresh, first_access = store.get("refresh_token"), store.get("access_token")
    cloud.expire_access_tokens()
    device.info["access_expires_at"] = 0
    token = device.access_token()  # expired locally: refreshes first
    assert token != first_access and store.get("refresh_token") != first_refresh
    assert cloud.refresh[first_refresh]["rotated"]
    # someone presents the rotated token again: accounts revokes the device
    with Api(ACCOUNTS, transport=cloud.transport()) as api:
        r = api.request(
            "POST",
            "/v1/device/token",
            json={"grant_type": "refresh_token", "client_id": "resink-catalog", "refresh_token": first_refresh},
        )
    assert r.status_code == 400 and "reused" in r.json()["error_description"]
    device.info["access_expires_at"] = 0
    with pytest.raises(CloudError, match="resink-catalog login` again"):
        device.access_token()
    assert store.get("refresh_token") is None and store.get("access_token") is None  # useless tokens are deleted
    assert not device.path.exists()


def test_a_401_from_the_receiver_refreshes_once_and_retries(cloud, device, catalog):
    catalog.write_table("shop.orders", rows(50))
    cloud.expire_access_tokens()  # the server says the token expired before the client thinks so
    rep = make_pusher(catalog, device, cloud).push("shop.orders")
    assert rep.snapshots == 1
    assert len(cloud.calls("POST", "/v1/device/token")) == 2  # sign-in, then one refresh


def test_logout_revokes_the_device_and_deletes_the_tokens(cloud, device, store):
    old_access = store.get("access_token")
    logout(device)
    assert cloud.devices["dev_1"].revoked
    assert store.get("access_token") is None and store.get("refresh_token") is None and not device.path.exists()
    with httpx2.Client(transport=cloud.transport()) as c:  # the receiver refuses the old token
        r = c.get(f"{PUSH}/push/v0/capabilities", headers={"Authorization": f"Bearer {old_access}"})
    assert r.status_code == 403
    with pytest.raises(CloudError, match="not signed in"):
        Device.load(store=store)


def test_logout_with_an_expired_access_token_refreshes_then_revokes(cloud, device):
    device.info["access_expires_at"] = 0
    logout(device)
    assert cloud.devices["dev_1"].revoked


def test_logout_keeps_the_tokens_when_accounts_cannot_be_reached(device, store):
    def down(req):
        raise httpx2.ConnectError("down")

    t = Sleeps()
    device.transport, device.sleep, device.clock = httpx2.MockTransport(down), t.append, t.clock
    device.info["access_expires_at"] = 1e12
    with pytest.raises(CloudError, match="cannot reach"):
        logout(device)
    assert store.get("refresh_token") is not None
    logout(device, local_only=True)
    assert store.get("refresh_token") is None


# ---------------------------------------------------------------- where the tokens live


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX; Windows uses the Credential Manager")
def test_the_token_file_is_0600_in_a_0700_directory(device, store, cred_dir):
    for p in (store.path, cred_dir / "device.json"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    assert stat.S_IMODE(cred_dir.stat().st_mode) == 0o700


def test_tokens_are_never_in_catalog_db(cloud, device, store, catalog, home):
    catalog.write_table("shop.orders", rows(50))
    make_pusher(catalog, device, cloud).push("shop.orders")
    secrets_ = [store.get("access_token"), store.get("refresh_token")]
    for p in home.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert not any(s.encode() in data for s in secrets_), p
    assert not any(home in p.parents for p in (store.path, device.path))
