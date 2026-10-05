"""The agent operator: turns a PersonalAgent into a running agent workload.

Registration happens here, driven by kopf. This slice provisions the workload:
a ConfigMap, a Secret, a NetworkPolicy and a StatefulSet. Registering the Matrix
bot, opening the DM room and welcoming the owner come in later slices; their
status conditions are declared in the CRD so the shape is stable.

Reconcile is level-triggered and idempotent: it runs on a short timer, reads
the current objects, and only writes what differs. That makes a crash during
provisioning harmless and gives transient failures their retry.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import kopf

from . import naming
from .cluster import Cluster
from .config import Config
from .objects import build_objects
from .template import Template

logger = logging.getLogger(__name__)

GROUP = "twake-space.linagora.com"
VERSION = "v1alpha1"
PLURAL = "personalagents"

PHASE_PENDING = "Pending"
PHASE_PROVISIONING = "Provisioning"
PHASE_READY = "Ready"
PHASE_FAILED = "Failed"

#: How often the operator re-checks a PersonalAgent. Short enough that a
#: hand-applied resource settles in well under the two-minute promise, long
#: enough not to hammer the API server. Read at import: the timer's interval is
#: fixed by the decorator, and the tests set the variable before importing.
RECONCILE_INTERVAL = float(os.environ.get("PROVISIONER_RECONCILE_INTERVAL", "15"))


@dataclass
class Context:
    """What the handlers need, built once at startup."""

    config: Config
    template: Template
    cluster: Cluster


@kopf.on.startup()
async def startup(memo: kopf.Memo, settings: kopf.OperatorSettings, **_: Any) -> None:
    """Watch only our own namespace, with peering and scanning off.

    Namespace-scoped permissions are enough, and two copies of the provisioner
    never fight over the same resource. Landing here means the CRD is present.
    """
    # No peering: the provisioner runs as one replica under the Recreate
    # strategy, so there is never a second copy to coordinate with.
    settings.peering.standalone = True
    # No cluster-wide listing: the namespace-scoped Role is enough.
    settings.scanning.disabled = True
    settings.posting.enabled = True
    settings.posting.level = logging.INFO
    # A hand-applied resource should start provisioning immediately, not after
    # the first full interval.
    settings.execution.max_workers = 4

    config = Config.from_env()
    template = Template.load(config.template_path)
    cluster = await Cluster.connect()
    memo["context"] = Context(config=config, template=template, cluster=cluster)
    logger.info(
        "provisioner started in namespace %s, template %s",
        config.namespace,
        config.template_path,
    )


@kopf.on.cleanup()
async def cleanup(memo: kopf.Memo, **_: Any) -> None:
    context: Context | None = memo.get("context")
    if context is not None:
        await context.cluster.close()


@kopf.on.timer(
    GROUP, VERSION, PLURAL, id="reconcile", interval=RECONCILE_INTERVAL, initial_delay=1.0
)
async def reconcile(body: kopf.Body, patch: kopf.Patch, memo: kopf.Memo, **_: Any) -> None:
    """One provisioning pass over a PersonalAgent."""
    context: Context = memo["context"]
    spec = body.get("spec") or {}
    username = spec.get("username")
    name = body["metadata"]["name"]
    log = logging.LoggerAdapter(logger, {"username": username, "agent": name})

    if not username:
        _fail(body, patch, log, "MissingUsername", "spec.username is required")
        return

    if not naming.name_matches_username(name, username):
        _fail(
            body,
            patch,
            log,
            "NameMismatch",
            f"resource name {name!r} is not the one derived from username "
            f"{username!r} (expected {naming.derived_name(username)!r})",
        )
        return

    if not naming.is_valid_username(username):
        _fail(body, patch, log, "InvalidUsername", f"invalid username {username!r}")
        return

    # The phase as it was before this pass, so a step's event is posted on the
    # transition into it, not on every reconcile.
    previous_phase = (body.get("status") or {}).get("phase")
    patch.status["observedGeneration"] = body["metadata"].get("generation")
    patch.status["phase"] = PHASE_PROVISIONING
    patch.status["reason"] = "provisioning"

    profile = spec.get("profile") or {}
    api_server_key = await _reuse_or_generate_api_server_key(context, name)
    objects = build_objects(
        name=name,
        username=username,
        profile=profile,
        template=context.template,
        api_server_key=api_server_key,
        owner=body,
        ai_gateway_secret_name=context.config.ai_gateway_secret_name,
    )

    # The ConfigMap, the NetworkPolicy and the StatefulSet carry no generated
    # state, so replacing them is safe: the StatefulSet's checksum annotations
    # are what roll the pod when the configuration or the template changes.
    # The Secret holds the generated key and must not be rewritten.
    await context.cluster.apply_updatable(objects.configmap, context.config.namespace)
    await context.cluster.apply(objects.secret, context.config.namespace)
    await context.cluster.apply_updatable(objects.networkpolicy, context.config.namespace)
    await context.cluster.apply_updatable(objects.statefulset, context.config.namespace)
    log.info("workload applied for username %s", username)
    # An event marks the step, not every pass: a level-triggered reconcile runs
    # repeatedly, but the workload is applied once.
    if previous_phase != PHASE_PROVISIONING:
        kopf.info(body, reason="WorkloadApplied", message=f"workload applied for {username}")

    if await _pod_is_ready(context, name):
        patch.status["phase"] = PHASE_READY
        patch.status["reason"] = "ready"
        patch.status["conditions"] = _with_condition(
            body, "workloadReady", "True", "the agent pod is ready"
        )
        log.info("agent ready for username %s", username)
        if previous_phase != PHASE_READY:
            kopf.info(body, reason="Ready", message=f"the agent pod is ready for {username}")
    else:
        patch.status["conditions"] = _with_condition(
            body, "workloadReady", "False", "waiting for the agent pod"
        )


def _fail(
    body: kopf.Body,
    patch: kopf.Patch,
    log: logging.LoggerAdapter,
    reason_code: str,
    message: str,
) -> None:
    """Refuse a resource: set a clear status reason, create nothing, warn."""
    patch.status["phase"] = PHASE_FAILED
    patch.status["reason"] = reason_code
    log.warning(message)
    kopf.warn(body, reason=reason_code, message=message)


async def _reuse_or_generate_api_server_key(context: Context, name: str) -> str:
    """The API server key: generated once, kept across reconciles."""
    existing = await context.cluster.get_secret(
        context.config.namespace, naming.secret_name(name)
    )
    if existing is not None:
        encoded = (existing.get("data") or {}).get("API_SERVER_KEY")
        if encoded:
            return base64.b64decode(encoded).decode()
    # A dense random key: the API server compares it as a bearer token.
    return secrets.token_urlsafe(32)


async def _pod_is_ready(context: Context, name: str) -> bool:
    pods = await context.cluster.list_pods(
        context.config.namespace,
        label_selector=f"app.kubernetes.io/instance={naming.statefulset_name(name)}",
    )
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for pod in pods
        for condition in (pod.get("status", {}).get("conditions") or [])
    )


def _with_condition(
    body: kopf.Body, type_: str, status_: str, message: str
) -> list[dict[str, Any]]:
    """The condition list, with one entry replaced or appended.

    ``lastTransitionTime`` moves only when the status changes, per the
    Kubernetes condition convention: the time marks the transition, not the
    last time the operator looked.
    """
    existing = list((body.get("status") or {}).get("conditions") or [])
    previous = next((c for c in existing if c.get("type") == type_), None)
    others = [c for c in existing if c.get("type") != type_]
    unchanged = previous is not None and previous.get("status") == status_
    if unchanged and previous.get("lastTransitionTime"):
        transition = previous["lastTransitionTime"]
    else:
        transition = _now()
    others.append(
        {
            "type": type_,
            "status": status_,
            "message": message,
            "lastTransitionTime": transition,
        }
    )
    return others


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
