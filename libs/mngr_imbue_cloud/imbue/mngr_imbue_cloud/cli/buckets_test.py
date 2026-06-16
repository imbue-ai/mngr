from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.buckets import bucket


def test_bucket_group_lists_subcommands() -> None:
    result = CliRunner().invoke(bucket, ["--help"])
    assert result.exit_code == 0
    for name in ("create", "list", "info", "destroy", "keys"):
        assert name in result.output


def test_bucket_keys_group_lists_subcommands() -> None:
    result = CliRunner().invoke(bucket, ["keys", "--help"])
    assert result.exit_code == 0
    for name in ("create", "list", "destroy"):
        assert name in result.output
