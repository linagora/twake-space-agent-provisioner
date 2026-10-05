"""Names derived from a username.

The resource name is derived from the username: lowercased, with dots replaced
by hyphens. Every other name derives from that resource name, using the same
pattern as the pilot personal agent deployed by hand, so the operator can take
the pilot over.

Common settings only accepts usernames of 3 to 30 letters, digits and single
inner dots, and not made only of digits. The derived name is therefore always a
valid DNS label.
"""

import re

#: Common settings' own rule for a username.
USERNAME_PATTERN = re.compile(r"^(?!\d+$)[a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)*$")

MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 30


def derived_name(username: str) -> str:
    """The resource name for a username: lowercased, dots replaced by hyphens."""
    return username.lower().replace(".", "-")


def is_valid_username(username: str) -> bool:
    """Whether common settings would accept this username."""
    if not MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH:
        return False
    return bool(USERNAME_PATTERN.match(username))


def name_matches_username(name: str, username: str) -> bool:
    """Whether a resource name is the one derived from its username.

    The operator refuses anything else, so two usernames that would map to the
    same agent name are refused rather than merged.
    """
    return name == derived_name(username)


def statefulset_name(name: str) -> str:
    return f"hermes-user-{name}"


def volume_claim_name(name: str) -> str:
    """The StatefulSet's `data` volume claim, as the pilot names it."""
    return f"data-hermes-user-{name}-0"


def configmap_name(name: str) -> str:
    return f"{statefulset_name(name)}-managed"


def secret_name(name: str) -> str:
    return f"{statefulset_name(name)}-managed-env"


def networkpolicy_name(name: str) -> str:
    return statefulset_name(name)


def bot_user_id(name: str, server_name: str) -> str:
    return f"@twake-space-assistant-{name}:{server_name}"


def device_id(name: str) -> str:
    """`HERMES` followed by the name uppercased without hyphens."""
    return "HERMES" + name.upper().replace("-", "")


def owner_id(username: str, server_name: str) -> str:
    """The owner's Matrix ID, matching how the homeserver maps OIDC sign-ins."""
    return f"@{username.lower()}:{server_name}"
