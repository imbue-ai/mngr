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


class UsageCliOptions(CommonCliOptions):
    """Options for the `mngr usage` command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

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
    except OSError as e:
        logger.debug("Failed to read rate-limit cache at {}: {}", path, e)
        return None
    except json.JSONDecodeError as e:
        logger.warning("Rate-limit cache at {} is corrupt and will be ignored: {}", path, e)
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


_NO_DATA_HINT = (
    "No rate-limit data yet. The cache is populated by a per-agent statusline "
    "shim that fires whenever an interactive Claude session renders. To get "
    "data flowing: ensure imbue-mngr-usage is installed in whichever env runs "
    "your `mngr` (so the plugin entry point is loaded), then run "
    "`mngr create ... claude` and send the new agent any prompt. Existing "
    "agents whose settings.json was generated before this plugin was active "
    "won't have the shim until they're re-provisioned."
)


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
            any_with_percentage = False
            for key, label in (("five_hour", "5h"), ("seven_day", "7d"), ("overage", "overage")):
                snap = model.windows[key]
                if snap.updated_at is None:
                    continue
                write_human_line(_format_human_line(label, snap, now))
                any_present = True
                if snap.used_percentage is not None:
                    any_with_percentage = True
            has_usable_data = any_present and any_with_percentage
            if not has_usable_data:
                write_human_line(_NO_DATA_HINT)
            if has_usable_data and model.is_stale:
                logger.warning("Rate-limit cache is stale; values may not reflect latest API state.")


@click.command("usage")
@click.option(
    "--max-age",
    default=None,
    help="Stale-warning threshold (e.g. '300', '5m', '2h'). Default: from plugin config.",
)
@add_common_options
@click.pass_context
def usage(ctx: click.Context, **kwargs: Any) -> None:
    """Show Claude Code rolling-window quota usage (5h, 7d, overage).

    Reads from a shared cache populated by per-agent statusline shims. The
    shim ships rate-limit JSON to a small writer that atomically merges into
    the cache; `mngr usage` is purely a reader.
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

    model = _build_render_model(cache, effective_max_age, now)
    _emit_output(model, output_opts.output_format, output_opts.format_template, now)


CommandHelpMetadata(
    key="usage",
    one_line_description="Show Claude Code rolling-window quota usage (5h, 7d, overage)",
    synopsis="mngr usage [OPTIONS]",
    description="""Reports Claude Code's rolling 5-hour, 7-day, and overage quota windows.

The data is sourced from the JSON snapshot Claude Code feeds to its statusline
on every render; a small shim installed in each per-agent settings.json
captures it into a shared cache under your profile_dir. `mngr usage` is purely
a reader -- the cache is populated by interactive Claude sessions as a side
effect of normal use, with no API cost.""",
    examples=(
        ("Show current usage", "mngr usage"),
        ("Treat the cache as stale after 60s (warning only)", "mngr usage --max-age 60"),
        ("Machine-readable output", "mngr usage --format json"),
        ("Custom format template", "mngr usage --format '{five_hour.used_percentage}/{seven_day.used_percentage}'"),
    ),
).register()

add_pager_help_option(usage)
