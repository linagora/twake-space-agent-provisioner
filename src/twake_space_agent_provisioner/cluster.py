"""Thin access to the Kubernetes API.

Every call the operator makes goes through here, so the seam 2 tests drive the
same path the operator does, and so the object kinds the operator touches are
listed in one place.
"""

from __future__ import annotations

from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException


class Cluster:
    """The API client the operator uses for its own object writes.

    kopf holds a client of its own for the resources it watches and patches;
    this one is for the objects the operator creates and reads.
    """

    @classmethod
    async def connect(cls) -> Cluster:
        """Load kubeconfig: in-cluster first, then the local file."""
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        # The configuration the loader just resolved, not an empty one: this is
        # what points the client at the cluster rather than at localhost.
        return cls(client.Configuration.get_default_copy())

    def __init__(self, configuration: client.Configuration) -> None:
        self._api = client.ApiClient(configuration)
        self.core = client.CoreV1Api(self._api)
        self.apps = client.AppsV1Api(self._api)
        self.networking = client.NetworkingV1Api(self._api)
        self.custom = client.CustomObjectsApi(self._api)

    async def close(self) -> None:
        await self._api.close()

    # -- reads -------------------------------------------------------------

    async def get_secret(self, namespace: str, name: str) -> dict[str, Any] | None:
        try:
            secret = await self.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return self._to_dict(secret)

    async def list_pods(self, namespace: str, label_selector: str) -> list[dict[str, Any]]:
        pods = await self.core.list_namespaced_pod(namespace, label_selector=label_selector)
        return [self._to_dict(p) for p in pods.items]

    # -- creates and updates -----------------------------------------------

    async def apply(self, obj: dict[str, Any], namespace: str) -> None:
        """Create the object, or leave an existing one in place.

        Used for the Secret, whose generated key must survive a reconcile: a
        rewrite would roll the pod on every pass.
        """
        kind = obj["kind"]
        try:
            await self._create(kind, namespace, obj)
        except ApiException as exc:
            if exc.status == 409:  # already exists
                return
            raise

    async def apply_updatable(self, obj: dict[str, Any], namespace: str) -> None:
        """Create the object, or replace it so a change reaches the cluster.

        Used for the ConfigMap, the NetworkPolicy and the StatefulSet, which
        carry no generated state. Replacing the StatefulSet is what makes its
        checksum annotations roll the pod when the configuration or the
        template changes.
        """
        kind = obj["kind"]
        name = obj["metadata"]["name"]
        try:
            await self._create(kind, namespace, obj)
        except ApiException as exc:
            if exc.status != 409:
                raise
            await self._replace(kind, namespace, name, obj)

    async def _create(self, kind: str, namespace: str, obj: dict[str, Any]) -> None:
        if kind == "ConfigMap":
            await self.core.create_namespaced_config_map(namespace, obj)
        elif kind == "Secret":
            await self.core.create_namespaced_secret(namespace, obj)
        elif kind == "NetworkPolicy":
            await self.networking.create_namespaced_network_policy(namespace, obj)
        elif kind == "StatefulSet":
            await self.apps.create_namespaced_stateful_set(namespace, obj)
        else:
            raise ValueError(f"unsupported kind: {kind}")

    async def _replace(self, kind: str, namespace: str, name: str, obj: dict[str, Any]) -> None:
        if kind == "ConfigMap":
            await self.core.replace_namespaced_config_map(name, namespace, obj)
        elif kind == "Secret":
            await self.core.replace_namespaced_secret(name, namespace, obj)
        elif kind == "NetworkPolicy":
            await self.networking.replace_namespaced_network_policy(name, namespace, obj)
        elif kind == "StatefulSet":
            await self.apps.replace_namespaced_stateful_set(name, namespace, obj)
        else:
            raise ValueError(f"unsupported kind: {kind}")

    @staticmethod
    def _to_dict(model: Any) -> dict[str, Any]:
        return model.to_dict()
