"""Frozen data types the smoke suite and its CI-job tooling pass between each other.

The central one is :class:`SmokeRunHandoff`: the setup job mints a throwaway
account and a mail.tm mailbox, and the test and teardown jobs -- different
processes, on different runners, holding different credentials -- need exactly
that. It carries its own three representations so the same object survives
every hop a run makes: env vars (what the CI test job reads), a JSON state file
(what a local run reads), and Vault leaves (what CI hands between its jobs).
"""

import json
from collections.abc import Mapping
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Final
from typing import Self

from playwright.sync_api import Browser
from playwright.sync_api import Page
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.deployment_tests.primitives import MailtmAccountId
from imbue.minds.deployment_tests.primitives import MailtmAddress
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.smoke_tests.primitives import MAILTM_ACCOUNT_ADDRESS_VAULT_KEY
from imbue.minds.smoke_tests.primitives import MAILTM_ACCOUNT_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import MAILTM_ACCOUNT_ID_VAULT_KEY
from imbue.minds.smoke_tests.primitives import MAILTM_ACCOUNT_JWT_VAULT_KEY
from imbue.minds.smoke_tests.primitives import SMOKE_RUN_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_EMAIL_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_USER_ID_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.primitives import THROWAWAY_ACCOUNT_EMAIL_VAULT_KEY
from imbue.minds.smoke_tests.primitives import THROWAWAY_ACCOUNT_PASSWORD_VAULT_KEY
from imbue.minds.smoke_tests.primitives import THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY

# Every env var one run's handoff travels through, which is also the key set of
# its JSON state file.
HANDOFF_ENV_VARS: Final[tuple[str, ...]] = (
    SMOKE_RUN_ID_ENV_VAR,
    SMOKE_TIER_ENV_VAR,
    SMOKE_THROWAWAY_EMAIL_ENV_VAR,
    SMOKE_THROWAWAY_PASSWORD_ENV_VAR,
    SMOKE_THROWAWAY_USER_ID_ENV_VAR,
    MAILTM_ADDRESS_ENV_VAR,
    MAILTM_JWT_ENV_VAR,
    MAILTM_ACCOUNT_ID_ENV_VAR,
)
# The same values as Vault leaves. The run id lives in the Vault path and the
# tier is named by the reader, so neither is a leaf.
HANDOFF_VAULT_KEYS: Final[tuple[str, ...]] = (
    THROWAWAY_ACCOUNT_EMAIL_VAULT_KEY,
    THROWAWAY_ACCOUNT_PASSWORD_VAULT_KEY,
    THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY,
    MAILTM_ACCOUNT_ADDRESS_VAULT_KEY,
    MAILTM_ACCOUNT_JWT_VAULT_KEY,
    MAILTM_ACCOUNT_ID_VAULT_KEY,
)


class SmokeBackend(FrozenModel):
    """The deployed Imbue Studio environment a smoke run tests against.

    Resolved from the env's ``client.toml`` and its tier's ``deploy.toml``, so
    no scenario ever names a URL itself.
    """

    env_name: DevEnvName = Field(description="The minds env name: ``production``, ``staging``, or a dev / ci env.")
    tier: NonEmptyStr = Field(
        description="The tier the env belongs to (``production``, ``staging``, ``dev``, ``ci``)."
    )
    connector_url: AnyUrl = Field(description="Base URL of the env's ``remote_service_connector``.")
    litellm_proxy_url: AnyUrl = Field(description="Base URL of the env's LiteLLM proxy.")
    accounts_origin: AnyUrl = Field(
        description="Origin of the hosted accounts surface (the connector URL on tiers without a dedicated one)."
    )
    chrome_origin: AnyUrl | None = Field(
        description="Origin of the hosted web chrome, or None on tiers without an ``[origins]`` block."
    )
    legacy_chrome_origins: tuple[AnyUrl, ...] = Field(
        description="Former web-chrome origins the connector must redirect to ``chrome_origin``."
    )
    update_feed_base_url: AnyUrl | None = Field(
        description="Root URL of the release feed, or None on tiers that publish no channel manifests."
    )
    is_waitlist_enabled: bool = Field(
        description="Whether the tier's deploy config waitlists accounts created without a signup code."
    )


