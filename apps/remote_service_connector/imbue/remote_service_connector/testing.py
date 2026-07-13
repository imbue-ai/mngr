"""Test utilities for remote_service_connector."""

import base64
import json
import secrets
import uuid
from types import SimpleNamespace
from typing import Any
from typing import Final
from uuid import UUID

# Note: psycopg2.errors is reachable through the base import, matching app.py;
# an explicit ``import psycopg2.errors`` makes ty resolve the module and then
# reject its dynamically-generated members (UniqueViolation) as unknown.
import psycopg2
import pytest
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.emailverification.types import EmailVerificationUser
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.recipe.thirdparty.types import RawUserInfoFromProvider
from supertokens_python.recipe.thirdparty.types import ThirdPartyInfo
from supertokens_python.recipe.thirdparty.types import UserInfo
from supertokens_python.recipe.thirdparty.types import UserInfoEmail
from supertokens_python.recipe.webauthn.types.base import WebauthnInfo
from supertokens_python.types import LoginMethod
from supertokens_python.types import RecipeUserId
from supertokens_python.types import User
from supertokens_python.types.base import AccountInfoInput

from imbue.remote_service_connector.app import CloudflareApiError
from imbue.remote_service_connector.app import ForwardingCtx
from imbue.remote_service_connector.app import PoolHostCleanupError
from imbue.remote_service_connector.app import R2BucketNotEmptyError
from imbue.remote_service_connector.app import R2BucketNotFoundError
from imbue.remote_service_connector.app import SyncActiveAgentConflictError
from imbue.remote_service_connector.app import SyncRevisionConflictError
from imbue.remote_service_connector.app import _ONE_ACTIVE_PER_AGENT_INDEX_NAME
from imbue.remote_service_connector.app import _WORKSPACE_RECORD_COLUMNS


class FakeCloudflareOps:
    """In-memory fake implementing the CloudflareOps protocol for testing."""

    def __init__(self) -> None:
        self.tunnels: dict[str, dict[str, Any]] = {}
        self.tunnel_configs: dict[str, dict[str, Any]] = {}
        self.dns_records: list[dict[str, Any]] = []
        self.access_apps: dict[str, dict[str, Any]] = {}
        self.access_policies: dict[str, list[dict[str, Any]]] = {}
        self.kv_store: dict[str, str] = {}
        self._next_tunnel_id = 1
        self._next_record_id = 1
        self._next_access_app_id = 1
        self._next_policy_id = 1
        # R2 state
        self.account_id = "test-account"
        self.buckets: dict[str, dict[str, Any]] = {}
        # Per-bucket object lists; tests append to mark a bucket non-empty.
        self.bucket_objects: dict[str, list[str]] = {}
        self.account_tokens: dict[str, dict[str, Any]] = {}
        self._next_r2_token_id = 1

    def create_tunnel(self, name: str) -> dict[str, Any]:
        tunnel_id = f"tunnel-{self._next_tunnel_id}"
        self._next_tunnel_id += 1
        tunnel = {"id": tunnel_id, "name": name}
        self.tunnels[tunnel_id] = tunnel
        return tunnel

    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]:
        results = list(self.tunnels.values())
        if include_prefix:
            results = [t for t in results if t["name"].startswith(include_prefix)]
        return results

    def get_tunnel_by_name(self, name: str) -> dict[str, Any] | None:
        for tunnel in self.tunnels.values():
            if tunnel["name"] == name:
                return tunnel
        return None

    def get_tunnel_by_id(self, tunnel_id: str) -> dict[str, Any] | None:
        return self.tunnels.get(tunnel_id)

    def get_tunnel_token(self, tunnel_id: str) -> str:
        return f"token-for-{tunnel_id}"

    def delete_tunnel(self, tunnel_id: str) -> None:
        self.tunnels.pop(tunnel_id, None)
        self.tunnel_configs.pop(tunnel_id, None)

    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        return self.tunnel_configs.get(tunnel_id, {"config": {"ingress": [{"service": "http_status:404"}]}})

    def put_tunnel_config(self, tunnel_id: str, config: dict[str, Any]) -> None:
        self.tunnel_configs[tunnel_id] = config

    def create_cname(self, name: str, target: str) -> dict[str, Any]:
        for existing in self.dns_records:
            if existing["name"] == name:
                raise CloudflareApiError(
                    status_code=400,
                    errors=[{"code": 81053, "message": "An A, AAAA, or CNAME record with that host already exists."}],
                )
        record_id = f"record-{self._next_record_id}"
        self._next_record_id += 1
        record = {"id": record_id, "name": name, "content": target, "type": "CNAME"}
        self.dns_records.append(record)
        return record

    def list_dns_records(self, name: str = "") -> list[dict[str, Any]]:
        if name:
            return [r for r in self.dns_records if r["name"] == name]
        return list(self.dns_records)

    def delete_dns_record(self, record_id: str) -> None:
        self.dns_records = [r for r in self.dns_records if r["id"] != record_id]

    def create_access_app(self, hostname: str, app_name: str, allowed_idps: list[str] | None = None) -> dict[str, Any]:
        app_id = f"access-app-{self._next_access_app_id}"
        self._next_access_app_id += 1
        access_app: dict[str, Any] = {"id": app_id, "domain": hostname, "name": app_name}
        if allowed_idps is not None:
            access_app["allowed_idps"] = allowed_idps
        self.access_apps[app_id] = access_app
        self.access_policies[app_id] = []
        return access_app

    def delete_access_app(self, app_id: str) -> None:
        self.access_apps.pop(app_id, None)
        self.access_policies.pop(app_id, None)

    def get_access_app_by_domain(self, hostname: str) -> dict[str, Any] | None:
        for access_app in self.access_apps.values():
            if access_app["domain"] == hostname:
                return access_app
        return None

    def list_access_policies(self, app_id: str) -> list[dict[str, Any]]:
        return list(self.access_policies.get(app_id, []))

    def create_access_policy(self, app_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        policy_id = f"policy-{self._next_policy_id}"
        self._next_policy_id += 1
        stored = {**policy, "id": policy_id}
        if app_id not in self.access_policies:
            self.access_policies[app_id] = []
        self.access_policies[app_id].append(stored)
        return stored

    def update_access_policy(self, app_id: str, policy_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        policies = self.access_policies.get(app_id, [])
        for i, p in enumerate(policies):
            if p["id"] == policy_id:
                policies[i] = {**policy, "id": policy_id}
                return policies[i]
        return {**policy, "id": policy_id}

    def delete_access_policy(self, app_id: str, policy_id: str) -> None:
        if app_id in self.access_policies:
            self.access_policies[app_id] = [p for p in self.access_policies[app_id] if p["id"] != policy_id]

    def kv_get(self, key: str) -> str | None:
        return self.kv_store.get(key)

    def kv_put(self, key: str, value: str) -> None:
        self.kv_store[key] = value

    def kv_delete(self, key: str) -> None:
        self.kv_store.pop(key, None)

    def create_service_token(self, name: str) -> dict[str, Any]:
        token_id = f"svc-token-{self._next_policy_id}"
        self._next_policy_id += 1
        return {
            "id": token_id,
            "client_id": f"client-{token_id}",
            "client_secret": f"secret-{token_id}",
            "name": name,
        }

    def list_service_tokens(self) -> list[dict[str, Any]]:
        return []

    def delete_service_token(self, token_id: str) -> None:
        pass

    # -- R2 bucket + token operations --

    def create_bucket(self, name: str) -> dict[str, Any]:
        if name in self.buckets:
            raise CloudflareApiError(status_code=400, errors=[{"message": f"bucket already exists: {name}"}])
        bucket = {"name": name}
        self.buckets[name] = bucket
        self.bucket_objects.setdefault(name, [])
        return bucket

    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]:
        return [bucket for name, bucket in self.buckets.items() if name_contains in name]

    def delete_bucket(self, name: str) -> None:
        if name not in self.buckets:
            raise R2BucketNotFoundError(name)
        if self.bucket_objects.get(name):
            raise R2BucketNotEmptyError(name)
        del self.buckets[name]
        self.bucket_objects.pop(name, None)

    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]:
        token_id = f"r2tok-{self._next_r2_token_id}"
        self._next_r2_token_id += 1
        self.account_tokens[token_id] = {
            "id": token_id,
            "name": token_name,
            "bucket_name": bucket_name,
            "access": access,
        }
        return {"id": token_id, "value": f"token-value-{token_id}"}

    def delete_bucket_token(self, token_id: str) -> None:
        self.account_tokens.pop(token_id, None)


