"""Test utilities for bifrost_service.

Exposes a concrete ``FakeBifrostAdminClient`` with the same public surface as
``BifrostAdminClient`` so tests can exercise the real FastAPI handlers without
needing a running bifrost subprocess, a Neon database, or live HTTP calls.
"""

import secrets
import uuid
from typing import Any

from imbue.bifrost_service.app import BifrostAdminApiError
from imbue.bifrost_service.app import BifrostAdminClient
from imbue.bifrost_service.app import VirtualKeyNotFoundError


class FakeBifrostAdminClient(BifrostAdminClient):
    """In-memory drop-in for ``BifrostAdminClient``.

    Stores virtual keys and their budget state in dicts; all five public
    ``*_virtual_key`` / ``*_virtual_keys`` methods from the parent are
    overridden to operate on that state. Inheriting from the real client
    means ``FakeBifrostAdminClient`` is a valid ``BifrostAdminClient`` from
    a type-checking perspective, so it can be returned directly from a
    FastAPI ``dependency_overrides`` entry without casts.

    Counters (``create_count``, ``delete_count``) are exposed so tests can
    assert that a handler actually called the admin API, not just that it
    returned OK with a pre-seeded key.
    """

    virtual_key_by_id: dict[str, dict[str, Any]]
    create_count: int
    delete_count: int
    should_fail_on_create: bool

    def __init__(self) -> None:
        # Skip the real HTTP client setup; we override every method that
        # would have touched ``self.client``, so no outbound requests happen.
        super().__init__(base_url="http://fake.invalid", admin_token="fake")
        self.virtual_key_by_id = {}
        self.create_count = 0
        self.delete_count = 0
        self.should_fail_on_create = False

    def create_virtual_key(
        self,
        name: str,
        budget_dollars: float,
        budget_reset_duration: str,
    ) -> dict[str, Any]:
        if self.should_fail_on_create:
            raise BifrostAdminApiError(409, f"Key '{name}' already exists")
        self.create_count = self.create_count + 1
        key_id = f"vk-{uuid.uuid4().hex}"
        key_value = f"sk-bf-{secrets.token_hex(16)}"
        record: dict[str, Any] = {
            "id": key_id,
            "name": name,
            "value": key_value,
            "is_active": True,
            "budgets": [
                {
                    "max_limit": budget_dollars,
                    "reset_duration": budget_reset_duration,
                    "current_usage": 0.0,
                    "last_reset": None,
                }
            ],
        }
        self.virtual_key_by_id[key_id] = record
        return record

    def list_virtual_keys(self, search: str | None = None) -> list[dict[str, Any]]:
        records = list(self.virtual_key_by_id.values())
        if search is None:
            return records
        return [r for r in records if search in str(r.get("name", ""))]

    def get_virtual_key(self, key_id: str) -> dict[str, Any]:
        record = self.virtual_key_by_id.get(key_id)
        if record is None:
            raise VirtualKeyNotFoundError(key_id)
        return record

    def update_virtual_key_budget(
        self,
        key_id: str,
        budget_dollars: float,
        budget_reset_duration: str,
    ) -> dict[str, Any]:
        record = self.get_virtual_key(key_id)
        existing_usage = 0.0
        if record.get("budgets"):
            existing_usage = float(record["budgets"][0].get("current_usage", 0.0))
        updated_record = {
            **record,
            "budgets": [
                {
                    "max_limit": budget_dollars,
                    "reset_duration": budget_reset_duration,
                    "current_usage": existing_usage,
                    "last_reset": None,
                }
            ],
        }
        self.virtual_key_by_id[key_id] = updated_record
        return updated_record

    def delete_virtual_key(self, key_id: str) -> None:
        self.get_virtual_key(key_id)
        self.delete_count = self.delete_count + 1
        del self.virtual_key_by_id[key_id]

    def record_usage(self, key_id: str, amount: float) -> None:
        """Test helper: simulate that an agent spent ``amount`` dollars on this key."""
        record = self.get_virtual_key(key_id)
        if not record.get("budgets"):
            return
        existing = record["budgets"][0]
        record["budgets"][0] = {
            **existing,
            "current_usage": float(existing.get("current_usage", 0.0)) + amount,
        }


def make_fake_admin_client() -> FakeBifrostAdminClient:
    return FakeBifrostAdminClient()
