"""Driving the operator inside the test process, against the real cluster.

kopf's documented way: ``KopfRunner`` runs the operator in a parallel thread in
this process, so a test can create resources through the API and watch the
operator act on them. No test reaches into the operator, and the only fake is
the stub agent image.

The harness also gives the tests the reads an administrator would make with
kubectl.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from kopf.testing import KopfRunner

#: The interval the operator reconciles on, in tests. Short, so a test settles
#: in seconds.
TEST_RECONCILE_INTERVAL = "1"


def _kubectl(
    *args: str, context: str, namespace: str | None = None
) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", context]
    if namespace:
        cmd += ["-n", namespace]
    return subprocess.run([*cmd, *args], capture_output=True, text=True)


class Kubectl:
    """Reads and writes through kubectl, as an administrator would."""

    def __init__(self, context: str, namespace: str) -> None:
        self.context = context
        self.namespace = namespace

    def create(self, manifest: dict[str, Any]) -> None:
        proc = subprocess.run(
            [
                "kubectl", "--context", self.context, "-n", self.namespace,
                "create", "-f", "-",
            ],
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise RuntimeError(f"create failed: {proc.stderr}")

    def get(self, kind: str, name: str) -> dict[str, Any] | None:
        proc = _kubectl(
            "get", kind, name, "-o", "json", context=self.context, namespace=self.namespace
        )
        return json.loads(proc.stdout) if proc.returncode == 0 else None

    def patch(self, kind: str, name: str, patch: dict[str, Any]) -> None:
        """Merge-patch an object, the way an administrator would edit it."""
        proc = subprocess.run(
            [
                "kubectl", "--context", self.context, "-n", self.namespace,
                "patch", kind, name, "--type=merge", "-p", json.dumps(patch),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"patch failed: {proc.stderr}")

    def list(self, kind: str, selector: str | None = None) -> list[dict[str, Any]]:
        args = ["get", kind, "-o", "json"]
        if selector:
            args += ["-l", selector]
        proc = _kubectl(*args, context=self.context, namespace=self.namespace)
        return json.loads(proc.stdout).get("items", []) if proc.returncode == 0 else []

    def events(self, name: str) -> list[dict[str, Any]]:
        proc = _kubectl(
            "get", "events", "-o", "json",
            "--field-selector", f"involvedObject.name={name}",
            context=self.context, namespace=self.namespace,
        )
        return json.loads(proc.stdout).get("items", []) if proc.returncode == 0 else []


class OperatorHarness:
    """The operator, running against the test's namespace."""

    def __init__(
        self,
        *,
        namespace: str,
        template_path: Path,
        context: str,
        reconcile_interval: str = TEST_RECONCILE_INTERVAL,
        ai_gateway_secret_name: str | None = None,
    ) -> None:
        self.namespace = namespace
        self.template_path = template_path
        self.context = context
        self.reconcile_interval = reconcile_interval
        self.ai_gateway_secret_name = ai_gateway_secret_name
        self.kubectl = Kubectl(context=context, namespace=namespace)
        self._runner: KopfRunner | None = None
        self._captured_output = ""

    def __enter__(self) -> OperatorHarness:
        # Read by the operator's startup handler, at run time.
        os.environ["AGENT_TEMPLATE_PATH"] = str(self.template_path)
        os.environ["PROVISIONER_NAMESPACE"] = self.namespace
        if self.ai_gateway_secret_name:
            os.environ["AI_GATEWAY_SECRET_NAME"] = self.ai_gateway_secret_name
        # Read when the operator module is imported, hence once per process.
        os.environ["PROVISIONER_RECONCILE_INTERVAL"] = self.reconcile_interval
        self._runner = KopfRunner(
            [
                "run",
                "--namespace", self.namespace,
                "--standalone",
                "-m", "twake_space_agent_provisioner.operator",
            ],
            timeout=30,
        )
        self._runner.__enter__()
        return self

    def __exit__(self, exc_type=None, exc_val=None, exc_tb=None) -> None:
        self.stop()
        for key in ("AGENT_TEMPLATE_PATH", "PROVISIONER_NAMESPACE", "AI_GATEWAY_SECRET_NAME"):
            os.environ.pop(key, None)

    def stop(self) -> None:
        """Stop the operator and keep its captured output.

        ``KopfRunner.output`` waits for the operator to finish, so it must not
        be read while the run is in progress. Idempotent: the fixture's
        teardown calls it again.
        """
        if self._runner is not None:
            runner, self._runner = self._runner, None
            runner.__exit__(None, None, None)
            self._captured_output = runner.output

    @property
    def output(self) -> str:
        """The operator's captured output, empty until it has stopped."""
        if self._runner is not None:
            return "(operator still running; call stop() to read its output)"
        return self._captured_output

    def wait_for(
        self,
        predicate,
        *,
        timeout: float = 60.0,
        interval: float = 0.4,
        message: str = "condition not met",
    ):
        """Poll until the predicate returns something truthy, or fail."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(interval)
        raise AssertionError(
            f"{message} after {timeout}s; operator output:\n{self.output}"
        )

    def __del__(self) -> None:
        # A test that fails mid-run must not leave the operator thread alive.
        self.stop()