class InMemoryKeyStore:
    """In-memory KeyStore implementation for testing the bucket-key endpoints."""

    def __init__(self) -> None:
        # access_key_id -> stored row dict
        self.keys_by_access_key_id: dict[str, dict[str, Any]] = {}
        self._created_counter = 0

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None:
        self._created_counter += 1
        self.keys_by_access_key_id[access_key_id] = {
            "access_key_id": access_key_id,
            "owner_user_id": owner_user_id,
            "bucket_name": bucket_name,
            "access": access,
            "alias": alias,
            "created_at": f"2026-01-01T00:00:{self._created_counter:02d}+00:00",
        }

    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self.keys_by_access_key_id.values() if r["owner_user_id"] == owner_user_id]
        if bucket_name is not None:
            rows = [r for r in rows if r["bucket_name"] == bucket_name]
        return sorted(rows, key=lambda r: r["created_at"])

    def get_key(self, access_key_id: str) -> dict[str, Any] | None:
        row = self.keys_by_access_key_id.get(access_key_id)
        return dict(row) if row is not None else None

    def delete_key(self, access_key_id: str) -> None:
        self.keys_by_access_key_id.pop(access_key_id, None)

    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]:
        removed = [
            r
            for r in self.keys_by_access_key_id.values()
            if r["owner_user_id"] == owner_user_id and r["bucket_name"] == bucket_name
        ]
        for row in removed:
            del self.keys_by_access_key_id[row["access_key_id"]]
        return removed


def make_fake_key_store() -> InMemoryKeyStore:
    """Construct an empty in-memory KeyStore for tests."""
    return InMemoryKeyStore()


class FakeForwardingCtx(ForwardingCtx):
    """ForwardingCtx backed by FakeCloudflareOps for testing."""

    fake: FakeCloudflareOps


def make_fake_forwarding_ctx(
    domain: str = "example.com",
    allowed_idps: list[str] | None = None,
) -> FakeForwardingCtx:
    """Create a FakeForwardingCtx for testing."""
    fake = FakeCloudflareOps()
    ctx = FakeForwardingCtx(ops=fake, domain=domain, allowed_idps=allowed_idps)
    ctx.fake = fake
    return ctx


def make_fake_tunnel_token(tunnel_id: str) -> str:
    """Create a fake tunnel token (base64-encoded JSON) for testing."""
    token_data = json.dumps({"a": "test-account", "t": tunnel_id, "s": "test-secret"})
    return base64.b64encode(token_data.encode()).decode()


# ---------------------------------------------------------------------------
# SuperTokens SDK fakes
#
# The remote_service_connector service wraps the SuperTokens SDK behind /auth/*
# endpoints. Exercising those endpoints against a real SuperTokens core is
# slow (Docker) and unreliable in CI, so the tests install the fakes below as
# drop-in replacements for every SDK function the handlers call. The backend
# state (accounts, sessions, reset tokens) lives on a single
# ``FakeSuperTokensBackend`` instance; ``FakeSuperTokensBackend.install_on_app_module``
# swaps the SDK references on ``remote_service_connector.app`` over to methods on
# that instance. Swapping the ``app`` module's bound references (rather than
# the SDK's source module) means handlers see fakes without needing to
# initialize the real SuperTokens SDK, which would fail without a live core.
# ---------------------------------------------------------------------------


_USER_ID_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _deterministic_user_id(email: str, provider: str) -> str:
    return str(uuid.uuid5(_USER_ID_NAMESPACE, f"{provider}:{email}"))


class FakeAccount:
    """In-memory record for a single SuperTokens account.

    Kept as a plain attribute bag so it can be mutated freely; not part of the
    ``FakeSuperTokensBackend`` public API.
    """

    user_id: str
    email: str
    password: str | None
    is_verified: bool
    provider_id: str
    third_party_user_id: str | None
    display_name: str | None


def _make_account(
    email: str,
    password: str | None,
    provider_id: str,
    third_party_user_id: str | None,
    display_name: str | None,
    is_verified: bool,
) -> FakeAccount:
    account = FakeAccount()
    account.user_id = _deterministic_user_id(email, provider_id)
    account.email = email
    account.password = password
    account.is_verified = is_verified
    account.provider_id = provider_id
    account.third_party_user_id = third_party_user_id
    account.display_name = display_name
    return account


def _build_st_user(account: FakeAccount) -> User:
    """Build a supertokens-python User from a FakeAccount."""
    is_thirdparty = account.provider_id != "emailpassword"
    recipe_id = "thirdparty" if is_thirdparty else "emailpassword"
    third_party_info: ThirdPartyInfo | None = None
    if is_thirdparty and account.third_party_user_id is not None:
        third_party_info = ThirdPartyInfo(
            third_party_user_id=account.third_party_user_id,
            third_party_id=account.provider_id,
        )
    login_method = LoginMethod(
        recipe_id=recipe_id,
        recipe_user_id=account.user_id,
        tenant_ids=["public"],
        email=account.email,
        phone_number=None,
        third_party=third_party_info,
        webauthn=None,
        time_joined=0,
        verified=account.is_verified,
    )
    return User(
        user_id=account.user_id,
        is_primary_user=False,
        tenant_ids=["public"],
        emails=[account.email],
        phone_numbers=[],
        third_party=[],
        webauthn=WebauthnInfo(credential_ids=[]),
        login_methods=[login_method],
        time_joined=0,
    )


class FakeSessionContainer:
    """Minimal SessionContainer stand-in exposing the methods handlers use."""

    access_token: str
    refresh_token: str
    user_id: str

    def get_user_id(self) -> str:
        return self.user_id

    def get_all_session_tokens_dangerously(self) -> dict[str, str]:
        return {"accessToken": self.access_token, "refreshToken": self.refresh_token}


def _make_session(user_id: str) -> FakeSessionContainer:
    session = FakeSessionContainer()
    session.user_id = user_id
    session.access_token = f"at-{secrets.token_hex(8)}"
    session.refresh_token = f"rt-{secrets.token_hex(8)}"
    return session


