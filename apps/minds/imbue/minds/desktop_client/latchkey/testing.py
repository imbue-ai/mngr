"""Test doubles for the latchkey-extension HTTP client.

Per CLAUDE.md, do not create tests for this module itself; the helpers
are exercised through the tests that import them.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import set_permissions_for_scope


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
        try:
            existing = load_permissions(permissions_file_path)
        except LatchkeyStoreError:
            existing = LatchkeyPermissionsConfig()
        updated = set_permissions_for_scope(
            existing,
            scope=rule_key,
            granted_permissions=granted_tuple,
        )
        save_permissions(permissions_file_path, updated)

    def delete_permission_request(self, request_id: str) -> None:
        self._deleted_request_ids.append(request_id)


def build_fake_gateway_client() -> FakeLatchkeyGatewayClient:
    """Return a :class:`FakeLatchkeyGatewayClient` with throwaway credentials.

    Tests that just need *a* gateway client to satisfy the
    :class:`LatchkeyPermissionGrantHandler` constructor (rather than
    one with specific URL / password / JWT semantics) call this
    helper.
    """
    return FakeLatchkeyGatewayClient(
        base_url="http://127.0.0.1:0",
        password="fake-password",
        admin_jwt="fake-admin-jwt",
    )