class SmokeAccountCredentials(FrozenModel):
    """An email and password a smoke run signs in with."""

    email: NonEmptyStr = Field(description="The account's address, which always carries the smoke prefix.")
    password: SecretStr = Field(description="The account's password.")


class ThrowawayAccount(FrozenModel):
    """The per-run account the signup test verifies its email as.

    Minted unverified by the setup job -- its address is its mail.tm mailbox --
    and deleted by the teardown job, so no two runs ever share one.
    """

    email: MailtmAddress = Field(description="The account's address, which is its mail.tm mailbox.")
    password: SecretStr = Field(description="The password the account was created with.")
    user_id: NonEmptyStr = Field(description="The SuperTokens user id the signup answered with.")

    def as_credentials(self) -> SmokeAccountCredentials:
        """The account as something to sign in with."""
        return SmokeAccountCredentials(email=NonEmptyStr(str(self.email)), password=self.password)


class SmokeRunHandoff(FrozenModel):
    """What one smoke run's setup job created, as its test and teardown jobs read it.

    ``to_env`` is the canonical form: the state file is that mapping as JSON,
    and the Vault leaves are the same values under the names the per-job Vault
    policies grant.
    """

    run_id: SmokeRunId = Field(description="The run this handoff belongs to.")
    tier: NonEmptyStr = Field(description="The tier the throwaway account was minted on.")
    throwaway: ThrowawayAccount = Field(description="The run's throwaway account.")
    mailtm_address: MailtmAddress = Field(description="The mail.tm mailbox backing the throwaway address.")
    mailtm_jwt: SecretStr = Field(description="Bearer for the mail.tm API the signup test polls the mailbox with.")
    mailtm_account_id: MailtmAccountId = Field(description="The mail.tm account id teardown deletes the mailbox by.")

    def to_env(self) -> dict[str, str]:
        """The env vars a job needs in order to act as this run."""
        return {
            SMOKE_RUN_ID_ENV_VAR: str(self.run_id),
            SMOKE_TIER_ENV_VAR: str(self.tier),
            SMOKE_THROWAWAY_EMAIL_ENV_VAR: str(self.throwaway.email),
            SMOKE_THROWAWAY_PASSWORD_ENV_VAR: self.throwaway.password.get_secret_value(),
            SMOKE_THROWAWAY_USER_ID_ENV_VAR: str(self.throwaway.user_id),
            MAILTM_ADDRESS_ENV_VAR: str(self.mailtm_address),
            MAILTM_JWT_ENV_VAR: self.mailtm_jwt.get_secret_value(),
            MAILTM_ACCOUNT_ID_ENV_VAR: str(self.mailtm_account_id),
        }

    @classmethod
    def from_env_or_none(cls, environ: Mapping[str, str]) -> Self | None:
        """The handoff ``environ`` carries, or None when it carries none of it.

        None rather than an error because not every process is handed a run:
        a teardown invoked by hand reads its state file or Vault instead.
        """
        values = {name: environ.get(name, "") for name in HANDOFF_ENV_VARS}
        # The tier is exported by every smoke run, handoff or not, so it alone
        # does not mean a run was handed over.
        if not any(value for name, value in values.items() if name != SMOKE_TIER_ENV_VAR):
            return None
        require_handoff_values(values, source="the environment")
        return cls._from_env_values(values)

    def to_vault_values(self) -> dict[str, str]:
        """The leaves to write under ``run_handoff_vault_dir``."""
        return {
            THROWAWAY_ACCOUNT_EMAIL_VAULT_KEY: str(self.throwaway.email),
            THROWAWAY_ACCOUNT_PASSWORD_VAULT_KEY: self.throwaway.password.get_secret_value(),
            THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY: str(self.throwaway.user_id),
            MAILTM_ACCOUNT_ADDRESS_VAULT_KEY: str(self.mailtm_address),
            MAILTM_ACCOUNT_JWT_VAULT_KEY: self.mailtm_jwt.get_secret_value(),
            MAILTM_ACCOUNT_ID_VAULT_KEY: str(self.mailtm_account_id),
        }

    @classmethod
    def from_vault_values(cls, values: Mapping[str, str], *, run_id: SmokeRunId, tier: str) -> Self:
        """Rebuild a handoff from its Vault leaves, plus the run id and tier its reader named."""
        leaves = {key: values.get(key, "") for key in HANDOFF_VAULT_KEYS}
        require_handoff_values(leaves, source=f"the Vault handoff of run {str(run_id)!r}")
        return cls._from_env_values(
            {
                SMOKE_RUN_ID_ENV_VAR: str(run_id),
                SMOKE_TIER_ENV_VAR: tier,
                SMOKE_THROWAWAY_EMAIL_ENV_VAR: leaves[THROWAWAY_ACCOUNT_EMAIL_VAULT_KEY],
                SMOKE_THROWAWAY_PASSWORD_ENV_VAR: leaves[THROWAWAY_ACCOUNT_PASSWORD_VAULT_KEY],
                SMOKE_THROWAWAY_USER_ID_ENV_VAR: leaves[THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY],
                MAILTM_ADDRESS_ENV_VAR: leaves[MAILTM_ACCOUNT_ADDRESS_VAULT_KEY],
                MAILTM_JWT_ENV_VAR: leaves[MAILTM_ACCOUNT_JWT_VAULT_KEY],
                MAILTM_ACCOUNT_ID_ENV_VAR: leaves[MAILTM_ACCOUNT_ID_VAULT_KEY],
            }
        )

    def to_state_json(self) -> str:
        """The state file a local run writes: the env mapping, as JSON."""
        return json.dumps(self.to_env(), indent=2, sort_keys=True)

    @classmethod
    def from_state_json(cls, text: str) -> Self:
        """Rebuild a handoff from a state file written by :meth:`to_state_json`."""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SmokeConfigError(f"The smoke run state file is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SmokeConfigError(f"The smoke run state file holds a {type(parsed).__name__}, expected an object.")
        values = {name: str(parsed.get(name, "")) for name in HANDOFF_ENV_VARS}
        require_handoff_values(values, source="the smoke run state file")
        return cls._from_env_values(values)

    @classmethod
    def _from_env_values(cls, values: Mapping[str, str], /) -> Self:
        """Build a handoff from a complete env-var-keyed mapping (see :func:`require_handoff_values`)."""
        return cls(
            run_id=SmokeRunId(values[SMOKE_RUN_ID_ENV_VAR]),
            tier=NonEmptyStr(values[SMOKE_TIER_ENV_VAR]),
            throwaway=ThrowawayAccount(
                email=MailtmAddress(values[SMOKE_THROWAWAY_EMAIL_ENV_VAR]),
                password=SecretStr(values[SMOKE_THROWAWAY_PASSWORD_ENV_VAR]),
                user_id=NonEmptyStr(values[SMOKE_THROWAWAY_USER_ID_ENV_VAR]),
            ),
            mailtm_address=MailtmAddress(values[MAILTM_ADDRESS_ENV_VAR]),
            mailtm_jwt=SecretStr(values[MAILTM_JWT_ENV_VAR]),
            mailtm_account_id=MailtmAccountId(values[MAILTM_ACCOUNT_ID_ENV_VAR]),
        )


def require_handoff_values(values: Mapping[str, str], *, source: str) -> None:
    """Refuse a half-populated handoff, naming the keys ``source`` spells it with.

    A partial handoff is worse than none: the test job would act as an account
    the teardown job cannot find, so the run would leak it.
    """
    missing = sorted(key for key, value in values.items() if not value)
    if missing:
        raise SmokeConfigError(
            f"The smoke run handoff in {source} is missing {', '.join(missing)}; "
            "re-run the smoke setup command for this run id."
        )


class SmokeResourceKind(UpperCaseStrEnum):
    """What kind of resource a smoke run recorded in its manifest."""

    WORKSPACE = auto()
    SHARE = auto()


class SmokeRunManifestEntry(FrozenModel):
    """One resource a smoke test created, as the test job records it for teardown.

    The test job holds only user credentials, so it cannot delete anything
    itself; it appends a line here for every resource it creates, and the
    teardown job releases whatever the run leased even when the test process
    died before its own cleanup ran.
    """

    kind: SmokeResourceKind = Field(description="Which kind of resource the entry names.")
    identifier: NonEmptyStr = Field(description="The resource's id: a workspace's pool-row id, or a share's host id.")
    owner_email: NonEmptyStr = Field(description="The account that created it, so teardown knows whom to act as.")
    created_at: datetime = Field(description="When the test created it (timezone-aware UTC).")


class SmokeAccountDeletionReport(FrozenModel):
    """What the connector's finalize step reported for one account.

    Finalizing is what actually removes an account: the delete step only marks
    it and starts the grace period, so a teardown reads this -- not the delete
    answer -- as proof the account is gone.
    """

    user_id: NonEmptyStr = Field(description="The finalized account's SuperTokens user id.")
    finalized_at: NonEmptyStr = Field(description="When the connector finalized it.")
    rows_deleted_by_table: dict[str, int] = Field(
        description="Connector-DB rows the finalize step removed, keyed by table name."
    )
    is_litellm_user_deleted: bool = Field(description="Whether the account's LiteLLM user was removed.")


class ManifestArtifact(FrozenModel):
    """One installer a channel manifest lists, with the digest that identifies its bytes."""

    url: AnyUrl = Field(description="Where the CDN serves this installer.")
    sha512: NonEmptyStr = Field(description="The manifest's base64 SHA-512 of the installer's bytes.")


class UpdateManifest(FrozenModel):
    """The fields of an electron-updater channel manifest the smoke suite checks."""

    version: NonEmptyStr = Field(description="The app version the manifest offers (``major.minor.patch``).")
    rollout_percentage: int | None = Field(
        description="The manifest's ``stagingPercentage``, or None when it declares none (rolled out to 100%)."
    )
    artifacts: tuple[ManifestArtifact, ...] = Field(description="Every installer the manifest lists, in order.")


class WebChannelPin(FrozenModel):
    """A ``<channel>-web.json`` manifest: the default-workspace-template release tag browser creates pin to."""

    version: NonEmptyStr = Field(description="The app version the channel's desktop entry is at.")
    template_ref: NonEmptyStr = Field(
        description="The ``minds-v<version>`` template tag web creates lease an exact match on."
    )


class StudioBinaryKind(UpperCaseStrEnum):
    """Which kind of Imbue Studio build a smoke run launches."""

    PACKAGED = auto()
    DEV_CHECKOUT = auto()


class StudioBinary(FrozenModel):
    """How a smoke run launches the Imbue Studio app under test.

    A packaged build is one executable; the dev checkout is a bare Electron
    runtime that needs the checkout's main script handed to it.
    """

    kind: StudioBinaryKind = Field(description="A published / local packaged build, or this checkout's Electron.")
    executable: Path = Field(description="The program to run: the packaged app, or the checkout's Electron binary.")
    main_js: Path | None = Field(
        description="The Electron main script a dev checkout launch loads; None for a packaged build."
    )
    label: NonEmptyStr = Field(description="How a report names the build (its channel and version, or its path).")
    root_name: NonEmptyStr = Field(
        description=(
            "The ``MINDS_ROOT_NAME`` the app runs as: baked into a packaged build, exported to the dev checkout. "
            "It names the tier directory the app's data home is laid out under."
        )
    )


class StudioAppSession(FrozenModel):
    """One launched Imbue Studio app with Playwright attached to it over CDP."""

    model_config = {"arbitrary_types_allowed": True}

    browser: Browser = Field(description="The CDP connection to the app; its contexts hold every window.")
    page: Page = Field(description="The window's chrome page, on the app's own backend origin.")
    backend_origin: NonEmptyStr = Field(description="``http://localhost:<port>``: the app's own web UI and API.")
    data_home: Path = Field(description="The ``MINDS_DATA_HOME`` the app was launched with.")
    log_dir: Path = Field(
        description="Where the app writes ``minds.log``, ``electron.log`` and ``minds-events.jsonl``."
    )