class FakeProvider:
    """Stand-in for an OAuth provider exposing the async surface handlers use."""

    provider_id: str
    email: str
    third_party_user_id: str
    display_name: str | None
    is_verified: bool

    async def get_authorisation_redirect_url(
        self,
        redirect_uri_on_provider_dashboard: str,
        user_context: dict[str, Any],
    ) -> Any:
        class _Redirect:
            url_with_query_params: str

        redirect = _Redirect()
        redirect.url_with_query_params = (
            f"https://{self.provider_id}.example.com/auth?redirect_uri={redirect_uri_on_provider_dashboard}&state=s"
        )
        return redirect

    async def exchange_auth_code_for_oauth_tokens(
        self,
        redirect_uri_info: RedirectUriInfo,
        user_context: dict[str, Any],
    ) -> dict[str, str]:
        return {"access_token": "oauth-at"}

    async def get_user_info(
        self,
        oauth_tokens: dict[str, str],
        user_context: dict[str, Any],
    ) -> UserInfo:
        raw = RawUserInfoFromProvider(
            from_id_token_payload=None,
            from_user_info_api={"name": self.display_name} if self.display_name else None,
        )
        return UserInfo(
            third_party_user_id=self.third_party_user_id,
            email=UserInfoEmail(email=self.email, is_verified=self.is_verified),
            raw_user_info_from_provider=raw,
        )


