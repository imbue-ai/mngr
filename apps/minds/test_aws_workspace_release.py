"""End-to-end release test for the minds AWS compute provider.

Provisions a real EC2 instance through a minds-shaped per-region AWS provider
block (``[providers.aws-<region>]``, the same shape minds writes into its mngr
settings at startup) and asserts the agent runs in a **runsc (gVisor) hardened
Docker container** on the EC2 *outer* host -- the substrate the secure latchkey
gateway runs on, outside the agent's container.

This provisions and destroys a real EC2 instance, so it costs real money. It is
double-gated, matching ``mngr_aws``'s release tests:

- AWS credentials must resolve via boto3's default chain (e.g. ``AWS_PROFILE``).
- ``MNGR_AWS_RELEASE_TESTS=1`` must be set.

Run manually:

    AWS_PROFILE=josh MNGR_AWS_RELEASE_TESTS=1 \\
        just test apps/minds/test_aws_workspace_release.py

Scope: this verifies the AWS-specific substrate end to end -- that a minds
``aws-<region>`` provider block provisions an EC2 outer host and runs the agent
in a runsc container (the isolation boundary the latchkey gateway sits outside
of, via the provider-agnostic ``mngr_latchkey`` flow exercised by the
deployment-test orchestrator). The full ``mngr create
system-services@<host>.aws-<region> --template main --template aws`` command
shape minds builds, and the FCT ``[create_templates.aws]`` block, are covered by
the unit tests in ``agent_creator_test.py`` and the template repo; running the
heavy FCT Docker build here would only re-test provider-agnostic machinery.
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

# NOTE: deliberately NOT marked @pytest.mark.rsync. ``mngr create`` for a
# VPS-Docker provider only rsyncs the source when the source is a plain
# directory; here the source is a *git repo* (``temp_git_repo``), so the
# transfer resolves to GIT_MIRROR (git push), not rsync. The pytest rsync
# resource-guard's superfluous-mark check then fails a test that carries the
# mark but never invokes rsync. Verified empirically against real EC2: with the
# mark the run fails at teardown ("marked rsync but never invoked rsync");
# without it the run passes. Do not add the mark back.
pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (aws_credentials_available() and AWS_RELEASE_TESTS_OPT_IN),
        reason="AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set",
    ),
]

# The minds-written ``[providers.aws-<region>]`` block carries the gVisor/runsc
# hardening knobs; pin the provider name to the region so the create address
# (``...@<host>.aws-<region>``) selects exactly this block.
_REGION = AWS_DEFAULT_REGION
_AWS_PROVIDER_NAME = f"aws-{_REGION}"


def _frozen_aws_env(base_env: dict[str, str]) -> dict[str, str]:
    """Return ``base_env`` with the resolved AWS credentials frozen into static keys.

    Resolves credentials once (honoring ``AWS_PROFILE`` if set) and writes the
    static keys in, then drops ``AWS_PROFILE`` / config-file pointers so every
    mngr subprocess authenticates from the frozen keys alone -- independent of
    the on-disk ``~/.aws`` files (which may be mid-edit) and of the isolated
    ``HOME`` below. Mirrors ``mngr_aws``'s release harness.
    """
    profile = os.environ.get("AWS_PROFILE")
    creds = (boto3.Session(profile_name=profile) if profile else boto3.Session()).get_credentials()
    assert creds is not None, "AWS credentials must resolve (release-test skipif guards this)"
    frozen = creds.get_frozen_credentials()
    assert frozen.access_key and frozen.secret_key, "frozen AWS credentials must not be empty"
    env = dict(base_env)
    env["AWS_ACCESS_KEY_ID"] = frozen.access_key
    env["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
    if frozen.token:
        env["AWS_SESSION_TOKEN"] = frozen.token
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_CONFIG_FILE", None)
    env.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    return env


@pytest.fixture()
def aws_release_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Build the subprocess env + opted-in mngr config for the release test.

    Writes a self-contained project ``settings.toml`` (pointed at via
    ``MNGR_PROJECT_CONFIG_DIR``) defining the per-region AWS provider exactly as
    minds writes it at startup -- backend ``aws``, the region, the runsc
    hardening knobs -- plus the release-test-only ``auto_shutdown_seconds``
    safety net the production AwsProvider requires under pytest. ``MNGR_HOST_DIR``
    and ``HOME`` are isolated to tmp so no developer mngr profile / config is
    loaded (every loaded config must opt into pytest, and the dev's would not).
    """
    settings_dir = tmp_path / "config"
    settings_dir.mkdir()
    (settings_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        f"\n[providers.{_AWS_PROVIDER_NAME}]\n"
        'backend = "aws"\n'
        f'default_region = "{_REGION}"\n'
        # Match the minds default (t3.large, 8 GB); the t3.small default's 2 GB
        # is too small to give Docker + runsc room to work.
        'default_instance_type = "t3.large"\n'
        # gVisor/runsc: install + select the runsc runtime for the agent
        # container, exactly as the minds-written provider block does.
        "install_gvisor_runtime = true\n"
        'docker_runtime = "runsc"\n'
        f"auto_shutdown_seconds = {AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n'
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )
    env = _frozen_aws_env(dict(os.environ))
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    env["MNGR_HOST_DIR"] = str(tmp_path / "mngr_home")
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    env["AWS_REGION"] = _REGION
    yield env


