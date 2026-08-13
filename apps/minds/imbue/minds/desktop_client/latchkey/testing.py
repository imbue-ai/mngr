"""Test doubles for the latchkey-extension HTTP client and the latchkey CLI.

Per CLAUDE.md, do not create tests for this module itself; the helpers
are exercised through the tests that import them.
"""

import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import JsonValue
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tmp file + ``os.replace``).

    Mirrors the real gateway ``permissions`` extension, which never leaves a
    partially-written file behind. This matters because minds revokes across
    workspaces on a background thread while other code (and tests) may read the
    same file concurrently; a plain ``write_text`` truncates first, so a racing
    reader could observe an empty file and fail to parse it.
    """
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


class RecordedSetPermissionCall(FrozenModel):
    """Recorded args from one :meth:`FakeLatchkeyGatewayClient.set_permission_rule` call."""

    permissions_file_path: Path = Field(description="Target permissions file the caller asked us to edit.")
    rule_key: str = Field(description="Detent schema name being upserted as a rule.")
    granted_permissions: tuple[str, ...] = Field(description="Permission schemas the caller granted.")
    schemas: Mapping[str, JsonValue] = Field(
        default_factory=dict,
        description="Schema definitions the caller asked us to merge into the file.",
    )


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
    _deleted_rule_calls: list[tuple[Path, str]] = PrivateAttr(default_factory=list)

    @property
    def set_calls(self) -> tuple[RecordedSetPermissionCall, ...]:
        """Recorded set_permission_rule calls in the order they arrived."""
        return tuple(self._set_calls)

    @property
    def deleted_request_ids(self) -> tuple[str, ...]:
        """Request ids the test code asked to delete, in arrival order."""
        return tuple(self._deleted_request_ids)

    @property
    def deleted_rule_calls(self) -> tuple[tuple[Path, str], ...]:
        """``(path, rule_key)`` pairs the test code asked to delete, in arrival order."""
        return tuple(self._deleted_rule_calls)

    # ``get_permission_rules`` is deliberately *not* overridden: the real
    # implementation is a pure view over ``get_permissions_config``, which this
    # fake replaces, so it works unchanged (and cannot drift).

    def delete_permission_rule(
        self,
        permissions_file_path: Path,
        rule_key: str,
    ) -> None:
        """Remove the rule in-process, matching the real extension's filesystem effect."""
        self._deleted_rule_calls.append((permissions_file_path, rule_key))
        if not permissions_file_path.is_file():
            return
        existing = json.loads(permissions_file_path.read_text())
        existing_rules = existing.get("rules", [])
        new_rules = [rule for rule in existing_rules if rule_key not in rule]
        updated = {**existing, "rules": new_rules}
        _atomic_write_text(permissions_file_path, json.dumps(updated, indent=2))

    def get_permissions_config(
        self,
        permissions_file_path: Path,
    ) -> LatchkeyPermissionsConfig:
        """Read the on-disk file directly, matching the real extension's GET response."""
        if not permissions_file_path.is_file():
            return LatchkeyPermissionsConfig()
        return LatchkeyPermissionsConfig.model_validate_json(permissions_file_path.read_text())

    def set_permission_rule(
        self,
        permissions_file_path: Path,
        rule_key: str,
        granted_permissions: Sequence[str],
        schemas: Mapping[str, JsonValue] | None = None,
    ) -> None:
        """Apply the grant in-process, matching the real extension's filesystem effect."""
        granted_tuple = tuple(granted_permissions)
        self._set_calls.append(
            RecordedSetPermissionCall(
                permissions_file_path=permissions_file_path,
                rule_key=rule_key,
                granted_permissions=granted_tuple,
                schemas=dict(schemas or {}),
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
        # ... and its schema handling: whatever the caller supplied is merged in
        # by name (the extension authors nothing itself).
        if schemas:
            updated["schemas"] = {**existing.get("schemas", {}), **dict(schemas)}
        _atomic_write_text(permissions_file_path, json.dumps(updated, indent=2))

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


# -- The latchkey CLI's account surface -------------------------------------


# AWS is the browser-less service of the catalog below: latchkey cannot sign in
# to it and advertises the command that stores its credentials instead.
AWS_CREDENTIALS_EXAMPLE: str = "latchkey auth set-nocurl aws <access-key-id> <secret-access-key>"


def _default_credential_examples() -> dict[str, str | None]:
    """The default ``credential_example_by_service``, named rather than a lambda
    so its value type is the field's own.

    ``dict`` is invariant, so a lambda returning ``{"aws": AWS_CREDENTIALS_EXAMPLE}``
    infers ``dict[str, str]``, which is not assignable to ``dict[str, str | None]``
    -- the ``None`` a service advertising no example needs.
    """
    return {"aws": AWS_CREDENTIALS_EXAMPLE}


class FakeAccountsLatchkey(Latchkey):
    """``Latchkey`` double whose account commands run against an in-memory map.

    Covers the four calls every permissions surface makes: ``auth_list`` and
    ``services_info`` report the configured accounts, ``auth_set_credentials``
    stores one (so a connect really does produce an account on the next read),
    and ``auth_clear`` removes one -- which is what lets
    :func:`disconnect_account`'s follow-up read see the clear.

    ``credential_example_by_service`` names the services latchkey CANNOT sign in
    to through a browser, mapped to the ``setCredentialsExample`` each one
    advertises (``None`` for a service that advertises none). Everything absent
    from it signs in through a browser, so a caller that treats the two alike
    fails somewhere. It defaults to AWS, the browser-less service of
    :data:`PERMISSIONS_CATALOG_PAYLOAD`, so the two ways of connecting a service
    are both reachable without every test spelling the mapping out; a test that
    passes its own mapping replaces the default entirely.
    """

    accounts_by_service: dict[str, list[str]] = Field(default_factory=dict)
    credential_example_by_service: dict[str, str | None] = Field(
        default_factory=_default_credential_examples,
    )
    auth_set_result: tuple[bool, str] = Field(default=(True, ""))
    auth_set_calls: list[tuple[str, tuple[str, ...]]] = Field(default_factory=list)
    auth_clear_result: tuple[bool, str] = Field(default=(True, ""))
    cleared_calls: list[tuple[str, str | None]] = Field(default_factory=list)

    def _accounts_for(self, service_name: str) -> tuple[ServiceAccountCredential, ...]:
        return tuple(
            ServiceAccountCredential(account=account, credential_status=CredentialStatus.VALID)
            for account in self.accounts_by_service.get(service_name, [])
        )

    def auth_list(self, *, is_offline: bool = False) -> dict[str, tuple[ServiceAccountCredential, ...]]:
        del is_offline
        return {service: self._accounts_for(service) for service in self.accounts_by_service}

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo | None:
        del is_offline
        accounts = self._accounts_for(service_name)
        is_credentials_only = service_name in self.credential_example_by_service
        return LatchkeyServiceInfo(
            credential_status=CredentialStatus.VALID if accounts else CredentialStatus.MISSING,
            accounts=accounts,
            auth_options=frozenset({"set"} if is_credentials_only else {"browser", "set"}),
            set_credentials_example=self.credential_example_by_service.get(service_name),
        )

    def auth_set_credentials(self, service_name: str, argv: Sequence[str]) -> tuple[bool, str]:
        self.auth_set_calls.append((service_name, tuple(argv)))
        if not self.auth_set_result[0]:
            return self.auth_set_result
        # Mirror latchkey: a stored credential turns into an account of the service.
        account = argv[list(argv).index("--account") + 1]
        self.accounts_by_service.setdefault(service_name, []).append(account)
        return True, ""

    def auth_clear(
        self,
        service_name: str,
        *,
        account: str | None = None,
        is_all: bool = False,
    ) -> tuple[bool, str]:
        del is_all
        self.cleared_calls.append((service_name, account))
        if not self.auth_clear_result[0]:
            return self.auth_clear_result
        if account is not None and service_name in self.accounts_by_service:
            remaining = [stored for stored in self.accounts_by_service[service_name] if stored != account]
            if remaining:
                self.accounts_by_service[service_name] = remaining
            else:
                del self.accounts_by_service[service_name]
        return self.auth_clear_result


# One catalog for every permissions suite, covering the shapes they all need:
# a multi-permission scope with descriptions (Slack), a service with no account
# to offer under Add connection (GitHub), and a browser-less one (AWS).
PERMISSIONS_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": [
                {"name": "slack-read-all", "description": "All reads."},
                {"name": "slack-write-all", "description": "All writes."},
                {"name": "slack-chat-read", "description": "Get permalinks."},
                {"name": "slack-chat-write", "description": "Send messages."},
            ],
        },
    ],
    "github": [
        {
            "scope": "github-rest-api",
            "display_name": "GitHub",
            "permissions": [{"name": "github-read-all"}],
        },
    ],
    "aws": [
        {
            "scope": "aws-api",
            "display_name": "AWS",
            "permissions": [{"name": "aws-s3"}],
        },
    ],
}


def build_permissions_test_catalog() -> ServicesCatalog:
    """The catalog :data:`PERMISSIONS_CATALOG_PAYLOAD` describes."""
    return ServicesCatalog.from_catalog_payload(PERMISSIONS_CATALOG_PAYLOAD)


def seed_connector_grant(
    plugin_data_dir: Path,
    host_id: HostId,
    scope: str,
    account: str,
    permissions: tuple[str, ...],
) -> None:
    """Write the per-host permissions file production writes for a connector grant."""
    rule_key, granted, schemas = build_account_grant(scope, account, permissions)
    save_permissions(
        permissions_path_for_host(plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(rules=({rule_key: list(granted)},), schemas=schemas),
    )
