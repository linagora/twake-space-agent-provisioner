"""The Kubernetes objects the operator creates for a valid PersonalAgent.

Four objects, all owned by the PersonalAgent:

- a ConfigMap with Hermes' managed configuration, plus the owner's timezone;
- a Secret with the managed environment and the random API server key;
- a NetworkPolicy that denies all ingress;
- a StatefulSet shaped like the hermes-agent chart, with the hardening the
  organization agent already uses.

Every name derives from the resource name, using the pilot agent's pattern, so
the operator can take the pilot over.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import yaml

from . import naming
from .template import Homeserver, Template, owner_name

#: Where the provisioner keeps the shared AI Gateway key. Read by the pod as an
#: environment variable; never copied into the agent's own Secret.
AI_GATEWAY_KEY_ENV = "LINAGORA_API_KEY"

#: Where Hermes looks for its managed configuration and managed environment.
MANAGED_DIR = "/etc/hermes-managed"
DATA_DIR = "/opt/data"


@dataclass(frozen=True)
class AgentObjects:
    configmap: dict[str, Any]
    secret: dict[str, Any]
    networkpolicy: dict[str, Any]
    statefulset: dict[str, Any]

    def all(self) -> tuple[dict[str, Any], ...]:
        return (self.configmap, self.secret, self.networkpolicy, self.statefulset)


def _labels(name: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "hermes-agent",
        "app.kubernetes.io/instance": naming.statefulset_name(name),
        "app.kubernetes.io/managed-by": "twake-space-agent-provisioner",
    }


def _metadata(name: str, resource_name: str, owner: dict[str, Any] | None) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "labels": _labels(resource_name)}
    if owner is not None:
        meta["ownerReferences"] = [_owner_reference(owner)]
    return meta


def _owner_reference(owner: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": owner["apiVersion"],
        "kind": owner["kind"],
        "name": owner["metadata"]["name"],
        "uid": owner["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def build_objects(
    *,
    name: str,
    username: str,
    profile: dict[str, Any],
    template: Template,
    api_server_key: str,
    matrix_access_token: str,
    owner: dict[str, Any] | None = None,
    ai_gateway_secret_name: str | None = None,
    ai_gateway_key: str | None = None,
) -> AgentObjects:
    """Render every object for one owner.

    ``matrix_access_token`` is the bot's token, obtained once and kept in the
    Secret across reconciles. ``ai_gateway_key`` is only used to fold the key's
    identity into the pod checksum; the key itself is never written into any
    object.
    """
    owner_display = owner_name(
        profile.get("firstName"), profile.get("displayName"), username
    )
    language = profile.get("language")

    device = naming.device_id(name)
    rendered_config = _managed_config(template, profile)
    persona = template.persona(language, owner_display)
    managed_env = _managed_env(
        api_server_key,
        name=name,
        username=username,
        homeserver=template.homeserver,
        access_token=matrix_access_token,
        device_id=device,
    )

    configmap = _configmap(name, rendered_config, persona, owner)
    secret = _secret(name, managed_env, api_server_key, matrix_access_token, owner)
    networkpolicy = _networkpolicy(name, owner)
    statefulset = _statefulset(
        name=name,
        template=template,
        configmap=configmap,
        secret=secret,
        owner=owner,
        ai_gateway_secret_name=ai_gateway_secret_name,
        ai_gateway_key=ai_gateway_key,
    )
    return AgentObjects(
        configmap=configmap,
        secret=secret,
        networkpolicy=networkpolicy,
        statefulset=statefulset,
    )


def _managed_config(template: Template, profile: dict[str, Any]) -> dict[str, Any]:
    """The template's managed configuration, plus the owner's timezone."""
    config = dict(template.managed_config)
    timezone = profile.get("timezone")
    if timezone:
        config["timezone"] = timezone
    return config


def _managed_env(
    api_server_key: str,
    *,
    name: str,
    username: str,
    homeserver: Homeserver,
    access_token: str,
    device_id: str,
) -> dict[str, str]:
    """Hermes' managed environment, written as a .env file.

    The Matrix identity is the agent's own account, taken from the pilot agent's
    real configuration. ``MATRIX_ALLOWED_USERS`` is the owner alone, so the
    agent answers nobody else.
    """
    return {
        "API_SERVER_KEY": api_server_key,
        "MATRIX_ACCESS_TOKEN": access_token,
        "MATRIX_HOMESERVER": homeserver.url,
        "MATRIX_USER_ID": naming.bot_user_id(name, homeserver.server_name),
        "MATRIX_DEVICE_ID": device_id,
        # Encrypted rooms when crypto starts; plain rooms keep working if it
        # does not. Hermes writes the recovery key here on first start.
        "MATRIX_E2EE_MODE": "optional",
        "MATRIX_RECOVERY_KEY_OUTPUT_FILE": f"{DATA_DIR}/platforms/matrix/recovery-key.txt",
        "MATRIX_ALLOWED_USERS": naming.owner_id(username, homeserver.server_name),
    }


def _configmap(
    name: str, config: dict[str, Any], persona: str, owner: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(naming.configmap_name(name), name, owner),
        "data": {
            "config.yaml": yaml.safe_dump(config, sort_keys=True),
            "SOUL.md": persona,
        },
    }


def _secret(
    name: str,
    managed_env: dict[str, str],
    api_server_key: str,
    matrix_access_token: str,
    owner: dict[str, Any] | None,
) -> dict[str, Any]:
    lines = "".join(f"{key}={value}\n" for key, value in sorted(managed_env.items()))
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": _metadata(naming.secret_name(name), name, owner),
        "stringData": {
            ".env": lines,
            # Both are surfaced as individual keys so the operator can read the
            # existing ones back, and the pod can mount them where needed.
            "API_SERVER_KEY": api_server_key,
            "MATRIX_ACCESS_TOKEN": matrix_access_token,
        },
    }


def _networkpolicy(name: str, owner: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _metadata(naming.networkpolicy_name(name), name, owner),
        "spec": {
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/instance": naming.statefulset_name(name)}
            },
            "policyTypes": ["Ingress"],
            # No ingress rule: the agent only dials out and its API server
            # listens on loopback.
        },
    }


def _checksum(value: Any) -> str:
    blob = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _statefulset(
    *,
    name: str,
    template: Template,
    configmap: dict[str, Any],
    secret: dict[str, Any],
    owner: dict[str, Any] | None,
    ai_gateway_secret_name: str | None,
    ai_gateway_key: str | None,
) -> dict[str, Any]:
    sts_name = naming.statefulset_name(name)
    image = template.image.reference()

    annotations = {
        # Hermes reads its managed files at start, so restart it when any of
        # them changes.
        "checksum/managed-config": _checksum(configmap["data"]),
        "checksum/managed-env": _checksum(secret["stringData"]),
        "checksum/template": _checksum(rendered_template(template)),
    }
    if ai_gateway_key is not None:
        annotations["checksum/ai-gateway-key"] = _checksum(ai_gateway_key)

    env: list[dict[str, Any]] = [
        {"name": "HERMES_MANAGED_DIR", "value": MANAGED_DIR},
        {
            "name": "API_SERVER_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": naming.secret_name(name), "key": "API_SERVER_KEY"}
            },
        },
    ]
    if ai_gateway_secret_name is not None:
        # The shared AI Gateway key, read from the provisioner's shared Secret.
        # It is never copied into the agent's own Secret.
        env.append(
            {
                "name": AI_GATEWAY_KEY_ENV,
                "valueFrom": {
                    "secretKeyRef": {"name": ai_gateway_secret_name, "key": AI_GATEWAY_KEY_ENV}
                },
            }
        )

    seed_script = (
        f"cp /seed/SOUL.md {DATA_DIR}/SOUL.md\n"
        f"touch {DATA_DIR}/.no-bundled-skills\n"
    )

    pod_spec: dict[str, Any] = {
        # The agent has no business with the Kubernetes API.
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {
            "fsGroup": 10000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "initContainers": [
            {
                "name": "seed",
                "image": image,
                "imagePullPolicy": template.image.pull_policy,
                "command": ["/bin/sh", "-ec"],
                "args": [seed_script],
                "securityContext": {
                    "runAsUser": 10000,
                    "runAsGroup": 10000,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "data", "mountPath": DATA_DIR},
                    {"name": "seed", "mountPath": "/seed", "readOnly": True},
                ],
            }
        ],
        "containers": [
            {
                "name": "hermes",
                "image": image,
                "imagePullPolicy": template.image.pull_policy,
                # The image's default command is the interactive CLI.
                "args": ["gateway", "run"],
                "env": env,
                "securityContext": {
                    # s6 starts as root to fix the data volume ownership, then
                    # runs Hermes as UID 10000.
                    "runAsUser": 0,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {
                        "drop": ["ALL"],
                        # KILL lets s6 stop the gateway it runs as another user.
                        "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"],
                    },
                },
                # s6 keeps the container running when the gateway exits for
                # good, so probe the gateway itself, on loopback.
                "startupProbe": {
                    "exec": {"command": _health_command()},
                    "periodSeconds": 10,
                    "failureThreshold": 30,
                },
                "livenessProbe": {
                    "exec": {"command": _health_command()},
                    "periodSeconds": 30,
                    "timeoutSeconds": 5,
                    "failureThreshold": 3,
                },
                "volumeMounts": [
                    {"name": "data", "mountPath": DATA_DIR},
                    {"name": "managed", "mountPath": MANAGED_DIR, "readOnly": True},
                ],
            }
        ],
        "volumes": [
            {
                "name": "managed",
                "projected": {
                    # Root-owned, readable by the hermes group through fsGroup.
                    "defaultMode": 0o440,
                    "sources": [
                        {
                            "configMap": {
                                "name": naming.configmap_name(name),
                                "items": [{"key": "config.yaml", "path": "config.yaml"}],
                            }
                        },
                        {
                            "secret": {
                                "name": naming.secret_name(name),
                                "items": [{"key": ".env", "path": ".env"}],
                            }
                        },
                    ],
                },
            },
            {
                "name": "seed",
                "configMap": {
                    "name": naming.configmap_name(name),
                    "items": [{"key": "SOUL.md", "path": "SOUL.md"}],
                },
            },
        ],
    }

    if template.resources:
        pod_spec["containers"][0]["resources"] = template.resources

    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": _metadata(sts_name, name, owner),
        "spec": {
            # One gateway per data volume.
            "replicas": 1,
            "serviceName": sts_name,
            "selector": {"matchLabels": {"app.kubernetes.io/instance": sts_name}},
            "template": {
                "metadata": {
                    "labels": _labels(name),
                    "annotations": annotations,
                },
                "spec": pod_spec,
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        **(
                            {"storageClassName": template.persistence.storage_class}
                            if template.persistence.storage_class
                            else {}
                        ),
                        "resources": {
                            "requests": {"storage": template.persistence.size}
                        },
                    },
                }
            ],
            # The volume claim goes with the StatefulSet. `whenScaled` is
            # pinned to Retain: the reclaim on scale-down is a separate
            # question, and the default is not to be relied on.
            "persistentVolumeClaimRetentionPolicy": {
                "whenDeleted": "Delete",
                "whenScaled": "Retain",
            },
        },
    }


def _health_command() -> list[str]:
    return ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:8642/health"]


def rendered_template(template: Template) -> dict[str, Any]:
    """The template's own shape, for the template checksum.

    Only the fields the pod's shape depends on: the image, the resources, the
    storage and the managed configuration. A change to the persona or the
    welcome text rolls the pod through the ConfigMap checksum instead.
    """
    return {
        "image": template.image.reference(),
        "resources": template.resources,
        "persistence": {
            "size": template.persistence.size,
            "storageClass": template.persistence.storage_class,
        },
        "managedConfig": template.managed_config,
    }
