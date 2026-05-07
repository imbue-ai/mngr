"""Unit tests for mngr_usage.cli."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.config.consts import ROOT_CONFIG_FILENAME
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_usage.cli import _build_render_model
from imbue.mngr_usage.cli import _flatten_for_template
from imbue.mngr_usage.cli import _format_duration
from imbue.mngr_usage.cli import _load_cache
from imbue.mngr_usage.cli import _oldest_updated_at
from imbue.mngr_usage.cli import _parse_max_age
from imbue.mngr_usage.cli import cache_path
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import CacheDoc
from imbue.mngr_usage.data_types import WindowSnapshot


def _write_cache(path: Path, cache: CacheDoc) -> None:
    """Test helper: write a CacheDoc to disk in the canonical on-disk shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache.model_dump(mode="json")))


def test_parse_max_age_accepts_units() -> None:
    assert _parse_max_age("300") == 300
    assert _parse_max_age("60s") == 60
    assert _parse_max_age("5m") == 300
    assert _parse_max_age("2h") == 7200
    assert _parse_max_age("1d") == 86400
    assert _parse_max_age(None) is None
    assert _parse_max_age("") is None


def test_parse_max_age_rejects_bad_input() -> None:
    with pytest.raises(click.UsageError):
        _parse_max_age("forever")


def test_format_duration_hits_each_branch() -> None:
    assert _format_duration(0) == "now"
    assert _format_duration(-1) == "now"
    assert _format_duration(45) == "45s"
    assert _format_duration(60) == "1m"
    assert _format_duration(125) == "2m 5s"
    assert _format_duration(3600) == "1h"
    assert _format_duration(7325) == "2h 2m"
    assert _format_duration(86400) == "1d"
    assert _format_duration(360000) == "4d 4h"


def test_oldest_updated_at_picks_min() -> None:
    cache = CacheDoc(
        windows={
            "five_hour": WindowSnapshot(updated_at=100),
            "seven_day": WindowSnapshot(updated_at=200),
            "overage": WindowSnapshot(),
        }
    )
    assert _oldest_updated_at(cache) == 100
    assert _oldest_updated_at(None) is None
    assert _oldest_updated_at(CacheDoc()) is None


def test_load_cache_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert _load_cache(tmp_path / "missing.json") is None


def test_load_cache_returns_none_for_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.json"
    p.write_text("not json")
    assert _load_cache(p) is None


def test_load_cache_returns_none_for_non_utf8_bytes(tmp_path: Path) -> None:
    """A cache file with non-UTF-8 bytes should be treated as corrupt, not crash."""
    p = tmp_path / "binary.json"
    p.write_bytes(b"\xff\xfe\x00\x01\x02\x03")
    assert _load_cache(p) is None


def test_load_cache_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "cache.json"
    cache = CacheDoc(
        windows={
            "five_hour": WindowSnapshot(used_percentage=73.4, resets_at=1777673400, source="statusline", updated_at=1),
            "seven_day": WindowSnapshot(used_percentage=41.0, resets_at=1778000000, source="statusline", updated_at=2),
        }
    )
    _write_cache(p, cache)
    loaded = _load_cache(p)
    assert loaded is not None
    assert loaded.windows["five_hour"].used_percentage == 73.4
    assert loaded.windows["seven_day"].resets_at == 1778000000


def test_load_cache_drops_invalid_entries(tmp_path: Path) -> None:
    p = tmp_path / "mixed.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "windows": {
                    "five_hour": {"used_percentage": 50.0, "updated_at": 100},
                    "seven_day": "not a dict",
                    "bogus": {"used_percentage": 99},
                },
            }
        )
    )
    loaded = _load_cache(p)
    assert loaded is not None
    assert "five_hour" in loaded.windows
    assert "seven_day" not in loaded.windows
    assert "bogus" in loaded.windows


def test_render_model_marks_empty_cache_as_stale() -> None:
    model = _build_render_model(None, max_age=300, now=1000)
    assert model.is_stale is True
    assert all(model.windows[k].updated_at is None for k in ("five_hour", "seven_day", "overage"))


def test_render_model_computes_seconds_until_reset() -> None:
    cache = CacheDoc(windows={"five_hour": WindowSnapshot(used_percentage=42.0, resets_at=1500, updated_at=900)})
    model = _build_render_model(cache, max_age=300, now=1000)
    flat = _flatten_for_template(model, now=1000)
    assert flat["five_hour.used_percentage"] == "42.00"
    assert flat["five_hour.resets_at"] == "1500"
    assert flat["five_hour.seconds_until_reset"] == "500"
    assert flat["five_hour.is_present"] == "true"
    assert flat["seven_day.is_present"] == "false"


@pytest.fixture
def cli_profile_dir(temp_host_dir: Path, temp_profile_dir: Path) -> Path:
    """Pin the CLI's auto-resolved profile_dir to match temp_profile_dir.

    The CLI's load_config calls get_or_create_profile_dir(host_dir), which reads
    host_dir/config.toml's `profile = "<id>"` to pick which profile to use. Without
    this fixture the CLI would create a fresh profile each time, so writes via
    temp_mngr_ctx.profile_dir would not be visible to the CLI invocation.
    """
    config_path = temp_host_dir / ROOT_CONFIG_FILENAME
    config_path.write_text(f'profile = "{temp_profile_dir.name}"\n')
    return temp_profile_dir


def test_usage_command_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    cli_profile_dir: Path,
) -> None:
    cache = CacheDoc(
        windows={
            "five_hour": WindowSnapshot(
                used_percentage=73.4, resets_at=999_999_999_999, source="statusline", updated_at=999_999_999_999
            ),
        }
    )
    _write_cache(cache_path(temp_mngr_ctx), cache)

    result = cli_runner.invoke(usage, ["--max-age", "300"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "5h:" in result.output
    assert "73% used" in result.output


def test_usage_command_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    cli_profile_dir: Path,
) -> None:
    cache = CacheDoc(
        windows={
            "five_hour": WindowSnapshot(
                used_percentage=12.3, resets_at=999_999_999_999, source="statusline", updated_at=999_999_999_999
            ),
        }
    )
    _write_cache(cache_path(temp_mngr_ctx), cache)

    result = cli_runner.invoke(
        usage, ["--format", "json", "--max-age", "300"], obj=plugin_manager, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["schema_version"] == 1
    assert payload["five_hour"]["used_percentage"] == 12.3
    assert payload["five_hour"]["is_present"] is True
    assert payload["seven_day"]["is_present"] is False


def test_usage_command_format_template(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    cli_profile_dir: Path,
) -> None:
    cache = CacheDoc(
        windows={
            "five_hour": WindowSnapshot(
                used_percentage=88.0, resets_at=999_999_999_999, source="statusline", updated_at=999_999_999_999
            ),
            "seven_day": WindowSnapshot(
                used_percentage=44.0, resets_at=999_999_999_999, source="statusline", updated_at=999_999_999_999
            ),
        }
    )
    _write_cache(cache_path(temp_mngr_ctx), cache)

    result = cli_runner.invoke(
        usage,
        ["--format", "5h:{five_hour.used_percentage}/7d:{seven_day.used_percentage}", "--max-age", "300"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "5h:88.00/7d:44.00" in result.output


def test_usage_command_no_data_message(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    cli_profile_dir: Path,
) -> None:
    """Empty cache prints the install/wire-up hint."""
    result = cli_runner.invoke(usage, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "No rate-limit data yet" in result.output
    assert "imbue-mngr-usage" in result.output
    assert "mngr create" in result.output
