"""Test doubles for the latchkey-extension HTTP client.

Per CLAUDE.md, do not create tests for this module itself; the helpers
are exercised through the tests that import them.
"""

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient


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
