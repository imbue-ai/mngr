from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.output_helpers import emit_final_json
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_usage.data_types import CACHE_RELATIVE_PATH
from imbue.mngr_usage.data_types import CACHE_SCHEMA_VERSION
from imbue.mngr_usage.data_types import CacheDoc
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.data_types import WINDOW_KEYS
from imbue.mngr_usage.data_types import WindowSnapshot

_RATE_LIMITS_WRITER_SCRIPT = "claude_rate_limits_writer.sh"
_REFRESH_PROBE_PROMPT = "ok"
_REFRESH_PROBE_SYSTEM_PROMPT = "Respond with one word."
_REFRESH_TIMEOUT_SECONDS = 60.0


class UsageCliOptions(CommonCliOptions):
    """Options for the `mngr usage` command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    refresh: bool
    max_age: str | None


def cache_path(mngr_ctx: MngrContext) -> Path:
    """Path to the shared rate-limit cache."""
    return mngr_ctx.profile_dir / CACHE_RELATIVE_PATH


def _load_cache(path: Path) -> CacheDoc | None:
    """Load cache document from disk; return None if missing or unreadable."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read rate-limit cache at {}: {}", path, e)
        return None
    if not isinstance(raw, dict):
        return None
    windows_raw = raw.get("windows", {})
    if not isinstance(windows_raw, dict):
        windows_raw = {}
    windows: dict[str, WindowSnapshot] = {}
    for key, value in windows_raw.items():
        if not isinstance(value, dict):
            continue
        try:
            windows[str(key)] = WindowSnapshot.model_validate(value)
        except (TypeError, ValueError) as e:
            logger.debug("Skipping invalid window entry {!r}: {}", key, e)
    schema_version = raw.get("schema_version", CACHE_SCHEMA_VERSION)
    if not isinstance(schema_version, int):
        schema_version = CACHE_SCHEMA_VERSION
    return CacheDoc(schema_version=schema_version, windows=windows)


@pure
def _parse_max_age(value: str | None) -> int | None:
    """Parse a max-age value like "300" or "5m" into seconds.

    Accepts a bare integer (seconds), or a number followed by s/m/h/d.
    """
    if value is None:
        return None
    value = value.strip().lower()
    if not value:
        return None
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", value)
    if match is None:
        raise click.UsageError(f"Invalid --max-age value: {value!r}. Expected e.g. '300', '5m', '2h'.")
    n = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * multiplier


@pure
def _oldest_updated_at(cache: CacheDoc | None) -> int | None:
    """Return the smallest updated_at across all windows, or None if no entries."""
    if cache is None or not cache.windows:
        return None
    timestamps = [w.updated_at for w in cache.windows.values() if w.updated_at is not None]
    if not timestamps:
        return None
    return min(timestamps)


def _should_refresh(
    cache: CacheDoc | None,
    plugin_config: UsagePluginConfig,
    refresh_flag: bool,
    max_age_override: int | None,
    now: int,
) -> bool:
    """Decide whether to trigger a refresh.

    - Explicit --refresh always wins.
    - When cache is empty and auto_refresh is on, refresh once.
    - When cache is populated, refresh if oldest updated_at is older than the resolved max-age.
    """
    if refresh_flag:
        return True
    if not plugin_config.auto_refresh:
        return False
    max_age = max_age_override if max_age_override is not None else plugin_config.max_age_seconds
    oldest = _oldest_updated_at(cache)
    if oldest is None:
        return cache is None or not cache.windows
    return (now - oldest) >= max_age


def _build_refresh_command(plugin_config: UsagePluginConfig) -> list[str]:
    """Build the argv for the refresh probe.

    The --setting-sources "" arg is load-bearing: it suppresses inherited Stop hooks
    that otherwise turn the probe into a recursive Claude session.
    """
    return [
        "claude",
        "-p",
        "--output-format=stream-json",
        "--verbose",
        "--setting-sources",
        "",
        "--model",
        plugin_config.refresh_model,
        "--tools",
        "",
        "--system-prompt",
        _REFRESH_PROBE_SYSTEM_PROMPT,
        _REFRESH_PROBE_PROMPT,
    ]


