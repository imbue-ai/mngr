#!/usr/bin/env python3
"""Chunk, sign, and publish a pre-baked Lima image to an R2 chunk store.

This is the local, operator-run publish half of the Lima image distribution (the
build half is ``scripts/lima_image/build.sh``). It is intentionally NOT wired
into CI: the R2 credentials and the minisign signing key stay on the operator's
machine, never in GitHub.

For each (version, arch) it:
  1. ``desync make``s the raw image into a content-addressed chunk store + index,
  2. merges the arch entry into the release's root manifest (downloading any
     existing manifest so a second arch published later is added, not replaced),
  3. signs the manifest with ``minisign`` (detached), and
  4. uploads the new chunks + index + manifest + signature to R2.

Upload backends:
  * ``s3`` (default, recommended for production): boto3 against R2's S3 API.
    Reads ``R2_ACCOUNT_ID`` / ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY``.
  * ``cloudflare-api``: the Cloudflare REST object API using
    ``CLOUDFLARE_API_TOKEN`` + ``CLOUDFLARE_ACCOUNT_ID``. Handy when you only
    have an account API token (no S3 keys).

Content-addressed chunks are skipped when already present, so re-publishing a
near-identical image only uploads the changed chunks.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from abc import ABC
from abc import abstractmethod
from datetime import datetime
from datetime import timezone
from pathlib import Path

import boto3
import click
import httpx
from botocore.client import Config

_MANIFEST_PREFIX = "manifests"
_INDEX_PREFIX = "indexes"
_STORE_PREFIX = "store"
_ROOT_MANIFEST_FILENAME = "root.json"
_SIGNATURE_SUFFIX = ".minisig"
_SCHEMA_VERSION = 1
_VALID_ARCHES = ("aarch64", "x86_64")


class ObjectStore(ABC):
    """Minimal put/get/exists over a flat object namespace (R2)."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def get_optional(self, key: str) -> bytes | None: ...


class S3ObjectStore(ObjectStore):
    """boto3 against the R2 S3-compatible endpoint (production path)."""

    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str, bucket: str) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
            region_name="auto",
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except self._client.exceptions.ClientError:
            return False

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def get_optional(self, key: str) -> bytes | None:
        # Only a genuinely-absent key maps to None. Any other ClientError
        # (permission, throttling, transient outage) must propagate: silently
        # treating it as "no manifest yet" would make a second-arch publish drop
        # the first arch's already-published entry when it rewrites the manifest.
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None
        return response["Body"].read()


class CloudflareApiObjectStore(ObjectStore):
    """Cloudflare REST object API using an account API token (no S3 keys needed)."""

    def __init__(self, account_id: str, api_token: str, bucket: str) -> None:
        self._base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects"
        self._headers = {"Authorization": f"Bearer {api_token}"}

    def exists(self, key: str) -> bool:
        response = httpx.head(f"{self._base}/{key}", headers=self._headers, timeout=30.0)
        return response.status_code == httpx.codes.OK

    def put(self, key: str, data: bytes, content_type: str) -> None:
        response = httpx.put(
            f"{self._base}/{key}",
            headers={**self._headers, "Content-Type": content_type},
            content=data,
            timeout=120.0,
        )
        response.raise_for_status()

    def get_optional(self, key: str) -> bytes | None:
        response = httpx.get(f"{self._base}/{key}", headers=self._headers, timeout=60.0)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.content


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_image(raw_image: Path, work_dir: Path) -> tuple[Path, Path]:
    """Run ``desync make`` to produce a local chunk store + index for ``raw_image``."""
    store_dir = work_dir / _STORE_PREFIX
    store_dir.mkdir(parents=True, exist_ok=True)
    index_path = work_dir / "image.caibx"
    subprocess.run(["desync", "make", "-s", str(store_dir), str(index_path), str(raw_image)], check=True)
    return store_dir, index_path


def _upload_store(store_dir: Path, store: ObjectStore) -> int:
    """Upload chunk files that are not already present; return the count uploaded."""
    uploaded = 0
    for chunk_path in sorted(store_dir.rglob("*")):
        if not chunk_path.is_file():
            continue
        key = f"{_STORE_PREFIX}/{chunk_path.relative_to(store_dir).as_posix()}"
        if store.exists(key):
            continue
        store.put(key, chunk_path.read_bytes(), "application/octet-stream")
        uploaded += 1
    return uploaded


