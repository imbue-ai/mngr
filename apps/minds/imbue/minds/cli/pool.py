"""``minds pool {create,list,destroy}`` -- env-aware wrapper around ``mngr imbue_cloud admin pool``.

Responsibility split:

* ``mngr imbue_cloud admin pool create`` (in ``libs/mngr_imbue_cloud``) is the
  provider-generic host-creation step. It accepts a required ``--region`` and
  repeatable ``--tag KEY=VALUE`` and knows nothing about minds environments.
* This module is the env-aware layer. From the activated minds env
  (``MINDS_ROOT_NAME``) it:
    1. injects ``--tag minds_env=<env-name>`` so ``minds env destroy`` can
       later enumerate + delete every VPS the env owns (via the OVH IAM v2
       tag walker in :mod:`imbue.minds.envs.providers.ovh_tags`);
    2. reads the activated tier's OVH AK/AS/CK from Vault
       (``<vault_path_prefix>/ovh``) and injects them into the admin
       subprocess env so the inner ``mngr create ... --template ovh`` has
       credentials;
    3. derives the management public key from the activated tier's
       ``<vault_path_prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY`` Vault entry
       (the connector runs with the SAME private key as a Modal Secret) and
       passes it to the admin's ``--management-public-key-file`` -- so the
       key injected on the VPS at bake time always matches the connector's
       at lease time. This closes the keypair-mismatch class of bake
       failures that hand-rolled ``--management-public-key-file`` paths used
       to leak. Operators can still pass ``--management-public-key-file``
       to force a specific key (escape hatch for one-off / non-vault setups).
  All other admin flags (``--count`` / ``--attributes`` / ``--workspace-dir``
  / ``--database-url`` / ``--mngr-source``) forward 1:1.

Transport is subprocess (``mngr imbue_cloud admin pool ...``) to match the
rest of the minds env CLI's mngr invocations and to keep the minds -> mngr
dependency direction unchanged.

The argument-construction logic (``build_*_args``) is split out from the
click commands so unit tests can verify the env-name injection + flag
forwarding behaviour without standing up a fake subprocess runner.
"""

import contextlib
import os
import shlex
import sys
import tempfile
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.cli._activated_env import PRODUCTION_ENV_NAME
from imbue.minds.cli._activated_env import STAGING_ENV_NAME
from imbue.minds.cli._activated_env import require_activated_env_name
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.utils.secret_redaction import redact_secret_flag_values

# Hard cap on the admin pool-create subprocess. Generous (12h) so a large bulk bake
# (e.g. `--count 20` in waves of `--max-concurrency`, slow on a loaded box) is never
# killed mid-run. If it ever does fire, the slice backend reaps its orphans on
# SIGTERM, but the point of 12h is to not hit it in normal operation.
_POOL_COMMAND_TIMEOUT_SECONDS: Final[int] = 43200

# Flags whose values are secrets and must be masked when the admin command is
# rendered into the "Running: ..." log line. ``--database-url`` carries the
# Neon pool DSN (username + password); leaking it into logs/terminals is the
# exact issue this redaction closes.
_SECRET_BEARING_FLAGS: Final[tuple[str, ...]] = ("--database-url",)

# OVH provider-config env vars consumed by ``OvhProviderConfig`` (in
# ``libs/mngr_ovh``). The three AK/AS/CK keys are required; the
# endpoint is optional (defaults to ``ovh-us`` in the provider config).
_OVH_REQUIRED_ENV_VARS: Final[tuple[str, ...]] = (
    "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_CONSUMER_KEY",
)
_OVH_OPTIONAL_ENV_VARS: Final[tuple[str, ...]] = ("OVH_ENDPOINT",)

# Vault key the management SSH private key lives under (per host-pool-setup.md
# step 2). The connector deploys with this private key pushed to a Modal
# Secret; the pool VPS's authorized_keys must hold the matching public key.
_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD: Final[str] = "POOL_SSH_PRIVATE_KEY"
# How long ``ssh-keygen -y`` should take to derive a public key from a
# small ed25519/RSA private key. Generous so a contended box doesn't
# spuriously fail the bake at the very first step.
_SSH_KEYGEN_DERIVE_TIMEOUT_SECONDS: Final[float] = 10.0
# Vault field (under ``<vault_prefix>/neon``) holding the pooled host_pool DSN.
_POOL_DSN_VAULT_FIELD: Final[str] = "DATABASE_URL"
# Shared ``--database-url`` help text for the create / list / destroy commands.
# Hoisted to one constant so the three subcommands' ``--help`` output can't drift.
_DATABASE_URL_HELP: Final[str] = (
    "Neon PostgreSQL connection string for the pool DB. Optional: for "
    "staging/production it is read from Vault (secrets/minds/<tier>/neon); "
    "for dev/ci it auto-resolves from the activated env's secrets.toml. "
    "Pass explicitly only when overriding."
)
# Pool host backends understood by ``mngr imbue_cloud admin pool create``. ``ovh_vps``
# orders an OVH classic VPS on demand; ``slice`` carves a lima VM on a pre-registered,
# prepped bare-metal box (see ``mngr imbue_cloud admin server``).
_BACKEND_OVH_VPS: Final[str] = "ovh_vps"
_BACKEND_SLICE: Final[str] = "slice"
# Env var the admin slice path reads the pool management private key from (see
# ``mngr_imbue_cloud.cli.server._pool_private_key_path``). It is the SAME key whose
# public form the OVH backend bakes via --management-public-key-file; the slice
# backend needs the private key itself to SSH the box and carve the lima VM.
_POOL_PRIVATE_KEY_ENV_VAR: Final[str] = "POOL_SSH_PRIVATE_KEY"


