from click.testing import CliRunner
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.cli.backups import backup_repo
from imbue.mngr_imbue_cloud.cli.backups import credentials_to_json
from imbue.mngr_imbue_cloud.cli.buckets import bucket
from imbue.mngr_imbue_cloud.wire_types import BackupRepositoryCredentials


def test_backup_repo_group_lists_subcommands() -> None:
    result = CliRunner().invoke(backup_repo, ["--help"])
    assert result.exit_code == 0
    for name in ("create", "list", "roll-token", "destroy", "usage"):
        assert name in result.output


def test_backup_repo_destroy_documents_yes_flag() -> None:
    result = CliRunner().invoke(backup_repo, ["destroy", "--help"])
    assert result.exit_code == 0
    assert "-y" in result.output
    assert "ACTIVE" in result.output


def test_bucket_group_keeps_freeze_for_the_migration() -> None:
    result = CliRunner().invoke(bucket, ["--help"])
    assert result.exit_code == 0
    assert "freeze" in result.output


def test_credentials_to_json_reveals_the_token_once() -> None:
    credentials = BackupRepositoryCredentials(
        workspace_id="agent-abc",
        repository_url="rest:https://backups.example.com/user1/agent-abc/",
        rest_username="agent-abc",
        rest_password=SecretStr("tok-secret"),
    )
    assert credentials_to_json(credentials) == {
        "workspace_id": "agent-abc",
        "repository_url": "rest:https://backups.example.com/user1/agent-abc/",
        "rest_username": "agent-abc",
        "rest_password": "tok-secret",
    }
