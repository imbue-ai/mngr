#!/usr/bin/env python3
"""Provision one environment's Cloudflare R2 hosting for the pre-baked Lima image.

This is the one-time setup that must run once per environment before
``publish.py`` can upload anything. It is idempotent: re-running against an
already-provisioned environment reports what exists and changes nothing, so it is
safe to run against production after running it against dev.

It performs exactly the three things the image distribution needs:

  1. Creates the bucket ``minds-lima-images-<env>``.
  2. Attaches a custom domain to it. This is required, not a preference: a client
     download fetches tens of thousands of chunks, and the managed ``r2.dev``
     origin is rate-limited, so an extract served from it dies partway through
     with ``429`` and the image never assembles. A custom domain is served
     through Cloudflare's CDN and is not throttled.
  3. Mints an R2 API token scoped to *that one bucket*, and prints the S3
     credentials ``publish.py`` needs. The account-wide token this script runs
     with is never what publishes; the operator who publishes only ever holds a
     bucket-scoped credential.

The environment name is a full environment, not a tier: ``production``,
``staging``, or a per-developer dev environment such as ``dev-weishi``. Each gets
its own bucket and hostname, so one developer's republish cannot overwrite
another's image or production's.

Nothing here is a runtime secret. The app fetches a public URL and verifies a
public minisign key; the credentials below are only ever used by an operator at
publish time.

Reads ``CLOUDFLARE_API_TOKEN`` / ``CLOUDFLARE_ACCOUNT_ID`` / ``CLOUDFLARE_ZONE_ID``
/ ``CLOUDFLARE_DOMAIN`` -- i.e. the environment's existing Vault ``cloudflare``
entry:

    export VAULT_ADDR=... VAULT_NAMESPACE=admin
    for key in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID CLOUDFLARE_DOMAIN; do
      export $key=$(vault kv get -mount=secrets -field=value minds/<tier>/cloudflare/$key)
    done
    uv run python scripts/lima_image/setup_tier.py --env production
"""

import hashlib
import os
import sys
from dataclasses import dataclass

import click
import httpx

_API_ROOT = "https://api.cloudflare.com/client/v4"
_BUCKET_PREFIX = "minds-lima-images"
_HOSTNAME_PREFIX = "lima-images"

# Cloudflare permission groups, scoped to a single bucket rather than the whole
# account. Both are needed: publish.py probes each chunk before uploading it, so a
# write-only token would fail every presence check.
_R2_BUCKET_ITEM_READ = "6a018a9f2fc74eb6b293b0c548f38b39"
_R2_BUCKET_ITEM_WRITE = "2efd5506f9c8494dacb1fa10a3e7d5b6"

_DEFAULT_JURISDICTION = "default"


@dataclass(frozen=True)
class CloudflareEnv:
    """The four values an environment's Vault `cloudflare` entry provides."""

    api_token: str
    account_id: str
    zone_id: str
    domain: str


def _read_env() -> CloudflareEnv:
    missing = [
        name
        for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_DOMAIN")
        if not os.environ.get(name)
    ]
    if missing:
        raise click.ClickException(f"Missing required environment variable(s): {', '.join(missing)}")
    return CloudflareEnv(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
        domain=os.environ["CLOUDFLARE_DOMAIN"],
    )