def build_create_admin_args(
    *,
    env_name: str,
    backend: str,
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    management_public_key_file: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    is_dry_run: bool,
    is_deferred_install_wait_skipped: bool,
    server_id: str | None = None,
    max_concurrency: int | None = None,
) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool create`` argv from minds-side inputs.

    For the ``ovh_vps`` backend, auto-injects ``--tag minds_env=<env_name>`` (so
    ``minds env destroy`` can enumerate the VPSes the env owns) and forwards
    ``--management-public-key-file``. For the ``slice`` backend it instead forwards
    ``--slice-env-name <env_name>`` (stamped into each slice's lima names, so a
    shared box can attribute the slice to this env) -- slices are not OVH-IAM-tagged
    and authorize the pool key from POOL_SSH_PRIVATE_KEY at carve time. Every other
    user-supplied flag forwards verbatim. Split out from the click command so tests
    can exercise the wiring without faking a subprocess.

    The bake source is exactly one of ``--from-tag`` (production, clones a tag)
    or ``--workspace-dir`` (dev, a working tree); the admin CLI derives the
    canonical ``repo_url`` / ``repo_branch_or_tag`` from it, so ``--attributes``
    carries only non-identity attributes (and may be omitted).

    ``--database-url`` is forwarded only when ``database_url`` is non-None.
    The caller (``pool_create`` via :func:`resolve_host_pool_dsn`) supplies a
    Vault-resolved DSN for staging / production and None for dev / ci; when
    None is passed through here the admin CLI auto-resolves the DSN from the
    activated minds env's ``secrets.toml`` (which the deploy wrote).

    ``--no-recycle`` (when ``is_recycle_enabled`` is False) is ovh_vps-only;
    ``--server-id`` (the explicitly-chosen bare-metal box), ``--dry-run`` (when
    ``is_dry_run`` is True), and ``--max-concurrency`` (when non-None) are
    slice-only; each is forwarded only when set.
    """
    args = [
        "create",
        "--backend",
        backend,
        "--count",
        str(count),
        "--region",
        region,
    ]
    if backend == _BACKEND_OVH_VPS:
        assert management_public_key_file is not None, "ovh_vps requires a management public key"
        args.extend(["--tag", f"minds_env={env_name}"])
        args.extend(["--management-public-key-file", management_public_key_file])
    if from_tag is not None:
        args.extend(["--from-tag", from_tag])
    if repo_url is not None:
        args.extend(["--repo-url", repo_url])
    if workspace_dir is not None:
        args.extend(["--workspace-dir", workspace_dir])
    if repo_branch_or_tag_override is not None:
        args.extend(["--repo-branch-or-tag", repo_branch_or_tag_override])
    if attributes_json is not None:
        args.extend(["--attributes", attributes_json])
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if mngr_source is not None:
        args.extend(["--mngr-source", mngr_source])
    if backend == _BACKEND_OVH_VPS and not is_recycle_enabled:
        args.append("--no-recycle")
    if backend == _BACKEND_SLICE:
        # Stamp the owning env into each slice's lima names so multiple dev envs can
        # share one bare-metal box (occupancy read from the box; reap scoped to this env).
        args.extend(["--slice-env-name", env_name])
    if backend == _BACKEND_SLICE and server_id is not None:
        args.extend(["--server-id", server_id])
    if backend == _BACKEND_SLICE and is_dry_run:
        args.append("--dry-run")
    if backend == _BACKEND_SLICE and max_concurrency is not None:
        args.extend(["--max-concurrency", str(max_concurrency)])
    if is_deferred_install_wait_skipped:
        args.append("--skip-deferred-install-wait")
    return args


def build_teardown_slices_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool teardown-slices`` argv.

    Forwards ``--database-url`` only when non-None (dev auto-resolves it from the
    activated env's secrets.toml; staging/production pass the Vault-resolved DSN).
    """
    args = ["teardown-slices"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def tear_down_env_pool_slices(env_name: str) -> None:
    """Tear down the env's unleased pool slices on their boxes before the env's DB is deleted.

    Resolves the pool SSH key (Vault) + host_pool DSN exactly like ``pool create``,
    then shells to ``mngr imbue_cloud admin pool teardown-slices``. Leased slices are
    left to their agent's release path. A missing pool SSH key is a bad state, not a
    "nothing to clean up" signal -- it raises (failing the destroy) so we never
    silently leak the env's slice VMs; a genuine teardown failure (an unreachable
    box) likewise raises rather than leaking.
    """
    try:
        pool_private_key = read_pool_private_key_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
        ) from exc
    database_url = resolve_host_pool_dsn(env_name, None)
    args = build_teardown_slices_admin_args(database_url=database_url)
    _raise_on_failure(
        "teardown-slices", _run_admin_command(args, extra_env={_POOL_PRIVATE_KEY_ENV_VAR: pool_private_key})
    )


