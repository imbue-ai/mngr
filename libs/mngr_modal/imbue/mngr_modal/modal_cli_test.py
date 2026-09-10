import json
from typing import Final

import pytest

from imbue.mngr_modal.errors import ModalCliOutputError
from imbue.mngr_modal.modal_cli import parse_modal_app_listings
from imbue.mngr_modal.modal_cli import parse_modal_volume_listings

# Captured verbatim from `uv run modal app list --json` on modal client 1.5.4.
REAL_APP_LIST_OUTPUT: Final[str] = """[
  {
    "app_id": "ap-IeL5OYVwRdUiaADn3cPISA",
    "description": "offload-checkpoint-sandbox",
    "state": "deployed",
    "tasks": "0",
    "created_at": "2026-05-29 12:34:22+12:00",
    "stopped_at": null
  },
  {
    "app_id": "ap-jsbCKQu5YW904KXfugSfng",
    "description": "mngr_test-2026-07-30-04-12-01-9f2a",
    "state": "stopped",
    "tasks": "0",
    "created_at": "2026-07-30 16:12:03+12:00",
    "stopped_at": "2026-07-30 16:19:44+12:00"
  }
]"""

# Captured verbatim from `uv run modal volume list --json` on modal client 1.5.4.
REAL_VOLUME_LIST_OUTPUT: Final[str] = """[
  {
    "name": "minds-eval-modal-profile",
    "created_at": "2026-07-31 18:12:37+12:00",
    "created_by": "qi-1"
  }
]"""

# `modal app list`'s human-readable column headings, which are not its JSON keys.
COLUMN_HEADING_APP_LIST_OUTPUT: Final[str] = """[
  {
    "App ID": "ap-IeL5OYVwRdUiaADn3cPISA",
    "Description": "offload-checkpoint-sandbox",
    "State": "deployed"
  }
]"""

COLUMN_HEADING_VOLUME_LIST_OUTPUT: Final[str] = """[
  {
    "Name": "minds-eval-modal-profile",
    "Created at": "2026-07-31 18:12:37+12:00"
  }
]"""


def test_parse_modal_app_listings_reads_the_keys_modal_actually_emits() -> None:
    apps = parse_modal_app_listings(json.loads(REAL_APP_LIST_OUTPUT))

    assert [(app.app_id, app.description, app.state) for app in apps] == [
        ("ap-IeL5OYVwRdUiaADn3cPISA", "offload-checkpoint-sandbox", "deployed"),
        ("ap-jsbCKQu5YW904KXfugSfng", "mngr_test-2026-07-30-04-12-01-9f2a", "stopped"),
    ]


def test_parse_modal_volume_listings_reads_the_keys_modal_actually_emits() -> None:
    volumes = parse_modal_volume_listings(json.loads(REAL_VOLUME_LIST_OUTPUT))

    assert [volume.name for volume in volumes] == ["minds-eval-modal-profile"]


def test_parse_modal_app_listings_raises_when_the_app_keys_are_missing() -> None:
    with pytest.raises(ModalCliOutputError) as exc_info:
        parse_modal_app_listings(json.loads(COLUMN_HEADING_APP_LIST_OUTPUT))

    assert "modal app list" in str(exc_info.value)


def test_parse_modal_volume_listings_raises_when_the_volume_keys_are_missing() -> None:
    with pytest.raises(ModalCliOutputError) as exc_info:
        parse_modal_volume_listings(json.loads(COLUMN_HEADING_VOLUME_LIST_OUTPUT))

    assert "modal volume list" in str(exc_info.value)


def test_parse_modal_app_listings_raises_when_the_payload_is_not_a_list() -> None:
    with pytest.raises(ModalCliOutputError):
        parse_modal_app_listings({"app_id": "ap-IeL5OYVwRdUiaADn3cPISA"})


def test_parse_modal_app_listings_accepts_an_empty_listing() -> None:
    assert parse_modal_app_listings(json.loads("[]")) == ()
