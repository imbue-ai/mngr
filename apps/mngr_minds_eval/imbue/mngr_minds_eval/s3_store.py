"""S3 layout + reads for eval batches.

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

AWS_ENV_PATH = Path.home() / ".minds-eval" / "aws.env"
BATCH_CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
TRANSCRIPT_KEY = "artifacts/full_transcript.jsonl"


class AwsNotConfiguredError(RuntimeError):
    pass


def load_aws_env() -> dict:
    """Read the scoped eval AWS creds (KEY=VALUE file), falling back to the process env.

    Raises AwsNotConfiguredError when neither source has usable credentials -- the eval flow
    cannot proceed without S3 (the sandboxes write all results there).
    """
    env: dict[str, str] = {}
    if AWS_ENV_PATH.is_file():
        for raw in AWS_ENV_PATH.read_text().splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "MINDS_EVAL_BUCKET"):
        env.setdefault(key, os.environ.get(key, ""))
    if not (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY") and env.get("MINDS_EVAL_BUCKET")):
        raise AwsNotConfiguredError(
            "No eval AWS credentials. Expected {} with AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / "
            "MINDS_EVAL_BUCKET (or the same in the environment).".format(AWS_ENV_PATH)
        )
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return env


def make_client(env: dict):
    import boto3

    return boto3.client(
        "s3",
        aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
        region_name=env.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def case_prefix(batch: str, eval_name: str, case_name: str) -> str:
    return "{}/{}_{}".format(batch, eval_name, case_name)


def restic_repo_url(env: dict, case_prefix_value: str) -> str:
    """Per-case restic repo, inside the same bucket as the plain objects."""
    region = env.get("AWS_DEFAULT_REGION", "us-east-1")
    return "s3:s3.{}.amazonaws.com/{}/{}/restic".format(region, env["MINDS_EVAL_BUCKET"], case_prefix_value)


def put_json(client, bucket: str, key: str, payload: dict) -> None:
    client.put_object(
        Bucket=bucket, Key=key, Body=json.dumps(payload, indent=2).encode(), ContentType="application/json"
    )


def get_json(client, bucket: str, key: str) -> dict | None:
    """Parsed JSON at `key`, or None only when the object genuinely does not exist. A transient
    S3/network error propagates instead of masquerading as absence (which would silently drop a
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
