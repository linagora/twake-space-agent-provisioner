"""The derived names the whole design hangs on.

The resource name is derived from the username, and every other name derives
from it, using the same pattern as the pilot agent deployed by hand. The
operator refuses a resource whose name is not the derived one, so two users
never share an agent.
"""

import pytest

from twake_space_agent_provisioner import naming


class TestDerivedName:
    def test_lowercases_the_username(self) -> None:
        assert naming.derived_name("MMaudet") == "mmaudet"

    def test_replaces_dots_with_hyphens(self) -> None:
        assert naming.derived_name("jean.dupont") == "jean-dupont"

    def test_keeps_digits_and_hyphenless_names(self) -> None:
        assert naming.derived_name("jean2dupont") == "jean2dupont"


class TestNameMatchesUsername:
    def test_accepts_the_derived_name(self) -> None:
        assert naming.name_matches_username("jean-dupont", "jean.dupont")

    def test_accepts_a_name_that_is_already_the_username(self) -> None:
        assert naming.name_matches_username("mmaudet", "mmaudet")

    def test_refuses_a_hand_picked_name(self) -> None:
        assert not naming.name_matches_username("my-agent", "jean.dupont")

    def test_refuses_a_name_that_differs_by_case(self) -> None:
        assert not naming.name_matches_username("MMaudet", "MMaudet")


class TestValidUsername:
    @pytest.mark.parametrize("username", ["abc", "mmaudet", "jean.dupont", "a1b2c3"])
    def test_accepts_common_settings_usernames(self, username: str) -> None:
        assert naming.is_valid_username(username)

    @pytest.mark.parametrize(
        "username",
        [
            "",  # empty
            "ab",  # too short
            "a" * 31,  # too long
            "jean..dupont",  # double inner dot
            ".jean",  # leading dot
            "jean.",  # trailing dot
            "jean-dupont",  # hyphen is not a common settings username char
            "jean_dupont",  # underscore is not one either
            "123456",  # digits only
        ],
    )
    def test_refuses_bad_usernames(self, username: str) -> None:
        assert not naming.is_valid_username(username)


class TestDerivedNames:
    def test_statefulset_uses_the_pilot_pattern(self) -> None:
        assert naming.statefulset_name("jean-dupont") == "hermes-user-jean-dupont"

    def test_volume_claim_uses_the_pilot_pattern(self) -> None:
        assert naming.volume_claim_name("jean-dupont") == "data-hermes-user-jean-dupont-0"

    def test_bot_id_is_namespaced_by_the_server_name(self) -> None:
        assert (
            naming.bot_user_id("jean-dupont", "dev.twake.lin-saas.com")
            == "@twake-space-assistant-jean-dupont:dev.twake.lin-saas.com"
        )

    def test_device_id_is_hermes_plus_the_uppercased_name(self) -> None:
        assert naming.device_id("jean-dupont") == "HERMESJEANDUPONT"

    def test_owner_id_is_the_lowercased_username(self) -> None:
        assert (
            naming.owner_id("Jean.Dupont", "dev.twake.lin-saas.com")
            == "@jean.dupont:dev.twake.lin-saas.com"
        )


class TestObjectNames:
    def test_configmap_and_secret_derive_from_the_name(self) -> None:
        assert naming.configmap_name("jean-dupont") == "hermes-user-jean-dupont-managed"
        assert naming.secret_name("jean-dupont") == "hermes-user-jean-dupont-managed-env"
