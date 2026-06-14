"""End-to-end release test for the minds AWS compute provider.

Drives a real AWS workspace create through the same command shape the minds
desktop client builds (``mngr create system-services@<host>.aws-<region>
--template main --template aws ...``) against a local forever-claude-template
checkout, then asserts the workspace came up as a runsc-hardened Docker
container on the EC2 *outer* host -- the substrate the secure latchkey gateway
runs on, outside the agent's container.

This provisions and destroys a real EC2 instance and runs a full FCT Docker
build on it, so it costs real money and takes several minutes. It is
double-gated, matching ``mngr_aws``'s release tests:

- AWS credentials must resolve via boto3's default chain.
- ``MNGR_AWS_RELEASE_TESTS=1`` must be set.

It also needs a forever-claude-template checkout whose ``.mngr/settings.toml``
contains the ``[create_templates.aws]`` block. Point ``MINDS_AWS_RELEASE_FCT_PATH``
at one; it defaults to the sibling worktree this branch creates under
``.external_worktrees/forever-claude-template``.

Run manually:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        MNGR_AWS_RELEASE_TESTS=1 \\
        MINDS_AWS_RELEASE_FCT_PATH=/path/to/forever-claude-template \\
        just test apps/minds/test_aws_workspace_release.py

Note on scope: this verifies the AWS-specific substrate end to end (an EC2
outer host running the agent in a runsc container). The full minds latchkey
flow -- minds' discovery handler calling ``provision_remote_gateway`` to stand
the gateway up on that outer host and reverse-tunnel it into the container --
is provider-agnostic and exercised by the deployment-test orchestrator; it
relies only on the outer-host/container separation this test confirms AWS
provides.
"""

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest

from imbue.mngr_aws.testing import AWS_DEFAULT_REGION
from imbue.mngr_aws.testing import AWS_RELEASE_TESTS_OPT_IN
from imbue.mngr_aws.testing import AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_aws.testing import aws_credentials_available

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        not (aws_credentials_available() and AWS_RELEASE_TESTS_OPT_IN),
        reason="AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set",
    ),
]

_DEFAULT_FCT_PATH = Path(__file__).resolve().parents[2] / ".external_worktrees" / "forever-claude-template"


def _fct_checkout_path() -> Path:
    """Resolve the forever-claude-template checkout used as the create source."""
    configured = os.environ.get("MINDS_AWS_RELEASE_FCT_PATH")
    path = Path(configured).expanduser().resolve() if configured else _DEFAULT_FCT_PATH
    if not (path / ".mngr" / "settings.toml").is_file():
        raise AssertionError(
            f"forever-claude-template checkout with .mngr/settings.toml not found at {path}. "
            "Set MINDS_AWS_RELEASE_FCT_PATH to a checkout that contains the "
            "[create_templates.aws] block."
        )
    return path


@pytest.fixture()
def aws_release_settings_dir(tmp_path: Path) -> Iterator[Path]:
    """Write an opted-in mngr settings.toml defining the region-specific AWS provider.

    Mirrors what minds writes at startup (``[providers.aws-<region>]`` with the
    runsc hardening knobs), plus the release-test-only ``auto_shutdown_seconds``
    safety net the production AwsProvider requires under pytest so a killed run
    can't leak an instance.
    """
    region = AWS_DEFAULT_REGION
    (tmp_path / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        f"\n[providers.aws-{region}]\n"
        'backend = "aws"\n'
        f'default_region = "{region}"\n'
        "install_gvisor_runtime = true\n"
        'docker_runtime = "runsc"\n'
        f"auto_shutdown_seconds = {AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n'
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )
    yield tmp_path


def _run_mngr(settings_dir: Path, cwd: Path, *args: str, timeout: int = 1500) -> subprocess.CompletedProcess[str]:
    """Run a mngr command with the opted-in test settings.toml and frozen AWS creds in scope."""
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    # Freeze boto3's resolved credentials into the env so they survive any
    # HOME/profile isolation the subprocess applies (mirrors mngr_aws's harness).
    creds = boto3.Session().get_credentials()
    assert creds is not None, "AWS credentials must resolve (release-test skipif guards this)"
    frozen = creds.get_frozen_credentials()
    env["AWS_ACCESS_KEY_ID"] = frozen.access_key
    env["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
    if frozen.token:
        env["AWS_SESSION_TOKEN"] = frozen.token
    log_path = settings_dir / f"mngr-{args[0] if args else 'cmd'}.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            ["uv", "run", "mngr", *args],
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
            returncode = 124
    return subprocess.CompletedProcess(args=list(args), returncode=returncode, stdout=log_path.read_text(), stderr="")


def test_aws_workspace_create_runs_in_runsc_container_on_ec2(aws_release_settings_dir: Path) -> None:
    """A minds-shaped AWS create lands a runsc-hardened container on a real EC2 outer host."""
    fct_path = _fct_checkout_path()
    region = AWS_DEFAULT_REGION
    host_name = f"test-aws-mind-{uuid.uuid4().hex}"

    # One-time security-group setup for the region (read-only-first; a no-op
    # describe when already prepared).
    prepare = _run_mngr(aws_release_settings_dir, fct_path, "aws", "prepare", "--region", region, timeout=120)
    assert prepare.returncode == 0, f"aws prepare failed:\n{prepare.stdout}"

    # The same address + template shape minds' _build_mngr_create_command builds
    # for LaunchMode.AWS. ``--type command`` keeps the create lightweight (no
    # claude agent setup) while still exercising the full FCT aws template +
    # EC2 + runsc container path.
    address = f"system-services@{host_name}.aws-{region}"
    create = _run_mngr(
        aws_release_settings_dir,
        fct_path,
        "create",
        address,
        "--new-host",
        "--template",
        "main",
        "--template",
        "aws",
        "--no-connect",
        "-b",
        f"--aws-region={region}",
        "--type",
        "command",
        "--",
        "sleep",
        "99999",
    )
    assert create.returncode == 0, f"AWS create failed:\n{create.stdout}"

    try:
        # The agent's host shows up as AWS-backed and running.
        listing = _run_mngr(aws_release_settings_dir, fct_path, "list", timeout=120)
        assert listing.returncode == 0, f"list failed:\n{listing.stdout}"
        assert host_name in listing.stdout
        assert f"aws-{region}" in listing.stdout

        # The agent runs inside a gVisor (runsc) sandbox: gVisor advertises
        # itself in /proc/version, which a normal Linux kernel never does. This
        # confirms both that the agent is containerized (not on the bare EC2
        # host) and that the container uses the runsc runtime -- the isolation
        # boundary the latchkey gateway sits outside of.
        proc_version = _run_mngr(
            aws_release_settings_dir,
            fct_path,
            "exec",
            f"system-services@{host_name}",
            "cat /proc/version",
            timeout=120,
        )
        assert proc_version.returncode == 0, f"exec failed:\n{proc_version.stdout}"
        assert "gvisor" in proc_version.stdout.lower(), (
            f"expected a gVisor (runsc) kernel signature in /proc/version, got:\n{proc_version.stdout}"
        )
    finally:
        # Best-effort cleanup; --force skips the destroy confirmation.
        _run_mngr(
            aws_release_settings_dir, fct_path, "destroy", f"system-services@{host_name}", "--force", timeout=180
        )