class FakeSuperTokensBackend:
    """In-memory SuperTokens replacement for unit-testing the /auth/* handlers.

    Tracks every piece of server-side state the handlers depend on (accounts,
    sessions, email-verification tokens, password-reset tokens, OAuth provider
    configuration) so the fake can answer any SDK call the handlers make
    without talking to a real SuperTokens core.

    The counters below (``sent_verification_emails``, ``sent_reset_emails``)
    let tests assert that side-effecting SDK calls actually fired, not just
    that the handler returned OK.
    """

    accounts_by_id: dict[str, FakeAccount]
    accounts_by_email: dict[str, FakeAccount]
    sessions_by_access_token: dict[str, FakeSessionContainer]
    sessions_by_refresh_token: dict[str, FakeSessionContainer]
    reset_tokens: dict[str, str]
    verification_tokens: dict[str, tuple[str, str]]
    registered_providers: dict[str, FakeProvider]
    sent_verification_emails: list[tuple[str, str]]
    sent_reset_emails: list[tuple[str, str]]
    # Error-injection hook: if a method name is a key here, the corresponding
    # SDK fake raises the stored exception instead of producing a result. Lets
    # tests exercise the /auth/* SDK-outage code paths through the real handler
    # without patching module-level attributes.
    sdk_errors_by_method: dict[str, Exception]

    def install_on_app_module(self, app_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Swap every SuperTokens SDK call site on ``app_mod`` with a fake.

        Driving the patches through a single dict + loop keeps this helper to
        exactly one attribute-patch call no matter how many SDK functions we
        stub, which limits the blast radius on the test-patching ratchet.
        """
        fakes: dict[str, Any] = {
            "ep_sign_up": self.sign_up,
            "ep_sign_in": self.sign_in,
            "is_email_verified": self.is_email_verified,
            "send_email_verification_email": self.send_email_verification_email,
            "create_new_session_without_request_response": self.create_new_session,
            "refresh_session_without_request_response": self.refresh_session,
            "revoke_all_sessions_for_user": self.revoke_all_sessions_for_user,
            "get_user": self.get_user,
            "get_session_without_request_response": self.get_session,
            "list_users_by_account_info": self.list_users_by_account_info,
            "send_reset_password_email": self.send_reset_password_email,
            "consume_password_reset_token": self.consume_password_reset_token,
            "update_email_or_password": self.update_email_or_password,
            "verify_email_using_token": self.verify_email_using_token,
            "get_provider": self.get_provider,
            "manually_create_or_update_user": self.manually_create_or_update_user,
        }
        for name, fake in fakes.items():
            monkeypatch.setattr(app_mod, name, fake)

    def register_provider(
        self,
        provider_id: str,
        *,
        email: str = "oauth@example.com",
        third_party_user_id: str = "tp-user-1",
        display_name: str | None = "OAuth User",
        is_verified: bool = True,
    ) -> None:
        """Register an OAuth provider so ``get_provider`` returns it."""
        provider = FakeProvider()
        provider.provider_id = provider_id
        provider.email = email
        provider.third_party_user_id = third_party_user_id
        provider.display_name = display_name
        provider.is_verified = is_verified
        self.registered_providers[provider_id] = provider

    def mark_email_verified(self, user_id: str) -> None:
        """Force-flip an account to verified (bypassing the token flow)."""
        account = self.accounts_by_id.get(user_id)
        if account is not None:
            account.is_verified = True

    def issue_reset_token(self, user_id: str) -> str:
        """Issue a password-reset token directly, without going through forgot-password."""
        token = f"reset-{secrets.token_hex(8)}"
        self.reset_tokens[token] = user_id
        return token

    def raise_on(self, method_name: str, exc: Exception) -> None:
        """Arrange for the named SDK-fake method to raise ``exc`` on its next call.

        The fake SDK methods check ``sdk_errors_by_method`` at entry; this
        helper lets tests simulate SuperTokens core outages through the real
        handler's try/except blocks without patching module-level attributes.
        """
        self.sdk_errors_by_method[method_name] = exc

    def _raise_if_configured(self, method_name: str) -> None:
        exc = self.sdk_errors_by_method.get(method_name)
        if exc is not None:
            raise exc

    def sign_up(
        self,
        *,
        tenant_id: str,
        email: str,
        password: str,
        user_context: dict[str, Any] | None = None,
    ) -> EPSignUpOkResult | EmailAlreadyExistsError:
        del tenant_id, user_context
        self._raise_if_configured("sign_up")
        if email in self.accounts_by_email:
            return EmailAlreadyExistsError()
        account = _make_account(
            email=email,
            password=password,
            provider_id="emailpassword",
            third_party_user_id=None,
            display_name=None,
            is_verified=False,
        )
        self.accounts_by_email[email] = account
        self.accounts_by_id[account.user_id] = account
        user = _build_st_user(account)
        return EPSignUpOkResult(user=user, recipe_user_id=RecipeUserId(account.user_id))

    def sign_in(
        self,
        *,
        tenant_id: str,
        email: str,
        password: str,
        user_context: dict[str, Any] | None = None,
    ) -> EPSignInOkResult | WrongCredentialsError:
        del tenant_id, user_context
        self._raise_if_configured("sign_in")
        account = self.accounts_by_email.get(email)
        if account is None or account.password != password:
            return WrongCredentialsError()
        user = _build_st_user(account)
        return EPSignInOkResult(user=user, recipe_user_id=RecipeUserId(account.user_id))

    def is_email_verified(
        self,
        *,
        recipe_user_id: RecipeUserId,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> bool:
        del email, user_context
        account = self.accounts_by_id.get(recipe_user_id.get_as_string())
        return account is not None and account.is_verified

    def send_email_verification_email(
        self,
        *,
        tenant_id: str,
        user_id: str,
        recipe_user_id: RecipeUserId,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> None:
        del tenant_id, recipe_user_id, user_context
        token = f"verify-{secrets.token_hex(8)}"
        self.verification_tokens[token] = (user_id, email)
        self.sent_verification_emails.append((user_id, email))

    def create_new_session(
        self,
        *,
        tenant_id: str,
        recipe_user_id: RecipeUserId,
        access_token_payload: dict[str, Any] | None = None,
        session_data_in_database: dict[str, Any] | None = None,
        disable_anti_csrf: bool = False,
        user_context: dict[str, Any] | None = None,
    ) -> FakeSessionContainer:
        del tenant_id, access_token_payload, session_data_in_database, disable_anti_csrf, user_context
        session = _make_session(recipe_user_id.get_as_string())
        self.sessions_by_access_token[session.access_token] = session
        self.sessions_by_refresh_token[session.refresh_token] = session
        return session

    def refresh_session(
        self,
        *,
        refresh_token: str,
        anti_csrf_token: str | None = None,
        disable_anti_csrf: bool = False,
        user_context: dict[str, Any] | None = None,
    ) -> FakeSessionContainer:
        del anti_csrf_token, disable_anti_csrf, user_context
        old = self.sessions_by_refresh_token.get(refresh_token)
        if old is None:
            raise ValueError("Invalid refresh token")
        del self.sessions_by_refresh_token[refresh_token]
        self.sessions_by_access_token.pop(old.access_token, None)
        session = _make_session(old.user_id)
        self.sessions_by_access_token[session.access_token] = session
        self.sessions_by_refresh_token[session.refresh_token] = session
        return session

    def revoke_all_sessions_for_user(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        revoke_across_all_tenants: bool = True,
        user_context: dict[str, Any] | None = None,
    ) -> list[str]:
        del tenant_id, revoke_across_all_tenants, user_context
        revoked: list[str] = []
        for session in list(self.sessions_by_access_token.values()):
            if session.user_id == user_id:
                revoked.append(session.access_token)
                self.sessions_by_access_token.pop(session.access_token, None)
                self.sessions_by_refresh_token.pop(session.refresh_token, None)
        return revoked

    def get_user(self, user_id: str, user_context: dict[str, Any] | None = None) -> User | None:
        del user_context
        account = self.accounts_by_id.get(user_id)
        if account is None:
            return None
        return _build_st_user(account)

    def get_session(
        self,
        *,
        access_token: str,
        anti_csrf_check: bool = False,
        session_required: bool = True,
        override_global_claim_validators: Any = None,
        user_context: dict[str, Any] | None = None,
    ) -> FakeSessionContainer | None:
        del anti_csrf_check, session_required, override_global_claim_validators, user_context
        return self.sessions_by_access_token.get(access_token)

    def list_users_by_account_info(
        self,
        *,
        tenant_id: str,
        account_info: AccountInfoInput,
        do_union_of_account_info: bool = False,
        user_context: dict[str, Any] | None = None,
    ) -> list[User]:
        del tenant_id, do_union_of_account_info, user_context
        account = self.accounts_by_email.get(account_info.email) if account_info.email else None
        if account is None:
            return []
        return [_build_st_user(account)]

    def send_reset_password_email(
        self,
        *,
        tenant_id: str,
        user_id: str,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> str:
        del tenant_id, user_context
        if user_id not in self.accounts_by_id:
            return "UNKNOWN_USER_ID_ERROR"
        token = f"reset-{secrets.token_hex(8)}"
        self.reset_tokens[token] = user_id
        self.sent_reset_emails.append((user_id, email))
        return "OK"

    def consume_password_reset_token(
        self,
        *,
        tenant_id: str,
        token: str,
        user_context: dict[str, Any] | None = None,
    ) -> ConsumePasswordResetTokenOkResult | Any:
        del tenant_id, user_context
        user_id = self.reset_tokens.pop(token, None)
        if user_id is None:

            class _Invalid:
                status: str = "RESET_PASSWORD_INVALID_TOKEN_ERROR"

            return _Invalid()
        account = self.accounts_by_id[user_id]
        return ConsumePasswordResetTokenOkResult(email=account.email, user_id=user_id)

    def update_email_or_password(
        self,
        *,
        recipe_user_id: RecipeUserId,
        email: str | None = None,
        password: str | None = None,
        apply_password_policy: bool = True,
        tenant_id_for_password_policy: str = "public",
        user_context: dict[str, Any] | None = None,
    ) -> UpdateEmailOrPasswordOkResult:
        del apply_password_policy, tenant_id_for_password_policy, user_context
        account = self.accounts_by_id[recipe_user_id.get_as_string()]
        if email is not None:
            account.email = email
        if password is not None:
            account.password = password
        return UpdateEmailOrPasswordOkResult()

    def verify_email_using_token(
        self,
        *,
        tenant_id: str,
        token: str,
        attempt_account_linking: bool = True,
        user_context: dict[str, Any] | None = None,
    ) -> VerifyEmailUsingTokenOkResult | Any:
        del tenant_id, attempt_account_linking, user_context
        pair = self.verification_tokens.pop(token, None)
        if pair is None:

            class _Invalid:
                status: str = "EMAIL_VERIFICATION_INVALID_TOKEN_ERROR"

            return _Invalid()
        user_id, email = pair
        account = self.accounts_by_id[user_id]
        account.is_verified = True
        return VerifyEmailUsingTokenOkResult(
            user=EmailVerificationUser(recipe_user_id=RecipeUserId(user_id), email=email),
        )

    def get_provider(
        self,
        *,
        tenant_id: str,
        third_party_id: str,
        client_type: str | None = None,
        user_context: dict[str, Any] | None = None,
    ) -> FakeProvider | None:
        del tenant_id, client_type, user_context
        return self.registered_providers.get(third_party_id)

    def manually_create_or_update_user(
        self,
        *,
        tenant_id: str,
        third_party_id: str,
        third_party_user_id: str,
        email: str,
        is_verified: bool,
        user_context: dict[str, Any] | None = None,
    ) -> ManuallyCreateOrUpdateUserOkResult:
        del tenant_id, user_context
        existing = self.accounts_by_email.get(email)
        created_new = existing is None
        if existing is None:
            account = _make_account(
                email=email,
                password=None,
                provider_id=third_party_id,
                third_party_user_id=third_party_user_id,
                display_name=None,
                is_verified=is_verified,
            )
            self.accounts_by_email[email] = account
            self.accounts_by_id[account.user_id] = account
        else:
            account = existing
            account.is_verified = account.is_verified or is_verified
        user = _build_st_user(account)
        return ManuallyCreateOrUpdateUserOkResult(
            user=user,
            recipe_user_id=RecipeUserId(account.user_id),
            created_new_recipe_user=created_new,
        )


def make_fake_supertokens_backend() -> FakeSuperTokensBackend:
    """Construct an empty in-memory SuperTokens backend."""
    backend = FakeSuperTokensBackend()
    backend.accounts_by_id = {}
    backend.accounts_by_email = {}
    backend.sessions_by_access_token = {}
    backend.sessions_by_refresh_token = {}
    backend.reset_tokens = {}
    backend.verification_tokens = {}
    backend.registered_providers = {}
    backend.sent_verification_emails = []
    backend.sent_reset_emails = []
    backend.sdk_errors_by_method = {}
    return backend


# ---------------------------------------------------------------------------
# Host pool fakes
#
# Similar to FakeSuperTokensBackend, this provides an in-memory replacement
# for the psycopg2 database and paramiko SSH operations used by the host pool
# endpoints.  ``FakePoolBackend.install_on_app_module`` patches the module
# references through a single for-loop (same pattern as the SuperTokens fakes)
# so the test-patching ratchet count increases by exactly one line.
# ---------------------------------------------------------------------------


# Placeholder host public keys for fake pool rows. The fake replaces the real
# SSH layer (``_append_authorized_key``), so these are never parsed/pinned -- they
# only need to be non-null so the lease fail-closed check passes.
_FAKE_OUTER_HOST_PUBLIC_KEY: Final[str] = "ssh-ed25519 AAAAFAKEouterhostkey"
_FAKE_CONTAINER_HOST_PUBLIC_KEY: Final[str] = "ssh-ed25519 AAAAFAKEcontainerhostkey"


class FakePoolRow:
    """In-memory record for a single pool_hosts row."""

    host_id: UUID
    vps_address: str
    vps_instance_id: str
    agent_id: str
    host_id_str: str
    host_name: str
    ssh_port: int
    ssh_user: str
    container_ssh_port: int
    status: str
    version: str
    attributes: dict[str, Any] | None
    region: str | None
    leased_to_user: str | None
    leased_at: str | None
    released_at: str | None
    lima_instance_name: str | None
    lima_disk_name: str | None
    bare_metal_server_id: UUID | None
    outer_host_public_key: str | None
    container_host_public_key: str | None


def _row_attributes(row: "FakePoolRow") -> dict[str, Any]:
    """Return the JSONB attributes view of a fake row.

    Existing tests pass ``version="v…"`` for ergonomics; we synthesise a
    matching attributes dict from that here so the fake's behaviour mirrors
    what production does once admin pool create writes attributes directly.
    """
    if isinstance(row.attributes, dict):
        return dict(row.attributes)
    return {"version": row.version}


def _attributes_contain(row_attrs: dict[str, Any], requested: dict[str, Any]) -> bool:
    """Reproduce PostgreSQL's ``@>`` containment for primitive-valued attribute dicts."""
    for key, value in requested.items():
        if key not in row_attrs:
            return False
        if row_attrs[key] != value:
            return False
    return True


def _make_pool_row(
    host_id: UUID,
    vps_address: str,
    agent_id: str,
    host_id_str: str,
    ssh_port: int,
    ssh_user: str,
    container_ssh_port: int,
    version: str,
    status: str = "available",
    leased_to_user: str | None = None,
    leased_at: str | None = None,
    host_name: str | None = None,
    region: str | None = None,
    outer_host_public_key: str | None = _FAKE_OUTER_HOST_PUBLIC_KEY,
    container_host_public_key: str | None = _FAKE_CONTAINER_HOST_PUBLIC_KEY,
) -> FakePoolRow:
    row = FakePoolRow()
    row.host_id = host_id
    row.vps_address = vps_address
    row.vps_instance_id = f"vps-{host_id}"
    row.agent_id = agent_id
    row.host_id_str = host_id_str
    # Matches the migration's backfill: pre-leased rows default to host_id_str
    # so they remain visible under a stable name until something leases them.
    row.host_name = host_name if host_name is not None else host_id_str
    row.ssh_port = ssh_port
    row.ssh_user = ssh_user
    row.container_ssh_port = container_ssh_port
    row.status = status
    row.version = version
    row.leased_to_user = leased_to_user
    row.leased_at = leased_at
    row.released_at = None
    row.attributes = None
    row.region = region
    # Slice-specific tests set these explicitly.
    row.lima_instance_name = None
    row.lima_disk_name = None
    row.bare_metal_server_id = None
    row.outer_host_public_key = outer_host_public_key
    row.container_host_public_key = container_host_public_key
    return row


# Fixed timestamps for fake workspace-sync rows; list order stands in for
# ORDER BY created_at (rows are only ever appended).
_SYNC_ROW_CREATED_AT: Final[str] = "2026-01-01T00:00:00+00:00"
_SYNC_ROW_UPDATED_AT: Final[str] = "2026-01-02T00:00:00+00:00"

# Derived from the production column list so the fake's tuple order can never
# drift from what PostgresSyncStore SELECTs.
_WORKSPACE_RECORD_COLUMN_NAMES: Final[tuple[str, ...]] = tuple(
    name.strip() for name in _WORKSPACE_RECORD_COLUMNS.split(",")
)


def _adapted_bytes(value: Any) -> bytes | None:
    """Unwrap a psycopg2.Binary bind parameter back to the raw bytes (None passes through)."""
    if value is None:
        return None
    return bytes(value.adapted)


class _OneActivePerAgentViolation(psycopg2.errors.UniqueViolation):
    """UniqueViolation whose diagnostics carry the partial-index name, as postgres reports it."""

    @property
    def diag(self) -> Any:
        return SimpleNamespace(constraint_name=_ONE_ACTIVE_PER_AGENT_INDEX_NAME)


class FakeCursor:
    """In-memory cursor that simulates psycopg2 cursor behavior against FakePoolBackend."""

    _backend: "FakePoolBackend"
    _results: list[tuple[Any, ...]]
    rowcount: int

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        """Route SQL queries to the in-memory store."""
        self._results = []
        self._result_idx = 0
        self.rowcount = 0
        query_lower = query.strip().lower()

        if "from pool_hosts" in query_lower and "status = 'available'" in query_lower:
            # The connector serialises the request attributes via json.dumps
            # before passing them to the SQL bind parameter, so we always get
            # a JSON string here. A hard ``region`` (WHERE clause), if present,
            # follows it in the param tuple.
            raw = params[0]
            requested = json.loads(raw) if isinstance(raw, str) else dict(raw)
            # A hard ``region`` bind param, when present, always immediately
            # follows the attributes JSON param (index 0), so its index is 1.
            hard_region: str | None = None
            if "and region = %s" in query_lower:
                hard_region = params[1]
            candidate_rows = [
                row
                for row in self._backend.pool_rows
                if row.status == "available"
                and _attributes_contain(_row_attributes(row), requested)
                and (hard_region is None or row.region == hard_region)
            ]
            if candidate_rows:
                chosen = candidate_rows[0]
                self._results = [
                    (
                        chosen.host_id,
                        chosen.vps_address,
                        chosen.ssh_port,
                        chosen.ssh_user,
                        chosen.container_ssh_port,
                        chosen.agent_id,
                        chosen.host_id_str,
                        _row_attributes(chosen),
                        chosen.outer_host_public_key,
                        chosen.container_host_public_key,
                    )
                ]

        elif "update pool_hosts set status = 'leased'" in query_lower:
            # Lease SQL now also writes the user-supplied host_name on the
            # same UPDATE so the friendly name is set atomically with the
            # status flip.
            username, host_name, host_id = params
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.status = "leased"
                    row.leased_to_user = username
                    row.leased_at = "2026-01-01T00:00:00+00:00"
                    row.host_name = host_name
                    break

        elif "select leased_to_user, status from pool_hosts" in query_lower:
            # Rename endpoint: a narrow ownership/status lookup by id (only
            # ``leased_to_user`` and ``status``). Matched before the broader
            # release lookup below, which selects additional columns.
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    self._results = [(row.leased_to_user, row.status)]
                    break

        elif "update pool_hosts set host_name" in query_lower:
            # Rename endpoint: set the mutable friendly name by id.
            host_name, raw_host_id = params
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.host_name = host_name
                    break

        elif (
            "from pool_hosts" in query_lower
            and "leased_to_user" in query_lower
            and "select leased_to_user" in query_lower
        ):
            # Release endpoint: lookup by id. The connector stringifies
            # the UUID before passing it as a bind param (psycopg2 can't
            # adapt Python ``UUID`` directly), so accept either form.
            # Returns ``(leased_to_user, status, lima_instance_name,
            # lima_disk_name, bare_metal_server_id)`` so the route can distinguish
            # already-released / removing / leased and has the slice's lima fields
            # needed for VM teardown.
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    self._results = [
                        (
                            row.leased_to_user,
                            row.status,
                            row.lima_instance_name,
                            row.lima_disk_name,
                            row.bare_metal_server_id,
                        )
                    ]
                    break

        elif (
            "from pool_hosts" in query_lower and "status = 'leased'" in query_lower and "leased_to_user" in query_lower
        ):
            # List endpoint: lookup by user
            username = params[0]
            for row in self._backend.pool_rows:
                if row.status == "leased" and row.leased_to_user == username:
                    self._results.append(
                        (
                            row.host_id,
                            row.vps_address,
                            row.ssh_port,
                            row.ssh_user,
                            row.container_ssh_port,
                            row.agent_id,
                            row.host_id_str,
                            row.host_name,
                            _row_attributes(row),
                            row.leased_at,
                            row.outer_host_public_key,
                            row.container_host_public_key,
                        )
                    )

        elif "update pool_hosts set status = 'removing'" in query_lower:
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.status = "removing"
                    row.released_at = "2026-01-02T00:00:00+00:00"
                    break

        elif "from paid_emails" in query_lower and "select 1" in query_lower:
            entry = self._backend.paid_emails.get(params[0])
            if entry is not None and entry["is_paid"]:
                self._results = [(1,)]

        elif "from paid_domains" in query_lower and "select 1" in query_lower:
            entry = self._backend.paid_domains.get(params[0])
            if entry is not None and entry["is_paid"]:
                self._results = [(1,)]

        elif "from paid_emails" in query_lower and "select email" in query_lower:
            self._results = self._backend.list_paid_entries(
                self._backend.paid_emails, paid_only="is_paid = true" in query_lower
            )

        elif "from paid_domains" in query_lower and "select domain" in query_lower:
            self._results = self._backend.list_paid_entries(
                self._backend.paid_domains, paid_only="is_paid = true" in query_lower
            )

        elif "insert into paid_emails" in query_lower:
            self._backend.activate_paid_entry(self._backend.paid_emails, params[0])

        elif "insert into paid_domains" in query_lower:
            self._backend.activate_paid_entry(self._backend.paid_domains, params[0])

        elif "update paid_emails set is_paid = false" in query_lower:
            self._backend.deactivate_paid_entry(self._backend.paid_emails, params[0])

        elif "update paid_domains set is_paid = false" in query_lower:
            self._backend.deactivate_paid_entry(self._backend.paid_domains, params[0])

        elif query_lower.startswith("delete from pool_hosts where id"):
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            self._backend.pool_rows = [r for r in self._backend.pool_rows if r.host_id != host_id]

        elif "from workspace_records" in query_lower and "for update" in query_lower:
            record_row = self._backend.find_sync_record(params[0], params[1])
            if record_row is not None:
                self._results = [self._backend.sync_record_tuple(record_row)]

        elif "from workspace_records" in query_lower and "order by created_at" in query_lower:
            self._results = [
                self._backend.sync_record_tuple(row)
                for row in self._backend.sync_record_rows
                if row["user_id"] == params[0]
            ]

        elif query_lower.startswith("insert into workspace_records"):
            self._results = [self._backend.sync_record_tuple(self._backend.insert_sync_record(params))]

        elif query_lower.startswith("update workspace_records set encrypted_secrets = null"):
            self.rowcount = self._backend.scrub_sync_secrets(params[0])

        elif query_lower.startswith("update workspace_records"):
            updated_row = self._backend.update_sync_record(params)
            if updated_row is not None:
                self._results = [self._backend.sync_record_tuple(updated_row)]

        elif query_lower.startswith("delete from workspace_records"):
            user_id, record_host_id = params
            self._backend.sync_record_rows = [
                row
                for row in self._backend.sync_record_rows
                if not (row["user_id"] == user_id and row["host_id"] == record_host_id)
            ]

        elif query_lower.startswith("select") and "from account_key_bundles" in query_lower:
            bundle = self._backend.sync_bundle_by_user.get(params[0])
            if bundle is not None:
                self._results = [
                    (
                        bundle["kdf_salt"],
                        bundle["kdf_time_cost"],
                        bundle["kdf_memory_kib"],
                        bundle["kdf_parallelism"],
                        bundle["wrapped_dek"],
                        bundle["key_epoch"],
                        _SYNC_ROW_UPDATED_AT,
                    )
                ]

        elif query_lower.startswith("insert into account_key_bundles"):
            user_id, kdf_salt, kdf_time_cost, kdf_memory_kib, kdf_parallelism, wrapped_dek, key_epoch = params
            self._backend.sync_bundle_by_user[user_id] = {
                "kdf_salt": _adapted_bytes(kdf_salt),
                "kdf_time_cost": kdf_time_cost,
                "kdf_memory_kib": kdf_memory_kib,
                "kdf_parallelism": kdf_parallelism,
                "wrapped_dek": _adapted_bytes(wrapped_dek),
                "key_epoch": key_epoch,
            }

        elif query_lower.startswith("delete from account_key_bundles"):
            self._backend.sync_bundle_by_user.pop(params[0], None)

        else:
            pass

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._results:
            return self._results[0]
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._results)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _make_fake_cursor(backend: "FakePoolBackend") -> FakeCursor:
    cursor = FakeCursor()
    cursor._backend = backend
    cursor._results = []
    return cursor


