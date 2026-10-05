"""Run the agent operator.

One process, one replica. It watches its own namespace only.
"""

from __future__ import annotations

import logging

import kopf

# Imported for its side effect: the decorators in the module register the
# handlers on kopf's default registry, which kopf.run then reads.
from . import operator  # noqa: F401
from .config import Config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    kopf.run(
        clusterwide=False,
        namespaces=[config.namespace],
        standalone=True,
        registry=kopf.get_default_registry(),
        liveness_endpoint="http://0.0.0.0:8080/healthz",
        memo={"context": None},
    )


if __name__ == "__main__":
    main()
