"""End-to-end release tests for the AWS provider.

These tests provision and destroy real EC2 instances on AWS. They cost
real money — typically a few cents per run for a ~5-minute t3.small —
and are double-gated:

- AWS credentials must be resolvable via boto3's default credential chain
  (env vars, shared credentials file, AWS_PROFILE, or EC2 IMDS). See
  ``testing.aws_credentials_available`` -- this is the same probe used by
  the session-end cleanup hook.
- ``MNGR_AWS_RELEASE_TESTS=1`` must be set explicitly

Three layers of damage control prevent leaked EC2 cost (see
``conftest.py`` in this package for the full picture):

1. Each test's ``finally`` calls ``mngr destroy --force``.
2. ``pytest_sessionfinish`` in ``conftest.py`` force-terminates any
   instance tagged ``mngr-pytest-launched=true`` (added by
   ``AwsVpsClient.create_instance`` whenever ``PYTEST_CURRENT_TEST`` is
   set) and older than the TTL at session end, and fails the session.
3. The subprocess that runs ``mngr create`` is pointed at a temporary
   ``settings.toml`` (via ``MNGR_PROJECT_CONFIG_DIR``) that sets
   ``[providers.aws] auto_shutdown_seconds``. This propagates into
   cloud-init as ``shutdown -P +N`` on each test instance; combined with
   the launch flag ``InstanceInitiatedShutdownBehavior=terminate``, this
   auto-terminates the instance from the inside even if pytest itself
   is killed. The production AwsProvider refuses to create EC2 instances
   under pytest without this set, so a missed override fails closed.

Run manually:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        MNGR_AWS_RELEASE_TESTS=1 \\
        just test libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
"""

import os
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError

from imbue.mngr.providers.provider_release_testing import run_provider_release_trip1
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip2
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip3
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip4
from imbue.mngr_aws.client import AWS_PYTEST_LAUNCHED_TAG
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import DEFAULT_AMI_BY_REGION
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import AWS_DEFAULT_REGION
from imbue.mngr_aws.testing import AWS_RELEASE_TESTS_OPT_IN
from imbue.mngr_aws.testing import AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_aws.testing import AWS_TEST_NAME_PREFIX
from imbue.mngr_aws.testing import aws_credentials_available
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.testing import VpsCloudReleaseProfile
from imbue.mngr_vps.testing import find_handle_by_launched_label

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (aws_credentials_available() and AWS_RELEASE_TESTS_OPT_IN),
        reason="AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set",
    ),
]


def _write_release_settings(
    settings_dir: Path, *, terminate_on_shutdown: bool = True, isolation: str | None = None
) -> None:
    """Write the release-test ``settings.toml`` into ``settings_dir``.

    Shared by the prepare fixture and the per-test settings fixture so both the
    ``mngr aws prepare`` and ``mngr create`` subprocesses load the same opted-in
    config. ``is_allowed_in_pytest = true`` is required because the subprocesses
    inherit ``PYTEST_CURRENT_TEST`` and mngr refuses to load any config that does
    not opt in -- without it, a developer machine with a real mngr profile would
    fail before any AWS call.

    ``terminate_on_shutdown`` sets EC2's ``InstanceInitiatedShutdownBehavior``.
    The default (``True``) makes the release-test instances ephemeral /
    self-cleaning: any OS shutdown (the ``auto_shutdown_seconds`` time cap, or
    the idle watcher) TERMINATES the instance, so a leaked instance auto-destroys
    at the cap. Trip 2's auto-shutdown settings override it to ``False`` so an idle
    poweroff STOPS (not terminates) the instance and can be resumed.

    ``MNGR_PROJECT_CONFIG_DIR`` is the literal directory containing
    ``settings.toml`` (see ``resolve_project_config_dir`` in
    ``mngr/config/pre_readers.py``); it is *not* a project root that gets a
    ``.<root_name>/`` subdirectory appended. So the file is written directly
    into ``settings_dir``.

    ``isolation`` selects the placement shape: ``None`` leaves the default
    (Docker container); ``"NONE"`` writes ``isolation = "NONE"`` so the bare
    (no-container) realizer runs the agent directly on the EC2 instance's OS.
    """
    isolation_line = f'isolation = "{isolation}"\n' if isolation is not None else ""
    (settings_dir / "settings.toml").write_text(
        # Opt this config past the pytest guard: the subprocess inherits
        # ``PYTEST_CURRENT_TEST`` and refuses to load any config that does not
        # set this. Top-level key, so it must precede the first table.
        "is_allowed_in_pytest = true\n"
        "\n[providers.aws]\n"
        'backend = "aws"\n'
        f"{isolation_line}"
        # Auto-shutdown via cloud-init if pytest is killed before the per-test
        # cleanup runs. With terminate_on_shutdown=true the shutdown terminates
        # the instance (self-cleaning); with false it stops it (resumable), and
        # the conftest session-end scanner reaps a leak.
        f"auto_shutdown_seconds = {AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        f"terminate_on_shutdown = {'true' if terminate_on_shutdown else 'false'}\n"
        # Default is already ("0.0.0.0/0",), but write it explicitly so the
        # test settings file is self-documenting -- the test SSH connection
        # from the developer laptop / CI runner needs ingress from any IP.
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n'
        # Disable other remote providers so the create-host preflight (and
        # ``mngr list``) doesn't trip on them looking for credentials.
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.azure]\nis_enabled = false\n"
        "\n[providers.gcp]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )


