"""The seam 2 test bench: `PersonalAgent` -> running agent.

A kind cluster, the operator running inside the test process, and a stub agent
image that answers the health probe. Tests check the Kubernetes objects through
the API, the same way an administrator would with kubectl.

This is the bench every later operator ticket builds on.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.seam2.fake_homeserver import FakeHomeserver
from tests.seam2.harness import OperatorHarness

REPO_ROOT = Path(__file__).resolve().parents[2]
CRD_PATH = REPO_ROOT / "deploy" / "crd.yaml"
STUB_DIR = REPO_ROOT / "tests" / "stub"

STUB_IMAGE = "twake-space-agent-stub:test"
#: The appservice token both the operator and the fake homeserver agree on.
APPSERVICE_TOKEN = "test-appservice-token"
#: An interval short enough that the suite stays quick. Set before the operator
#: module is imported, since the timer's interval is read then.
RECONCILE_INTERVAL = "1"
os.environ.setdefault("PROVISIONER_RECONCILE_INTERVAL", RECONCILE_INTERVAL)


def _kubectl(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(scope="session")
def homeserver_server() -> Iterator[FakeHomeserver]:
    """The fake homeserver: the only fake, standing for Synapse.

    Session-scoped so the port is stable for every template that points at it.
    Tests take `homeserver`, which resets this between them.
    """
    server = FakeHomeserver(appservice_token=APPSERVICE_TOKEN).start()
    yield server
    server.stop()


@pytest.fixture
def homeserver(homeserver_server: FakeHomeserver) -> Iterator[FakeHomeserver]:
    """The fake homeserver, emptied for this test.

    Its recorded calls must start empty: a test asserting "one login" would
    otherwise pass or fail on the order pytest ran in.
    """
    homeserver_server.reset()
    yield homeserver_server


@pytest.fixture(scope="session")
def cluster_context() -> str:
    """The kind cluster the bench runs against.

    Set ``PROVISIONER_TEST_CONTEXT`` to reuse one; otherwise the bench uses the
    current context.
    """
    context = os.environ.get("PROVISIONER_TEST_CONTEXT")
    if context:
        return context
    result = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("no Kubernetes context: run the seam 2 bench against kind")
    return result.stdout.strip()


@pytest.fixture(scope="session")
def stub_image(cluster_context: str) -> str:
    """Build the stub agent image and load it into the kind cluster.

    The image answers the health probe, so pods become Ready without Hermes.
    """
    subprocess.run(
        ["docker", "build", "-t", STUB_IMAGE, str(STUB_DIR)],
        check=True,
        capture_output=True,
    )
    cluster_name = cluster_context.removeprefix("kind-")
    subprocess.run(
        ["kind", "load", "docker-image", STUB_IMAGE, "--name", cluster_name],
        check=True,
        capture_output=True,
    )
    return STUB_IMAGE


@pytest.fixture(scope="session")
def cluster_ready(cluster_context: str, stub_image: str) -> Iterator[None]:
    """Install the CRD once per session."""
    _kubectl("--context", cluster_context, "apply", "-f", str(CRD_PATH))
    yield
    # Leave the CRD in place: its keep policy is part of the design.


@pytest.fixture
def namespace(cluster_context: str, cluster_ready: None) -> Iterator[str]:
    """An empty namespace per test, so tests never see each other's objects."""
    name = f"test-{int(time.time() * 1000) % 10_000_000}"
    subprocess.run(
        ["kubectl", "--context", cluster_context, "create", "namespace", name],
        capture_output=True,
        text=True,
        check=True,
    )
    yield name

    # kopf adds a finalizer to a PersonalAgent it handles with a timer. The
    # erase path that removes it is a later slice, so strip it here: otherwise
    # the namespace hangs in Terminating once the operator has stopped.
    for agent in _personalagent_names(cluster_context, name):
        subprocess.run(
            [
                "kubectl", "--context", cluster_context, "-n", name,
                "patch", "personalagent", agent,
                "--type=merge", "-p", '{"metadata":{"finalizers":null}}',
            ],
            capture_output=True,
            text=True,
        )
    subprocess.run(
        [
            "kubectl", "--context", cluster_context, "delete", "namespace", name,
            "--wait=false",
        ],
        capture_output=True,
        text=True,
    )


def _personalagent_names(context: str, namespace: str) -> list[str]:
    proc = subprocess.run(
        [
            "kubectl", "--context", context, "-n", namespace,
            "get", "personalagent", "-o", "name",
        ],
        capture_output=True,
        text=True,
    )
    return [line.removeprefix("personalagent.twake-space.linagora.com/")
            for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture
def template_file(
    tmp_path: Path, stub_image: str, homeserver_server: FakeHomeserver
) -> Path:
    """The agent template, pointed at the stub image and the fake homeserver."""
    path = tmp_path / "template.yaml"
    path.write_text(
        f"""
image:
  repository: "{stub_image.split(':')[0]}"
  tag: "{stub_image.split(':')[1]}"
  pullPolicy: IfNotPresent
resources:
  requests: {{cpu: 10m, memory: 16Mi}}
  limits: {{cpu: 100m, memory: 64Mi}}
persistence:
  size: 10Mi
  storageClass: ""
homeserver:
  url: {homeserver_server.url}
  serverName: {homeserver_server.server_name}
ceiling: 50
defaultLanguage: fr
managedConfig:
  model:
    provider: custom
    default: qwen3.8
    base_url: https://ai-api.test.invalid/v1
    key_env: LINAGORA_API_KEY
  display:
    show_reasoning: false
persona:
  fr: "Tu es l'assistant personnel de {{name}}."
  en: "You are {{name}}'s personal assistant."
welcome:
  fr: "Bonjour {{name}}."
  en: "Hello {{name}}."
"""
    )
    return path


@pytest.fixture
def operator(
    namespace: str, template_file: Path, cluster_context: str
) -> Iterator[OperatorHarness]:
    """The operator, running inside this test process against the real cluster.

    kopf's documented way: KopfRunner starts the operator in a parallel thread
    on the real API, so the tests drive the same code path a deployed
    provisioner does.
    """
    harness = OperatorHarness(
        namespace=namespace,
        template_path=template_file,
        context=cluster_context,
        reconcile_interval=RECONCILE_INTERVAL,
        appservice_token=APPSERVICE_TOKEN,
    )
    with harness:
        yield harness


@pytest.fixture
def operator_with_bad_token(
    namespace: str, template_file: Path, cluster_context: str
) -> Iterator[OperatorHarness]:
    """The operator holding an appservice token the homeserver rejects.

    A permanent refusal: the agent must fail rather than retry forever.
    """
    harness = OperatorHarness(
        namespace=namespace,
        template_path=template_file,
        context=cluster_context,
        reconcile_interval=RECONCILE_INTERVAL,
        appservice_token="the-wrong-token",
    )
    with harness:
        yield harness


@pytest.fixture
def operator_with_ai_key(
    namespace: str, template_file: Path, cluster_context: str
) -> Iterator[OperatorHarness]:
    """The operator, configured with the provisioner's shared AI key Secret."""
    shared_secret = "provisioner-shared"
    subprocess.run(
        [
            "kubectl", "--context", cluster_context, "-n", namespace,
            "create", "secret", "generic", shared_secret,
            "--from-literal=LINAGORA_API_KEY=shared-ai-key-value",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    harness = OperatorHarness(
        namespace=namespace,
        template_path=template_file,
        context=cluster_context,
        reconcile_interval=RECONCILE_INTERVAL,
        ai_gateway_secret_name=shared_secret,
        appservice_token=APPSERVICE_TOKEN,
    )
    with harness:
        yield harness
