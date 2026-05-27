"""Optional S3 upload of the TMR HTML report.

When ``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY`` are present in
the environment, the recipe mirrors each freshly-written
``index.html`` to ``s3://int8-shared-internal/tmr-reports/<run>.html``
in ``us-west-2`` and exposes it via the internal short link
``http://go/shared/tmr-reports/<run>.html``. Without those credentials,
the upload is a no-op and the helper returns ``None``.
"""

import os
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger

_BUCKET = "int8-shared-internal"
_REGION = "us-west-2"
_KEY_PREFIX = "tmr-reports"
_URL_BASE = "http://go/shared/tmr-reports"


def maybe_upload_report(html_path: Path, run_name: str) -> str | None:
    """Upload ``html_path`` to the shared S3 bucket and return its public URL.

    Returns ``None`` (and logs nothing user-facing) when AWS credentials
    are not configured. Logs a warning and returns ``None`` on upload
    failure -- a failed upload should not break the pipeline.
    """
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        return None

    key = f"{_KEY_PREFIX}/{run_name}.html"
    try:
        client = boto3.client("s3", region_name=_REGION)
        client.upload_file(
            str(html_path),
            _BUCKET,
            key,
            ExtraArgs={"ContentType": "text/html; charset=utf-8"},
        )
    except (BotoCoreError, ClientError, OSError) as exc:
        logger.warning("Failed to upload report to s3://{}/{}: {}", _BUCKET, key, exc)
        return None

    return f"{_URL_BASE}/{run_name}.html"
