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
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
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
    stop_retention_seconds: NonNegativeInt | None = Field(
        description=(
            "How long a stopped workspace's machine keeps its place on its server, as the tier's deploy config "
            "stamps it; None when the tier stamps no window of its own."
        )
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
        # The tier and the run id name a smoke run whether or not it was handed
        # an account, so they alone do not mean a handoff.
        if not any(value for name, value in values.items() if name not in (SMOKE_TIER_ENV_VAR, SMOKE_RUN_ID_ENV_VAR)):
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
    data_home: Path = Field(description="The ``STUDIO_ROOTS_OVERRIDE`` the app was launched with.")
    state_dir: Path = Field(description="Where the app keeps its tier's state, its Sentry install id among it.")
    log_dir: Path = Field(
        description="Where the app writes ``minds.log``, ``electron.log`` and ``minds-events.jsonl``."
    )
    launch_cwd: Path = Field(description="The empty directory outside any checkout the app was started from.")
    mngr_executable: Path = Field(
        description="The app's own mngr: the packaged build's venv's, or this checkout's for the dev checkout."
    )
    mngr_host_dir: Path = Field(description="The ``MNGR_HOST_DIR`` the app runs mngr with, under its data home.")
    mngr_prefix: NonEmptyStr = Field(description="The ``MNGR_PREFIX`` the app runs mngr with.")


class DwtTestGroup(FrozenModel):
    """One test group of a default-workspace-template offload config: a pytest selection and its CI retry budget."""

    name: NonEmptyStr = Field(description="The group's key in the config (``all``, ``flaky``).")
    filters: str = Field(description="The pytest arguments selecting the group's tests, in shell syntax.")
    retry_count: NonNegativeInt = Field(
        description="How many more attempts CI gives the group's failed tests before they count as failed."
    )


class DwtTestSuite(FrozenModel):
    """One default-workspace-template pytest root, as the offload config its CI runs it with describes it."""

    name: NonEmptyStr = Field(description="The last segment of the config's report directory (``root``, ``chat``).")
    command: NonEmptyStr = Field(
        description="The command running this root's pytest from the repo root, in shell syntax."
    )
    run_args: str = Field(description="The arguments CI adds to every pytest run of this root, in shell syntax.")
    environment: dict[str, str] = Field(description="The variables CI sets for this root's pytest runs.")
    groups: tuple[DwtTestGroup, ...] = Field(description="The root's test groups, in the config's order.")


class DwtTestOutcome(UpperCaseStrEnum):
    """How a default-workspace-template test ended in the workspace, across the attempts CI would give it."""

    PASSED = auto()
    FAILED = auto()
    SKIPPED = auto()


class DwtJunitCase(FrozenModel):
    """One test case of a pytest junit report the runner left in the workspace."""

    test_id: NonEmptyStr = Field(description="The pytest node id, relative to the suite's root.")
    outcome: DwtTestOutcome = Field(description="How the case ended in that report.")
    detail: str = Field(description="Its failure text or skip reason; empty for a pass.")


class DwtGroupAttempt(FrozenModel):
    """One pytest run of a test group, as the runner left it in the workspace."""

    number: PositiveInt = Field(description="1 for the group's run, then one more per retry of its failures.")
    exit_code: int | None = Field(description="pytest's exit code, or None when the attempt never finished.")
    cases: tuple[DwtJunitCase, ...] | None = Field(description="Its junit report's cases, or None when it wrote none.")
    log_tail: str = Field(description="The end of the attempt's output, which says why a run went wrong.")


class DwtTestResult(FrozenModel):
    """One default-workspace-template test's result in the workspace, or one group run that reported no tests."""

    suite_name: NonEmptyStr = Field(description="The suite the test belongs to.")
    test_id: NonEmptyStr = Field(
        description="The pytest node id relative to the suite's root, or ``[<group> group]`` for a run's own failure."
    )
    outcome: DwtTestOutcome = Field(description="Its outcome on its last attempt; a pass on any attempt is a pass.")
    attempt_count: PositiveInt = Field(description="How many attempts ran it.")
    detail: str = Field(description="The failure or skip text of its last attempt; empty for a pass.")


class SmokeRunVerdict(FrozenModel):
    """What failed in one smoke session, for the workflow deciding whether a rerun could help."""

    dwt_failure_count: NonNegativeInt = Field(
        description="Failed default-workspace-template tests, which fail the same way on a rerun."
    )
    other_failure_count: NonNegativeInt = Field(
        description="Every other failure: a journey step, a probe, a fixture, or the run of the DWT suites itself."
    )


class SmokeStepEventKind(UpperCaseStrEnum):
    """What one line of a scenario's steps file says happened to a step."""

    STARTED = auto()
    COMPLETED = auto()
    SKIPPED = auto()


class SmokeStepEvent(FrozenModel):
    """One line of a scenario's steps file."""

    label: NonEmptyStr = Field(description="The step's name, as the scenario reports it.")
    kind: SmokeStepEventKind = Field(description="What happened to the step.")
    recorded_at: datetime = Field(description="When it happened (timezone-aware UTC).")
    detail: str = Field(description="Why a skipped step was skipped; empty otherwise.")


class SmokeStepOutcome(UpperCaseStrEnum):
    """How one step of a scenario ended: an unfinished step started and never completed."""

    COMPLETED = auto()
    UNFINISHED = auto()
    SKIPPED = auto()


class SmokeStepRecord(FrozenModel):
    """One step of a scenario, as its steps file says it went."""

    label: NonEmptyStr = Field(description="The step's name, as the scenario reports it.")
    outcome: SmokeStepOutcome = Field(description="How the step ended.")
    started_at: datetime = Field(description="When the step started, or was skipped.")
    finished_at: datetime | None = Field(description="When it completed or was skipped; None while unfinished.")
    detail: str = Field(description="Why a skipped step was skipped; empty otherwise.")


class SmokeScenarioOutcome(UpperCaseStrEnum):
    """How one scenario of a smoke run ended.

    An error is a failure outside the scenario's own body, in a fixture that
    sets it up or tears it down; a scenario not run was selected, but the
    session ended before it ran.
    """

    FAILED = auto()
    ERROR = auto()
    NOT_RUN = auto()
    PASSED = auto()
    SKIPPED = auto()


class SmokeTestPhase(UpperCaseStrEnum):
    """The pytest phase a scenario failed in."""

    SETUP = auto()
    CALL = auto()
    TEARDOWN = auto()


class SmokeSubtestResult(FrozenModel):
    """One subtest a scenario reported, such as one default-workspace-template test of the journey."""

    label: NonEmptyStr = Field(description="The subtest's label, e.g. ``dwt:<suite>::<node id>``.")
    outcome: SmokeScenarioOutcome = Field(description="Passed, failed or skipped.")
    duration_seconds: NonNegativeFloat = Field(description="How long the subtest's block ran.")
    summary: str = Field(description="Its failure message or skip reason; empty for a pass.")
    failure_text: str = Field(description="Its full failure report; empty unless it failed.")


class SmokeArtifactKind(UpperCaseStrEnum):
    """How the report shows a file a scenario left behind."""

    IMAGE = auto()
    FILE = auto()


class SmokeArtifact(FrozenModel):
    """One file a scenario wrote to its output directory during the run."""

    relative_path: NonEmptyStr = Field(description="The file's path relative to the report, with forward slashes.")
    kind: SmokeArtifactKind = Field(description="Whether the report shows the file inline as an image.")


class SmokeScenarioResult(FrozenModel):
    """One scenario (one test of the smoke suite) as the run's report shows it."""

    node_id: NonEmptyStr = Field(description="The pytest node id.")
    name: NonEmptyStr = Field(description="The test's name, with its parameters.")
    description: str = Field(description="The first paragraph of the test's docstring; empty when it has none.")
    source_path: NonEmptyStr = Field(description="The test file's path relative to the checkout root.")
    source_line: PositiveInt = Field(description="The line the test function starts on.")
    source_url: AnyUrl | None = Field(
        description="The test function at the run's commit; None when the commit is unknown."
    )
    outcome: SmokeScenarioOutcome = Field(description="How the scenario ended.")
    failed_phase: SmokeTestPhase | None = Field(description="Where it failed or errored; None otherwise.")
    started_at: datetime | None = Field(description="When its setup started; None when it was not run.")
    setup_seconds: NonNegativeFloat = Field(description="How long its fixtures took to set up.")
    call_seconds: NonNegativeFloat = Field(description="How long its body ran.")
    teardown_seconds: NonNegativeFloat = Field(description="How long its fixtures took to tear down.")
    summary: str = Field(description="Why it failed, errored or skipped, in a few lines; empty for a pass.")
    failure_text: str = Field(description="The full failure report of the phase it failed in.")
    captured_output: str = Field(description="The end of what that phase printed and logged.")
    subtests: tuple[SmokeSubtestResult, ...] = Field(description="Every subtest it reported, in order.")
    steps: tuple[SmokeStepRecord, ...] = Field(description="Every step its steps file records, in order.")
    output_dir_relative_path: str | None = Field(
        description="Its output directory relative to the report; None when it wrote none."
    )
    artifacts: tuple[SmokeArtifact, ...] = Field(description="The files it wrote to that directory during the run.")
    app_install_id: str | None = Field(
        description="The Sentry install id (``user.id``) of the app it launched; None when it launched none."
    )
    sentry_url: AnyUrl | None = Field(
        description="That install's Sentry issues around the scenario; None without an install id or SENTRY_ORG."
    )


class SmokeCollectionError(FrozenModel):
    """A test file of the suite that pytest could not collect."""

    node_id: NonEmptyStr = Field(description="The file's pytest node id.")
    summary: str = Field(description="Why it could not be collected, in a few lines.")
    failure_text: str = Field(description="The full collection error.")


class SmokeRunParameterSpec(FrozenModel):
    """One input a smoke run reads from its environment, as the report explains it."""

    env_var: NonEmptyStr = Field(description="The environment variable.")
    meaning: NonEmptyStr = Field(description="What the variable selects.")
    is_value_hidden: bool = Field(description="Whether the report shows only that it is set, never its value.")


class SmokeRunParameter(FrozenModel):
    """One input of a smoke run, with the value the run was given."""

    spec: SmokeRunParameterSpec = Field(description="Which input, and what it means.")
    is_set: bool = Field(description="Whether the environment set it, overriding what an unset value means.")
    shown_value: str | None = Field(description="Its value; None when it is unset or its value is hidden.")


class SmokeRunCheckout(FrozenModel):
    """The commit a smoke run's suite ran from."""

    root_dir: Path = Field(description="The checkout's root, which the report's source paths are relative to.")
    git_sha: NonEmptyStr = Field(description="The checkout's HEAD.")
    branch_name: str = Field(description="The checked-out branch; empty for a detached HEAD.")
    is_dirty: bool = Field(description="Whether tracked files differ from the commit, so the code that ran is not it.")
    is_pushed: bool = Field(description="Whether a remote-tracking branch contains the commit, so its links resolve.")
    commit_url: AnyUrl = Field(description="The commit on GitHub.")


class SmokeRunEnvironment(FrozenModel):
    """Where a smoke run ran, from which commit, and how pytest was invoked."""

    host_name: NonEmptyStr = Field(description="The machine's host name.")
    platform_description: NonEmptyStr = Field(description="The operating system and architecture.")
    python_version: NonEmptyStr = Field(description="The Python the suite ran under.")
    pytest_version: NonEmptyStr = Field(description="The pytest the suite ran under.")
    pytest_arguments: tuple[str, ...] = Field(description="The arguments pytest was invoked with.")
    ci_run_url: AnyUrl | None = Field(description="The GitHub Actions run it ran in; None outside CI.")
    ci_run_description: str = Field(description="The workflow, attempt, job and runner; empty outside CI.")
    repository_url: AnyUrl = Field(description="The GitHub repository its source links point into.")
    checkout: SmokeRunCheckout | None = Field(description="The commit it ran from; None when git could not say.")


class SmokeRunReport(FrozenModel):
    """Everything a smoke run's HTML report shows."""

    run_id: SmokeRunId | None = Field(description="The run's id; None when no scenario needed one.")
    backend: SmokeBackend | None = Field(
        description="The deployed environment under test, once a scenario resolved it."
    )
    studio_binary: StudioBinary | None = Field(description="The build under test, once a scenario resolved it.")
    started_at: datetime = Field(description="When the first scenario started (or the run ended, if none did).")
    finished_at: datetime = Field(description="When the session finished.")
    exit_status: int = Field(description="pytest's exit status for the session.")
    environment: SmokeRunEnvironment = Field(description="Where and how the run ran.")
    parameters: tuple[SmokeRunParameter, ...] = Field(description="Every input the run reads, and what it was given.")
    scenarios: tuple[SmokeScenarioResult, ...] = Field(description="Every selected scenario, in the order it ran.")
    collection_errors: tuple[SmokeCollectionError, ...] = Field(description="Test files that could not be collected.")
    deselected_count: NonNegativeInt = Field(description="Scenarios the run's selection (``-k``, ``-m``) left out.")
    verdict: SmokeRunVerdict = Field(description="The session's failures, by kind.")
    created_resources: tuple[SmokeRunManifestEntry, ...] = Field(description="What the run's manifest recorded.")
