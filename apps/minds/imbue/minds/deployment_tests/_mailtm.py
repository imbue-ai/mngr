"""Tiny mail.tm HTTP client used by the realistic-signup tests.

A test run creates one disposable mail.tm account and exports its address +
JWT as env vars (``MAILTM_ACCOUNT_ADDRESS``, ``MAILTM_ACCOUNT_JWT``). The
deployment tests' per-test signups use ``+<uuid>`` local-part suffixes against
that shared address so no test has to provision a mail.tm account itself; the
smoke suite's throwaway account signs up as the mailbox address itself.

:func:`create_mailtm_account` and :func:`delete_mailtm_account` are the
account lifecycle both orchestrators drive (``test_deployments.py`` and
``smoke_tests.py``), with the bookkeeping each one keeps left to the caller.
For the mailbox itself, the public entrypoint is the ``signup_email`` fixture
in ``conftest.py``.
"""

import re
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from typing import Final
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.primitives import InvalidMailtmAddressError
from imbue.minds.deployment_tests.primitives import MailtmAccountId
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
# Account create / delete are single round-trips against mail.tm's own API.
_ACCOUNT_API_TIMEOUT_SECONDS: Final[float] = 20.0


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
        regex on the canonical URL shape Imbue Studio emits. Raises
        :class:`MailtmFetchError` on timeout or if the message lacks a
        recognizable token.
        """
        # "verif", not "verify": the connector's verification email is
        # subject-lined "Email verification instructions", which contains
        # "verification" but not "verify". The shared prefix still excludes the
        # one-time sign-in email ("sign").
        body = self._wait_for_message_body(timeout_seconds=timeout_seconds, subject_substring="verif")
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


def wait_for_verification_token_with_resends(
    inbox: MailtmInbox,
    *,
    resend_verification_email: Callable[[], None],
    # How long each round waits, which must exceed the connector's per-user send cooldown for the resend to land.
    wait_seconds_per_round: float,
    round_count: int,
) -> VerificationToken:
    """Poll the mailbox for the verification token, resending after the cooldown on a miss.

    The caller has already dispatched the first email. A single silently
    dropped send (the SuperTokens SDK swallows send failures) is healed by the
    resend; a sustained outage of the hosted email path exhausts the rounds
    and re-raises the final round's :class:`MailtmFetchError`.
    """
    for round_idx in range(round_count):
        try:
            return inbox.wait_for_verification_token(timeout_seconds=wait_seconds_per_round)
        except MailtmFetchError:
            if round_idx == round_count - 1:
                raise
            logger.info("No verification email for {} after round {}; resending", inbox.address, round_idx + 1)
            resend_verification_email()
    raise MailtmFetchError(f"No verification rounds were run for {inbox.address!r} (round_count={round_count})")


class MailtmAccount(FrozenModel):
    """A disposable mail.tm account a test run owns for its lifetime.

    Holds what the owning orchestrator needs afterwards: the ``account_id`` to
    delete it by, the ``jwt`` that authenticates both the deletion and the
    inbox polling, and the ``address`` handed to the tests. The password is
    consumed once by ``/token`` during creation and deliberately not retained.
    """

    account_id: MailtmAccountId
    address: MailtmAddress
    jwt: SecretStr


def create_mailtm_account(*, local_part: str) -> MailtmAccount:
    """Create a disposable mail.tm account at ``<local_part>@<a mail.tm domain>``.

    mail.tm hands out the domain, so the caller chooses only the local part
    (which is how a run stamps its own prefix and id onto the address). The
    created address is logged before the JWT is minted, so a mailbox whose
    token mint failed still leaves a trail in the run's log.
    """
    with httpx.Client(base_url=_MAILTM_API_BASE, timeout=_ACCOUNT_API_TIMEOUT_SECONDS) as client:
        domains_response = client.get("/domains")
        domains_response.raise_for_status()
        domains = domains_response.json().get("hydra:member", [])
        if not domains:
            raise MailtmFetchError("mail.tm returned an empty domains list; cannot create a test account.")
        address = MailtmAddress(f"{local_part}@{domains[0]['domain']}")
        password = uuid4().hex
        account_response = client.post("/accounts", json={"address": str(address), "password": password})
        account_response.raise_for_status()
        account_id = MailtmAccountId(account_response.json()["id"])
        logger.info("Created mail.tm account {} ({})", address, account_id)
        token_response = client.post("/token", json={"address": str(address), "password": password})
        token_response.raise_for_status()
        jwt = SecretStr(token_response.json()["token"])
    return MailtmAccount(account_id=account_id, address=address, jwt=jwt)


def delete_mailtm_account(*, account_id: MailtmAccountId, jwt: SecretStr) -> bool:
    """Delete a mail.tm account by id; False when it was already gone.

    Idempotent, which a re-run of a teardown needs. mail.tm answers 401 rather
    than 404 for an account that no longer exists, because the JWT is scoped to
    the account it was minted for -- so a 401 counts as already-gone too. An
    actually-expired JWT therefore reads the same way, at the cost of leaving
    one disposable mailbox for mail.tm to expire on its own.
    """
    with httpx.Client(base_url=_MAILTM_API_BASE, timeout=_ACCOUNT_API_TIMEOUT_SECONDS) as client:
        response = client.delete(
            f"/accounts/{account_id}",
            headers={"Authorization": f"Bearer {jwt.get_secret_value()}"},
        )
        if response.status_code in (401, 404):
            return False
        response.raise_for_status()
        return True


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