@pytest.fixture(scope="session")
def _aws_release_test_security_group_prepared(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Run ``mngr aws prepare`` once per test session before any lifecycle test.

    ``create_instance`` no longer auto-creates the security group on the hot
    path (so users with restricted IAM can run mngr create); the privileged
    SG-creation step lives in ``mngr aws prepare``. The release tests need
    to run prepare once so subsequent creates can attach the SG.

    Runs against an opted-in test ``settings.toml`` (via ``MNGR_PROJECT_CONFIG_DIR``)
    and an isolated mngr home (``MNGR_HOST_DIR`` + ``HOME``) so the subprocess
    doesn't load the developer's real mngr *profile*
    (``$MNGR_HOST_DIR/profiles/.../settings.toml``), which the pytest guard
    rejects -- without isolation this fixture passes only in CI (no profile) and
    fails on a developer machine. This session-scoped fixture runs before the
    per-test host-dir isolation, so it must isolate the host dir itself; AWS
    credentials are resolved here (under the real HOME) and frozen into the
    subprocess env so boto3 still authenticates after the HOME swap (mirrors what
    ``conftest.setup_test_mngr_env`` does for the per-test subprocesses).
    """
    settings_dir = tmp_path_factory.mktemp("aws_prepare_settings")
    _write_release_settings(settings_dir)
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    env["MNGR_HOST_DIR"] = str(tmp_path_factory.mktemp("aws_prepare_mngr_home"))
    env["HOME"] = str(tmp_path_factory.mktemp("aws_prepare_home"))
    # Resolve boto3's credential chain *before* the HOME swap hides
    # ~/.aws/credentials and ~/.aws/config, then export the frozen credentials
    # as env vars so they survive isolation (the release-test ``skipif``
    # guarantees creds resolve here).
    creds = boto3.Session().get_credentials()
    assert creds is not None, "AWS credentials must resolve (release-test skipif guards this)"
    frozen = creds.get_frozen_credentials()
    assert frozen.access_key and frozen.secret_key, "frozen credentials must not be empty"
    env["AWS_ACCESS_KEY_ID"] = frozen.access_key
    env["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
    if frozen.token:
        env["AWS_SESSION_TOKEN"] = frozen.token
    # Stop boto3 inside the isolated HOME from re-reading the config/credentials
    # files (which won't exist there anyway).
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_CONFIG_FILE", None)
    env.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    cmd = [
        "uv",
        "run",
        "mngr",
        "aws",
        "prepare",
        "--region",
        AWS_DEFAULT_REGION,
        "--allowed-ssh-cidr",
        "0.0.0.0/0",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, (
        f"`mngr aws prepare` failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _run_mngr(
    project_config_dir: Path,
    cwd: Path,
    *args: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a mngr command with the test settings.toml in scope.

    ``cwd`` must be inside a git repository -- ``mngr create`` reads the
    source from the current git checkout unless ``--from`` is passed. The
    release tests supply the ``temp_git_repo`` fixture for this.

    Streams stdout+stderr to a file under ``project_config_dir`` rather
    than buffering with ``capture_output=True``. The buffered mode loses
    everything on ``TimeoutExpired``, which makes diagnosing a stuck
    ``mngr create`` impossible -- the assertion message just says "the
    subprocess timed out" with no provisioning-phase context.
    """
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    cmd = ["uv", "run", "mngr", *args]
    log_path = Path(project_config_dir) / f"mngr-{args[0] if args else 'cmd'}.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            # 124 is the GNU-coreutils ``timeout`` convention.
            returncode = 124
    log_text = log_path.read_text()
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=log_text,
        stderr=""
        if returncode == 0
        else (
            "see stdout (subprocess stderr was merged into stdout)\n"
            + (f"subprocess timed out after {timeout}s\n" if returncode == 124 else "")
        ),
    )


# =============================================================================
# Trip 1 -- the shared provider release lifecycle (create -> stop/start ->
# sketchy kill -> gc), parametrized over isolation mode. See
# `imbue.mngr.providers.provider_release_testing` and
# `specs/provider-release-tests.md`.
# =============================================================================


