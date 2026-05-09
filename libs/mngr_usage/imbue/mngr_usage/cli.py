from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import assert_never

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
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_usage.data_types import UsagePluginConfig
from imbue.mngr_usage.data_types import UsageSnapshot
from imbue.mngr_usage.data_types import WINDOW_KEYS
from imbue.mngr_usage.data_types import WindowSnapshot

# Discovery convention: each agent's state dir holds rate-limits events at
#   <agent_state_dir>/events/<source>/rate_limits/events.jsonl
# This mirrors the common_transcript pattern (events/<source>/common_transcript/
# events.jsonl) used by `mngr transcript`. The <source> segment names the
# writer (a free-form identifier chosen by the writer plugin) and becomes the
# UsageSnapshot.source_name for the event.
_RATE_LIMITS_EVENTS_LEAF: tuple[str, str] = ("rate_limits", "events.jsonl")

# Standard window labels for the human-format renderer. Providers may
# return windows with other names; those render with the literal key.
_DEFAULT_WINDOW_LABELS: dict[str, str] = {
    "five_hour": "5h",
    "seven_day": "7d",
    "overage": "overage",
}

_NO_DATA_HINT = (
    "No usage data yet -- check that a usage writer plugin is installed in the env "
    "running `mngr`, and that you've sent a prompt to an agent that (a) was created "
    "after that plugin was installed and (b) is still alive."
)


