"""Test doubles for the latchkey-extension HTTP client.

Per CLAUDE.md, do not create tests for this module itself; the helpers
are exercised through the tests that import them.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.latchkey.gateway_client import AvailableServiceEntry
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError

# Default catalog returned by :meth:`FakeLatchkeyGatewayClient.get_available_services`.
# Mirrors a single Slack-shaped service from the gateway's real services.json so tests
# that don't care about the catalog don't have to construct one. Each service maps to a
# list of scope entries (a service may expose more than one detent scope).
_DEFAULT_AVAILABLE_SERVICES_PAYLOAD: Final[dict[str, object]] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "description": "Any interaction with the Slack API.",
            "permissions": [
                {"name": "slack-read-all", "description": "All read operations across the Slack API."},
                {"name": "slack-write-all", "description": "All write operations across the Slack API."},
                {"name": "slack-chat-read", "description": "Get message permalinks and list scheduled messages."},
            ],
        },
    ],
}


class RecordedSetPermissionCall(FrozenModel):
    """Recorded args from one :meth:`FakeLatchkeyGatewayClient.set_permission_rule` call."""

    permissions_file_path: Path = Field(description="Target permissions file the caller asked us to edit.")
    rule_key: str = Field(description="Detent scope schema being upserted.")
    granted_permissions: tuple[str, ...] = Field(description="Permission schemas the caller granted.")


class FakeLatchkeyGatewayClient(LatchkeyGatewayClient):
    """In-process double for :class:`LatchkeyGatewayClient`.

    Behaves like the real client minus the HTTP layer:

    * ``set_permission_rule`` actually mutates the named file on disk
      via the same :mod:`imbue.mngr_latchkey.store` helpers the real
      extension uses, so tests that assert on the post-grant
      permissions file work unchanged.
    * ``delete_permission_request`` records the deleted ids in memory.
    * ``iter_permission_requests`` raises -- streaming is not modelled
      by this fake; tests that need streaming should use a custom
      subclass or talk to a real gateway.
    """

    available_services_payload: dict[str, object] = Field(
        default_factory=lambda: dict(_DEFAULT_AVAILABLE_SERVICES_PAYLOAD),
        description=(
            "Payload that :meth:`get_available_services` returns. Defaults to a minimal Slack-only "
            "catalog so tests that just need *any* catalog work without setup."
        ),
    )

    _set_calls: list[RecordedSetPermissionCall] = PrivateAttr(default_factory=list)
    _deleted_request_ids: list[str] = PrivateAttr(default_factory=list)

    @property
    def set_calls(self) -> tuple[RecordedSetPermissionCall, ...]:
        """Recorded set_permission_rule calls in the order they arrived."""
        return tuple(self._set_calls)

    @property
    def deleted_request_ids(self) -> tuple[str, ...]:
        """Request ids the test code asked to delete, in arrival order."""
        return tuple(self._deleted_request_ids)

    def get_available_services(self) -> dict[str, tuple[AvailableServiceEntry, ...]]:
        """Validate and return the configured payload.

        Mirrors the real client: structural failures (non-array values,
        missing fields, wrong types) raise
        :class:`LatchkeyGatewayClientError`, so tests that point
        :attr:`available_services_payload` at malformed data exercise the
        same code path as production.
        """
        validated: dict[str, tuple[AvailableServiceEntry, ...]] = {}
        for service_name, raw_entries in self.available_services_payload.items():
            if not isinstance(raw_entries, list):
                raise LatchkeyGatewayClientError(
                    f"Configured fake payload value for {service_name!r} is not a list: {raw_entries!r}",
                )
            entries: list[AvailableServiceEntry] = []
            for index, raw_entry in enumerate(raw_entries):
                try:
                    entries.append(AvailableServiceEntry.model_validate(raw_entry))
                except ValidationError as e:
                    raise LatchkeyGatewayClientError(
                        f"Configured fake payload entry {index} for {service_name!r} is invalid: {e}",
                    ) from e
            validated[service_name] = tuple(entries)
        return validated

    def get_granted_permissions_for_scopes(
        self,
        permissions_file_path: Path,
        scopes: Sequence[str],
    ) -> frozenset[str]:
        """Read the on-disk file directly, matching the real extension's GET response."""
        if not permissions_file_path.is_file():
            return frozenset()
        rules = json.loads(permissions_file_path.read_text()).get("rules", [])
        scopes_set = set(scopes)
        granted: set[str] = set()
        for rule in rules:
            for scope_name, permissions in rule.items():
                if scope_name in scopes_set:
                    granted.update(permissions)
        return frozenset(granted)

    def set_permission_rule(
        self,
        permissions_file_path: Path,
        rule_key: str,
        granted_permissions: Sequence[str],
    ) -> None:
        """Apply the grant in-process, matching the real extension's filesystem effect."""
        granted_tuple = tuple(granted_permissions)
        self._set_calls.append(
            RecordedSetPermissionCall(
                permissions_file_path=permissions_file_path,
                rule_key=rule_key,
                granted_permissions=granted_tuple,
            ),
        )
        permissions_file_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {"rules": []}
        if permissions_file_path.is_file():
            existing = json.loads(permissions_file_path.read_text())
        existing_rules = existing.get("rules", [])
        replaced = False
        new_rules: list[dict[str, list[str]]] = []
        for rule in existing_rules:
            if rule_key not in rule:
                new_rules.append(rule)
            elif not replaced:
                new_rules.append({rule_key: list(granted_tuple)})
                replaced = True
            else:
                # Duplicate rule for the same scope; drop it.
                continue
        if not replaced:
            new_rules.append({rule_key: list(granted_tuple)})
        # Mirror the real extension's spread semantics: every key other
        # than ``rules`` is preserved verbatim (notably ``schemas``).
        updated = {**existing, "rules": new_rules}
        permissions_file_path.write_text(json.dumps(updated, indent=2))

    def delete_permission_request(self, request_id: str) -> None:
        self._deleted_request_ids.append(request_id)


def build_fake_gateway_client() -> FakeLatchkeyGatewayClient:
    """Return a :class:`FakeLatchkeyGatewayClient` ready for use in tests.

    Tests that just need *a* gateway client to satisfy the
    :class:`LatchkeyPermissionGrantHandler` constructor (rather than
    one with specific URL / password / JWT semantics) call this
    helper. The fake overrides every method that would otherwise touch
    the credentials, so it needs none of them set.
    """
    return FakeLatchkeyGatewayClient()
