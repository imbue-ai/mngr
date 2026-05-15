#!/usr/bin/env python3
"""Pull tier secrets from HCP Vault and push them into Modal Secrets.

For each service named in the tier's ``deploy.toml`` ``[secrets].services``
list, this script:

1. Reads ``<vault_path_prefix>/<service>`` from HCP Vault via the local
   ``vault`` CLI (so authentication piggybacks on whatever ``vault login``
   set up).
2. Validates that every key declared by ``.minds/template/<service>.sh``
   is present in the Vault entry. Empty values are fine (declared but
   intentionally unset on Modal); missing keys are an error.
3. Pushes the non-empty subset to Modal as the secret
   ``<service>-<tier>`` via ``modal secret create --force``.

Vault values are kept in process memory only -- never written to disk.

Usage:
    uv run scripts/push_modal_secrets.py <tier> [--dry-run]

Examples:
    uv run scripts/push_modal_secrets.py production
    uv run scripts/push_modal_secrets.py staging --dry-run

The ``<tier>`` argument must match one of the directories under
``apps/minds/imbue/minds/config/envs/`` (i.e. ``dev``, ``staging``,
``production``). The Vault path prefix and the list of services to push
come from ``apps/minds/imbue/minds/config/envs/<tier>/deploy.toml``.

Dynamic dev env secrets do **not** flow through this script -- they live
on the developer's machine only and are pushed to per-Modal-env secrets
by ``minds env create``.
"""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

# Ensure the in-repo packages are importable when this script runs via uv.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "apps" / "minds"))

from imbue.minds.config.loader import EnvConfigError  # noqa: E402
from imbue.minds.config.loader import load_deploy_config  # noqa: E402
from imbue.minds.envs.primitives import VaultReadError  # noqa: E402
from imbue.minds.envs.vault_reader import VaultPath  # noqa: E402
from imbue.minds.envs.vault_reader import read_vault_kv  # noqa: E402

_TEMPLATE_DIR_RELATIVE = Path(".minds") / "template"


def _parse_template_keys(template_dir: Path, service: str) -> tuple[str, ...]:
    """Return the expected key names declared in ``<service>.sh``.

    Each template file is a shell-style ``export KEY=`` listing (with
    optional comment lines). We treat any ``export KEY=`` or ``KEY=``
    occurrence as a declaration, ignoring the value.
    """
    template_path = template_dir / f"{service}.sh"
    if not template_path.is_file():
        raise FileNotFoundError(
            f"No template schema found for service {service!r}: expected {template_path}. "
            f"Add a .minds/template/{service}.sh file or remove {service!r} from the "
            "tier's deploy.toml [secrets].services list."
        )
    keys: list[str] = []
    for raw_line in template_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        equals = line.find("=")
        if equals <= 0:
            continue
        key = line[:equals].strip()
        if key.isidentifier():
            keys.append(key)
    return tuple(keys)


def _validate_against_template(
    expected_keys: tuple[str, ...],
    vault_values: dict[str, str],
    service: str,
    vault_path: VaultPath,
) -> None:
    missing = [key for key in expected_keys if key not in vault_values]
    if missing:
        raise SystemExit(
            f"error: Vault entry {vault_path} (service {service!r}) is missing keys declared in "
            f".minds/template/{service}.sh: {sorted(missing)}. "
            f"Set every key (empty value is fine; the push will skip empties)."
        )


def _upsert_modal_secret(
    name: str,
    values: dict[str, str],
    *,
    modal_env: str | None,
    is_dry_run: bool,
) -> None:
    """Create or overwrite the Modal secret with the non-empty subset of ``values``.

    When ``modal_env`` is set, the secret is created in that Modal env;
    otherwise it lands in the workspace's default env. Modal Secrets are
    env-scoped, so the env passed here must match the env the Modal app
    is deployed into.
    """
    non_empty = {k: v for k, v in values.items() if v}
    if not non_empty:
        print(f"[skip] {name}: every value was empty", file=sys.stderr)
        return
    args = ["uv", "run", "modal", "secret", "create", "--force"]
    if modal_env is not None:
        args.extend(["--env", modal_env])
    args.append(name)
    for key, value in non_empty.items():
        args.append(f"{key}={value}")
    printable = [a if "=" not in a else f"{a.split('=', 1)[0]}=***" for a in args]
    print(f"[push] {name}: {len(non_empty)} key(s)")
    print(f"       {' '.join(shlex.quote(p) for p in printable)}")
    if is_dry_run:
        return
    subprocess.run(args, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "tier",
        help="Environment tier name (must match a directory under apps/minds/imbue/minds/config/envs/)",
    )
    parser.add_argument(
        "services",
        nargs="*",
        help=(
            "Optional list of services to push (e.g. `litellm cloudflare`). "
            "Every name must appear in the tier's deploy.toml [secrets].services list. "
            "When omitted, every service from deploy.toml is pushed."
        ),
    )
    parser.add_argument(
        "--env",
        dest="modal_env",
        default=None,
        help=(
            "Modal environment to push the secrets into (e.g. `main` or a per-developer env name). "
            "When omitted, the Modal CLI uses the workspace's default env. Modal Secrets are "
            "env-scoped, so this should match the env the deploy targets."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Modal commands without executing them",
    )
    parser.add_argument(
        "--template-dir",
        default=str(_REPO_ROOT / _TEMPLATE_DIR_RELATIVE),
        help="Override the .minds/template/ schema directory (default: repo root)",
    )
    args = parser.parse_args()

    try:
        deploy_config = load_deploy_config(args.tier)
    except EnvConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    template_dir = Path(args.template_dir)
    if not template_dir.is_dir():
        print(f"error: template dir not found: {template_dir}", file=sys.stderr)
        return 2

    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    declared_services = deploy_config.secrets.services
    if not declared_services:
        print(
            f"error: tier {args.tier!r} declares no services in [secrets].services; nothing to push.",
            file=sys.stderr,
        )
        return 2

    if args.services:
        unknown = sorted(set(args.services) - {str(s) for s in declared_services})
        if unknown:
            print(
                f"error: services {unknown} are not declared in tier {args.tier!r}'s [secrets].services. "
                f"Add them to deploy.toml or remove them from the command line.",
                file=sys.stderr,
            )
            return 2
        services = tuple(s for s in declared_services if str(s) in set(args.services))
    else:
        services = declared_services

    for service in services:
        vault_path = VaultPath(f"{vault_prefix}/{service}")
        try:
            vault_values = read_vault_kv(vault_path)
        except VaultReadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        expected_keys = _parse_template_keys(template_dir, str(service))
        _validate_against_template(expected_keys, vault_values, str(service), vault_path)

        modal_secret_name = f"{service}-{args.tier}"
        # Only push the keys the template declares; ignore stray extras
        # so a Vault entry can carry operator-only notes without leaking
        # into Modal.
        filtered = {key: vault_values[key] for key in expected_keys}
        _upsert_modal_secret(
            modal_secret_name,
            filtered,
            modal_env=args.modal_env,
            is_dry_run=args.dry_run,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