class FakeConnection:
    """In-memory connection that simulates psycopg2 connection behavior."""

    _backend: "FakePoolBackend"

    def cursor(self) -> FakeCursor:
        return _make_fake_cursor(self._backend)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _make_fake_connection(backend: "FakePoolBackend") -> FakeConnection:
    conn = FakeConnection()
    conn._backend = backend
    return conn


_PAID_ENTRY_CREATED_AT = "2026-01-01T00:00:00+00:00"
_PAID_ENTRY_UPDATED_AT = "2026-01-02T00:00:00+00:00"


class FakePoolBackend:
    """In-memory pool database replacement for testing host pool + paid-list endpoints."""

    pool_rows: list[FakePoolRow]
    append_key_calls: list[tuple[str, int, str, str, str, str]]
    # Recorded slice-VM teardowns (the box SSH is faked); set
    # ``slice_teardown_should_fail`` to simulate a teardown that cannot complete.
    slice_teardowns: list[tuple[Any, Any, str | None, str | None]]
    slice_teardown_should_fail: bool
    # Paid-list stores: value -> {"is_paid", "created_at", "updated_at"}.
    paid_domains: dict[str, dict[str, Any]]
    paid_emails: dict[str, dict[str, Any]]
    # Workspace-sync stores: rows keyed by (user_id, host_id) held as dicts in
    # insertion order; bundles keyed by user_id. Secrets/salts are raw bytes.
    sync_record_rows: list[dict[str, Any]]
    sync_bundle_by_user: dict[str, dict[str, Any]]
    # Failure-injection knobs for PostgresSyncStore tests. When set, the next
    # workspace_records INSERT commits this "winner" row and then raises the
    # primary-key UniqueViolation, simulating a concurrent first push.
    sync_insert_race_winner: dict[str, Any] | None
    # When true, workspace_records UPDATEs return no row, simulating the
    # RETURNING invariant breaking.
    sync_update_returns_no_row: bool

    def add_paid_domain(self, domain: str, is_paid: bool = True) -> None:
        """Seed a paid-domains row (lowercased), defaulting to active."""
        self.paid_domains[domain.lower()] = {
            "is_paid": is_paid,
            "created_at": _PAID_ENTRY_CREATED_AT,
            "updated_at": _PAID_ENTRY_UPDATED_AT,
        }

    def add_paid_email(self, email: str, is_paid: bool = True) -> None:
        """Seed a paid-emails row (lowercased), defaulting to active."""
        self.paid_emails[email.lower()] = {
            "is_paid": is_paid,
            "created_at": _PAID_ENTRY_CREATED_AT,
            "updated_at": _PAID_ENTRY_UPDATED_AT,
        }

    def list_paid_entries(self, store: dict[str, dict[str, Any]], paid_only: bool) -> list[tuple[Any, ...]]:
        """Return ``(value, is_paid, created_at, updated_at)`` rows, sorted by value."""
        return [
            (value, entry["is_paid"], entry["created_at"], entry["updated_at"])
            for value, entry in sorted(store.items())
            if entry["is_paid"] or not paid_only
        ]

    def activate_paid_entry(self, store: dict[str, dict[str, Any]], value: str) -> None:
        """Upsert ``value`` to is_paid=true, keeping created_at on reactivation."""
        existing = store.get(value)
        store[value] = {
            "is_paid": True,
            "created_at": existing["created_at"] if existing else _PAID_ENTRY_CREATED_AT,
            "updated_at": _PAID_ENTRY_UPDATED_AT,
        }

    def deactivate_paid_entry(self, store: dict[str, dict[str, Any]], value: str) -> None:
        """Soft-delete ``value`` (is_paid=false). No-op when absent."""
        existing = store.get(value)
        if existing is not None:
            existing["is_paid"] = False
            existing["updated_at"] = _PAID_ENTRY_UPDATED_AT

    def install_on_app_module(self, app_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Swap DB and SSH functions on the app module with fakes.

        Uses the same single-loop-setattr pattern as FakeSuperTokensBackend to
        minimize the test-patching ratchet count.
        """
        fakes: dict[str, Any] = {
            "_get_pool_db_connection": self.get_connection,
            "_append_authorized_key": self.append_authorized_key,
            "clean_up_slice_on_box": self.clean_up_slice_on_box,
        }
        for name, fake in fakes.items():
            monkeypatch.setattr(app_mod, name, fake)

    def get_connection(self) -> FakeConnection:
        return _make_fake_connection(self)

    def find_sync_record(self, user_id: str, host_id: str) -> dict[str, Any] | None:
        """Return the workspace-record row for (user_id, host_id), or None."""
        for row in self.sync_record_rows:
            if row["user_id"] == user_id and row["host_id"] == host_id:
                return row
        return None

    def sync_record_tuple(self, row: dict[str, Any]) -> tuple[Any, ...]:
        """Project a stored row into the SELECT column order PostgresSyncStore uses."""
        return tuple(row[name] for name in _WORKSPACE_RECORD_COLUMN_NAMES)

    def check_one_active_sync_record_per_agent(self, user_id: str, host_id: str, agent_id: str, state: str) -> None:
        """Enforce the partial unique index on (user_id, agent_id) WHERE state = 'active'."""
        if state != "active":
            return
        for row in self.sync_record_rows:
            if (
                row["user_id"] == user_id
                and row["host_id"] != host_id
                and row["agent_id"] == agent_id
                and row["state"] == "active"
            ):
                raise _OneActivePerAgentViolation(f"duplicate active workspace record for agent {agent_id}")

    def insert_sync_record(self, params: tuple[Any, ...]) -> dict[str, Any]:
        """Simulate the workspace_records INSERT, including its unique-violation modes."""
        (
            user_id,
            host_id,
            agent_id,
            display_name,
            color,
            provider_kind,
            hosting_device_id,
            device_label,
            state,
            restored_from_host_id,
            backup_kind,
            encrypted_secrets,
            revision,
        ) = params
        if self.sync_insert_race_winner is not None:
            winner = dict(self.sync_insert_race_winner)
            self.sync_insert_race_winner = None
            winner.setdefault("created_at", _SYNC_ROW_CREATED_AT)
            winner.setdefault("updated_at", _SYNC_ROW_CREATED_AT)
            self.sync_record_rows.append(winner)
            raise psycopg2.errors.UniqueViolation("concurrent insert won the primary key")
        if self.find_sync_record(user_id, host_id) is not None:
            raise psycopg2.errors.UniqueViolation(f"duplicate primary key ({user_id}, {host_id})")
        self.check_one_active_sync_record_per_agent(user_id, host_id, agent_id, state)
        row = {
            "user_id": user_id,
            "host_id": host_id,
            "agent_id": agent_id,
            "display_name": display_name,
            "color": color,
            "provider_kind": provider_kind,
            "hosting_device_id": hosting_device_id,
            "device_label": device_label,
            "state": state,
            "restored_from_host_id": restored_from_host_id,
            "backup_kind": backup_kind,
            "encrypted_secrets": _adapted_bytes(encrypted_secrets),
            "revision": revision,
            "created_at": _SYNC_ROW_CREATED_AT,
            "updated_at": _SYNC_ROW_CREATED_AT,
        }
        self.sync_record_rows.append(row)
        return row

    def update_sync_record(self, params: tuple[Any, ...]) -> dict[str, Any] | None:
        """Simulate the workspace_records CAS UPDATE; returns the updated row or None."""
        (
            agent_id,
            display_name,
            color,
            provider_kind,
            hosting_device_id,
            device_label,
            state,
            restored_from_host_id,
            backup_kind,
            encrypted_secrets,
            revision,
            user_id,
            host_id,
        ) = params
        if self.sync_update_returns_no_row:
            return None
        row = self.find_sync_record(user_id, host_id)
        if row is None:
            return None
        self.check_one_active_sync_record_per_agent(user_id, host_id, agent_id, state)
        row.update(
            agent_id=agent_id,
            display_name=display_name,
            color=color,
            provider_kind=provider_kind,
            hosting_device_id=hosting_device_id,
            device_label=device_label,
            state=state,
            restored_from_host_id=restored_from_host_id,
            backup_kind=backup_kind,
            encrypted_secrets=_adapted_bytes(encrypted_secrets),
            revision=revision,
            updated_at=_SYNC_ROW_UPDATED_AT,
        )
        return row

    def scrub_sync_secrets(self, user_id: str) -> int:
        """Null out every non-null encrypted_secrets for the user; returns the row count."""
        scrubbed = 0
        for row in self.sync_record_rows:
            if row["user_id"] == user_id and row["encrypted_secrets"] is not None:
                row["encrypted_secrets"] = None
                row["updated_at"] = _SYNC_ROW_UPDATED_AT
                scrubbed += 1
        return scrubbed

    def clean_up_slice_on_box(
        self,
        conn: Any,
        host_db_id: Any,
        bare_metal_server_id: Any,
        lima_instance_name: str | None,
        lima_disk_name: str | None,
    ) -> None:
        """Record a slice teardown (the real box SSH is not exercised in unit tests)."""
        if self.slice_teardown_should_fail:
            raise PoolHostCleanupError(f"simulated slice teardown failure for {host_db_id}")
        self.slice_teardowns.append((host_db_id, bare_metal_server_id, lima_instance_name, lima_disk_name))

    def append_authorized_key(
        self,
        host: str,
        port: int,
        user: str,
        management_key_pem: str,
        public_key_to_add: str,
        expected_host_public_key: str,
    ) -> None:
        self.append_key_calls.append(
            (host, port, user, management_key_pem, public_key_to_add, expected_host_public_key)
        )

    def add_available_host(
        self,
        host_id: UUID,
        version: str,
        vps_address: str = "203.0.113.10",
        ssh_port: int = 22,
        ssh_user: str = "root",
        container_ssh_port: int = 2222,
        agent_id: str = "agent-abc123",
        host_id_str: str = "host-xyz",
        host_name: str | None = None,
        region: str | None = None,
        outer_host_public_key: str | None = _FAKE_OUTER_HOST_PUBLIC_KEY,
        container_host_public_key: str | None = _FAKE_CONTAINER_HOST_PUBLIC_KEY,
    ) -> FakePoolRow:
        """Add an available host to the in-memory pool."""
        row = _make_pool_row(
            host_id=host_id,
            vps_address=vps_address,
            agent_id=agent_id,
            host_id_str=host_id_str,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            container_ssh_port=container_ssh_port,
            version=version,
            host_name=host_name,
            region=region,
            outer_host_public_key=outer_host_public_key,
            container_host_public_key=container_host_public_key,
        )
        self.pool_rows.append(row)
        return row

    def add_leased_host(
        self,
        host_id: UUID,
        version: str,
        leased_to_user: str,
        vps_address: str = "203.0.113.10",
        ssh_port: int = 22,
        ssh_user: str = "root",
        container_ssh_port: int = 2222,
        agent_id: str = "agent-abc123",
        host_id_str: str = "host-xyz",
        host_name: str | None = None,
    ) -> FakePoolRow:
        """Add a leased host to the in-memory pool."""
        row = _make_pool_row(
            host_id=host_id,
            vps_address=vps_address,
            agent_id=agent_id,
            host_id_str=host_id_str,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            container_ssh_port=container_ssh_port,
            version=version,
            status="leased",
            leased_to_user=leased_to_user,
            leased_at="2026-01-01T00:00:00+00:00",
            host_name=host_name,
        )
        self.pool_rows.append(row)
        return row

    def add_removing_host(
        self,
        host_id: UUID,
        version: str,
        leased_to_user: str = "some-user",
        vps_address: str = "203.0.113.10",
        agent_id: str = "agent-abc123",
        host_id_str: str = "host-xyz",
    ) -> FakePoolRow:
        """Add a host already marked 'removing' (an interrupted release) to the pool."""
        row = _make_pool_row(
            host_id=host_id,
            vps_address=vps_address,
            agent_id=agent_id,
            host_id_str=host_id_str,
            ssh_port=22,
            ssh_user="root",
            container_ssh_port=2222,
            version=version,
            status="removing",
            leased_to_user=leased_to_user,
            leased_at="2026-01-01T00:00:00+00:00",
        )
        row.released_at = "2026-01-02T00:00:00+00:00"
        self.pool_rows.append(row)
        return row


def make_fake_pool_backend() -> FakePoolBackend:
    """Construct an empty in-memory pool backend (no pool rows, empty paid lists)."""
    backend = FakePoolBackend()
    backend.pool_rows = []
    backend.append_key_calls = []
    backend.slice_teardowns = []
    backend.slice_teardown_should_fail = False
    backend.paid_domains = {}
    backend.paid_emails = {}
    backend.sync_record_rows = []
    backend.sync_bundle_by_user = {}
    backend.sync_insert_race_winner = None
    backend.sync_update_returns_no_row = False
    return backend


class InMemorySyncStore:
    """In-memory SyncStore implementation for testing the workspace-sync endpoints.

    Mirrors PostgresSyncStore's semantics: CAS on revision for updates, at
    most one ACTIVE record per (user_id, agent_id), scrub, and the per-user
    key bundle. Records are keyed (user_id, host_id); secrets are raw bytes.
    """

    def __init__(self) -> None:
        self.records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self.bundle_by_user_id: dict[str, dict[str, Any]] = {}
        self._created_counter = 0

    def _next_timestamp(self) -> str:
        self._created_counter += 1
        return f"2026-01-01T00:00:{self._created_counter:02d}+00:00"

    def _encode_secrets(self, record: dict[str, Any]) -> dict[str, Any]:
        encoded = dict(record)
        secrets_bytes = record.get("encrypted_secrets")
        encoded["encrypted_secrets"] = (
            base64.b64encode(secrets_bytes).decode("ascii") if secrets_bytes is not None else None
        )
        return encoded

    def list_records(self, user_id: str) -> list[dict[str, Any]]:
        rows = [self._encode_secrets(record) for (uid, _), record in self.records_by_key.items() if uid == user_id]
        return sorted(rows, key=lambda record: record["created_at"])

    def put_record(self, user_id: str, record: dict[str, Any]) -> dict[str, Any]:
        key = (user_id, record["host_id"])
        existing = self.records_by_key.get(key)
        if existing is not None and record["revision"] != existing["revision"] + 1:
            raise SyncRevisionConflictError(self._encode_secrets(existing))
        if record["state"] == "active":
            for (uid, host_id), other in self.records_by_key.items():
                is_other_row = uid == user_id and host_id != record["host_id"]
                if is_other_row and other["agent_id"] == record["agent_id"] and other["state"] == "active":
                    raise SyncActiveAgentConflictError(
                        f"another ACTIVE record already exists for agent {record['agent_id']}"
                    )
        stored = dict(record)
        stored["created_at"] = existing["created_at"] if existing is not None else self._next_timestamp()
        stored["updated_at"] = self._next_timestamp()
        self.records_by_key[key] = stored
        return self._encode_secrets(stored)

    def delete_record(self, user_id: str, host_id: str) -> None:
        self.records_by_key.pop((user_id, host_id), None)

    def scrub_secrets(self, user_id: str) -> int:
        scrubbed = 0
        for (uid, _), record in self.records_by_key.items():
            if uid == user_id and record.get("encrypted_secrets") is not None:
                record["encrypted_secrets"] = None
                record["updated_at"] = self._next_timestamp()
                scrubbed += 1
        return scrubbed

    def get_bundle(self, user_id: str) -> dict[str, Any] | None:
        bundle = self.bundle_by_user_id.get(user_id)
        if bundle is None:
            return None
        encoded = dict(bundle)
        encoded["kdf_salt"] = base64.b64encode(bundle["kdf_salt"]).decode("ascii")
        encoded["wrapped_dek"] = base64.b64encode(bundle["wrapped_dek"]).decode("ascii")
        return encoded

    def put_bundle(self, user_id: str, bundle: dict[str, Any]) -> None:
        stored = dict(bundle)
        stored["updated_at"] = self._next_timestamp()
        self.bundle_by_user_id[user_id] = stored

    def delete_bundle(self, user_id: str) -> None:
        self.bundle_by_user_id.pop(user_id, None)


def make_fake_sync_store() -> InMemorySyncStore:
    """Construct an empty in-memory SyncStore for tests."""
    return InMemorySyncStore()
