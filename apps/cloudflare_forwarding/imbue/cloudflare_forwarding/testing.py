"""Test utilities for cloudflare_forwarding."""

import base64
import json
import secrets
import uuid
from typing import Any

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
from supertokens_python.types import LoginMethod
from supertokens_python.types import RecipeUserId
from supertokens_python.types import User
from supertokens_python.types.base import AccountInfoInput

from imbue.cloudflare_forwarding.app import ForwardingCtx


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
# The cloudflare_forwarding service wraps the SuperTokens SDK behind /auth/*
# endpoints. Exercising those endpoints against a real SuperTokens core is
# slow (Docker) and unreliable in CI, so the tests install the fakes below as
# drop-in replacements for every SDK function the handlers call. The backend
# state (accounts, sessions, reset tokens) lives on a single
# ``FakeSuperTokensBackend`` instance; ``install_supertokens_fakes`` swaps the
# SDK references on ``cloudflare_forwarding.app`` over to methods on that
# instance. Swapping the ``app`` module's bound references (rather than the
# SDK's source module) means handlers see fakes without needing to initialize
# the real SuperTokens SDK, which would fail without a live core.
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
        webauthn=None,
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

    async def sign_up(
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

    async def sign_in(
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

    async def is_email_verified(
        self,
        *,
        recipe_user_id: RecipeUserId,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> bool:
        del email, user_context
        account = self.accounts_by_id.get(recipe_user_id.get_as_string())
        return account is not None and account.is_verified

    async def send_email_verification_email(
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

    async def create_new_session(
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

    async def refresh_session(
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

    async def revoke_all_sessions_for_user(
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

    async def list_users_by_account_info(
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

    async def send_reset_password_email(
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

    async def consume_password_reset_token(
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

    async def update_email_or_password(
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

    async def verify_email_using_token(
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

    async def get_provider(
        self,
        *,
        tenant_id: str,
        third_party_id: str,
        client_type: str | None = None,
        user_context: dict[str, Any] | None = None,
    ) -> FakeProvider | None:
        del tenant_id, client_type, user_context
        return self.registered_providers.get(third_party_id)

    async def manually_create_or_update_user(
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
