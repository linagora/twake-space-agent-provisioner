"""Reading the agent template and rendering it for one owner.

The template is versioned in the deployment repository and mounted into the
provisioner. It holds the image, the resources, the storage, Hermes' managed
configuration, the persona and the welcome text per language, the homeserver
details, and the agent ceiling.
"""

import textwrap
from pathlib import Path

import pytest

from twake_space_agent_provisioner.template import Template, owner_name

TEMPLATE_YAML = textwrap.dedent(
    """
    image:
      repository: docker.io/nousresearch/hermes-agent
      tag: v2026.9.24
      digest: sha256:abc123
      pullPolicy: IfNotPresent
    resources:
      requests:
        cpu: 250m
        memory: 1Gi
      limits:
        cpu: "2"
        memory: 3Gi
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
    persona:
      fr: "Tu es l'assistant personnel de {name}."
      en: "You are {name}'s personal assistant."
    welcome:
      fr: "Bonjour {name}, je suis votre assistant."
      en: "Hello {name}, I am your assistant."
    """
)


@pytest.fixture
def template(tmp_path: Path) -> Template:
    path = tmp_path / "template.yaml"
    path.write_text(TEMPLATE_YAML)
    return Template.load(path)


class TestLoad:
    def test_loads_the_image_with_the_digest(self, template: Template) -> None:
        assert template.image.reference() == (
            "docker.io/nousresearch/hermes-agent:v2026.9.24@sha256:abc123"
        )

    def test_loads_the_storage_and_the_homeserver(self, template: Template) -> None:
        assert template.persistence.size == "5Gi"
        assert template.persistence.storage_class is None
        assert template.homeserver.url == "https://matrix.dev.twake.lin-saas.com"
        assert template.homeserver.server_name == "dev.twake.lin-saas.com"

    def test_loads_the_ceiling_and_the_default_language(self, template: Template) -> None:
        assert template.ceiling == 50
        assert template.default_language == "fr"

    def test_keeps_the_managed_config_verbatim(self, template: Template) -> None:
        assert template.managed_config["model"]["default"] == "qwen3.8"

    def test_a_missing_digest_leaves_the_tag_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "template.yaml"
        path.write_text(TEMPLATE_YAML.replace("digest: sha256:abc123", 'digest: ""'))
        assert Template.load(path).image.reference() == (
            "docker.io/nousresearch/hermes-agent:v2026.9.24"
        )


class TestPersona:
    def test_uses_the_owners_language(self, template: Template) -> None:
        assert template.persona("en", "Michel") == "You are Michel's personal assistant."

    def test_falls_back_to_the_default_language(self, template: Template) -> None:
        assert template.persona("de", "Michel") == "Tu es l'assistant personnel de Michel."

    def test_falls_back_when_the_language_is_absent(self, template: Template) -> None:
        assert template.persona(None, "Michel") == "Tu es l'assistant personnel de Michel."


class TestWelcome:
    def test_renders_in_the_owners_language(self, template: Template) -> None:
        assert template.welcome("en", "Michel") == "Hello Michel, I am your assistant."

    def test_falls_back_to_the_default_language(self, template: Template) -> None:
        assert template.welcome("pt", "Michel") == "Bonjour Michel, je suis votre assistant."


class TestOwnerName:
    def test_prefers_the_first_name(self) -> None:
        assert owner_name("Michel", "M. M.", "mmaudet") == "Michel"

    def test_falls_back_to_the_display_name(self) -> None:
        assert owner_name(None, "M. M.", "mmaudet") == "M. M."

    def test_falls_back_to_the_username(self) -> None:
        assert owner_name(None, None, "mmaudet") == "mmaudet"

    def test_treats_an_empty_first_name_as_absent(self) -> None:
        assert owner_name("", "", "mmaudet") == "mmaudet"
