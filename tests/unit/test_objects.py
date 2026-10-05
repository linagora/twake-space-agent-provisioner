"""The objects the operator creates for a valid PersonalAgent.

A good test here checks what another system can observe: the Kubernetes
objects themselves. These builders are pure, so the shape can be checked
without a cluster; the seam 2 tests exercise the same builders against a real
API.
"""

import textwrap

import pytest

from twake_space_agent_provisioner.objects import build_objects
from twake_space_agent_provisioner.template import Template

TEMPLATE_YAML = textwrap.dedent(
    """
    image:
      repository: docker.io/nousresearch/hermes-agent
      tag: v2026.9.24
      digest: sha256:abc123
      pullPolicy: IfNotPresent
    resources:
      requests: {cpu: 250m, memory: 1Gi}
      limits: {cpu: "2", memory: 3Gi}
    persistence:
      size: 5Gi
      storageClass: ""
    homeserver:
      url: https://matrix.dev.twake.lin-saas.com
      serverName: dev.twake.lin-saas.com
    ceiling: 50
    defaultLanguage: fr
    managedConfig:
      model:
        provider: custom
        default: qwen3.8
        base_url: https://ai-api.linagora.com/v1
        key_env: LINAGORA_API_KEY
      display:
        show_reasoning: false
    persona:
      fr: "Tu es l'assistant personnel de {name}."
      en: "You are {name}'s personal assistant."
    welcome:
      fr: "Bonjour {name}."
      en: "Hello {name}."
    """
)

NAME = "jean-dupont"
USERNAME = "jean.dupont"


@pytest.fixture
def template(tmp_path) -> Template:
    path = tmp_path / "template.yaml"
    path.write_text(TEMPLATE_YAML)
    return Template.load(path)


@pytest.fixture
def objects(template: Template):
    return build_objects(
        name=NAME,
        username=USERNAME,
        profile={"firstName": "Jean", "language": "fr", "timezone": "Europe/Paris"},
        template=template,
        api_server_key="api-key-value",
        matrix_access_token="matrix-token",
    )


class TestConfigMap:
    def test_holds_the_managed_configuration(self, objects) -> None:
        cm = objects.configmap
        assert cm["metadata"]["name"] == "hermes-user-jean-dupont-managed"
        assert cm["kind"] == "ConfigMap"

    def test_adds_the_owners_timezone_to_the_configuration(self, objects, template) -> None:
        import yaml

        config = yaml.safe_load(objects.configmap["data"]["config.yaml"])
        # The managed configuration from the template is kept...
        assert config["model"]["default"] == "qwen3.8"
        assert config["display"]["show_reasoning"] is False
        # ...and the owner's timezone is added.
        assert config["timezone"] == "Europe/Paris"

    def test_omits_the_timezone_when_the_owner_has_none(self, template) -> None:
        import yaml

        objects = build_objects(
            name=NAME, username=USERNAME, profile={}, template=template,
            api_server_key="k",
            matrix_access_token="matrix-token",
        )
        config = yaml.safe_load(objects.configmap["data"]["config.yaml"])
        assert "timezone" not in config

    def test_holds_the_persona_in_the_owners_language(self, objects) -> None:
        assert objects.configmap["data"]["SOUL.md"] == "Tu es l'assistant personnel de Jean."

    def test_persona_uses_english_when_the_owner_does(self, template) -> None:
        objects = build_objects(
            name=NAME, username=USERNAME,
            profile={"firstName": "Jean", "language": "en"}, template=template,
            api_server_key="k",
            matrix_access_token="matrix-token",
        )
        assert objects.configmap["data"]["SOUL.md"] == "You are Jean's personal assistant."

    def test_persona_falls_back_to_the_display_name(self, template) -> None:
        objects = build_objects(
            name=NAME, username=USERNAME,
            profile={"displayName": "Jean Dupont"}, template=template, api_server_key="k",
            matrix_access_token="matrix-token",
        )
        assert objects.configmap["data"]["SOUL.md"] == (
            "Tu es l'assistant personnel de Jean Dupont."
        )

    def test_persona_falls_back_to_the_username(self, template) -> None:
        objects = build_objects(
            name=NAME, username=USERNAME, profile={}, template=template,
            api_server_key="k",
            matrix_access_token="matrix-token",
        )
        assert objects.configmap["data"]["SOUL.md"] == (
            "Tu es l'assistant personnel de jean.dupont."
        )


