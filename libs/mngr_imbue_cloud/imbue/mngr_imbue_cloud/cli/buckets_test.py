from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.buckets import bucket


def test_bucket_group_lists_subcommands() -> None:
    result = CliRunner().invoke(bucket, ["--help"])
    assert result.exit_code == 0
    for name in ("create", "list", "info", "destroy", "keys"):
        assert name in result.output


def test_bucket_keys_group_lists_subcommands() -> None:
    """Single-key model: only listing remains under `bucket keys` (rolling is `bucket roll-key`)."""
    result = CliRunner().invoke(bucket, ["keys", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    for removed in ("create", "destroy"):
        assert removed not in result.output


def test_bucket_group_includes_roll_key() -> None:
    result = CliRunner().invoke(bucket, ["--help"])
    assert result.exit_code == 0
    assert "roll-key" in result.output