class UsageCliOptions(CommonCliOptions):
    """Options for the `mngr usage` command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    max_age: str | None


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


# =============================================================================
# Discovery + parsing
# =============================================================================


def _iter_rate_limit_event_files(host_dir: Path) -> Iterable[tuple[Path, str]]:
    """Yield ``(events_file, source_name)`` pairs across all agents on this host.

    Pattern: ``<host_dir>/agents/agent-*/events/<source>/rate_limits/events.jsonl``
    -- the same shape ``mngr transcript`` uses for ``events/<source>/common_transcript/...``.
    The ``<source>`` segment is what we use as the snapshot source name.
    Missing path components yield nothing rather than raising.

    ``host_dir`` is ``expanduser()``'d defensively because mngr's pydantic
    default for ``default_host_dir`` is the literal unexpanded ``Path("~/.mngr")``
    when neither ``MNGR_HOST_DIR`` env var nor a config file overrides it
    (see ``libs/mngr/imbue/mngr/config/loader.py``). Without the expansion,
    a clean shell with no ``MNGR_HOST_DIR`` would walk a non-existent
    ``~/.mngr/agents`` and silently report no usage data.
    """
    agents_dir = host_dir.expanduser() / "agents"
    if not agents_dir.is_dir():
        return
    for agent_state_dir in agents_dir.iterdir():
        if not agent_state_dir.is_dir():
            continue
        events_dir = agent_state_dir / "events"
        if not events_dir.is_dir():
            continue
        for source_dir in events_dir.iterdir():
            if not source_dir.is_dir():
                continue
            candidate = source_dir / _RATE_LIMITS_EVENTS_LEAF[0] / _RATE_LIMITS_EVENTS_LEAF[1]
            if candidate.is_file():
                yield candidate, source_dir.name


def _read_last_event(events_file: Path) -> dict[str, Any] | None:
    """Read the last well-formed JSON object from a JSONL events file.

    Walks lines from the end; tolerates a truncated trailing line by skipping
    it and trying the previous one. Returns None if no valid line exists.
    """
    try:
        text = events_file.read_text()
    except OSError as e:
        logger.debug("Could not read {}: {}", events_file, e)
        return None
    for line in reversed([raw for raw in text.splitlines() if raw.strip()]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            # A truncated trailing line (writer mid-flight, no newline yet) is the
            # most common case; skip it and try the previous one. We log at warning
            # level for visibility because corrupt earlier lines indicate something
            # worse and the user should know about it; expected truncation will
            # resolve on the next render.
            logger.warning("Skipping malformed event line in {}: {}", events_file, e)
            continue
        if isinstance(event, dict):
            return event
    return None


@pure
def _parse_iso_timestamp(value: Any) -> int | None:
    """Convert an ISO 8601 ``timestamp`` field to a Unix timestamp.

    Returns None on any parse failure. The writer emits a fixed-width
    nanosecond-precision form (``%Y-%m-%dT%H:%M:%S.000000000Z``) but we
    accept any form ``datetime.fromisoformat`` handles.
    """
    if not isinstance(value, str):
        return None
    normalized = value.rstrip("Z") + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return int(dt.timestamp())


@pure
def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@pure
def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@pure
def _coerce_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@pure
def _coerce_optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _windows_from_event(event: dict[str, Any]) -> dict[str, WindowSnapshot]:
    """Reshape an event's ``rate_limits`` payload into UsageSnapshot windows.

    The on-disk event shape (matching what the writer emits) is:

        {"source": "<agent_type>/rate_limits", "type": "rate_limit_snapshot",
         "event_id": ..., "timestamp": ...,
         "rate_limits": {"five_hour": {"used_percentage": 11, "resets_at": ...},
                         "seven_day": {...}, "overage": {...}}}

    Unknown window names are passed through; missing fields are coerced to None.
    """
    rate_limits = event.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return {}
    windows: dict[str, WindowSnapshot] = {}
    for window_key, window_value in rate_limits.items():
        if not isinstance(window_value, dict):
            continue
        windows[str(window_key)] = WindowSnapshot(
            used_percentage=_coerce_optional_float(window_value.get("used_percentage")),
            resets_at=_coerce_optional_int(window_value.get("resets_at")),
            status=_coerce_optional_str(window_value.get("status")),
            is_using_overage=_coerce_optional_bool(window_value.get("is_using_overage")),
        )
    return windows


def _snapshot_from_event(event: dict[str, Any], source_name: str) -> UsageSnapshot | None:
    """Reshape one events.jsonl line into a UsageSnapshot, or None if unusable."""
    timestamp = _parse_iso_timestamp(event.get("timestamp"))
    if timestamp is None:
        return None
    windows = _windows_from_event(event)
    if not windows:
        return None
    return UsageSnapshot(source_name=source_name, windows=windows, updated_at=timestamp)


def _gather_snapshots(host_dir: Path) -> list[UsageSnapshot]:
    """Walk events files across all agents, return one UsageSnapshot per source.

    Multiple agents may share the same source name -- the writer plugin
    chooses the segment, so all agents that share a writer write to the
    same ``events/<source>/rate_limits/events.jsonl`` under their own state
    dirs. For each (events_file, source_name) we extract the last event;
    if several events files exist for the same source_name,
    ``_pick_freshest`` later picks across them on ``updated_at``.
    """
    snapshots: list[UsageSnapshot] = []
    for events_file, source_name in _iter_rate_limit_event_files(host_dir):
        event = _read_last_event(events_file)
        if event is None:
            continue
        snapshot = _snapshot_from_event(event, source_name)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


@pure
def _pick_freshest(snapshots: list[UsageSnapshot]) -> UsageSnapshot | None:
    """Return the snapshot with the largest updated_at, or None if empty.

    Ties are broken by source_name so the choice is deterministic in tests.
    """
    if not snapshots:
        return None
    return max(snapshots, key=lambda s: (s.updated_at, s.source_name))


# =============================================================================
# Rendering
# =============================================================================


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
def _format_reset_phrase(resets_at: int, now: int) -> str:
    """Render the reset timestamp in past or future tense."""
    delta = resets_at - now
    if delta > 0:
        return f"resets in {_format_duration(delta)}"
    if delta < 0:
        return f"reset {_format_duration(-delta)} ago"
    return "just reset"


@pure
def _format_human_line(window_label: str, window: WindowSnapshot, now: int) -> str:
    """Render one window's status as a single human-readable line."""
    if window.used_percentage is None:
        return f"{window_label}: no data"
    parts = [f"{window_label}: {window.used_percentage:.0f}% used,"]
    if window.resets_at is not None:
        parts.append(_format_reset_phrase(window.resets_at, now))
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
        "seconds_until_reset": seconds_until,
        "is_present": "true" if window.used_percentage is not None or window.resets_at is not None else "false",
    }