def build_list_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool list`` argv.

    ``--database-url`` forwarded only when explicitly supplied; see
    :func:`build_create_admin_args`.
    """
    args = ["list"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def build_backfill_host_keys_admin_args(*, database_url: str | None) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool backfill-host-keys`` argv.

    ``--database-url`` forwarded only when explicitly supplied; see
    :func:`build_create_admin_args`.
    """
    args = ["backfill-host-keys"]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    return args


def build_destroy_admin_args(
    *, pool_host_id: str, database_url: str | None, force: bool, skip_vps_cancel: bool
) -> list[str]:
    """Compose the ``mngr imbue_cloud admin pool destroy`` argv."""
    args = ["destroy", pool_host_id]
    if database_url is not None:
        args.extend(["--database-url", database_url])
    if force:
        args.append("--force")
    if skip_vps_cancel:
        args.append("--skip-vps-cancel")
    return args


def _stream_subprocess_line(line: str, is_stdout: bool) -> None:
    """Mirror a child-process line to our stderr in real time.

    Match the line-streaming helper in ``mngr_imbue_cloud.cli.admin``:
    we want to faithfully echo the inner ``mngr imbue_cloud admin pool``
    output without loguru's timestamp/level prefix, so a multi-host bake
    isn't a silent black box. ``logger.info`` would distort the format;
    ``write_human_line`` is for one-shot status messages, not streamed
    subprocess output.
    """
    suffix = "" if line.endswith("\n") else "\n"
    sys.stderr.write(line + suffix)
    sys.stderr.flush()


def merge_extra_env_into_subprocess_env(
    *, shell_env: Mapping[str, str], extra_env: Mapping[str, str]
) -> dict[str, str]:
    """Build the subprocess env: start from ``shell_env``, then layer ``extra_env`` on top.

    Injects per-tier secrets resolved from Vault into the admin subprocess without
    mutating the parent process's environment: the OVH AK/AS/CK for the ``ovh_vps``
    backend, or ``POOL_SSH_PRIVATE_KEY`` for the ``slice`` backend. Vault values from
    the activated tier win over whatever the operator may have lying around in their
    shell. The operator's mental model when running ``minds pool create`` (with an
    activated env) is "this provisions hosts for the active tier" -- so the active
    tier's secrets are the source of truth, not a stale value that might still be
    exported from a different tier's session last week.

    Pure function so the precedence rule is testable without a fake
    subprocess runner or a fake Vault.
    """
    merged = dict(shell_env)
    merged.update(extra_env)
    return merged


