"""Seam 2: the agent gets its own Matrix bot.

The operator runs inside the test process against kind, and the fake homeserver
records what it was asked. Tests check the Kubernetes objects and the Matrix
calls together: that is what another system would see.
"""

from __future__ import annotations

import base64
import time

import pytest
from tests.seam2.fake_homeserver import FakeHomeserver
from tests.seam2.harness import OperatorHarness

pytestmark = pytest.mark.seam2

NAME = "jean-dupont"
USERNAME = "jean.dupont"
BOT_LOCALPART = "twake-space-assistant-jean-dupont"
BOT_USER_ID = f"@{BOT_LOCALPART}:test.invalid"
DEVICE_ID = "HERMESJEANDUPONT"
SECRET_NAME = "hermes-user-jean-dupont-managed-env"


def agent_manifest(namespace: str, *, name: str = NAME, username: str = USERNAME) -> dict:
    return {
        "apiVersion": "twake-space.linagora.com/v1alpha1",
        "kind": "PersonalAgent",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "username": username,
            "profile": {
                "firstName": "Jean",
                "language": "fr",
                "timezone": "Europe/Paris",
            },
        },
    }


def _secret(operator: OperatorHarness) -> dict | None:
    return operator.kubectl.get("secret", SECRET_NAME)


def _token(operator: OperatorHarness) -> str | None:
    secret = _secret(operator)
    if not secret:
        return None
    return secret["data"].get("MATRIX_ACCESS_TOKEN")


def _env(secret: dict) -> dict[str, str]:
    lines = base64.b64decode(secret["data"][".env"]).decode()
    return dict(line.split("=", 1) for line in lines.splitlines())


def test_registers_the_bot_once_and_keeps_its_token(
    operator: OperatorHarness, homeserver: FakeHomeserver
) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    secret = operator.wait_for(
        lambda: _secret(operator), message="the secret was not created"
    )
    token = secret["data"]["MATRIX_ACCESS_TOKEN"]

    # Several reconcile intervals pass.
    time.sleep(4)

    # One registration and one login, never more: a second login would mean a
    # new device and new encryption keys.
    registrations = [
        c for c in homeserver.registrations() if c.body.get("username") == BOT_LOCALPART
    ]
    logins = homeserver.logins()
    assert len(registrations) == 1, [c.body for c in registrations]
    assert len(logins) == 1, [c.body for c in logins]
    assert logins[0].body["device_id"] == DEVICE_ID

    # The token is the one the fake issued, and stays put.
    assert _token(operator) == token


def test_registration_reads_user_in_use_as_success(
    operator: OperatorHarness, homeserver: FakeHomeserver
) -> None:
    """A bot that already exists is not an error: the agent still gets a token.

    This is the ordinary case after a reinstall: the account survived, the
    Secret did not, so registration answers ``M_USER_IN_USE`` and the operator
    proceeds to log in rather than refusing to provision.
    """
    homeserver.existing_user(BOT_LOCALPART)
    operator.kubectl.create(agent_manifest(operator.namespace))

    operator.wait_for(
        lambda: _token(operator),
        message="the agent got no token for an existing bot",
    )


def test_the_agent_answers_its_owner_alone(
    operator: OperatorHarness, homeserver: FakeHomeserver
) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    secret = operator.wait_for(
        lambda: _secret(operator), message="the secret was not created"
    )
    env = _env(secret)

    assert env["MATRIX_HOMESERVER"] == homeserver.url
    assert env["MATRIX_USER_ID"] == BOT_USER_ID
    assert env["MATRIX_DEVICE_ID"] == DEVICE_ID
    assert env["MATRIX_E2EE_MODE"] == "optional"
    assert env["MATRIX_RECOVERY_KEY_OUTPUT_FILE"] == (
        "/opt/data/platforms/matrix/recovery-key.txt"
    )
    assert env["MATRIX_ALLOWED_USERS"] == "@jean.dupont:test.invalid"


def test_the_status_shows_the_bot_identity(operator: OperatorHarness) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    def bot_registered() -> dict | None:
        agent = operator.kubectl.get("personalagent", NAME)
        if not agent:
            return None
        status = agent.get("status") or {}
        return agent if status.get("botUserId") else None

    agent = operator.wait_for(bot_registered, message="the bot identity never showed")
    status = agent["status"]
    assert status["botUserId"] == BOT_USER_ID
    assert status["deviceId"] == DEVICE_ID
    conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
    assert conditions.get("botRegistered") == "True"


def test_the_operator_honours_the_rate_limits_retry_delay(
    operator: OperatorHarness, homeserver: FakeHomeserver
) -> None:
    """A 429 with a retry delay: the reason shows it and the agent recovers.

    The homeserver knows its own load, so its delay sets the pace when it asks
    for longer than the operator's own backoff.
    """
    homeserver.rate_limit(1, retry_after_ms=1500)
    operator.kubectl.create(agent_manifest(operator.namespace))

    seen_rate_limit = {"value": False}

    def until_recovered() -> str | None:
        agent = operator.kubectl.get("personalagent", NAME) or {}
        reason = str((agent.get("status") or {}).get("reason") or "")
        if reason.startswith("homeserver:M_LIMIT_EXCEEDED"):
            seen_rate_limit["value"] = True
        return _token(operator)

    operator.wait_for(until_recovered, timeout=60, message="the agent never recovered")
    assert seen_rate_limit["value"], "the rate limit never showed in the status reason"


def test_a_permanent_refusal_fails_the_agent_instead_of_looping(
    operator_with_bad_token: OperatorHarness,
) -> None:
    """A bad appservice token is not something a retry fixes."""
    operator = operator_with_bad_token
    operator.kubectl.create(agent_manifest(operator.namespace))

    def failed() -> dict | None:
        agent = operator.kubectl.get("personalagent", NAME)
        if not agent:
            return None
        status = agent.get("status") or {}
        return agent if status.get("phase") == "Failed" else None

    agent = operator.wait_for(failed, message="the agent never failed on a bad token")
    reason = str(agent["status"].get("reason") or "")
    assert reason.startswith("Homeserver") or reason == "NoAppserviceToken"


def test_the_appservice_token_never_reaches_the_agents_objects(
    operator: OperatorHarness, homeserver: FakeHomeserver
) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    def workload_there() -> dict | None:
        return operator.kubectl.get("statefulset", "hermes-user-jean-dupont")

    operator.wait_for(workload_there, message="no workload")

    secret = _secret(operator)
    configmap = operator.kubectl.get("configmap", "hermes-user-jean-dupont-managed")
    statefulset = operator.kubectl.get("statefulset", "hermes-user-jean-dupont")
    blob = " ".join(str(o) for o in (secret, configmap, statefulset))
    assert homeserver.appservice_token not in blob


def test_the_operator_recovers_from_a_homeserver_outage(
    operator: OperatorHarness, homeserver: FakeHomeserver
) -> None:
    """A homeserver that fails then heals: the agent still gets its bot.

    The first two calls answer 500; the operator backs off and retries, and the
    agent settles with a token rather than staying stuck.
    """
    homeserver.fail_with_server_error(2)
    operator.kubectl.create(agent_manifest(operator.namespace))

    operator.wait_for(
        lambda: _token(operator), timeout=90, message="the agent never recovered"
    )

    # The status no longer carries the outage as its reason.
    agent = operator.kubectl.get("personalagent", NAME)
    assert not str((agent.get("status") or {}).get("reason", "")).startswith("homeserver:")