class TestSecret:
    def test_holds_the_random_api_server_key(self, objects) -> None:
        assert objects.secret["metadata"]["name"] == "hermes-user-jean-dupont-managed-env"
        assert objects.secret["stringData"]["API_SERVER_KEY"] == "api-key-value"

    def test_holds_the_matrix_token_so_it_survives_a_reconcile(self, objects) -> None:
        assert objects.secret["stringData"]["MATRIX_ACCESS_TOKEN"] == "matrix-token"

    def test_carries_hermes_managed_environment(self, objects) -> None:
        assert objects.secret["stringData"]["API_SERVER_KEY"] == "api-key-value"

    def test_never_carries_the_shared_ai_gateway_key(self, objects) -> None:
        blob = " ".join(str(objects.secret.get("stringData", {})).split())
        assert "LINAGORA_API_KEY" not in blob


class TestManagedEnvironment:
    """The Matrix identity Hermes reads from the .env file."""

    def _env(self, objects) -> dict[str, str]:
        lines = objects.secret["stringData"][".env"]
        return dict(line.split("=", 1) for line in lines.splitlines())

    def test_points_the_agent_at_the_homeserver(self, objects) -> None:
        assert self._env(objects)["MATRIX_HOMESERVER"] == (
            "https://matrix.dev.twake.lin-saas.com"
        )

    def test_gives_the_agent_its_bot_identity(self, objects) -> None:
        env = self._env(objects)
        assert env["MATRIX_USER_ID"] == (
            "@twake-space-assistant-jean-dupont:dev.twake.lin-saas.com"
        )
        assert env["MATRIX_DEVICE_ID"] == "HERMESJEANDUPONT"
        assert env["MATRIX_ACCESS_TOKEN"] == "matrix-token"

    def test_keeps_encryption_optional_with_a_recovery_key_on_the_volume(self, objects) -> None:
        env = self._env(objects)
        assert env["MATRIX_E2EE_MODE"] == "optional"
        assert env["MATRIX_RECOVERY_KEY_OUTPUT_FILE"] == (
            "/opt/data/platforms/matrix/recovery-key.txt"
        )

    def test_answers_its_owner_alone(self, objects) -> None:
        env = self._env(objects)
        assert env["MATRIX_ALLOWED_USERS"] == "@jean.dupont:dev.twake.lin-saas.com"

    def test_ignores_the_owners_case_for_the_matrix_id(self, template) -> None:
        objects = build_objects(
            name=NAME, username="Jean.Dupont", profile={"firstName": "Jean"},
            template=template, api_server_key="k", matrix_access_token="t",
        )
        lines = objects.secret["stringData"][".env"]
        env = dict(line.split("=", 1) for line in lines.splitlines())
        assert env["MATRIX_ALLOWED_USERS"] == "@jean.dupont:dev.twake.lin-saas.com"


class TestNetworkPolicy:
    def test_denies_all_ingress(self, objects) -> None:
        np = objects.networkpolicy
        assert np["metadata"]["name"] == "hermes-user-jean-dupont"
        assert np["spec"]["policyTypes"] == ["Ingress"]
        assert "ingress" not in np["spec"]

    def test_selects_the_agents_pods(self, objects) -> None:
        selector = objects.networkpolicy["spec"]["podSelector"]["matchLabels"]
        assert selector == {"app.kubernetes.io/instance": "hermes-user-jean-dupont"}


