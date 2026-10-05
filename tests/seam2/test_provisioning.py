"""Seam 2: a PersonalAgent becomes a running agent workload.

The operator runs inside the test process against kind, and the template points
at a stub image that answers the health probe. Tests check the Kubernetes
objects, the same way an administrator would with kubectl.
"""

from __future__ import annotations

import time

import pytest
import yaml
from tests.seam2.harness import OperatorHarness

pytestmark = pytest.mark.seam2

NAME = "jean-dupont"
USERNAME = "jean.dupont"


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


def test_creates_the_objects_with_the_expected_shape(operator: OperatorHarness) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    def objects_there() -> dict | None:
        configmap = operator.kubectl.get("configmap", "hermes-user-jean-dupont-managed")
        if not configmap:
            return None
        return {
            "configmap": configmap,
            "secret": operator.kubectl.get("secret", "hermes-user-jean-dupont-managed-env"),
            "networkpolicy": operator.kubectl.get("networkpolicy", "hermes-user-jean-dupont"),
            "statefulset": operator.kubectl.get("statefulset", "hermes-user-jean-dupont"),
        }

    objects = operator.wait_for(objects_there, message="the four objects were not created")
    assert all(objects.values()), objects

    statefulset = objects["statefulset"]["spec"]
    assert statefulset["replicas"] == 1
    assert statefulset["template"]["spec"]["automountServiceAccountToken"] is False
    assert statefulset["template"]["spec"]["securityContext"]["fsGroup"] == 10000
    assert statefulset["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Delete"

    container = statefulset["template"]["spec"]["containers"][0]
    assert container["args"] == ["gateway", "run"]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert set(container["securityContext"]["capabilities"]["add"]) == {
        "CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID",
    }

    # Denial of all ingress.
    assert objects["networkpolicy"]["spec"]["policyTypes"] == ["Ingress"]
    assert "ingress" not in objects["networkpolicy"]["spec"]

    # The owner's timezone reaches Hermes' managed configuration.
    config = yaml.safe_load(objects["configmap"]["data"]["config.yaml"])
    assert config["timezone"] == "Europe/Paris"

    # Every object is owned by the PersonalAgent.
    for kind, obj in objects.items():
        owners = obj["metadata"].get("ownerReferences") or []
        assert any(o["kind"] == "PersonalAgent" and o["name"] == NAME for o in owners), kind


def test_a_profile_change_rolls_the_pod_through_the_checksum(
    operator: OperatorHarness,
) -> None:
    """The owner's timezone change must reach the running pod.

    The configuration is a projected file, so Hermes only reads it at start.
    The StatefulSet carries a checksum of the configuration, and changing it is
    what makes Kubernetes roll the pod.
    """
    operator.kubectl.create(agent_manifest(operator.namespace))

    def provisioned() -> dict | None:
        statefulset = operator.kubectl.get("statefulset", "hermes-user-jean-dupont")
        if not statefulset:
            return None
        return statefulset

    before = operator.wait_for(provisioned, message="no workload")
    before_checksum = before["spec"]["template"]["metadata"]["annotations"][
        "checksum/managed-config"
    ]

    # The owner changes their timezone in the settings.
    operator.kubectl.patch(
        "personalagent",
        NAME,
        {"spec": {"profile": {"timezone": "America/New_York"}}},
    )

    def rolled() -> dict | None:
        configmap = operator.kubectl.get("configmap", "hermes-user-jean-dupont-managed")
        statefulset = operator.kubectl.get("statefulset", "hermes-user-jean-dupont")
        if not configmap or not statefulset:
            return None
        timezone = yaml.safe_load(configmap["data"]["config.yaml"]).get("timezone")
        checksum = statefulset["spec"]["template"]["metadata"]["annotations"].get(
            "checksum/managed-config"
        )
        if timezone != "America/New_York" or checksum == before_checksum:
            return None
        return statefulset

    # A changed checksum on the pod template is what rolls the pod.
    operator.wait_for(rolled, message="the workload did not roll on the profile change")


def test_api_server_key_stays_the_same_across_reconciles(operator: OperatorHarness) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    def secret_there() -> dict | None:
        return operator.kubectl.get("secret", "hermes-user-jean-dupont-managed-env")

    first = operator.wait_for(secret_there, message="the secret was not created")
    first_key = first["data"]["API_SERVER_KEY"]

    # Wait through several reconcile intervals.
    time.sleep(4)

    second = operator.kubectl.get("secret", "hermes-user-jean-dupont-managed-env")
    assert second is not None
    assert second["data"]["API_SERVER_KEY"] == first_key


def test_name_not_derived_from_username_is_refused(operator: OperatorHarness) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace, name="my-agent"))

    def refused() -> dict | None:
        agent = operator.kubectl.get("personalagent", "my-agent")
        if not agent:
            return None
        status = agent.get("status") or {}
        return agent if status.get("phase") == "Failed" else None

    agent = operator.wait_for(refused, message="the mismatched name was not refused")
    assert agent["status"]["reason"] == "NameMismatch"

    # Nothing is created for it.
    assert operator.kubectl.get("statefulset", "hermes-user-my-agent") is None
    # And the message names the expected resource name.
    events = operator.kubectl.events("my-agent")
    messages = " ".join(e.get("message", "") for e in events)
    assert "jean-dupont" in messages


