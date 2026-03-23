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
_RATE_LIMIT_MAX_RETRIES = 7
_RATE_LIMIT_INITIAL_BACKOFF_SECONDS = 2.0
_RATE_LIMIT_MAX_BACKOFF_SECONDS = 60.0


def _is_transient_latchkey_error(error: LatchkeyInvocationError) -> bool:
    """Check if a latchkey error is transient and worth retrying."""
    # curl exit code 35 = SSL connection error (e.g. "Connection reset by peer")
    # curl exit code 56 = failure in receiving network data
    # curl exit code 7 = failed to connect to host
    # curl exit code 28 = operation timed out (distinct from our subprocess timeout)
    transient_curl_exit_codes = {7, 28, 35, 56}
    return error.return_code in transient_curl_exit_codes


def retry_on_transient_error(
    api_caller: Callable[[str, dict[str, str] | None], dict[str, Any]],
    sleep_fn: Callable[[float], None],
    method: str,
    query_params: dict[str, str] | None,
) -> dict[str, Any]:
    """Call an API method with exponential backoff retry on transient errors.

    Retries on Slack rate limit errors and transient network errors (SSL resets,
    connection failures, etc.). Retries up to _RATE_LIMIT_MAX_RETRIES times
    (~3 minutes total). Non-transient errors are raised immediately.
    """
    backoff = _RATE_LIMIT_INITIAL_BACKOFF_SECONDS
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            return api_caller(method, query_params)
        except SlackApiError as e:
            if e.error != "ratelimited" or attempt == _RATE_LIMIT_MAX_RETRIES:
                raise
            logger.warning(
                "Rate limited by Slack API (%s), retrying in %.0fs (attempt %d/%d)",
                method,
                backoff,
                attempt + 1,
                _RATE_LIMIT_MAX_RETRIES,
            )
        except LatchkeyInvocationError as e:
            if not _is_transient_latchkey_error(e) or attempt == _RATE_LIMIT_MAX_RETRIES:
                raise
            logger.warning(
                "Transient network error calling %s (exit %d), retrying in %.0fs (attempt %d/%d)",
                method,
                e.return_code,
                backoff,
                attempt + 1,
                _RATE_LIMIT_MAX_RETRIES,
            )
        sleep_fn(backoff)
        backoff = min(backoff * 2, _RATE_LIMIT_MAX_BACKOFF_SECONDS)
    raise AssertionError("unreachable")


def call_slack_api(
    method: str,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call a Slack API method via latchkey curl, with automatic retry on transient errors.

    Retries with exponential backoff on rate limit and network errors (up to ~3 minutes).
    Raises LatchkeyInvocationError if the subprocess fails with a non-transient error,
    or SlackApiError if the Slack API returns a non-rate-limit error.
    """
    return retry_on_transient_error(_call_slack_api_once, time.sleep, method, query_params)


def _call_slack_api_once(
    method: str,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute a single Slack API call via latchkey curl."""
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
