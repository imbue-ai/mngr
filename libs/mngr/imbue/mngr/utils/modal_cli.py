"""Typed reads of the JSON that the Modal CLI's list commands print.

`modal app list --json` and `modal volume list --json` emit snake_case keys.
Read as plain dicts, a key that is not there yields a default instead of an
error, so every caller that filters on it quietly finds nothing -- which is
indistinguishable from an account with nothing to reap. Going through these
models turns that into a raised `ModalCliOutputError`.

Callers decode the JSON themselves, so a garbled payload stays whatever each
one already treats it as; only a well-formed payload of the wrong shape lands
here.
"""

from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import ModalCliOutputError


class ModalAppListing(FrozenModel):
    """One app, as reported by `modal app list --json`."""

    # Modal prints more columns than we read; gaining another must not fail a sweep.
    model_config = ConfigDict(extra="ignore")

    app_id: str = Field(description="Modal's id for the app, e.g. 'ap-IeL5OYVwRdUiaADn3cPISA'")
    description: str = Field(description="The app's name, as given to Modal when the app was created")
    state: str = Field(description="Modal's lifecycle state for the app, e.g. 'deployed' or 'stopped'")


class ModalVolumeListing(FrozenModel):
    """One volume, as reported by `modal volume list --json`."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="The volume's name, unique within its Modal environment")


_APP_LISTINGS_ADAPTER: Final[TypeAdapter[tuple[ModalAppListing, ...]]] = TypeAdapter(tuple[ModalAppListing, ...])
_VOLUME_LISTINGS_ADAPTER: Final[TypeAdapter[tuple[ModalVolumeListing, ...]]] = TypeAdapter(
    tuple[ModalVolumeListing, ...]
)


def parse_modal_app_listings(payload: object) -> tuple[ModalAppListing, ...]:
    """Parse the decoded JSON body of `modal app list --json`.

    Raises ModalCliOutputError if the body is not a list of apps carrying the keys we read.
    """
    try:
        return _APP_LISTINGS_ADAPTER.validate_python(payload)
    except ValidationError as e:
        raise ModalCliOutputError("modal app list", str(e)) from e


def parse_modal_volume_listings(payload: object) -> tuple[ModalVolumeListing, ...]:
    """Parse the decoded JSON body of `modal volume list --json`.

    Raises ModalCliOutputError if the body is not a list of volumes carrying the keys we read.
    """
    try:
        return _VOLUME_LISTINGS_ADAPTER.validate_python(payload)
    except ValidationError as e:
        raise ModalCliOutputError("modal volume list", str(e)) from e
