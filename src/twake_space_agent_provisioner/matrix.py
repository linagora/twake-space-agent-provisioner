"""The Matrix appservice client.

The operator registers each agent's bot and logs it in through the Matrix
application service, whose exclusive user namespace covers
``@twake-space-assistant-*``. Calls go to the homeserver's client API and
identify themselves with the appservice token; registration and login name the
bot in the request body, so no call has to impersonate it with ``?user_id=``.

This is the only collaborator the operator talks to over the network, so it is
kept behind one small boundary: the seam 2 bench substitutes a fake homeserver
for it, and no test mocks the operator's own code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class HomeserverError(Exception):
    """The homeserver refused a call.

    ``retryable`` says whether trying again later could help: a server error or
    a rate limit is transient, a bad request is not.
    """

    errcode: str = "M_UNKNOWN"
    message: str = ""
    status: int = 0
    retryable: bool = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.errcode} ({self.status}): {self.message}"


class RateLimited(HomeserverError):
    """The homeserver asked to wait before the next attempt."""

    def __init__(self, *, retry_after_ms: int = 0, **kwargs: Any) -> None:
        super().__init__(retryable=True, **kwargs)
        self.retry_after_ms = retry_after_ms


def classify(status: int, body: Any) -> HomeserverError:
    """Turn a failed Matrix answer into an error the caller can act on.

    A body that is not a JSON object (an HTML error page, say) still yields a
    usable error rather than raising a second time.
    """
    if not isinstance(body, dict):
        body = {}
    errcode = body.get("errcode") or "M_UNKNOWN"
    message = body.get("error") or ""
    if status == 429:
        return RateLimited(
            errcode=errcode,
            message=message,
            status=status,
            retry_after_ms=int(body.get("retry_after_ms") or 0),
        )
    return HomeserverError(
        errcode=errcode,
        message=message,
        status=status,
        retryable=status >= 500 or status in (408, 429),
    )


def retry_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    rate_limit: RateLimited | None = None,
) -> float:
    """Seconds to wait before try ``attempt`` (1 is the first retry).

    A doubling backoff, capped. A rate limit that asks for longer than the
    backoff sets the pace instead: the homeserver knows its own load.
    """
    delay = min(base * (2 ** (attempt - 1)), cap)
    if rate_limit is not None:
        asked = rate_limit.retry_after_ms / 1000
        delay = max(delay, min(asked, cap))
    return delay


def bot_localpart(user_id: str) -> str:
    """`@twake-space-assistant-jean-dupont:server` -> `twake-space-assistant-jean-dupont`.

    Registration names the bot by its localpart, not by the full Matrix ID.
    """
    return user_id.split(":", 1)[0].lstrip("@")


@dataclass
class BotSession:
    """What a login gives back: the token and the device it belongs to."""

    user_id: str
    access_token: str
    device_id: str


class MatrixAppservice:
    """Appservice calls against one homeserver.

    One instance per operator run: the token is the provisioner's own, held in
    its configuration, and never written into any agent object.
    """

    def __init__(
        self,
        *,
        base_url: str,
        appservice_token: str,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = appservice_token
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        response = await self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            error = classify(response.status_code, body)
            logger.warning(
                "homeserver refused %s %s: %s", method, path, error
            )
            raise error
        return response

    async def register_bot(self, user_id: str) -> None:
        """Make sure the bot account exists.

        ``M_USER_IN_USE`` means it is already there, which is the expected
        answer on a re-activation or a takeover, not a failure.
        """
        try:
            await self._call(
                "POST",
                "/_matrix/client/v3/register",
                json={"type": "m.login.application_service", "username": bot_localpart(user_id)},
            )
        except HomeserverError as error:
            if error.errcode == "M_USER_IN_USE":
                return
            raise

    async def login_bot(self, user_id: str, device_id: str) -> BotSession:
        """Log the bot in on its fixed device.

        The device ID is fixed so the agent keeps one encryption identity
        across restarts and takeovers.
        """
        response = await self._call(
            "POST",
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.application_service",
                "identifier": {"type": "m.id.user", "user": user_id},
                "device_id": device_id,
            },
        )
        body = response.json()
        return BotSession(
            user_id=body.get("user_id", user_id),
            access_token=body["access_token"],
            device_id=body.get("device_id", device_id),
        )