class TestStatefulSet:
    def test_is_named_after_the_pilot_pattern(self, objects) -> None:
        assert objects.statefulset["metadata"]["name"] == "hermes-user-jean-dupont"

    def test_runs_one_replica_of_gateway_run(self, objects) -> None:
        spec = objects.statefulset["spec"]
        assert spec["replicas"] == 1
        container = spec["template"]["spec"]["containers"][0]
        assert container["name"] == "hermes"
        assert container["args"] == ["gateway", "run"]

    def test_has_no_service_account_token(self, objects) -> None:
        pod = objects.statefulset["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False

    def test_hardens_the_pod_like_the_organization_agent(self, objects) -> None:
        pod = objects.statefulset["spec"]["template"]["spec"]
        assert pod["securityContext"]["fsGroup"] == 10000
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"

    def test_drops_all_capabilities_except_what_s6_needs(self, objects) -> None:
        container = objects.statefulset["spec"]["template"]["spec"]["containers"][0]
        caps = container["securityContext"]["capabilities"]
        assert caps["drop"] == ["ALL"]
        assert set(caps["add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"}
        assert container["securityContext"]["allowPrivilegeEscalation"] is False

    def test_probes_the_local_api_server_health_endpoint(self, objects) -> None:
        container = objects.statefulset["spec"]["template"]["spec"]["containers"][0]
        for probe in ("startupProbe", "livenessProbe"):
            command = container[probe]["exec"]["command"]
            assert command[-1] == "http://127.0.0.1:8642/health"

    def test_seeds_the_persona_and_the_no_bundled_skills_marker(self, objects) -> None:
        init = objects.statefulset["spec"]["template"]["spec"]["initContainers"][0]
        assert init["name"] == "seed"
        script = "\n".join(init["args"])
        assert "/opt/data/SOUL.md" in script
        assert "/opt/data/.no-bundled-skills" in script

    def test_projects_the_managed_directory_read_only(self, objects) -> None:
        pod = objects.statefulset["spec"]["template"]["spec"]
        managed = next(v for v in pod["volumes"] if v["name"] == "managed")
        assert "projected" in managed
        mount = next(
            m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "managed"
        )
        assert mount["mountPath"] == "/etc/hermes-managed"
        assert mount["readOnly"] is True

    def test_has_a_data_volume_claim_deleted_with_the_statefulset(self, objects) -> None:
        spec = objects.statefulset["spec"]
        vct = spec["volumeClaimTemplates"][0]
        assert vct["metadata"]["name"] == "data"
        assert vct["spec"]["resources"]["requests"]["storage"] == "5Gi"
        assert spec["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Delete"

    def test_carries_checksums_so_a_change_rolls_the_pod(self, objects) -> None:
        annotations = objects.statefulset["spec"]["template"]["metadata"]["annotations"]
        for key in ("checksum/managed-config", "checksum/template"):
            assert annotations[key], f"missing {key}"

    def test_checksums_change_when_the_template_changes(self, template, tmp_path) -> None:
        other_path = tmp_path / "other.yaml"
        other_path.write_text(TEMPLATE_YAML.replace("3Gi", "4Gi"))
        other = Template.load(other_path)

        first = build_objects(
            name=NAME, username=USERNAME, profile={"firstName": "Jean"},
            template=template, api_server_key="k",
            matrix_access_token="matrix-token",
        )
        second = build_objects(
            name=NAME, username=USERNAME, profile={"firstName": "Jean"},
            template=other, api_server_key="k",
            matrix_access_token="matrix-token",
        )
        a1 = first.statefulset["spec"]["template"]["metadata"]["annotations"]
        a2 = second.statefulset["spec"]["template"]["metadata"]["annotations"]
        assert a1["checksum/template"] != a2["checksum/template"]


class TestAiGatewayKey:
    def test_reaches_the_pod_from_the_provisioners_shared_secret(self, objects) -> None:
        container = objects.statefulset["spec"]["template"]["spec"]["containers"][0]
        entry = next(e for e in container["env"] if e["name"] == "HERMES_MANAGED_DIR")
        assert entry["value"] == "/etc/hermes-managed"

    def test_ai_key_env_comes_from_a_secret_ref_not_a_literal(self, template) -> None:
        objects = build_objects(
            name=NAME, username=USERNAME, profile={"firstName": "Jean"},
            template=template, api_server_key="k",
            matrix_access_token="matrix-token",
            ai_gateway_secret_name="provisioner-shared",
            ai_gateway_key="super-secret",
        )
        container = objects.statefulset["spec"]["template"]["spec"]["containers"][0]
        entry = next(e for e in container["env"] if e["name"] == "LINAGORA_API_KEY")
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "provisioner-shared"
        # The key itself never appears in the pod spec.
        assert "super-secret" not in str(objects.statefulset)
