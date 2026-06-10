"""Tiny mail.tm HTTP client used by the realistic-signup test.

The orchestrator creates one disposable mail.tm account per run and
exports its address + JWT as env vars (``MAILTM_ACCOUNT_ADDRESS``,
``MAILTM_ACCOUNT_JWT``). Per-test signups use ``+<uuid>`` local-part
suffixes against that shared address so we never have to provision a
mail.tm account inside a test.

This module is private (``_mailtm``) -- the public entrypoint is the
``signup_email`` fixture in ``conftest.py``.
"""

import re
import time
from datetime import datetime
from datetime import timezone
from typing import Final

import httpx
from loguru import logger
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.primitives import InvalidMailtmAddressError
from imbue.minds.deployment_tests.primitives import MailtmAddress
from imbue.minds.deployment_tests.primitives import MailtmFetchError
from imbue.minds.deployment_tests.primitives import OneTimeLoginCode
from imbue.minds.deployment_tests.primitives import SignupEmailAddress
from imbue.minds.deployment_tests.primitives import VerificationToken

_MAILTM_API_BASE: Final[str] = "https://api.mail.tm"

# How long to wait for an inbound email before giving up. Generous because
# real email delivery can take many seconds.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_POLL_INTERVAL_SECONDS: Final[float] = 2.0


class _MailtmMessage(FrozenModel):
    """One message returned by mail.tm's inbox listing endpoint."""

    id: NonEmptyStr
    to_addresses: tuple[str, ...]
    subject: str
    created_at: datetime


class MailtmInbox(MutableModel):
    """Pydantic-modeled view of the per-run mail.tm account, scoped to one local-part.

    A test holds a :class:`MailtmInbox` for ``signup-<uuid>+<uuid>@<host>``
    and uses :meth:`wait_for_verification_token` /
    :meth:`wait_for_one_time_code` to poll for the matching inbound
    email, respecting the ``+<uuid>`` filter so concurrent tests'
    messages do not collide. Tracks seen-message ids as private state to
    de-duplicate across poll iterations.
    """

    address: SignupEmailAddress
    account_address: MailtmAddress
    jwt: SecretStr

    _seen_message_ids: set[str] = PrivateAttr(default_factory=set)

    def wait_for_verification_token(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> VerificationToken:
        """Poll mail.tm for an email-verification message and return the extracted token.

        Looks for the verification-link token in the email body using a
        regex on the canonical URL shape minds emits. Raises
        :class:`MailtmFetchError` on timeout or if the message lacks a
        recognizable token.
        """
        body = self._wait_for_message_body(timeout_seconds=timeout_seconds, subject_substring="verify")
        match = re.search(r"verify[^?]*\?token=([^\s&\"]+)", body, re.IGNORECASE)
        if match is None:
            raise MailtmFetchError(
                f"Verification email for {self.address!r} arrived but did not contain a recognizable token: "
                f"body excerpt={body[:200]!r}"
            )
        return VerificationToken(match.group(1))

    def wait_for_one_time_code(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> OneTimeLoginCode:
        """Poll mail.tm for a one-time-code sign-in email and return the extracted code.

        Looks for the first 6-10 character case-insensitive alphanumeric
        token bounded by word boundaries in the email body. Raises
        :class:`MailtmFetchError` on timeout or unrecognized format.
        """
        body = self._wait_for_message_body(timeout_seconds=timeout_seconds, subject_substring="sign")
        match = re.search(r"\b([A-Z0-9]{6,10})\b", body, re.IGNORECASE)
        if match is None:
            raise MailtmFetchError(
                f"Sign-in email for {self.address!r} arrived but did not contain a recognizable one-time code: "
                f"body excerpt={body[:200]!r}"
            )
        return OneTimeLoginCode(match.group(1))

    def _wait_for_message_body(self, *, timeout_seconds: float, subject_substring: str) -> str:
        deadline = time.monotonic() + timeout_seconds
        last_error: httpx.HTTPError | None = None
        while time.monotonic() < deadline:
            try:
                message = self._poll_for_new_message(subject_substring=subject_substring)
                if message is not None:
                    body = self._fetch_message_body(message.id)
                    # Mark the message id seen only after the body fetch succeeds, so a
                    # transient fetch failure leaves the message available for the next
                    # iteration to retry instead of being silently filtered out.
                    self._seen_message_ids.add(message.id)
                    return body
            except httpx.HTTPError as exc:
                last_error = exc
            # Polling a remote HTTP API (mail.tm) for inbound email; time.sleep is
            # the right primitive for this case (no event-driven alternative
            # without standing up an IMAP listener). Counted in the
            # PREVENT_TIME_SLEEP ratchet's snapshot in test_ratchets.py.
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise MailtmFetchError(
            f"Timed out after {timeout_seconds:.0f}s waiting for an email to {self.address!r} "
            f"matching subject substring {subject_substring!r}. last_http_error={last_error!r}"
        )

    def _poll_for_new_message(self, *, subject_substring: str) -> _MailtmMessage | None:
        """Return one unseen inbox message matching the filters, or ``None``."""
        with httpx.Client(base_url=_MAILTM_API_BASE, timeout=10.0) as client:
            response = client.get(
                "/messages",
                headers={"Authorization": f"Bearer {self.jwt.get_secret_value()}"},
                params={"page": 1},
            )
            response.raise_for_status()
            for raw in response.json().get("hydra:member", []):
                message_id = raw.get("id")
                if not message_id or message_id in self._seen_message_ids:
                    continue
                to_addresses = tuple(addr.get("address", "") for addr in raw.get("to", []) if addr.get("address"))
                if str(self.address) not in to_addresses:
                    continue
                subject = raw.get("subject", "")
                if subject_substring.lower() not in subject.lower():
                    continue
                return _MailtmMessage(
                    id=NonEmptyStr(message_id),
                    to_addresses=to_addresses,
                    subject=subject,
                    created_at=_parse_iso_timestamp(raw.get("createdAt", "")),
                )
        return None

    def _fetch_message_body(self, message_id: str) -> str:
        with httpx.Client(base_url=_MAILTM_API_BASE, timeout=10.0) as client:
            response = client.get(
                f"/messages/{message_id}",
                headers={"Authorization": f"Bearer {self.jwt.get_secret_value()}"},
            )
            response.raise_for_status()
            data = response.json()
        text = data.get("text") or ""
        html = data.get("html") or []
        if isinstance(html, list):
            html = "\n".join(str(part) for part in html)
        return f"{text}\n{html}"


def make_signup_address(account_address: MailtmAddress, suffix: str) -> SignupEmailAddress:
    """Build a fresh ``<local>+<suffix>@<domain>`` address against the shared mail.tm account.

    The orchestrator's mail.tm account is e.g. ``test-runner-abc123@<host>``;
    a test that needs an isolated inbox passes its own ``suffix`` (e.g.
    a per-test uuid) and reads only matching messages back via
    :class:`MailtmInbox`.
    """
    if "@" not in str(account_address):
        raise InvalidMailtmAddressError(f"mail.tm account address {account_address!r} is malformed: missing '@'.")
    local, _, domain = str(account_address).partition("@")
    return SignupEmailAddress(f"{local}+{suffix}@{domain}")


def _parse_iso_timestamp(raw: str) -> datetime:
    """Parse mail.tm's ISO timestamps; falls back to ``epoch`` on unparseable input."""
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable mail.tm timestamp {!r}; falling back to epoch.", raw)
        return datetime.fromtimestamp(0, tz=timezone.utc)
