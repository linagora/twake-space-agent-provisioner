"""A fake homeserver: the only fake in the system.

It stands for Synapse at the system boundary, so the operator's own code runs
untouched in tests. It implements only the calls the operator makes, and
reproduces the Synapse behaviours the design leans on:

- ``M_USER_IN_USE`` on re-registration, which the operator treats as success;
- the appservice's exclusive user namespace, enforced on registration;
- a login that returns the token for the requested device.

Every call is recorded, so a test can check what the operator asked for and,
just as often, that it did not ask twice.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


@dataclass
class Call:
    """One request the fake received."""

    method: str
    path: str
    body: dict[str, Any]
    authorization: str | None


@dataclass
class _User:
    user_id: str
    devices: dict[str, str] = field(default_factory=dict)


class FakeHomeserver:
    """An in-process homeserver speaking the appservice API.

    ``namespace`` is the appservice's exclusive localpart prefix; a
    registration outside it is refused, as a real homeserver does.
    """

    def __init__(
        self,
        *,
        server_name: str = "test.invalid",
        appservice_token: str = "test-appservice-token",
        namespace: str = "twake-space-assistant-",
    ) -> None:
        self.server_name = server_name
        self.appservice_token = appservice_token
        self.namespace = namespace
        self.calls: list[Call] = []
        self.users: dict[str, _User] = {}
        #: How many more calls to fail with a 500, for the outage test.
        self.fail_next = 0
        #: How many more calls to answer 429, and the delay to ask for.
        self.rate_limit_next = 0
        self.rate_limit_ms = 0
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        assert self._httpd is not None, "the fake homeserver is not running"
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeHomeserver:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeHomeserver:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def reset(self) -> None:
        """Forget every recorded call and every user.

        The server outlives one test (its port is fixed for the whole session),
        so each test must start from a clean slate: a test that counts calls
        would otherwise pass or fail on the order pytest ran in.
        """
        with self._lock:
            self.calls.clear()
            self.users.clear()
            self.fail_next = 0
            self.rate_limit_next = 0
            self.rate_limit_ms = 0

    # -- recorded calls ----------------------------------------------------

    def calls_to(self, path_contains: str) -> list[Call]:
        return [c for c in self.calls if path_contains in c.path]

    def registrations(self) -> list[Call]:
        return self.calls_to("/register")

    def logins(self) -> list[Call]:
        return self.calls_to("/login")

    # -- behaviour ---------------------------------------------------------

    def fail_with_server_error(self, times: int) -> None:
        """Make the next ``times`` calls answer 500, to test the retry."""
        with self._lock:
            self.fail_next = times

    def existing_user(self, localpart: str) -> str:
        """Pre-create a user as an earlier run would have.

        A registration for it then answers ``M_USER_IN_USE``, the answer the
        operator must read as success.
        """
        user_id = f"@{localpart}:{self.server_name}"
        with self._lock:
            self.users.setdefault(user_id, _User(user_id=user_id))
        return user_id

    def rate_limit(self, times: int, *, retry_after_ms: int) -> None:
        """Make the next ``times`` calls answer 429 with a retry delay."""
        with self._lock:
            self.rate_limit_next = times
            self.rate_limit_ms = retry_after_ms

    def _claim_failure(self) -> bool:
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                return True
            return False

    def _claim_rate_limit(self) -> int | None:
        """The retry delay to ask for, or None if this call is not limited."""
        with self._lock:
            if self.rate_limit_next > 0:
                self.rate_limit_next -= 1
                return self.rate_limit_ms
            return None

    def record(self, call: Call) -> None:
        with self._lock:
            self.calls.append(call)

    def _authorised(self, authorization: str | None) -> bool:
        return authorization == f"Bearer {self.appservice_token}"

    def register(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        username = body.get("username") or ""
        if not username.startswith(self.namespace):
            return 400, {
                "errcode": "M_EXCLUSIVE",
                "error": f"username is not in the appservice namespace {self.namespace}*",
            }
        user_id = f"@{username}:{self.server_name}"
        with self._lock:
            if user_id in self.users:
                return 400, {"errcode": "M_USER_IN_USE", "error": "User ID already taken."}
            self.users[user_id] = _User(user_id=user_id)
        return 200, {"user_id": user_id}

    def login(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        identifier = body.get("identifier") or {}
        user_id = identifier.get("user") or ""
        device_id = body.get("device_id") or ""
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                return 403, {"errcode": "M_FORBIDDEN", "error": "Unknown user."}
            # A fresh token per login, as a real homeserver issues: the operator
            # must keep the first one rather than log in again.
            token = f"tok-{user_id}-{device_id}-{len(self.calls)}"
            user.devices[device_id] = token
        return 200, {"user_id": user_id, "access_token": token, "device_id": device_id}


def _make_handler(homeserver: FakeHomeserver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:  # keep the test output clean
            return

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._read_body()
            homeserver.record(
                Call(
                    method="POST",
                    path=path,
                    body=body,
                    authorization=self.headers.get("Authorization"),
                )
            )
            if homeserver._claim_failure():
                self._respond(500, {"errcode": "M_UNKNOWN", "error": "boom"})
                return
            retry_after_ms = homeserver._claim_rate_limit()
            if retry_after_ms is not None:
                self._respond(
                    429,
                    {
                        "errcode": "M_LIMIT_EXCEEDED",
                        "error": "Too many requests",
                        "retry_after_ms": retry_after_ms,
                    },
                )
                return
            if not homeserver._authorised(self.headers.get("Authorization")):
                self._respond(401, {"errcode": "M_UNKNOWN_TOKEN", "error": "bad token"})
                return
            if path.endswith("/register"):
                self._respond(*homeserver.register(body))
            elif path.endswith("/login"):
                self._respond(*homeserver.login(body))
            else:
                self._respond(404, {"errcode": "M_UNRECOGNIZED", "error": "no such path"})

        def do_GET(self) -> None:
            homeserver.record(
                Call(
                    method="GET",
                    path=urlparse(self.path).path,
                    body={},
                    authorization=self.headers.get("Authorization"),
                )
            )
            if homeserver._claim_failure():
                self._respond(500, {"errcode": "M_UNKNOWN", "error": "boom"})
                return
            self._respond(404, {"errcode": "M_UNRECOGNIZED", "error": "no such path"})

    return Handler
