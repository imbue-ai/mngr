from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.buckets import bucket

# These are deliberate *wiring* smoke tests: they assert only that the click
# groups register subcommands of the expected names, not that those subcommands
# behave correctly (the per-command behavior needs the connector and is covered
# by client_test.py). Their job is to catch an accidentally-unregistered or
# renamed subcommand.


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