def derive_public_key_from_private(
    private_key_pem: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Run ``ssh-keygen -y`` to derive the public key from a private key PEM.

    ``ssh-keygen`` only reads from a file (not stdin), so the private key
    is written to a 0600 temp file for the call and unlinked immediately
    after. The returned string is the standard ``"<type> <base64>"`` form
    (without a comment), suitable for an ``authorized_keys`` line.

    Raises ``click.ClickException`` if ``ssh-keygen`` is missing or fails.
    """
    cg = (
        parent_cg.make_concurrency_group(name="ssh-keygen-derive-pub")
        if parent_cg is not None
        else ConcurrencyGroup(name="ssh-keygen-derive-pub")
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix="_priv", delete=False) as tmp:
        tmp.write(private_key_pem)
        if not private_key_pem.endswith("\n"):
            tmp.write("\n")
        tmp_path = tmp.name
    try:
        os.chmod(tmp_path, 0o600)
        with cg:
            result = cg.run_process_to_completion(
                command=["ssh-keygen", "-y", "-f", tmp_path],
                timeout=_SSH_KEYGEN_DERIVE_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
    finally:
        os.unlink(tmp_path)
    if result.returncode != 0:
        raise click.ClickException(
            f"`ssh-keygen -y` failed (exit {result.returncode}) while deriving the management "
            f"public key from the Vault-stored private key: {result.stderr.strip()}"
        )
    derived = result.stdout.strip()
    if not derived:
        raise click.ClickException(
            "`ssh-keygen -y` produced empty output while deriving the management public key; "
            "the Vault-stored private key may be malformed."
        )
    return derived


def resolve_management_public_key_from_vault(
    env_name: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Read the activated tier's management private key from Vault, return its public form.

    Looks up the tier for ``env_name``, loads the corresponding deploy
    config to discover ``vault_path_prefix``, then reads
    ``<prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY`` via the standard
    ``read_vault_kv`` shellout. The returned public key is the openssh
    ``authorized_keys`` line form (no comment), ready to write to the
    file the inner admin CLI's ``--management-public-key-file`` reads.

    Same Vault entry as the one ``minds env deploy`` pushes into the
    ``pool-ssh-<tier>`` Modal Secret -- so the bake-time injection and
    the connector's lease-time SSH auth always come from the same
    keypair. The original "operator picks a public key by hand" path led
    to silent mismatches (the operator generated a fresh key, baked a
    VPS the connector then couldn't talk to); deriving here makes that
    failure mode unreachable for the minds-side caller.

    Raises ``click.ClickException`` if the Vault entry is missing the
    required private-key field or if ``ssh-keygen -y`` cannot parse it.
    Raises ``VaultReadError`` for any underlying Vault read failure.
    """
    private_key = read_pool_private_key_from_vault(env_name, parent_cg=parent_cg)
    return derive_public_key_from_private(private_key, parent_cg=parent_cg)


def read_pool_private_key_from_vault(
    env_name: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Read the activated tier's pool management private key PEM from Vault.

    Reads ``<vault_path_prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY`` -- the same entry
    ``minds env deploy`` pushes into the ``pool-ssh-<tier>`` Modal Secret the
    connector loads, so the key the slice bake authorizes on the VM matches the one
    the connector SSHes with at lease/release time. The OVH backend derives the
    public form of this key for ``--management-public-key-file``; the slice backend
    needs the private key itself to SSH the box and carve the lima VM.

    Raises ``click.ClickException`` if the entry lacks the private-key field.
    Raises ``VaultReadError`` for any underlying Vault read failure.
    """
    tier = tier_for_env_name(env_name)
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    secret = read_vault_kv(VaultPath(f"{vault_prefix}/pool-ssh"), parent_concurrency_group=parent_cg)
    private_key = secret.get(_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD, "")
    if not private_key:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/pool-ssh is missing {_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD!r}; "
            "see apps/minds/docs/host-pool-setup.md step 2 for the schema."
        )
    return private_key


@contextlib.contextmanager
def resolved_management_public_key_path(
    env_name: str,
    *,
    explicit_path: str | None,
    parent_cg: ConcurrencyGroup | None = None,
) -> Iterator[str]:
    """Yield a filesystem path the inner admin CLI can hand to ``--management-public-key-file``.

    Two source-of-truth modes, in precedence order:

    1. ``explicit_path`` (from ``--management-public-key-file``): operator
       override. Yielded unchanged. Escape hatch for one-off bakes where
       the operator deliberately wants a non-canonical key.
    2. Vault (default): :func:`resolve_management_public_key_from_vault`
       derives the public form from the activated tier's
       ``<vault_path_prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY`` entry. The
       derived key is written to a private temp file that's cleaned up
       when the context exits (so the inner CLI sees ``exists=True`` and
       no stale public-key files litter the operator's machine).
    """
    if explicit_path is not None:
        yield explicit_path
        return
    pub_text = resolve_management_public_key_from_vault(env_name, parent_cg=parent_cg)
    with tempfile.TemporaryDirectory(prefix="minds-pool-mgmt-pub-") as tmpdir:
        pub_path = Path(tmpdir) / "id_ed25519.pub"
        pub_path.write_text(pub_text + "\n")
        yield str(pub_path)


def resolve_ovh_env_from_vault(
    env_name: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> dict[str, str]:
    """Read the activated tier's OVH AK/AS/CK from Vault, return as env-var dict.

    Looks up the tier for ``env_name`` (``production`` / ``staging`` /
    ``dev``), loads the corresponding deploy config to discover
    ``vault_path_prefix``, then reads ``<prefix>/ovh`` from Vault via the
    standard ``read_vault_kv`` shellout (so the operator's existing
    ``vault login`` + ``VAULT_ADDR`` / ``VAULT_NAMESPACE`` are honored).

    The required keys ``OVH_APPLICATION_KEY`` / ``OVH_APPLICATION_SECRET``
    / ``OVH_CONSUMER_KEY`` must all be present and non-empty; the
    optional ``OVH_ENDPOINT`` is included if set. Missing required keys
    raise ``click.ClickException`` with a pointer at the setup doc.

    Raises ``VaultReadError`` if the Vault read itself fails (binary
    missing, not logged in, entry absent, malformed payload).
    """
    tier = tier_for_env_name(env_name)
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    secret = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"), parent_concurrency_group=parent_cg)
    missing = [key for key in _OVH_REQUIRED_ENV_VARS if not secret.get(key)]
    if missing:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/ovh is missing required key(s) {missing}; "
            "see apps/minds/docs/host-pool-setup.md step 3 for the schema."
        )
    env_vars: dict[str, str] = {key: secret[key] for key in _OVH_REQUIRED_ENV_VARS}
    for key in _OVH_OPTIONAL_ENV_VARS:
        if value := secret.get(key):
            env_vars[key] = value
    return env_vars


def resolve_host_pool_dsn(
    env_name: str,
    explicit_database_url: str | None,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str | None:
    """Return the host_pool DSN to forward to the admin command, or None.

    Precedence: an explicit ``--database-url`` always wins. Otherwise the shared
    tiers (``staging`` / ``production``) keep no local ``secrets.toml``, so their
    DSN is read from the tier's ``<vault_prefix>/neon/DATABASE_URL`` Vault entry
    -- the same entry the connector and ``minds env deploy`` use. Per-env tiers
    (``dev`` / ``ci``) return None so the admin CLI auto-resolves the DSN from
    the per-env ``secrets.toml`` that ``minds env deploy`` wrote (this path never
    touches Vault).

    This mirrors :func:`resolve_ovh_env_from_vault` /
    :func:`resolve_management_public_key_from_vault`: the wrapper resolves every
    per-tier secret the bake needs from the same Vault prefix, so the operator
    never hand-passes ``--database-url`` for staging / production.

    Raises ``click.ClickException`` if the Vault read fails or the entry lacks
    a non-empty ``DATABASE_URL``.
    """
    if explicit_database_url is not None:
        return explicit_database_url
    tier = tier_for_env_name(env_name)
    if tier not in (PRODUCTION_ENV_NAME, STAGING_ENV_NAME):
        return None
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    try:
        secret = read_vault_kv(VaultPath(f"{vault_prefix}/neon"), parent_concurrency_group=parent_cg)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the host_pool DSN from Vault ({vault_prefix}/neon) for env '{env_name}': {exc}"
        ) from exc
    dsn = secret.get(_POOL_DSN_VAULT_FIELD, "")
    if not dsn:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/neon is missing {_POOL_DSN_VAULT_FIELD!r}; "
            "see apps/minds/docs/host-pool-setup.md step 3 for the schema."
        )
    return dsn


def _run_admin_command(args: list[str], *, extra_env: Mapping[str, str] | None = None) -> FinishedProcess:
    """Run ``mngr imbue_cloud admin pool <args>`` and return the result.

    Streams the child's output line-by-line so a multi-host bake isn't a
    silent black box. Forwards the current process env, with ``extra_env``
    layered on top so callers can inject the activated tier's per-backend
    secrets (OVH AK/AS/CK for ovh_vps, POOL_SSH_PRIVATE_KEY for slice; both
    read from Vault) without mutating the parent process's environment.
    """
    full_command = ["mngr", "imbue_cloud", "admin", "pool"] + args
    loggable_command = redact_secret_flag_values(full_command, secret_bearing_flags=_SECRET_BEARING_FLAGS)
    logger.info("Running: {}", " ".join(shlex.quote(part) for part in loggable_command))
    subprocess_env: dict[str, str] | None = None
    if extra_env:
        subprocess_env = merge_extra_env_into_subprocess_env(shell_env=os.environ, extra_env=extra_env)
    cg = ConcurrencyGroup(name="minds-pool")
    with cg:
        return cg.run_process_to_completion(
            command=full_command,
            timeout=float(_POOL_COMMAND_TIMEOUT_SECONDS),
            is_checked_after=False,
            on_output=_stream_subprocess_line,
            env=subprocess_env,
        )


def _raise_on_failure(label: str, result: FinishedProcess) -> None:
    if result.returncode != 0:
        raise click.ClickException(f"mngr imbue_cloud admin pool {label} failed (exit {result.returncode}).")


def _run_ovh_vps_pool_create(
    *,
    env_name: str,
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    management_public_key_file: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Resolve OVH creds + management key from Vault, then bake OVH-VPS pool hosts."""
    try:
        ovh_env = resolve_ovh_env_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(f"Could not read OVH credentials from Vault for env '{env_name}': {exc}") from exc
    try:
        with resolved_management_public_key_path(
            env_name, explicit_path=management_public_key_file
        ) as effective_mgmt_pub_path:
            args = build_create_admin_args(
                env_name=env_name,
                backend=_BACKEND_OVH_VPS,
                count=count,
                region=region,
                from_tag=from_tag,
                repo_url=repo_url,
                workspace_dir=workspace_dir,
                repo_branch_or_tag_override=repo_branch_or_tag_override,
                attributes_json=attributes_json,
                management_public_key_file=effective_mgmt_pub_path,
                database_url=database_url,
                mngr_source=mngr_source,
                is_recycle_enabled=is_recycle_enabled,
                is_dry_run=False,
                is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
            )
            _raise_on_failure("create", _run_admin_command(args, extra_env=ovh_env))
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read management SSH key from Vault for env '{env_name}': {exc}"
        ) from exc


def _run_slice_pool_create(
    *,
    env_name: str,
    count: int,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    management_public_key_file: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    server_id: str | None,
    is_dry_run: bool,
    is_deferred_install_wait_skipped: bool,
    max_concurrency: int | None,
) -> None:
    """Resolve the pool private key from Vault, then bake bare-metal slice pool hosts.

    Rejects the ovh_vps-only flags up front (clearer than silently dropping them):
    slices authorize the pool key from the tier's Vault entry at carve time and
    never recycle an OVH VPS. Slice baking targets the explicitly-chosen
    ``--server-id`` bare-metal box (see ``mngr imbue_cloud admin server list``).
    """
    if management_public_key_file is not None:
        raise click.UsageError(
            "--management-public-key-file is not applicable to --backend slice "
            "(slices authorize the pool key from the tier's Vault entry at carve time)"
        )
    if not is_recycle_enabled:
        raise click.UsageError("--no-recycle is not applicable to --backend slice")
    if not server_id:
        raise click.UsageError(
            "--server-id is required for --backend slice (the bare-metal box to bake onto; "
            "see `mngr imbue_cloud admin server list`)"
        )
    try:
        pool_private_key = read_pool_private_key_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
        ) from exc
    args = build_create_admin_args(
        env_name=env_name,
        backend=_BACKEND_SLICE,
        count=count,
        region=region,
        from_tag=from_tag,
        repo_url=repo_url,
        workspace_dir=workspace_dir,
        repo_branch_or_tag_override=repo_branch_or_tag_override,
        attributes_json=attributes_json,
        management_public_key_file=None,
        database_url=database_url,
        mngr_source=mngr_source,
        is_recycle_enabled=is_recycle_enabled,
        server_id=server_id,
        is_dry_run=is_dry_run,
        is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
        max_concurrency=max_concurrency,
    )
    _raise_on_failure("create", _run_admin_command(args, extra_env={_POOL_PRIVATE_KEY_ENV_VAR: pool_private_key}))


@click.group()
def pool() -> None:
    """Pool-host orchestration for the currently activated minds env."""


@pool.command(name="create")
@click.option("--count", required=True, type=int, help="Number of pool hosts to create")
@click.option(
    "--backend",
    type=click.Choice([_BACKEND_OVH_VPS, _BACKEND_SLICE]),
    default=_BACKEND_SLICE,
    show_default=True,
    help=(
        "Which machine backs each pool host. ``slice`` (the default) carves a lima VM on a "
        "pre-registered + prepped bare-metal box (see `mngr imbue_cloud admin server`). ``ovh_vps`` "
        "is DEPRECATED: baking new OVH classic VPS pool hosts is no longer supported. Existing OVH "
        "VPS pool hosts can still be listed and destroyed."
    ),
)
@click.option(
    "--region",
    required=True,
    type=str,
    help=(
        "Lease/region code stamped on every new row (e.g. ``US-EAST-VA``, ``US-WEST-OR``) -- what "
        "the connector region-matches at lease time. For ``ovh_vps`` it is also the OVH datacenter "
        "the VPS is ordered in; for ``slice`` it is the lease-region label only (NOT the box's raw "
        "datacenter code)."
    ),
)
@click.option(
    "--from-tag",
    "from_tag",
    default=None,
    help="[production] Clone --repo-url at this tag and bake from it. Mutually exclusive with --workspace-dir.",
)
@click.option(
    "--repo-url",
    "repo_url",
    default=None,
    help="[--from-tag only] Canonical repo to clone the tag from (default: the FCT remote).",
)
@click.option(
    "--workspace-dir",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help="[dev] Bake from this template repo working tree. Mutually exclusive with --from-tag.",
)
@click.option(
    "--repo-branch-or-tag",
    "repo_branch_or_tag_override",
    default=None,
    help="[--workspace-dir only] Override the stamped branch label (default: the folder's current branch).",
)
@click.option(
    "--attributes",
    "attributes_json",
    required=False,
    default=None,
    help=(
        'Optional non-identity lease-attributes JSON (e.g. \'{"cpus":2,"memory_gb":4}\'). repo_url and '
        "repo_branch_or_tag are derived from the bake source, not passed here."
    ),
)
@click.option(
    "--management-public-key-file",
    required=False,
    default=None,
    type=click.Path(exists=True),
    help=(
        "[ovh_vps only] Override path for the management SSH public key injected on the pool VPS+container. "
        "Default (omitted): derive from the activated tier's Vault entry "
        "`<vault_path_prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY` -- the same private key the connector "
        "loads from its `pool-ssh-<tier>` Modal Secret, which guarantees the lease-time SSH-key "
        "injection authenticates. Pass this only when bypassing the tier's canonical keypair."
    ),
)
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=_DATABASE_URL_HELP,
)
@click.option(
    "--mngr-source",
    type=click.Path(exists=True),
    default=None,
    help="Path to the mngr monorepo root. If provided, rsyncs into the template's vendor/mngr/ before creating hosts.",
)
@click.option(
    "--no-recycle",
    "is_recycle_enabled",
    flag_value=False,
    default=True,
    help=(
        "[ovh_vps only] Force a fresh OVH VPS order instead of reclaiming a cancelled (still-billable) "
        "VPS. Useful for testing the fresh-provision path. Forwarded to the admin command as --no-recycle."
    ),
)
@click.option(
    "--server-id",
    "server_id",
    default=None,
    help=(
        "[slice only, required] The bare_metal_servers row id to bake the slices onto (from "
        "`mngr imbue_cloud admin server list`). Slice baking targets an explicitly-chosen, ready box."
    ),
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="[slice only] Report the chosen server + per-slice sizing; do not bake.",
)
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=None,
    help=(
        "[slice only] Max slices baked at once; the rest queue. Bounds box contention so each "
        "`mngr create` stays under its timeout. Omitted: the admin CLI's default applies."
    ),
)
@click.option(
    "--skip-deferred-install-wait",
    "is_deferred_install_wait_skipped",
    is_flag=True,
    default=False,
    help=(
        "[dev only] Don't wait for the FCT deferred-install (heavy apt + Playwright/Chromium) before "
        "stopping the baked services agent. Faster, but the baked container's deferred-install may be "
        "incomplete. Never use for production hosts."
    ),
)
def pool_create(
    count: int,
    backend: str,
    region: str,
    from_tag: str | None,
    repo_url: str | None,
    workspace_dir: str | None,
    repo_branch_or_tag_override: str | None,
    attributes_json: str | None,
    management_public_key_file: str | None,
    database_url: str | None,
    mngr_source: str | None,
    is_recycle_enabled: bool,
    server_id: str | None,
    is_dry_run: bool,
    max_concurrency: int | None,
    is_deferred_install_wait_skipped: bool,
) -> None:
    """Create bare-metal slice pool hosts for the activated minds env.

    Resolves the activated tier's secrets from Vault so the operator never exports
    them by hand: for ``slice`` (the default and only supported backend) the
    POOL_SSH_PRIVATE_KEY (used to SSH the bare-metal box and carve the lima VM). The
    activated env dictates the tier, keeping "I'm on dev, I bake against the dev
    account using the dev keypair" the unambiguous default and making the
    keypair-mismatch class of bake failures unreachable for the standard path.

    ``--backend ovh_vps`` is DEPRECATED and rejected up front: baking new OVH classic
    VPS pool hosts is no longer supported (existing ones stay listable/destroyable).
    """
    # Baking new OVH VPS pool hosts is deprecated -- Imbue Cloud serves agents on
    # bare-metal slices now. Reject fast, before any activated-env / Vault / OVH
    # credential resolution. Existing OVH VPS pool hosts stay listable/destroyable.
    if backend == _BACKEND_OVH_VPS:
        raise click.UsageError(
            "Baking new OVH VPS pool hosts is deprecated -- use --backend slice (bare-metal slices). "
            "Existing OVH VPS pool hosts can still be listed and destroyed."
        )
    env_name = require_activated_env_name()
    if backend == _BACKEND_OVH_VPS and is_dry_run:
        raise click.UsageError("--dry-run is only supported for --backend slice")
    if backend == _BACKEND_OVH_VPS and max_concurrency is not None:
        raise click.UsageError("--max-concurrency is only supported for --backend slice")
    if backend == _BACKEND_OVH_VPS and server_id is not None:
        raise click.UsageError("--server-id is only supported for --backend slice")
    effective_database_url = resolve_host_pool_dsn(env_name, database_url)
    if backend == _BACKEND_SLICE:
        _run_slice_pool_create(
            env_name=env_name,
            count=count,
            region=region,
            from_tag=from_tag,
            repo_url=repo_url,
            workspace_dir=workspace_dir,
            repo_branch_or_tag_override=repo_branch_or_tag_override,
            attributes_json=attributes_json,
            management_public_key_file=management_public_key_file,
            database_url=effective_database_url,
            mngr_source=mngr_source,
            is_recycle_enabled=is_recycle_enabled,
            server_id=server_id,
            is_dry_run=is_dry_run,
            is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
            max_concurrency=max_concurrency,
        )
    else:
        _run_ovh_vps_pool_create(
            env_name=env_name,
            count=count,
            region=region,
            from_tag=from_tag,
            repo_url=repo_url,
            workspace_dir=workspace_dir,
            repo_branch_or_tag_override=repo_branch_or_tag_override,
            attributes_json=attributes_json,
            management_public_key_file=management_public_key_file,
            database_url=effective_database_url,
            mngr_source=mngr_source,
            is_recycle_enabled=is_recycle_enabled,
            is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
        )