class _UsageRenderModel(FrozenModel):
    """Top-level view used both for JSON output and template rendering."""

    source_name: str
    now: int
    is_stale: bool
    snapshot_updated_at: int | None
    windows: dict[str, WindowSnapshot]


def _build_render_model(snapshot: UsageSnapshot, max_age: int, now: int) -> _UsageRenderModel:
    """Assemble the renderable view for a snapshot.

    Stale if either:
    - snapshot updated_at is older than max_age (no fresh event in a while), OR
    - any populated window's resets_at is in the past (the limit refreshed;
      cached used_percentage is from the prior window).
    """
    age_stale = (now - snapshot.updated_at) > max_age
    reset_stale = any(snap.resets_at is not None and snap.resets_at < now for snap in snapshot.windows.values())
    return _UsageRenderModel(
        source_name=snapshot.source_name,
        now=now,
        is_stale=age_stale or reset_stale,
        snapshot_updated_at=snapshot.updated_at,
        windows=snapshot.windows,
    )


def _window_render_dict(snap: WindowSnapshot, now: int) -> dict[str, Any]:
    """Window's snapshot fields plus computed seconds_until_reset / is_present."""
    seconds_until_reset = None if snap.resets_at is None else max(0, snap.resets_at - now)
    return {
        **snap.model_dump(),
        "seconds_until_reset": seconds_until_reset,
        "is_present": snap.used_percentage is not None or snap.resets_at is not None,
    }


def _render_one_source_for_json(model: _UsageRenderModel, now: int) -> dict[str, Any]:
    """JSON shape for a single source's snapshot."""
    out: dict[str, Any] = {
        "source": model.source_name,
        "updated_at": model.snapshot_updated_at,
        "is_stale": model.is_stale,
    }
    for key in WINDOW_KEYS:
        out[key] = _window_render_dict(model.windows.get(key, WindowSnapshot()), now)
    for key, snap in model.windows.items():
        if key not in WINDOW_KEYS:
            out[key] = _window_render_dict(snap, now)
    return out


def _flatten_primary_for_template(model: _UsageRenderModel, now: int) -> dict[str, str]:
    """Flatten the freshest source's render model for format-template substitution.

    The format-template surface is intentionally simple: it always reflects the
    primary (freshest) source's windows at top level. Multi-source consumers
    should use ``--format json``, which exposes every source separately.
    """
    flat: dict[str, str] = {
        "source": model.source_name,
        "now": str(now),
        "is_stale": str(model.is_stale).lower(),
        "updated_at": "" if model.snapshot_updated_at is None else str(model.snapshot_updated_at),
    }
    for key in WINDOW_KEYS:
        snap = model.windows.get(key, WindowSnapshot())
        for sub_key, value in _window_to_template_values(snap, now).items():
            flat[f"{key}.{sub_key}"] = value
    return flat


@pure
def _human_label_for(window_key: str) -> str:
    """Best-effort human label; unknown window keys render as the literal key."""
    return _DEFAULT_WINDOW_LABELS.get(window_key, window_key)


def _write_source_section(model: _UsageRenderModel, now: int, header: str) -> bool:
    """Render one source's window lines (always preceded by a ``[source]`` header).
    Returns True if any window with a percentage was rendered (drives the
    catch-all hint downstream).
    """
    write_human_line(header)
    any_with_percentage = False
    ordered_keys = [k for k in WINDOW_KEYS if k in model.windows] + [k for k in model.windows if k not in WINDOW_KEYS]
    for key in ordered_keys:
        snap = model.windows[key]
        if snap.used_percentage is None and snap.resets_at is None:
            continue
        write_human_line(_format_human_line(_human_label_for(key), snap, now))
        if snap.used_percentage is not None:
            any_with_percentage = True
    return any_with_percentage


