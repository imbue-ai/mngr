import pytest
from pydantic import Field

from imbue.app_sdk.errors import InvalidIdentityValueError
from imbue.app_sdk.frozen_model import FrozenModel
from imbue.app_sdk.primitives import OWNER_PARTICIPANT_KEY
from imbue.app_sdk.primitives import ParticipantKey
from imbue.app_sdk.primitives import UserId


class _Attribution(FrozenModel):
    user_id: UserId = Field(description="Who did it")
    participant: ParticipantKey = Field(description="Whose it is")


@pytest.mark.parametrize("value", ["user-bob-4471", "A", "a.b_c-9"])
def test_a_filename_safe_user_id_is_kept_as_given(value: str) -> None:
    assert UserId(value) == value


@pytest.mark.parametrize("value", ["", " bob", "../etc", "bob/1", "-leading-hyphen", "a" * 129])
def test_a_user_id_that_could_not_name_a_file_is_refused(value: str) -> None:
    with pytest.raises(InvalidIdentityValueError):
        UserId(value)


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "workspace-agent",
        "user:user-bob-4471",
        "email:bob@example.com",
        "agent:va-0123456789abcdef",
        "name:Bob from next door",
    ],
)
def test_every_form_of_a_participant_key_is_accepted(value: str) -> None:
    assert ParticipantKey(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "bob", "user:", "user:../x", "email:bob", "email:b/o@example.com", "agent:", "name:", "name:a/b", "owner "],
)
def test_a_participant_key_of_no_known_form_is_refused(value: str) -> None:
    with pytest.raises(InvalidIdentityValueError):
        ParticipantKey(value)


def test_a_name_key_is_trimmed_of_the_space_a_typed_name_ends_with() -> None:
    assert ParticipantKey("name:Bob ") == "name:Bob"


def test_the_owner_key_is_the_one_an_unshared_workspace_keys_everything_by() -> None:
    assert OWNER_PARTICIPANT_KEY == "owner"


def test_a_model_validates_the_primitives_it_declares() -> None:
    attribution = _Attribution.model_validate({"user_id": "user-bob-4471", "participant": "user:user-bob-4471"})
    assert type(attribution.user_id) is UserId
    assert type(attribution.participant) is ParticipantKey
    with pytest.raises(ValueError, match="invalid user id"):
        _Attribution.model_validate({"user_id": "bob/1", "participant": "owner"})