@pool.command(name="list")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=_DATABASE_URL_HELP,
)
def pool_list(database_url: str | None) -> None:
    """List pool_hosts rows (forwards to ``mngr imbue_cloud admin pool list``)."""
    # No env-name filter on the rows: the admin command does not know about
    # minds_env today and we don't want to start parsing its JSON output here
    # just to filter. Operators who only want rows for the active env can pipe
    # the JSON through ``jq``. The activated env name is still needed to resolve
    # the staging/production host_pool DSN from Vault.
    env_name = require_activated_env_name()
    args = build_list_admin_args(database_url=resolve_host_pool_dsn(env_name, database_url))
    _raise_on_failure("list", _run_admin_command(args))


@pool.command(name="backfill-host-keys")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=_DATABASE_URL_HELP,
)
def pool_backfill_host_keys(database_url: str | None) -> None:
    """One-time: keyscan + record SSH host public keys for pre-existing pool rows and boxes.

    Forwards to ``mngr imbue_cloud admin pool backfill-host-keys`` -- the single
    sanctioned trust-on-first-use, used once after deploying the host-key-pinning
    connector so rows baked before the host-key columns existed become leasable
    again. Resolves the staging / production host_pool DSN from the tier's
    ``<vault_prefix>/neon/DATABASE_URL`` Vault entry exactly like ``pool list`` /
    ``pool destroy``, so the operator never hand-passes ``--database-url``.
    Idempotent: rows that already have keys are skipped.
    """
    env_name = require_activated_env_name()
    args = build_backfill_host_keys_admin_args(database_url=resolve_host_pool_dsn(env_name, database_url))
    _raise_on_failure("backfill-host-keys", _run_admin_command(args))