def _emit_output(
    snapshots_with_models: list[tuple[UsageSnapshot, _UsageRenderModel]],
    output_format: OutputFormat,
    format_template: str | None,
    now: int,
) -> None:
    """Write output for zero or more sources, freshest-first.

    The no-data hint is emitted as a logger.warning rather than a stdout line,
    so it (a) lands on stderr and doesn't pollute machine-readable output,
    (b) matches the existing stale-cache warning's channel, and (c) carries
    actionable info regardless of which output format the caller chose.
    Two no-data cases trigger it: zero sources (no events files anywhere)
    and "every source's events lack used_percentage" (renders as percentage-
    less window lines, but still actionable for the user).
    """
    if format_template is not None:
        # Format templates always reference the primary (freshest) source's
        # windows at top level for ergonomics; multi-source consumers should
        # use --format json. With no sources at all, all fields are empty.
        if not snapshots_with_models:
            empty = _UsageRenderModel(
                source_name="",
                now=now,
                is_stale=True,
                snapshot_updated_at=None,
                windows={},
            )
            line = render_format_template(format_template, _flatten_primary_for_template(empty, now))
        else:
            _, primary_model = snapshots_with_models[0]
            line = render_format_template(format_template, _flatten_primary_for_template(primary_model, now))
        write_human_line(line)
        return

    if not snapshots_with_models:
        logger.warning(_NO_DATA_HINT)

    match output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            payload: dict[str, Any] = {
                "now": now,
                "sources": [_render_one_source_for_json(model, now) for _, model in snapshots_with_models],
            }
            emit_final_json(payload)
        case OutputFormat.HUMAN:
            if not snapshots_with_models:
                return
            any_with_percentage_anywhere = False
            stale_sources: list[tuple[str, int]] = []
            for index, (_, model) in enumerate(snapshots_with_models):
                if index > 0:
                    write_human_line("")
                section_had_percentage = _write_source_section(model, now, f"[{model.source_name}]")
                any_with_percentage_anywhere = any_with_percentage_anywhere or section_had_percentage
                if section_had_percentage and model.is_stale and model.snapshot_updated_at is not None:
                    stale_sources.append((model.source_name, max(0, now - model.snapshot_updated_at)))
            if not any_with_percentage_anywhere:
                logger.warning(_NO_DATA_HINT)
            for source_name, age_seconds in stale_sources:
                logger.warning(
                    "[{}] snapshot last updated {} ago",
                    source_name,
                    _format_duration(age_seconds),
                )
        case _ as unreachable:
            assert_never(unreachable)


@click.command("usage")
@click.option(
    "--max-age",
    default=None,
    help="Stale-warning threshold (e.g. '300', '5m', '2h'). Default: from plugin config.",
)
@add_common_options
@click.pass_context
def usage(ctx: click.Context, **kwargs: Any) -> None:
    """Show rolling-window usage / quota data captured by an agent's statusline.

    Walks ``<host_dir>/agents/*/events/<source>/rate_limits/events.jsonl``
    (matching the same convention ``mngr transcript`` uses for
    ``common_transcript``), parses the freshest event per source, picks the
    most recent across sources, and renders.
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

    snapshots = _gather_snapshots(mngr_ctx.config.default_host_dir)
    now = int(time.time())

    # Multi-source: build a render model per source, sort freshest-first.
    # Tiebreak by source_name so the order is stable in tests.
    snapshots_with_models = sorted(
        ((s, _build_render_model(s, effective_max_age, now)) for s in snapshots),
        key=lambda sm: (sm[0].updated_at, sm[0].source_name),
        reverse=True,
    )
    _emit_output(snapshots_with_models, output_opts.output_format, output_opts.format_template, now)


CommandHelpMetadata(
    key="usage",
    one_line_description="Show rolling-window usage / quota data from agent statusline events",
    synopsis="mngr usage [OPTIONS]",
    description="""Reports rolling-window usage / quota data captured by an agent's
statusline.

This command is agent-agnostic: it walks ``<host_dir>/agents/*/events/<source>/
rate_limits/events.jsonl`` and renders the most recent event. The pattern
mirrors how ``mngr transcript`` discovers ``common_transcript`` events --
writer plugins emit events to the conventional path; ``mngr usage`` discovers
them automatically without any agent-specific knowledge.""",
    examples=(
        ("Show current usage", "mngr usage"),
        ("Treat the snapshot as stale after 60s (warning only)", "mngr usage --max-age 60"),
        ("Machine-readable output", "mngr usage --format json"),
        ("Custom format template", "mngr usage --format '{five_hour.used_percentage}/{seven_day.used_percentage}'"),
    ),
).register()

add_pager_help_option(usage)