class CloudflareClient:
    """The slice of Cloudflare's API this setup needs, with errors surfaced rather than swallowed."""

    def __init__(self, env: CloudflareEnv, client: httpx.Client | None = None) -> None:
        self._env = env
        self._client = client if client is not None else httpx.Client(timeout=30.0)

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = self._client.request(
            method,
            f"{_API_ROOT}{path}",
            headers={"Authorization": f"Bearer {self._env.api_token}"},
            json=payload,
        )
        body = response.json()
        if not body.get("success"):
            raise click.ClickException(f"Cloudflare {method} {path} failed: {body.get('errors')}")
        return body.get("result") or {}

    def bucket_exists(self, bucket: str) -> bool:
        # Only a genuine 404 means the bucket is absent. Reading any other failure as
        # "absent" would turn a token that cannot list buckets into a confusing
        # "create failed" further down, instead of naming the permission problem here.
        response = self._client.get(
            f"{_API_ROOT}/accounts/{self._env.account_id}/r2/buckets/{bucket}",
            headers={"Authorization": f"Bearer {self._env.api_token}"},
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        if response.is_success:
            return True
        raise click.ClickException(
            f"Cloudflare GET r2/buckets/{bucket} failed ({response.status_code}): {response.text}"
        )

    def create_bucket(self, bucket: str) -> None:
        self._call("POST", f"/accounts/{self._env.account_id}/r2/buckets", {"name": bucket})

    def custom_domains(self, bucket: str) -> list[dict]:
        result = self._call("GET", f"/accounts/{self._env.account_id}/r2/buckets/{bucket}/domains/custom")
        return result.get("domains") or []

    def attach_custom_domain(self, bucket: str, hostname: str) -> None:
        self._call(
            "POST",
            f"/accounts/{self._env.account_id}/r2/buckets/{bucket}/domains/custom",
            {"domain": hostname, "zoneId": self._env.zone_id, "enabled": True, "minTLS": "1.2"},
        )

    def create_bucket_scoped_r2_token(self, bucket: str, name: str) -> tuple[str, str]:
        """Mint a token that can only read/write objects in ``bucket``; return (token_id, token_value)."""
        resource = f"com.cloudflare.edge.r2.bucket.{self._env.account_id}_{_DEFAULT_JURISDICTION}_{bucket}"
        result = self._call(
            "POST",
            f"/accounts/{self._env.account_id}/tokens",
            {
                "name": name,
                "policies": [
                    {
                        "effect": "allow",
                        "permission_groups": [{"id": _R2_BUCKET_ITEM_READ}, {"id": _R2_BUCKET_ITEM_WRITE}],
                        "resources": {resource: "*"},
                    }
                ],
            },
        )
        return result["id"], result["value"]


def bucket_name(env_name: str) -> str:
    """e.g. minds-lima-images-dev-weishi"""
    return f"{_BUCKET_PREFIX}-{env_name}"


def default_hostname(env_name: str, domain: str) -> str:
    """e.g. lima-images-production.minds.example"""
    return f"{_HOSTNAME_PREFIX}-{env_name}.{domain}"


def r2_s3_secret_access_key(token_value: str) -> str:
    """R2 derives an S3 secret access key as the SHA-256 of the API token's value."""
    return hashlib.sha256(token_value.encode()).hexdigest()


@click.command()
@click.option(
    "--env",
    "env_name",
    required=True,
    help="Environment to provision: production, staging, or a dev env such as dev-weishi",
)
@click.option("--hostname", default=None, help="Custom domain to serve the image from (default: derived)")
@click.option("--mint-token/--no-mint-token", default=True, help="Mint a bucket-scoped R2 token for publishing")
@click.option("--dry-run", is_flag=True, help="Report what would change without changing anything")
def main(env_name: str, hostname: str | None, mint_token: bool, dry_run: bool) -> None:
    env = _read_env()
    bucket = bucket_name(env_name)
    resolved_hostname = hostname if hostname is not None else default_hostname(env_name, env.domain)
    client = CloudflareClient(env)

    click.echo(f"Environment: {env_name}")
    click.echo(f"  bucket:   {bucket}")
    click.echo(f"  hostname: {resolved_hostname}")
    click.echo("")

    bucket_already_exists = client.bucket_exists(bucket)
    if bucket_already_exists:
        click.echo(f"[ok]   bucket {bucket} already exists")
    elif dry_run:
        click.echo(f"[plan] would create bucket {bucket}")
    else:
        client.create_bucket(bucket)
        click.echo(f"[new]  created bucket {bucket}")

    # Listing domains on a bucket that does not exist yet 404s, so only ask once it does.
    # A dry run still asks, so it reports what would actually change rather than assuming
    # the domain is missing and always claiming it would attach one.
    attached = [domain["domain"] for domain in client.custom_domains(bucket)] if bucket_already_exists else []
    if resolved_hostname in attached:
        click.echo(f"[ok]   custom domain {resolved_hostname} already attached")
    elif dry_run:
        click.echo(f"[plan] would attach custom domain {resolved_hostname} (zone {env.zone_id})")
    else:
        client.attach_custom_domain(bucket, resolved_hostname)
        click.echo(f"[new]  attached custom domain {resolved_hostname} (DNS + cert take a minute to go active)")

    if dry_run:
        if mint_token:
            click.echo("[plan] would mint a bucket-scoped R2 token")
        return

    click.echo("")
    if mint_token:
        token_id, token_value = client.create_bucket_scoped_r2_token(bucket, f"{bucket}-publish")
        click.echo("Publish credentials (scoped to this bucket only; store them, they are shown once):")
        click.echo("")
        click.echo(f"  export R2_ACCOUNT_ID={env.account_id}")
        click.echo(f"  export R2_ACCESS_KEY_ID={token_id}")
        click.echo(f"  export R2_SECRET_ACCESS_KEY={r2_s3_secret_access_key(token_value)}")
        click.echo("")

    click.echo(f"Commit into the tier's client.toml (both values are public), once {resolved_hostname} is live:")
    click.echo("")
    click.echo(f'  lima_image_base_url = "https://{resolved_hostname}"')
    click.echo('  lima_image_minisign_public_key = "RW..."   # line 2 of the tier\'s minisign .pub')


if __name__ == "__main__":
    sys.exit(main())