@pool.command(name="destroy")
@click.argument("pool_host_id")
@click.option(
    "--database-url",
    required=False,
    default=None,
    type=str,
    help=_DATABASE_URL_HELP,
)
@click.option("--force", is_flag=True, help="Drop the row even if status != 'released'")
@click.option(
    "--skip-vps-cancel",
    is_flag=True,
    default=False,
    help=(
        "Only drop the DB row; do NOT tear down the underlying machine (cancel the "
        "OVH VPS for an ovh_vps row, or destroy the lima VM for a slice row). Use "
        "only when the machine is already gone."
    ),
)
def pool_destroy(pool_host_id: str, database_url: str | None, force: bool, skip_vps_cancel: bool) -> None:
    """Full teardown of a pool host: tear down its underlying machine, then drop the row.

    Forwards to ``mngr imbue_cloud admin pool destroy``, which by default tears down
    the row's underlying machine before deleting the row -- cancelling the OVH VPS for
    an ``ovh_vps`` row, or destroying the lima VM (freeing the box slot) for a
    ``slice`` row. The teardown secrets are read from the activated tier's Vault
    entries and injected into the subprocess, mirroring ``pool create``. Pass
    ``--skip-vps-cancel`` to only drop the row when the machine is already gone.
    """
    env_name = require_activated_env_name()
    extra_env: dict[str, str] | None = None
    if not skip_vps_cancel:
        # The wrapper can't know the row's backend without an extra DB round-trip, so
        # inject BOTH teardown secrets the admin command might need; it uses only the
        # one matching the row's backend (OVH AK/AS/CK for an ovh_vps row,
        # POOL_SSH_PRIVATE_KEY for a slice row). Every tier with pool hosts has both
        # Vault entries (minds env deploy pushes both).
        try:
            ovh_env = resolve_ovh_env_from_vault(env_name)
        except VaultReadError as exc:
            raise click.ClickException(
                f"Could not read OVH credentials from Vault for env '{env_name}': {exc}"
            ) from exc
        try:
            pool_private_key = read_pool_private_key_from_vault(env_name)
        except VaultReadError as exc:
            raise click.ClickException(
                f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
            ) from exc
        extra_env = {**ovh_env, _POOL_PRIVATE_KEY_ENV_VAR: pool_private_key}
    args = build_destroy_admin_args(
        pool_host_id=pool_host_id,
        database_url=resolve_host_pool_dsn(env_name, database_url),
        force=force,
        skip_vps_cancel=skip_vps_cancel,
    )
    _raise_on_failure("destroy", _run_admin_command(args, extra_env=extra_env))