def _merge_manifest(store: ObjectStore, version: str, new_entry: dict) -> dict:
    """Load the existing root manifest (if any) and add/replace ``new_entry`` for its arch."""
    existing_bytes = store.get_optional(f"{_MANIFEST_PREFIX}/{version}/{_ROOT_MANIFEST_FILENAME}")
    if existing_bytes is not None:
        manifest = json.loads(existing_bytes)
        entries = [entry for entry in manifest.get("entries", []) if entry.get("arch") != new_entry["arch"]]
    else:
        entries = []
    entries.append(new_entry)
    return {
        "schema_version": _SCHEMA_VERSION,
        "minds_version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": sorted(entries, key=lambda entry: entry["arch"]),
    }


def _sign_manifest(manifest_bytes: bytes, secret_key_file: Path) -> bytes:
    """Sign ``manifest_bytes`` with minisign and return the detached signature bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / _ROOT_MANIFEST_FILENAME
        manifest_path.write_bytes(manifest_bytes)
        signature_path = manifest_path.with_suffix(manifest_path.suffix + _SIGNATURE_SUFFIX)
        subprocess.run(
            ["minisign", "-S", "-s", str(secret_key_file), "-m", str(manifest_path), "-x", str(signature_path)],
            check=True,
            input=b"",
        )
        return signature_path.read_bytes()


def _build_store(uploader: str, bucket: str) -> ObjectStore:
    if uploader == "s3":
        account_id = os.environ["R2_ACCOUNT_ID"]
        return S3ObjectStore(
            account_id=account_id,
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket=bucket,
        )
    return CloudflareApiObjectStore(
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        bucket=bucket,
    )


@click.command()
@click.option("--version", "version", required=True, help="minds release tag, e.g. minds-v0.3.4")
@click.option("--arch", required=True, type=click.Choice(_VALID_ARCHES), help="Image architecture")
@click.option("--raw-image", required=True, type=click.Path(exists=True, path_type=Path), help="Raw image to publish")
@click.option("--bucket", required=True, help="R2 bucket name")
@click.option(
    "--secret-key-file",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="minisign secret key (use an unencrypted -W key for non-interactive signing)",
)
@click.option("--uploader", type=click.Choice(["s3", "cloudflare-api"]), default="s3", help="Upload backend")
@click.option("--work-dir", type=click.Path(path_type=Path), default=None, help="Scratch dir for chunking")
def main(
    version: str, arch: str, raw_image: Path, bucket: str, secret_key_file: Path, uploader: str, work_dir: Path | None
) -> None:
    store = _build_store(uploader, bucket)
    with tempfile.TemporaryDirectory() as default_work:
        resolved_work_dir = work_dir if work_dir is not None else Path(default_work)
        resolved_work_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"Chunking {raw_image} ...")
        store_dir, index_path = _chunk_image(raw_image, resolved_work_dir)

        entry = {
            "arch": arch.upper(),
            "raw_index_object_key": f"{_INDEX_PREFIX}/{version}/{arch}.caibx",
            "raw_image_sha256": _sha256_file(raw_image),
            "raw_image_size_bytes": raw_image.stat().st_size,
        }

        click.echo("Uploading chunks ...")
        uploaded = _upload_store(store_dir, store)
        click.echo(f"Uploaded {uploaded} new chunk(s).")

        store.put(entry["raw_index_object_key"], index_path.read_bytes(), "application/octet-stream")

        manifest = _merge_manifest(store, version, entry)
        manifest_bytes = json.dumps(manifest).encode()
        signature_bytes = _sign_manifest(manifest_bytes, secret_key_file)
        store.put(f"{_MANIFEST_PREFIX}/{version}/{_ROOT_MANIFEST_FILENAME}", manifest_bytes, "application/json")
        store.put(
            f"{_MANIFEST_PREFIX}/{version}/{_ROOT_MANIFEST_FILENAME}{_SIGNATURE_SUFFIX}",
            signature_bytes,
            "application/octet-stream",
        )
        click.echo(f"Published {version} / {arch} (manifest entries: {len(manifest['entries'])}).")


if __name__ == "__main__":
    sys.exit(main())
