import json
import logging
import subprocess
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from imbue.slack_exporter.errors import LatchkeyInvocationError
from imbue.slack_exporter.errors import SlackApiError

logger = logging.getLogger(__name__)

_LATCHKEY_COMMAND_TIMEOUT_SECONDS = 60
_LATCHKEY_COMMAND_WARNING_THRESHOLD_SECONDS = 15


def call_slack_api(
    method: str,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call a Slack API method via latchkey curl and return the parsed JSON response.

    Raises LatchkeyInvocationError if the subprocess fails, or SlackApiError if
    the Slack API returns ok=false.
    """
    url = f"https://slack.com/api/{method}"
    if query_params:
        url = f"{url}?{urlencode(query_params)}"

    command = ["latchkey", "curl", url]
    logger.debug("Running: %s", " ".join(command))

    start_time = time.monotonic()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_LATCHKEY_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise LatchkeyInvocationError(
            command=" ".join(command),
            return_code=-1,
            stderr=f"Command timed out after {_LATCHKEY_COMMAND_TIMEOUT_SECONDS}s",
        ) from e
    elapsed = time.monotonic() - start_time

    if elapsed > _LATCHKEY_COMMAND_WARNING_THRESHOLD_SECONDS:
        logger.warning(
            "latchkey call to %s took %.1fs (threshold: %ds)",
            method,
            elapsed,
            _LATCHKEY_COMMAND_WARNING_THRESHOLD_SECONDS,
        )

    return parse_latchkey_response(
        command_str=" ".join(command),
        method=method,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def parse_latchkey_response(
    command_str: str,
    method: str,
    return_code: int,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Parse and validate the output from a latchkey curl invocation.

    Raises LatchkeyInvocationError on non-zero exit or invalid JSON.
    Raises SlackApiError if the Slack API returned ok=false.
    """
    if return_code != 0:
        raise LatchkeyInvocationError(
            command=command_str,
            return_code=return_code,
            stderr=stderr,
        )

    try:
        data: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise LatchkeyInvocationError(
            command=command_str,
            return_code=0,
            stderr=f"Invalid JSON response: {stdout[:200]}",
        ) from e

    if not data.get("ok"):
        raise SlackApiError(method=method, error=data.get("error", "unknown"))

    return data


def fetch_paginated(
    api_caller: Callable[[str, dict[str, str] | None], dict[str, Any]],
    method: str,
    base_params: dict[str, str],
    response_key: str,
) -> list[dict[str, Any]]:
    """Fetch all items from a paginated Slack API endpoint.

    Handles both cursor-based pagination and has_more-based pagination.
    """
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        params = dict(base_params)
        if cursor:
            params["cursor"] = cursor

        data = api_caller(method, params)
        all_items.extend(data.get(response_key, []))

        # Stop if has_more is explicitly False (used by history/replies endpoints)
        if "has_more" in data and not data["has_more"]:
            break

        # Stop if no pagination cursor
        next_cursor = extract_next_cursor(data)
        if not next_cursor:
            break
        cursor = next_cursor

    return all_items


def extract_next_cursor(data: dict[str, Any]) -> str | None:
    """Extract the pagination cursor from a Slack API response, if present."""
    response_metadata = data.get("response_metadata")
    if not isinstance(response_metadata, dict):
        return None
    next_cursor = response_metadata.get("next_cursor", "")
    if not next_cursor:
        return None
    return next_cursor
