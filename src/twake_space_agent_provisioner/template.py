"""The agent template, read from a mounted file.

The template is part of the provisioner release's configuration in the
deployment repository. It holds the Hermes image (by digest), the resources,
the volume size and the storage class, Hermes' managed configuration, the
persona and the welcome text in each language, the homeserver URL and server
name, and the agent ceiling.

It starts from the pilot personal agent's configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Image:
    repository: str
    tag: str
    digest: str = ""
    pull_policy: str = "IfNotPresent"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Image:
        return cls(
            repository=data["repository"],
            tag=data["tag"],
            digest=data.get("digest") or "",
            pull_policy=data.get("pullPolicy", "IfNotPresent"),
        )

    def reference(self) -> str:
        """`repo:tag`, and `repo:tag@digest` when a digest is pinned."""
        ref = f"{self.repository}:{self.tag}"
        return f"{ref}@{self.digest}" if self.digest else ref


@dataclass(frozen=True)
class Persistence:
    size: str
    storage_class: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Persistence:
        return cls(size=data["size"], storage_class=data.get("storageClass") or None)


@dataclass(frozen=True)
class Homeserver:
    url: str
    server_name: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Homeserver:
        return cls(url=data["url"], server_name=data["serverName"])


@dataclass(frozen=True)
class Template:
    image: Image
    resources: dict[str, Any]
    persistence: Persistence
    homeserver: Homeserver
    ceiling: int
    default_language: str
    managed_config: dict[str, Any]
    persona_texts: dict[str, str] = field(default_factory=dict)
    welcome_texts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Template:
        data = yaml.safe_load(Path(path).read_text())
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Template:
        return cls(
            image=Image.from_mapping(data["image"]),
            resources=data.get("resources") or {},
            persistence=Persistence.from_mapping(data["persistence"]),
            homeserver=Homeserver.from_mapping(data["homeserver"]),
            ceiling=int(data["ceiling"]),
            default_language=data["defaultLanguage"],
            managed_config=data.get("managedConfig") or {},
            persona_texts=data.get("persona") or {},
            welcome_texts=data.get("welcome") or {},
        )

    def _text(self, texts: dict[str, str], language: str | None, owner_name: str) -> str:
        """The template's text in the owner's language, or the default one.

        The text is filled with the owner's name; the caller resolves which of
        their first name, display name or username that is.
        """
        text = texts.get(language or "") or texts.get(self.default_language) or ""
        return render(text, owner_name)

    def persona(self, language: str | None, owner: str) -> str:
        return self._text(self.persona_texts, language, owner)

    def welcome(self, language: str | None, owner: str) -> str:
        return self._text(self.welcome_texts, language, owner)


def owner_name(first_name: str | None, display_name: str | None, username: str) -> str:
    """The owner's first name, or else their display name, or else their username.

    An empty string counts as absent, since the profile fields are optional.
    """
    return first_name or display_name or username


def render(text: str, name: str) -> str:
    return text.replace("{name}", name)
