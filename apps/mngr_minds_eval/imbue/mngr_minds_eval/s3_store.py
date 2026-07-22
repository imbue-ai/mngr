"""R2 (S3-compatible) layout + reads for eval batches.

The store speaks the S3 API against Cloudflare R2 via boto3's ``endpoint_url``. Credentials are a
scoped R2 key (mint one with ``mngr imbue_cloud bucket create``); the access key id / secret are
passed to boto3 and restic under the standard ``AWS_*`` names, which is what both tools read.

Layout (bucket root):
    <eval_name>_<datetime>/                     batch
        config.json                             the batch config (personas + settings)
        <eval_name>_<case_name>/                one per case
            state.json                          written by the in-sandbox eval worker
            artifacts/full_transcript.jsonl     written by the worker on the final turn
            (restic snapshots live in the case's restic repo, tagged post_message_<k>)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

R2_ENV_PATH = Path.home() / ".minds-eval" / "r2.env"
BATCH_CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
TRANSCRIPT_KEY = "artifacts/full_transcript.jsonl"


class AwsNotConfiguredError(RuntimeError):
    pass


def load_aws_env() -> dict:
    """Read the scoped eval R2 creds (KEY=VALUE file), falling back to the process env.

    Raises AwsNotConfiguredError when neither source has usable credentials -- the eval flow
    cannot proceed without R2 (the sandboxes write all results there).
    """
    env: dict[str, str] = {}
    if R2_ENV_PATH.is_file():
        for raw in R2_ENV_PATH.read_text().splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "MINDS_EVAL_BUCKET",
        "MINDS_EVAL_S3_ENDPOINT",
    ):
        env.setdefault(key, os.environ.get(key, ""))
    if not (
        env.get("AWS_ACCESS_KEY_ID")
        and env.get("AWS_SECRET_ACCESS_KEY")
        and env.get("MINDS_EVAL_BUCKET")
        and env.get("MINDS_EVAL_S3_ENDPOINT")
    ):
        raise AwsNotConfiguredError(
            "No eval R2 credentials. Expected {} with AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / "
            "MINDS_EVAL_BUCKET / MINDS_EVAL_S3_ENDPOINT (or the same in the environment). Mint them "
            "with `mngr imbue_cloud bucket create <name>`.".format(R2_ENV_PATH)
        )
    # R2 ignores the region, but boto3 and restic both require one; "auto" is R2's convention.
    if not env.get("AWS_DEFAULT_REGION"):
        env["AWS_DEFAULT_REGION"] = "auto"
    return env


def make_client(env: dict):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=env["MINDS_EVAL_S3_ENDPOINT"],
        aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
        region_name=env.get("AWS_DEFAULT_REGION") or "auto",
    )


def case_prefix(batch: str, eval_name: str, case_name: str) -> str:
    return "{}/{}_{}".format(batch, eval_name, case_name)


def restic_repo_url(env: dict, case_prefix_value: str) -> str:
    """Per-case restic repo, inside the same bucket as the plain objects. restic's S3 backend takes
    the endpoint (with scheme) directly in the URL, so it points at R2 the same way boto3 does."""
    endpoint = env["MINDS_EVAL_S3_ENDPOINT"].rstrip("/")
    return "s3:{}/{}/{}/restic".format(endpoint, env["MINDS_EVAL_BUCKET"], case_prefix_value)


def put_json(client, bucket: str, key: str, payload: dict) -> None:
    client.put_object(
        Bucket=bucket, Key=key, Body=json.dumps(payload, indent=2).encode(), ContentType="application/json"
    )


def get_json(client, bucket: str, key: str) -> dict | None:
    """Parsed JSON at `key`, or None only when the object genuinely does not exist. A transient
    R2/network error propagates instead of masquerading as absence (which would silently drop a
    finished case or report a real batch as missing)."""
    import botocore.exceptions

    try:
        return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read().decode())
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def list_batches(client, bucket: str) -> list[str]:
    """Top-level batch folders (batch = the unique eval name), alphabetical. The caller can order by
    creation time from each batch's config (created_at)."""
    paginator = client.get_paginator("list_objects_v2")
    names: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            names.add(common["Prefix"].rstrip("/"))
    return sorted(names)