class _AwsReleaseProfile(VpsCloudReleaseProfile):
    """AWS plumbing for the shared provider release trip."""

    provider_name = "aws"
    name_prefix = AWS_TEST_NAME_PREFIX

    # Trip 4: AWS curates the missing-credential help text toward `aws configure` (the spec's
    # divergence was fixed in this PR -- see `_aws_not_authorized_error` in mngr_aws/backend.py).
    has_curated_unavailable_help = True
    credential_setup_command = "aws configure"
    # AWS captures host_dir to the S3 state bucket at `mngr stop`, so a stopped host's host_dir is
    # readable offline (Trip 1's opt-in offline-host_dir step).
    supports_offline_host_dir = True

    def __init__(self, client: AwsVpsClient, isolation: IsolationMode) -> None:
        super().__init__(client, isolation)
        self._aws_client = client

    def unavailable_reason(self) -> str | None:
        if not (aws_credentials_available() and AWS_RELEASE_TESTS_OPT_IN):
            return "AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set"
        return None

    def write_settings(self, settings_dir: Path) -> None:
        _write_release_settings(settings_dir, isolation="NONE" if self._isolation is IsolationMode.NONE else None)

    def write_auto_shutdown_settings(self, settings_dir: Path) -> None:
        # Trip 2's idle poweroff must STOP (not terminate) the instance so `mngr start` can resume
        # it, which on EC2 requires ``InstanceInitiatedShutdownBehavior = stop`` -- the
        # ``terminate_on_shutdown = false`` variant the resumable-idle test uses.
        _write_release_settings(
            settings_dir,
            terminate_on_shutdown=False,
            isolation="NONE" if self._isolation is IsolationMode.NONE else None,
        )

    def create_extra_args(self) -> Sequence[str]:
        return ()

    def make_credentials_unresolvable_env(self) -> Mapping[str, str | None]:
        # Drop the frozen ``AWS_*`` creds the conftest exported and point the config/credentials
        # files at a path that does not exist, then disable IMDS so boto3's chain resolves nothing
        # -- ``AwsProviderConfig.get_session`` then raises ``AwsConfigError`` -> the contract
        # ``ProviderUnavailableError``. ``AWS_EC2_METADATA_DISABLED`` stops a slow IMDS probe (and
        # any instance-role fallback) on a CI runner with an attached role.
        return {
            "AWS_ACCESS_KEY_ID": None,
            "AWS_SECRET_ACCESS_KEY": None,
            "AWS_SESSION_TOKEN": None,
            "AWS_PROFILE": None,
            "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/aws/credentials",
            "AWS_CONFIG_FILE": "/nonexistent/aws/config",
            "AWS_EC2_METADATA_DISABLED": "true",
        }

    def find_launched_host_handle(self, host_name: str) -> str | None:
        return find_handle_by_launched_label(self._aws_client.list_instances(), AWS_PYTEST_LAUNCHED_TAG)


@pytest.mark.rsync
@pytest.mark.parametrize("isolation", [IsolationMode.CONTAINER, IsolationMode.NONE])
def test_provider_release_trip1(
    isolation: IsolationMode,
    tmp_path: Path,
    temp_git_repo: Path,
    aws_release_client: AwsVpsClient,
    _aws_release_test_security_group_prepared: None,
) -> None:
    run_provider_release_trip1(
        _AwsReleaseProfile(client=aws_release_client, isolation=isolation), tmp_path, temp_git_repo
    )


@pytest.mark.rsync
@pytest.mark.parametrize("isolation", [IsolationMode.CONTAINER, IsolationMode.NONE])
def test_provider_release_trip2(
    isolation: IsolationMode,
    tmp_path: Path,
    temp_git_repo: Path,
    aws_release_client: AwsVpsClient,
    _aws_release_test_security_group_prepared: None,
) -> None:
    run_provider_release_trip2(
        _AwsReleaseProfile(client=aws_release_client, isolation=isolation), tmp_path, temp_git_repo
    )


@pytest.mark.rsync
@pytest.mark.parametrize("isolation", [IsolationMode.CONTAINER, IsolationMode.NONE])
def test_provider_release_trip3(
    isolation: IsolationMode,
    tmp_path: Path,
    temp_git_repo: Path,
    aws_release_client: AwsVpsClient,
    _aws_release_test_security_group_prepared: None,
) -> None:
    run_provider_release_trip3(
        _AwsReleaseProfile(client=aws_release_client, isolation=isolation), tmp_path, temp_git_repo
    )


def test_provider_release_trip4(
    tmp_path: Path,
    temp_git_repo: Path,
    aws_release_client: AwsVpsClient,
) -> None:
    # No-boot CLI error-classification trip: not parametrized over isolation (the error paths are
    # isolation-agnostic) and no ``rsync`` mark (it never provisions a host).
    run_provider_release_trip4(
        _AwsReleaseProfile(client=aws_release_client, isolation=IsolationMode.CONTAINER), tmp_path, temp_git_repo
    )


