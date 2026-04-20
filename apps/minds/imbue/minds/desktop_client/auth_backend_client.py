"""HTTP client for the `cloudflare_forwarding` server's `/auth/*` endpoints.

The `cloudflare_forwarding` service holds the SuperTokens API key and OAuth
client secrets. All supertokens operations that the desktop client used to
perform directly via the SDK are now proxied through that service. This module
provides a small, typed HTTP client used by the desktop client's auth routes
and session refresh logic.
"""

import httpx
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

_DEFAULT_TIMEOUT_SECONDS = 30.0


class SessionTokens(FrozenModel):
    """Access + refresh token pair issued by the auth backend."""

    access_token: str = Field(description="SuperTokens JWT access token")
    refresh_token: str | None = Field(default=None, description="SuperTokens refresh token")


class AuthUser(FrozenModel):
    """User information returned by the auth backend."""

    user_id: str = Field(description="SuperTokens user ID")
    email: str = Field(description="User email address")
    display_name: str | None = Field(default=None, description="OAuth display name, if any")


class AuthResult(FrozenModel):
    """Normalized result of a sign-in / sign-up / OAuth callback."""

    status: str = Field(description="OK, WRONG_CREDENTIALS, EMAIL_ALREADY_EXISTS, FIELD_ERROR, or ERROR")
    message: str | None = Field(default=None, description="Human-readable message for non-OK statuses")
    user: AuthUser | None = Field(default=None, description="User info when status is OK")
    tokens: SessionTokens | None = Field(default=None, description="Session tokens when status is OK")
    needs_email_verification: bool = Field(
        default=False,
        description="True when the account's email has not yet been verified",
    )


class AuthBackendError(RuntimeError):
    """Raised when the auth backend returns a non-2xx, non-handled response."""


class AuthBackendClient(FrozenModel):
    """Thin HTTP client for the `/auth/*` endpoints on `cloudflare_forwarding`."""

    base_url: AnyUrl = Field(description="Base URL of the cloudflare_forwarding server")
    timeout_seconds: float = Field(default=_DEFAULT_TIMEOUT_SECONDS, description="HTTP request timeout")

    def _url(self, path: str) -> str:
        return str(self.base_url).rstrip("/") + path

    def _parse_auth_result(self, response: httpx.Response) -> AuthResult:
        if response.status_code == 503:
            raise AuthBackendError("Auth backend is not configured on the server (503)")
        if response.status_code >= 500:
            raise AuthBackendError(f"Auth backend returned {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise AuthBackendError("Auth backend returned non-JSON response") from exc
        return AuthResult.model_validate(data)

    def signup(self, email: str, password: str) -> AuthResult:
        """Create a new email/password account."""
        response = httpx.post(
            self._url("/auth/signup"),
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        return self._parse_auth_result(response)

    def signin(self, email: str, password: str) -> AuthResult:
        """Authenticate with email/password and return a session."""
        response = httpx.post(
            self._url("/auth/signin"),
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        return self._parse_auth_result(response)

    def refresh_session(self, refresh_token: str) -> SessionTokens | None:
        """Exchange a refresh token for a new access/refresh token pair."""
        response = httpx.post(
            self._url("/auth/session/refresh"),
            json={"refresh_token": refresh_token},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 500:
            logger.warning("Auth backend session refresh failed: {}", response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("Auth backend session refresh returned non-JSON response")
            return None
        if data.get("status") != "OK":
            logger.debug("Auth backend rejected refresh: {}", data.get("message"))
            return None
        tokens_raw = data.get("tokens")
        if not tokens_raw:
            return None
        return SessionTokens.model_validate(tokens_raw)

    def send_verification_email(self, user_id: str, email: str) -> bool:
        """(Re)send the verification email for a given user."""
        response = httpx.post(
            self._url("/auth/email/send-verification"),
            json={"user_id": user_id, "email": email},
            timeout=self.timeout_seconds,
        )
        return response.status_code == 200

    def is_email_verified(self, user_id: str, email: str) -> bool:
        """Return whether the given user's email is verified."""
        response = httpx.post(
            self._url("/auth/email/is-verified"),
            json={"user_id": user_id, "email": email},
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            return False
        try:
            data = response.json()
        except ValueError:
            return False
        return bool(data.get("verified"))

    def forgot_password(self, email: str) -> None:
        """Send a password reset email for the given address (always succeeds)."""
        httpx.post(
            self._url("/auth/password/forgot"),
            json={"email": email},
            timeout=self.timeout_seconds,
        )

    def reset_password(self, token: str, new_password: str) -> tuple[str, str | None]:
        """Consume a password reset token and set a new password.

        Returns ``(status, message)`` matching the auth backend's contract.
        """
        response = httpx.post(
            self._url("/auth/password/reset"),
            json={"token": token, "new_password": new_password},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 500:
            return "ERROR", f"Server error: {response.status_code}"
        try:
            data = response.json()
        except ValueError:
            return "ERROR", "Malformed response"
        return str(data.get("status", "ERROR")), data.get("message")

    def oauth_authorize_url(self, provider_id: str, callback_url: str) -> str | None:
        """Return the URL to which the user should be redirected to begin OAuth."""
        response = httpx.post(
            self._url("/auth/oauth/authorize"),
            json={"provider_id": provider_id, "callback_url": callback_url},
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            logger.warning("Auth backend OAuth authorize failed: {}", response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if data.get("status") != "OK":
            logger.warning("Auth backend OAuth authorize rejected: {}", data.get("message"))
            return None
        url = data.get("url")
        return str(url) if url else None

    def oauth_callback(
        self,
        provider_id: str,
        callback_url: str,
        query_params: dict[str, str],
    ) -> AuthResult:
        """Exchange OAuth callback query params for a supertokens session."""
        response = httpx.post(
            self._url("/auth/oauth/callback"),
            json={
                "provider_id": provider_id,
                "callback_url": callback_url,
                "query_params": query_params,
            },
            timeout=self.timeout_seconds,
        )
        return self._parse_auth_result(response)

    def get_user_provider(self, user_id: str) -> str:
        """Return the login provider for a user ('email' or a third-party ID)."""
        response = httpx.get(
            self._url(f"/auth/users/{user_id}"),
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            return "email"
        try:
            data = response.json()
        except ValueError:
            return "email"
        return str(data.get("provider", "email"))
