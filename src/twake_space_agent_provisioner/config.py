"""The provisioner's own configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NAMESPACE = "twake-space"
DEFAULT_TEMPLATE_PATH = "/etc/provisioner/template.yaml"


@dataclass(frozen=True)
class Config:
    namespace: str
    template_path: Path
    ai_gateway_secret_name: str | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        return cls(
            namespace=env.get("PROVISIONER_NAMESPACE", DEFAULT_NAMESPACE),
            template_path=Path(env.get("AGENT_TEMPLATE_PATH", DEFAULT_TEMPLATE_PATH)),
            ai_gateway_secret_name=env.get("AI_GATEWAY_SECRET_NAME") or None,
        )