# =============================================================================
# API client smoke tests (real network calls, read-only)
# =============================================================================


@pytest.fixture()
def aws_release_client() -> AwsVpsClient:
    """Real AWS API client for release-test read-only calls.

    Built with placeholder AMI / security-group IDs because the tests below
    only exercise read-only API operations (list_instances) that ignore
    those fields.
    """
    session = boto3.Session(region_name=AWS_DEFAULT_REGION)
    return AwsVpsClient(
        session=session,
        region=AWS_DEFAULT_REGION,
        ami_id="ami-placeholder",
        security_group=ExistingSecurityGroup(id="sg-placeholder"),
    )


def test_api_client_list_instances_does_not_error(aws_release_client: AwsVpsClient) -> None:
    instances = aws_release_client.list_instances()
    assert isinstance(instances, list)


def _latest_debian_12_amd64_ami_id(region: str) -> str | None:
    """Return the latest published Debian 12 amd64 AMI ID in ``region``, or ``None`` on error.

    Queries the canonical Debian publisher account (owner id 136693071363)
    and picks the newest ``debian-12-amd64-*`` image by creation date. Used
    to surface a copy-pasteable replacement when ``DEFAULT_AMI_BY_REGION``
    has gone stale; failures here (no creds, no matching images, etc.) are
    suppressed because the lookup is best-effort hint generation -- the
    primary assertion has already detected the staleness.
    """
    try:
        response = (
            boto3.Session(region_name=region)
            .client("ec2")
            .describe_images(
                Owners=["136693071363"],
                Filters=[
                    {"Name": "name", "Values": ["debian-12-amd64-*"]},
                    {"Name": "architecture", "Values": ["x86_64"]},
                    {"Name": "state", "Values": ["available"]},
                ],
            )
        )
    except ClientError:
        return None
    images = response.get("Images", [])
    if not images:
        return None
    latest = max(images, key=lambda img: img.get("CreationDate", ""))
    return latest.get("ImageId") or None


def test_default_amis_describe_successfully() -> None:
    """Every entry in DEFAULT_AMI_BY_REGION must still resolve via DescribeImages.

    Hard-coded AMI IDs go stale over time -- Debian publishes new ones every
    few months and older snapshots eventually get deprecated. A periodic
    release-test run is the cheapest way to catch this: skipif gates the test
    on AWS credentials, so local runs without creds skip silently.

    Collects errors across every region rather than aborting on the first
    failure, so a sweep produces a complete list of stale / inaccessible
    entries in one run. When any failure is detected, the test additionally
    queries the canonical Debian publisher and emits a copy-pasteable
    replacement dict so the fix is mechanical.
    """
    failures: list[str] = []
    suggestions: dict[str, str] = {}
    for region, ami_id in DEFAULT_AMI_BY_REGION.items():
        ec2 = boto3.Session(region_name=region).client("ec2")
        is_stale = False
        try:
            response = ec2.describe_images(ImageIds=[ami_id])
        except ClientError as e:
            # InvalidAMIID.NotFound -> deprecated; UnauthorizedOperation /
            # AuthFailure -> the cred set lacks access to this region.
            failures.append(f"{region}: AMI {ami_id} {e.response.get('Error', {}).get('Code', 'Unknown')}: {e}")
            is_stale = True
        else:
            images = response.get("Images", [])
            if not images:
                failures.append(f"{region}: AMI {ami_id} not found")
                is_stale = True
            else:
                image = images[0]
                state = image.get("State", "")
                if state != "available":
                    failures.append(f"{region}: AMI {ami_id} state={state!r} (expected 'available')")
                    is_stale = True
        if is_stale:
            latest = _latest_debian_12_amd64_ami_id(region)
            if latest is not None:
                suggestions[region] = latest

    if not failures:
        return
    message = "DEFAULT_AMI_BY_REGION has stale or inaccessible entries:\n  " + "\n  ".join(failures)
    if suggestions:
        message += "\n\nSuggested replacement (verified via DescribeImages, owner 136693071363):\n"
        message += "DEFAULT_AMI_BY_REGION = {\n"
        for region in DEFAULT_AMI_BY_REGION:
            ami_id = suggestions.get(region, DEFAULT_AMI_BY_REGION[region])
            message += f'    "{region}": "{ami_id}",\n'
        message += "}\n"
    else:
        message += (
            "\nCould not query the Debian publisher for replacement IDs (no creds for those "
            "regions, or DescribeImages rate-limited). See "
            "https://wiki.debian.org/Cloud/AmazonEC2Image for manual lookup.\n"
        )
    raise AssertionError(message)