def _run_refresh(mngr_ctx: MngrContext, plugin_config: UsagePluginConfig) -> None:
    """Spawn `claude -p` to nudge a rate_limit_event into the cache.

    Pipes claude's stdout through claude_rate_limits_writer.sh so the merge logic
    stays in one place. The writer is located via $MNGR_RATE_LIMITS_WRITER if set,
    else falls back to the most-recently-modified copy under any per-agent state dir.
    """
    cmd = _build_refresh_command(plugin_config)
    logger.info("Refreshing Claude rate-limit cache (cost ~$0.005)")
    try:
        result = mngr_ctx.concurrency_group.run_process_to_completion(
            cmd,
            timeout=_REFRESH_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    except FileNotFoundError:
        logger.warning("`claude` binary not found on PATH; cannot refresh rate-limit cache")
        return
    if result.returncode != 0:
        logger.warning("claude refresh probe exited {}; stderr: {}", result.returncode, result.stderr.strip())
        return
    _ingest_refresh_stdout(result.stdout, mngr_ctx)


def _ingest_refresh_stdout(stdout: str, mngr_ctx: MngrContext) -> None:
    """Parse rate_limit_event lines out of a `claude -p --output-format=stream-json --verbose` stream.

    Each line is a JSON object; we look for ``type == "rate_limit_event"`` records and
    fold them into the cache with last-write-wins semantics, matching the SDK-event
    half of claude_rate_limits_writer.sh.
    """
    path = cache_path(mngr_ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = _load_cache(path) or CacheDoc()
    windows = dict(cache.windows)
    now = int(time.time())
    updated = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info") or {}
        if not isinstance(info, dict):
            continue
        window_key = _normalize_window_key(info.get("rateLimitType"))
        if window_key is None:
            continue
        existing = windows.get(window_key, WindowSnapshot())
        windows[window_key] = WindowSnapshot(
            used_percentage=existing.used_percentage,
            resets_at=_coerce_optional_int(info.get("resetsAt")),
            status=info.get("status"),
            is_using_overage=info.get("isUsingOverage"),
            source="sdk",
            updated_at=now,
        )
        updated = True
    if updated:
        _atomic_write_cache(path, CacheDoc(schema_version=CACHE_SCHEMA_VERSION, windows=windows))


@pure
def _normalize_window_key(raw: Any) -> str | None:
    """Map an SDK rateLimitType value to one of WINDOW_KEYS, or None."""
    if not isinstance(raw, str):
        return None
    lowered = raw.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "5h": "five_hour",
        "five_hour": "five_hour",
        "fivehour": "five_hour",
        "7d": "seven_day",
        "seven_day": "seven_day",
        "sevenday": "seven_day",
        "overage": "overage",
    }
    return aliases.get(lowered)


@pure
def _coerce_optional_int(value: Any) -> int | None:
    """Best-effort cast to int; tolerate string timestamps."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _atomic_write_cache(path: Path, cache: CacheDoc) -> None:
    """Write the cache atomically (temp + rename) to avoid partial reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = cache.model_dump(mode="json")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)


@pure
def _format_duration(seconds: int) -> str:
    """Render seconds as a compact human duration: '1h 12m', '4d 3h', '45s'."""
    if seconds <= 0:
        return "now"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


@pure
def _format_human_line(window_label: str, window: WindowSnapshot, now: int) -> str:
    """Render one window's status as a single human-readable line."""
    parts = [f"{window_label}:"]
    if window.used_percentage is not None:
        parts.append(f"{window.used_percentage:.0f}% used,")
    elif window.status is not None:
        parts.append(f"status={window.status},")
    else:
        parts.append("no data,")
    if window.resets_at is not None:
        seconds_until = max(0, window.resets_at - now)
        parts.append(f"resets in {_format_duration(seconds_until)}")
    else:
        parts.append("reset time unknown")
    return " ".join(parts)


@pure
def _window_to_template_values(window: WindowSnapshot, now: int) -> dict[str, str]:
    """Render a window's fields into string values keyed for format-template substitution."""
    seconds_until: str
    if window.resets_at is not None:
        seconds_until = str(max(0, window.resets_at - now))
    else:
        seconds_until = ""
    used_percentage = "" if window.used_percentage is None else f"{window.used_percentage:.2f}"
    return {
        "used_percentage": used_percentage,
        "resets_at": "" if window.resets_at is None else str(window.resets_at),
        "status": window.status or "",
        "is_using_overage": "" if window.is_using_overage is None else str(window.is_using_overage).lower(),
        "source": window.source or "",
        "updated_at": "" if window.updated_at is None else str(window.updated_at),
        "seconds_until_reset": seconds_until,
        "is_present": "true" if window.updated_at is not None else "false",
    }


class _UsageRenderModel(FrozenModel):
    """Top-level view used both for JSON output and template rendering.

    Each window is the canonical WindowSnapshot plus two computed fields packed
    into the JSON dump: seconds_until_reset and is_present.
    """

    schema_version: int
    now: int
    is_stale: bool
    windows: dict[str, WindowSnapshot]


def _window_render_dict(snap: WindowSnapshot, now: int) -> dict[str, Any]:
    """Window's snapshot fields plus computed seconds_until_reset / is_present."""
    seconds_until_reset = None if snap.resets_at is None else max(0, snap.resets_at - now)
    return {
        **snap.model_dump(),
        "seconds_until_reset": seconds_until_reset,
        "is_present": snap.updated_at is not None,
    }


