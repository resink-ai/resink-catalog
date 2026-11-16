"""An in-process fake of the cloud the push client talks to: accounts' device sign-in (A8 behaviour) and the push
receiver (the OpenAPI contract). Every request the client sends and every response the fake gives is checked against
contracts/push-protocol-v0.openapi.yaml; a mismatch raises ContractViolation inside the client's call, failing the test.

Use `FakeCloud().transport()` as the client's httpx transport (no sockets), or `serve()` for a subprocess client.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from openapi_contract import check_request, check_response

ACCOUNTS = "https://accounts.test"
PUSH = "https://push.test"
LOOPBACK = re.compile(r"^http://127\.0\.0\.1:([0-9]{1,5})/callback$")  # accounts' own rule (A8, devices.py)


class Killed(BaseException):
    """Stands in for the client process dying: nothing in the client catches it."""


def _json(status: int, body, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=headers or {})


def problem(status: int, code: str, title: str, detail: str = "", **extra) -> httpx2.Response:
    body = {"type": "about:blank", "title": title, "status": status, "code": code, "detail": detail, **extra}
    headers = {"Content-Type": "application/problem+json"}
    if status in (429, 503):
        headers["Retry-After"] = str(extra.pop("retry_after", 1))
        body.pop("retry_after", None)
    return httpx2.Response(status, content=json.dumps(body).encode(), headers=headers)


def oauth_error(detail: str) -> httpx2.Response:
    """What accounts' /v1/device/token answers: an RFC 6749 error object (the contract's OAuthError)."""
    return httpx2.Response(400, json={"error": "invalid_grant", "error_description": detail})


@dataclass
class DeviceRec:
    id: str
    org_id: str
    revoked: bool = False


@dataclass
class Session:
    push_id: str
    device_id: str
    workspace_id: str
    table: dict
    body: dict
    files: dict  # file_id -> {"size", "received", "state", "buf"}
    admission: dict
    state: str = "open"
    polls_left: int = 1
    result: dict | None = None


@dataclass
class CloudTable:
    id: str
    source_table_uuid: str
    push_managed: bool = True
    snapshots: list = field(default_factory=list)  # [{"source", "cloud", "added", "removed", "schema"}]
    live: set = field(default_factory=set)


class FakeCloud:
    def __init__(self, *, chunk_min=256, chunk_max=1024, max_snapshots=100, expires_in=900, workspaces=None):
        self.lock = threading.RLock()
        self.workspaces = workspaces or [
            {"id": "ws_main", "name": "Main", "default": True},
            {"id": "ws_two", "name": "Two"},
        ]
        self.caps = {
            "protocol_versions": ["0"],
            "max_file_bytes": 1 << 30,
            "chunk_bytes_min": chunk_min,
            "chunk_bytes_max": chunk_max,
            "session_ttl_seconds": 86400,
            "staged_file_ttl_seconds": 86400,
            "max_snapshots_per_push": max_snapshots,
            "max_files_per_push": 10000,
        }
        self.expires_in = expires_in
        self.auth_requests: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.devices: dict[str, DeviceRec] = {}
        self.access: dict[str, tuple[str, float]] = {}  # token -> (device id, expiry)
        self.refresh: dict[str, dict] = {}  # token -> {"device", "rotated"}
        self.tables: dict[tuple, CloudTable] = {}
        self.blobs: dict[str, dict[str, bytes]] = {}  # workspace -> file_id -> bytes
        self.sessions: dict[str, Session] = {}
        self.idem: dict[tuple, tuple[str, str]] = {}  # (device, key) -> (digest, push_id)
        self.log: list[tuple[str, str, int]] = []  # (method, path, status)
        self.patch_bytes = 0
        self.patched: list[tuple[str, int, int]] = []  # (file_id, start, length) of accepted chunks
        self.faults: list[tuple[str, str, httpx2.Response]] = []  # (method, path regex, response), each used once
        self.kill_after_chunks: int | None = None
        self.over_included = False
        self.end_new_sessions = 0
        self.patch_delay = 0.0  # seconds per chunk, served mode only
        self.check_fault_responses = True  # off only where a test injects a response the contract forbids
        self.check_requests = True  # off only where a test sends a request the contract forbids on purpose

    # ------------------------------------------------------------------------------------------------ plumbing

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def handle(self, req: httpx2.Request) -> httpx2.Response:
        with self.lock:
            path = req.url.path
            for i, (method, pattern, resp) in enumerate(self.faults):
                if method == req.method and re.search(pattern, path):
                    del self.faults[i]
                    check_request(req)
                    if self.check_fault_responses:
                        check_response(req, resp)
                    self.log.append((req.method, path, resp.status_code))
                    return resp
            if self.check_requests:
                check_request(req)
            resp = self._route(req)
            check_response(req, resp)
            self.log.append((req.method, path, resp.status_code))
            return resp

    def calls(self, method: str, pattern: str = "") -> list[tuple[str, str, int]]:
        return [c for c in self.log if c[0] == method and re.search(pattern, c[1])]

    def _route(self, req: httpx2.Request) -> httpx2.Response:
        host, path, m = req.url.host, req.url.path, req.method
        if host == "accounts.test":
            if path == "/v1/device/authorize":
                return self.authorize(req)
            if path == "/v1/device/token":
                return self.token(json.loads(req.content))
            if path == "/v1/device/revoke":
                return self.revoke(req)
        device = self._device(req)
        if isinstance(device, httpx2.Response):
            return device
        if path == "/push/v0/capabilities":
            return _json(200, self.caps)
        if path == "/push/v0/table-state":
            return self.table_state(device, req)
        if path == "/push/v0/pushes" and m == "POST":
            return self.create(device, req)
        parts = path.split("/")
        s = self.sessions.get(parts[4])
        if s is None or s.device_id != device.id:
            return problem(404, "not_found", "Not found")
        if len(parts) == 5 and m == "GET":
            return _json(200, self._view(s))
        if len(parts) == 5 and m == "DELETE":  # the contract's abort semantics
            if s.state == "committing":
                return problem(409, "push_in_progress", "Push in progress", "the session is committing")
            if s.state in ("committed", "failed"):
                return problem(409, "session_not_open", "Session not open", f"the session is {s.state}")
            s.state = "aborted"  # open, or already aborted or expired: a no-op
            return httpx2.Response(204)
        if len(parts) == 7 and m == "PATCH":
            return self.patch(s, parts[6], req)
        if parts[-1] == "commit":
            return self.commit(s, req)
        return problem(404, "not_found", "Not found")

    # ------------------------------------------------------------------------------------------------ accounts

    def authorize(self, req: httpx2.Request) -> httpx2.Response:
        q = dict(req.url.params)
        if not LOOPBACK.match(q.get("redirect_uri", "")):
            return problem(
                400, "invalid_request", "invalid request", "redirect_uri must be http://127.0.0.1:<port>/callback"
            )
        rid = secrets.token_hex(8)
        self.auth_requests[rid] = q
        # the fake user signs in and confirms at once; accounts then sends the browser to the loopback
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"request": q, "used": False}
        loc = f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}"
        return httpx2.Response(302, headers={"Location": loc})

    def _issue(self, device: DeviceRec) -> httpx2.Response:
        access = f"at_{secrets.token_hex(16)}"
        refresh = f"rdt_{secrets.token_hex(4)}_{secrets.token_hex(16)}"
        self.access[access] = (device.id, time.time() + self.expires_in)
        self.refresh[refresh] = {"device": device.id, "rotated": False}
        return _json(
            200,
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "refresh_token": refresh,
                "device_id": device.id,
                "org_id": device.org_id,
                "local_workspace_id": "ws_local",
                "workspaces": self.workspaces,
            },
        )

    def token(self, body: dict) -> httpx2.Response:
        if body["grant_type"] == "authorization_code":
            c = self.codes.get(body["code"])
            if c is None:
                return oauth_error("unknown code")
            if c["used"]:
                return oauth_error("code already used")
            c["used"] = True
            q = c["request"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=")
            if challenge.decode() != q["code_challenge"]:
                return oauth_error("code_verifier does not match the challenge")
            if body["redirect_uri"] != q["redirect_uri"]:
                return oauth_error("client_id or redirect_uri does not match the authorization")
            device = DeviceRec(f"dev_{len(self.devices) + 1}", "org_1")
            self.devices[device.id] = device
            return self._issue(device)
        row = self.refresh.get(body["refresh_token"])
        if row is None:
            return oauth_error("unknown refresh token")
        device = self.devices[row["device"]]
        if device.revoked:
            return oauth_error("device revoked")
        if row["rotated"]:
            device.revoked = True
            return oauth_error("refresh token reused: the device is revoked")
        row["rotated"] = True
        return self._issue(device)

    def revoke(self, req: httpx2.Request) -> httpx2.Response:
        token = req.headers.get("Authorization", "").removeprefix("Bearer ")
        entry = self.access.get(token)
        if entry is None or entry[1] < time.time():
            return problem(401, "unauthenticated", "Unauthenticated")
        self.devices[entry[0]].revoked = True
        return httpx2.Response(204)

    def expire_access_tokens(self) -> None:
        self.access = {t: (d, 0.0) for t, (d, _) in self.access.items()}

    # ------------------------------------------------------------------------------------------------ receiver

    def _device(self, req: httpx2.Request):
        token = req.headers.get("Authorization", "").removeprefix("Bearer ")
        entry = self.access.get(token)
        if entry is None or entry[1] < time.time():
            return problem(401, "unauthenticated", "Unauthenticated", "missing, expired or invalid token")
        device = self.devices[entry[0]]
        if device.revoked:
            return problem(403, "forbidden", "Forbidden", "this device is revoked")
        return device

    def _ws_ok(self, ws: str) -> bool:
        return ws in {w["id"] for w in self.workspaces}

    def table(self, ws: str, ns: list, name: str) -> CloudTable | None:
        return self.tables.get((ws, tuple(ns), name))

    def table_state(self, device, req) -> httpx2.Response:
        ws = req.url.params["workspace_id"]
        if not self._ws_ok(ws):
            return problem(404, "not_found", "Not found")
        t = self.table(ws, req.url.params.get_list("namespace"), req.url.params["name"])
        if t is None:
            return _json(200, {"exists": False, "push_managed": False})
        last = t.snapshots[-1] if t.snapshots else None
        return _json(
            200,
            {
                "exists": True,
                "push_managed": t.push_managed,
                "source_table_uuid": t.source_table_uuid,
                "cloud_table_id": t.id,
                "last_pushed_source_snapshot_id": last["source"] if last else None,
                "cloud_snapshot_id": last["cloud"] if last else None,
                "last_pushed_at": None,
            },
        )

    def create(self, device, req) -> httpx2.Response:
        body = json.loads(req.content)
        key = req.headers["Idempotency-Key"]
        digest = hashlib.sha256(req.content).hexdigest()
        ws = body["target_workspace_id"]
        if not self._ws_ok(ws):
            return problem(403, "forbidden", "Forbidden", "workspace not in your organization")
        prior = self.idem.get((device.id, key))
        if prior is not None:
            if prior[0] != digest:
                return problem(409, "idempotency_conflict", "Idempotency conflict")
            return _json(201, self._view(self.sessions[prior[1]]))
        t = self.table(ws, body["table"]["namespace"], body["table"]["name"])
        if t is not None and not t.push_managed:
            return problem(409, "table_exists", "Table exists", "a table of that name exists and was not pushed")
        last = t.snapshots[-1]["source"] if t and t.snapshots else None
        if t is not None and t.source_table_uuid != body["source_table_uuid"]:
            return problem(409, "history_diverged", "History diverged", "created from another source table")
        if body.get("base_snapshot_id") != last:
            return problem(409, "history_diverged", "History diverged", f"the cloud's last is {last}")
        if not body["snapshots"]:
            return _json(200, {"state": "up_to_date", "table": body["table"], "last_pushed_source_snapshot_id": last})
        for s in self.sessions.values():
            if (s.workspace_id, s.table == body["table"]) == (ws, True) and s.state in ("open", "committing"):
                extra = {"push_id": s.push_id} if s.device_id == device.id else {}
                return problem(409, "push_in_progress", "Push in progress", "another push of this table runs", **extra)
        files = {}
        for snap in body["snapshots"]:
            for f in snap["added_files"]:
                present = f["file_id"] in self.blobs.get(ws, {})
                files[f["file_id"]] = {
                    "size": f["size_bytes"],
                    "received": f["size_bytes"] if present else 0,
                    "state": "present" if present else "needed",
                    "buf": bytearray(),
                }
        incoming = sum(f["size"] for f in files.values() if f["state"] == "needed")
        use = {"ceiling_bytes": 1 << 40, "committed_bytes": 0, "staged_bytes": 0, "reserved_bytes": incoming}
        admission = {"outcome": "accepted", "incoming_bytes": incoming, "org": use, "workspace": use}
        if self.over_included:
            admission |= {
                "outcome": "over_included",
                "over_included_bytes": incoming,
                "estimated_extra_monthly_cost": {"amount": "0.02", "currency": "USD"},
            }
        s = Session(f"push_{len(self.sessions) + 1}", device.id, ws, body["table"], body, files, admission)
        if self.end_new_sessions > 0:  # the session expires before the client gets to it
            self.end_new_sessions -= 1
            s.state = "expired"
        self.sessions[s.push_id] = s
        self.idem[(device.id, key)] = (digest, s.push_id)
        return _json(201, self._view(s))

    def _view(self, s: Session) -> dict:
        if s.state == "committing":
            s.polls_left -= 1
            if s.polls_left < 0:
                s.state = "committed"
        view = {
            "push_id": s.push_id,
            "state": s.state,
            "table": s.table,
            "target_workspace_id": s.workspace_id,
            "expires_at": "2030-01-01T00:00:00Z",
            "admission": s.admission,
            "files": [
                {"file_id": fid, "size_bytes": f["size"], "received_bytes": f["received"], "state": f["state"]}
                for fid, f in s.files.items()
            ],
        }
        if s.state == "committed":
            view["result"] = s.result
        return view

    def patch(self, s: Session, fid: str, req) -> httpx2.Response:
        f = s.files.get(fid)
        if f is None or s.state != "open":
            return problem(404, "not_found", "Not found")
        start, end, total = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)", req.headers["Content-Range"]).groups())
        data = req.content
        if total != f["size"] or end >= f["size"] or end - start + 1 != len(data):
            return problem(400, "invalid_request", "Invalid request", "bad Content-Range")
        if f["state"] in ("complete", "present"):
            return _json(
                200, {"file_id": fid, "size_bytes": f["size"], "received_bytes": f["size"], "state": f["state"]}
            )
        if start != f["received"]:
            return problem(409, "offset_mismatch", "Offset mismatch", "", received_bytes=f["received"])
        f["buf"] += data
        f["received"] += len(data)
        self.patch_bytes += len(data)
        self.patched.append((fid, start, len(data)))
        f["state"] = "partial"
        if f["received"] == f["size"]:
            if hashlib.sha256(bytes(f["buf"])).hexdigest() != fid:
                f.update(received=0, state="needed", buf=bytearray())
                return problem(422, "checksum_mismatch", "Checksum mismatch", "the file restarts from 0")
            self.blobs.setdefault(s.workspace_id, {})[fid] = bytes(f["buf"])
            f["state"] = "complete"
        if self.kill_after_chunks is not None:
            self.kill_after_chunks -= 1
            if self.kill_after_chunks <= 0:
                self.kill_after_chunks = None
                raise Killed("client killed after the receiver stored a chunk, before it saw the answer")
        return _json(
            200, {"file_id": fid, "size_bytes": f["size"], "received_bytes": f["received"], "state": f["state"]}
        )

    def commit(self, s: Session, req) -> httpx2.Response:
        if s.state in ("committing", "committed"):
            return _json(200 if s.state == "committed" else 202, self._view(s))
        body = json.loads(req.content) if req.content else {}
        if s.admission["outcome"] == "over_included" and not body.get("confirm_over_included"):
            return problem(
                409,
                "confirmation_required",
                "Confirmation required",
                "estimated extra 0.02 USD per month",
                estimated_extra_monthly_cost={"amount": "0.02", "currency": "USD"},
            )
        if any(f["state"] not in ("complete", "present") for f in s.files.values()):
            return problem(422, "invalid_parquet", "Files missing", "not every file is uploaded")
        key = (s.workspace_id, tuple(s.table["namespace"]), s.table["name"])
        t = self.tables.setdefault(key, CloudTable(str(uuid.uuid4()), s.body["source_table_uuid"]))
        mapping = []
        for snap in s.body["snapshots"]:
            added = sorted(f["file_id"] for f in snap["added_files"])
            t.live |= set(added)
            t.live -= set(snap["removed_file_ids"])
            cloud_id = len(t.snapshots) + 1
            t.snapshots.append(
                {
                    "source": snap["snapshot_id"],
                    "cloud": cloud_id,
                    "added": added,
                    "removed": sorted(snap["removed_file_ids"]),
                    "schema": snap["schema"],
                }
            )
            mapping.append({"source_snapshot_id": snap["snapshot_id"], "cloud_snapshot_id": cloud_id})
        s.result = {"cloud_table_id": t.id, "snapshots": mapping}
        s.state = "committing"
        return _json(202, self._view(s))

    def table_view(self, ws: str, ns: str, name: str) -> dict:
        """What a reader of the cloud table would see, for comparing two pushes."""
        t = self.table(ws, [ns], name)
        return {
            "snapshots": [(x["source"], x["added"], x["removed"], json.dumps(x["schema"])) for x in t.snapshots],
            "live": sorted(t.live),
            "bytes": {fid: hashlib.sha256(self.blobs[ws][fid]).hexdigest() for fid in sorted(t.live)},
        }

    # ------------------------------------------------------------------------------------------------ for a subprocess

    def serve(self) -> tuple[http.server.ThreadingHTTPServer, str]:
        """Serve both fake services on one 127.0.0.1 port (paths do not overlap). Returns (server, base URL)."""
        cloud = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = self.path
                host = "accounts.test" if path.startswith("/v1/") else "push.test"
                req = httpx2.Request(self.command, f"http://{host}{path}", headers=dict(self.headers), content=body)
                if self.command == "PATCH":
                    time.sleep(cloud.patch_delay)  # so a test can kill the client mid-upload on any machine
                try:
                    resp = cloud.handle(req)
                except Killed:
                    resp = httpx2.Response(500)
                payload = resp.content
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in ("content-length", "transfer-encoding"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PATCH = do_DELETE = _any  # noqa: N815

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------------------------------------ a fake browser


def browser(
    cloud: FakeCloud, *, tamper_state=False, error=None, replay_code: str | None = None, record: dict | None = None
):
    """What the user's browser does with the authorize URL: accounts (fake) answers with a redirect to the loopback,
    and the browser follows it to the client's listener (a real socket on 127.0.0.1), in the background."""
    record = record if record is not None else {}

    def open_url(url: str) -> None:
        record["authorize_url"] = url
        with httpx2.Client(transport=cloud.transport()) as c:
            r = c.get(url, follow_redirects=False)
        assert r.status_code == 302, r.text
        loc = urlsplit(r.headers["Location"])
        q = {k: v[0] for k, v in parse_qs(loc.query).items()}
        record["code"] = q["code"]
        if tamper_state:
            q["state"] = "x" * 32
        if error:
            q = {"error": error, "state": q["state"]}
        if replay_code:
            q["code"] = replay_code
        target = f"{loc.scheme}://{loc.netloc}{loc.path}?{urlencode(q)}"
        record["callback"] = target

        def follow():
            try:
                record["callback_status"] = httpx2.get(target, timeout=10).status_code
            except httpx2.HTTPError as e:
                record["callback_error"] = e

        t = threading.Thread(target=follow, daemon=True)
        t.start()
        record["thread"] = t

    return open_url