def test_phase_becomes_ready_when_the_pod_is_ready(operator: OperatorHarness) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    def ready() -> dict | None:
        agent = operator.kubectl.get("personalagent", NAME)
        if not agent:
            return None
        return agent if (agent.get("status") or {}).get("phase") == "Ready" else None

    operator.wait_for(ready, timeout=180, message="the agent never became Ready")

    statefulset = operator.kubectl.get("statefulset", "hermes-user-jean-dupont")
    assert statefulset["status"].get("readyReplicas") == 1


def test_each_step_posts_an_event(operator: OperatorHarness) -> None:
    operator.kubectl.create(agent_manifest(operator.namespace))

    def events_there() -> list | None:
        events = operator.kubectl.events(NAME)
        reasons = {e.get("reason") for e in events}
        return events if {"WorkloadApplied", "Ready"} <= reasons else None

    events = operator.wait_for(events_there, timeout=180, message="no step events")
    messages = " ".join(e.get("message", "") for e in events)
    assert f"workload applied for {USERNAME}" in messages


def test_logs_carry_the_username(
    operator: OperatorHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="twake_space_agent_provisioner.operator")
    operator.kubectl.create(agent_manifest(operator.namespace))
    operator.wait_for(
        lambda: operator.kubectl.get("statefulset", "hermes-user-jean-dupont"),
        message="no workload",
    )

    # The operator's log lines carry the username, so one user's story can be
    # followed in the logs.
    lines = [r.getMessage() for r in caplog.records]
    assert any("workload applied" in line and "jean.dupont" in line for line in lines), lines


def test_ai_gateway_key_reaches_the_pod_from_the_shared_secret(
    operator_with_ai_key: OperatorHarness,
) -> None:
    operator_with_ai_key.kubectl.create(agent_manifest(operator_with_ai_key.namespace))

    def workload_there() -> dict | None:
        return operator_with_ai_key.kubectl.get("statefulset", "hermes-user-jean-dupont")

    statefulset = operator_with_ai_key.wait_for(
        workload_there, message="no workload"
    )
    container = statefulset["spec"]["template"]["spec"]["containers"][0]
    entry = next(e for e in container["env"] if e["name"] == "LINAGORA_API_KEY")
    assert entry["valueFrom"]["secretKeyRef"]["name"] == "provisioner-shared"

    # The key is never copied into the agent's own Secret.
    agent_secret = operator_with_ai_key.kubectl.get(
        "secret", "hermes-user-jean-dupont-managed-env"
    )
    assert "shared-ai-key-value" not in str(agent_secret)
