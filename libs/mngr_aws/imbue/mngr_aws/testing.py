"""Shared test helpers for mngr_aws.

Lives outside ``conftest.py`` so test modules and library modules can
import these without depending on pytest collection semantics.
"""

import os


def aws_credentials_available() -> bool:
    """Return True if AWS credentials are plausibly present in the environment.

    Used to gate release tests (skipif) and the session-end cleanup hook
    (no-op when credentials are absent). Only checks the two env-var
    families that are sufficient for boto3's default chain to find
    credentials without further configuration -- this is intentionally a
    fast, non-network check, not a full boto3 ``get_credentials`` probe.
    """
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")) or bool(os.environ.get("AWS_PROFILE"))