def _build_render_model(cache: CacheDoc | None, max_age: int, now: int) -> _UsageRenderModel:
    """Assemble the renderable view for a cache snapshot."""
    windows: dict[str, WindowSnapshot] = {}
    if cache is not None:
        for key in WINDOW_KEYS:
            windows[key] = cache.windows.get(key, WindowSnapshot())
    else:
        for key in WINDOW_KEYS:
            windows[key] = WindowSnapshot()

    oldest = _oldest_updated_at(cache)
    is_stale = oldest is None or (now - oldest) > max_age

    return _UsageRenderModel(
        schema_version=cache.schema_version if cache is not None else CACHE_SCHEMA_VERSION,
        now=now,
        is_stale=is_stale,
        windows=windows,
    )


def _render_model_for_json(model: _UsageRenderModel, now: int) -> dict[str, Any]:
    """Convert the render model to a JSON-friendly dict (no Path/datetime types)."""
    return {
        "schema_version": model.schema_version,
        "now": now,
        "is_stale": model.is_stale,
        **{key: _window_render_dict(model.windows[key], now) for key in WINDOW_KEYS},
    }


def _flatten_for_template(model: _UsageRenderModel, now: int) -> dict[str, str]:
    """Flatten the render model into dot-keyed string fields for format-template substitution."""
    flat: dict[str, str] = {
        "schema_version": str(model.schema_version),
        "now": str(now),
        "is_stale": str(model.is_stale).lower(),
    }
    for key in WINDOW_KEYS:
        for sub_key, value in _window_to_template_values(model.windows[key], now).items():
            flat[f"{key}.{sub_key}"] = value
    return flat


def _emit_output(
    model: _UsageRenderModel,
    output_format: OutputFormat,
    format_template: str | None,
    now: int,
) -> None:
    """Write output in the requested format."""
    if format_template is not None:
        line = render_format_template(format_template, _flatten_for_template(model, now))
        write_human_line(line)
        return
    match output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_final_json(_render_model_for_json(model, now))
        case OutputFormat.HUMAN:
            any_present = False
            for key, label in (("five_hour", "5h"), ("seven_day", "7d"), ("overage", "overage")):
                snap = model.windows[key]
                if snap.updated_at is None:
                    continue
                write_human_line(_format_human_line(label, snap, now))
                any_present = True
            if not any_present:
                write_human_line(
                    "No rate-limit data yet -- re-run with --refresh or wait for an agent statusline tick."
                )
            if model.is_stale and any_present:
                logger.warning("Rate-limit cache is stale; values may not reflect latest API state.")


@click.command("usage")
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Force a refresh probe even if the cache is fresh. Spawns `claude -p` (~$0.005).",
)
@click.option(
    "--max-age",
    default=None,
    help="Override the freshness threshold (e.g. '300', '5m', '2h'). Default: from plugin config.",
)
@add_common_options
@click.pass_context
def usage(ctx: click.Context, **kwargs: Any) -> None:
    """Show Claude Code rolling-window quota usage (5h, 7d, overage).

    Reads from a shared cache populated by per-agent statusline shims; refreshes
    via a brief `claude -p` call when the cache is stale.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="usage",
        command_class=UsageCliOptions,
        is_format_template_supported=True,
    )
    plugin_config = mngr_ctx.get_plugin_config("usage", UsagePluginConfig)

    max_age_override = _parse_max_age(opts.max_age)
    effective_max_age = max_age_override if max_age_override is not None else plugin_config.max_age_seconds

    path = cache_path(mngr_ctx)
    cache = _load_cache(path)
    now = int(time.time())

    if _should_refresh(cache, plugin_config, opts.refresh, max_age_override, now):
        _run_refresh(mngr_ctx, plugin_config)
        cache = _load_cache(path)
        now = int(time.time())

    model = _build_render_model(cache, effective_max_age, now)
    _emit_output(model, output_opts.output_format, output_opts.format_template, now)


CommandHelpMetadata(
    key="usage",
    one_line_description="Show Claude Code rolling-window quota usage (5h, 7d, overage)",
    synopsis="mngr usage [OPTIONS]",
    description="""Reports Claude Code's rolling 5-hour, 7-day, and overage quota windows.

The data is sourced from response headers on every Claude Code API call and
captured into a shared cache by per-agent statusline shims. When the cache is
stale, `mngr usage` (by default) spawns a brief `claude -p` call to refresh it
(approx $0.005 per refresh).""",
    examples=(
        ("Show current usage", "mngr usage"),
        ("Force a refresh", "mngr usage --refresh"),
        ("Treat the cache as stale after 60s", "mngr usage --max-age 60"),
        ("Machine-readable output", "mngr usage --format json"),
        ("Custom format template", "mngr usage --format '{five_hour.used_percentage}/{seven_day.used_percentage}'"),
    ),
).register()

add_pager_help_option(usage)