@pytest.fixture()
def temp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo to use as ``mngr create``'s source checkout (cwd)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("aws release test source\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)
    return repo


def _run_mngr(env: dict[str, str], cwd: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run the monorepo's ``mngr`` (the dev shim on PATH) with the release env in scope.

    Invokes the bare ``mngr`` shim rather than ``uv run mngr``: ``uv run`` in an
    arbitrary cwd would try to build that directory's own venv, whereas the shim
    always routes to this checkout's mngr. Streams stdout+stderr to a file so a
    stuck create is still diagnosable on timeout. The log is written *outside*
    ``cwd`` so it doesn't dirty the source git repo (``mngr create`` enforces a
    clean working tree).
    """
    log_path = cwd.parent / f"mngr-{args[0] if args else 'cmd'}.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            ["mngr", *args],
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


def test_aws_workspace_runs_in_runsc_container_on_ec2(aws_release_env: dict[str, str], temp_git_repo: Path) -> None:
    """A minds ``aws-<region>`` provider lands a runsc-hardened container on a real EC2 outer host."""
    host_name = f"test-aws-mind-{uuid.uuid4().hex}"
    agent_address = f"agent@{host_name}.{_AWS_PROVIDER_NAME}"

    # One-time security-group setup for the region (read-only-first; a no-op
    # describe when already prepared).
    prepare = _run_mngr(
        aws_release_env, temp_git_repo, "aws", "prepare", "--provider", _AWS_PROVIDER_NAME, "--region", _REGION
    )
    assert prepare.returncode == 0, f"aws prepare failed:\n{prepare.stdout}"

    create = _run_mngr(
        aws_release_env,
        temp_git_repo,
        "create",
        agent_address,
        "--new-host",
        "--type",
        "command",
        "--no-connect",
        "-b",
        f"--aws-region={_REGION}",
        "--",
        "sleep",
        "99999",
    )
    assert create.returncode == 0, f"AWS create failed:\n{create.stdout}"

    try:
        # The host shows up as AWS-backed and running.
        listing = _run_mngr(aws_release_env, temp_git_repo, "list")
        assert listing.returncode == 0, f"list failed:\n{listing.stdout}"
        assert host_name in listing.stdout
        assert _AWS_PROVIDER_NAME in listing.stdout

        # The agent runs inside a gVisor (runsc) sandbox -- the isolation
        # boundary the latchkey gateway sits outside of. gVisor advertises
        # itself in the emulated kernel log (``dmesg`` starts with "Starting
        # gVisor...") and often in /proc/version; a normal Linux kernel does
        # neither. Probe both and require the signature in either, which also
        # confirms the agent is containerized (not on the bare EC2 host).
        runsc_probe = _run_mngr(
            aws_release_env,
            temp_git_repo,
            "exec",
            agent_address,
            "cat /proc/version; echo '---dmesg---'; dmesg 2>/dev/null | head -5",
        )
        assert runsc_probe.returncode == 0, f"exec failed:\n{runsc_probe.stdout}"
        assert "gvisor" in runsc_probe.stdout.lower(), (
            f"expected a gVisor (runsc) signature in /proc/version or dmesg, got:\n{runsc_probe.stdout}"
        )
    finally:
        # Best-effort cleanup; --force skips the destroy confirmation.
        _run_mngr(aws_release_env, temp_git_repo, "destroy", agent_address, "--force")
