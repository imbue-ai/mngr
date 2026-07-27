import pytest
from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.buckets import _destroy_emptying_on_refusal
from imbue.mngr_imbue_cloud.cli.buckets import bucket
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketError
from imbue.mngr_imbue_cloud.errors import ImbueCloudBucketNotEmptyError


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


def test_bucket_destroy_documents_force_and_yes_flags() -> None:
    result = CliRunner().invoke(bucket, ["destroy", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output
    assert "-y" in result.output
    assert "contents" in result.output


class _RecordingDestroyOps:
    """Records the interleaving of destroy attempts and emptying for the ordering helper."""

    def __init__(self, destroy_errors: list[Exception | None]) -> None:
        # One entry per expected destroy attempt: an exception to raise, or
        # None for success.
        self._destroy_errors = destroy_errors
        self.calls: list[str] = []

    def destroy(self) -> None:
        self.calls.append("destroy")
        error = self._destroy_errors.pop(0)
        if error is not None:
            raise error

    def empty(self) -> int:
        self.calls.append("empty")
        return 7


def test_destroy_emptying_on_refusal_never_empties_when_destroy_succeeds() -> None:
    ops = _RecordingDestroyOps([None])
    assert _destroy_emptying_on_refusal(ops.destroy, ops.empty, is_force=True) == 0
    assert ops.calls == ["destroy"]


def test_destroy_emptying_on_refusal_empties_then_retries_with_force() -> None:
    ops = _RecordingDestroyOps([ImbueCloudBucketNotEmptyError("not empty"), None])
    assert _destroy_emptying_on_refusal(ops.destroy, ops.empty, is_force=True) == 7
    # The destroy is attempted BEFORE any object is deleted, so server-side
    # refusals (e.g. the active-workspace interlock) preempt data loss.
    assert ops.calls == ["destroy", "empty", "destroy"]


def test_destroy_emptying_on_refusal_propagates_not_empty_without_force() -> None:
    ops = _RecordingDestroyOps([ImbueCloudBucketNotEmptyError("not empty")])
    with pytest.raises(ImbueCloudBucketNotEmptyError):
        _destroy_emptying_on_refusal(ops.destroy, ops.empty, is_force=False)
    assert ops.calls == ["destroy"]


def test_destroy_emptying_on_refusal_propagates_other_refusals_without_emptying() -> None:
    # The active-workspace interlock surfaces as a generic bucket error; it
    # must abort the whole destroy with the bucket contents untouched.
    ops = _RecordingDestroyOps([ImbueCloudBucketError("holds backups for an active workspace")])
    with pytest.raises(ImbueCloudBucketError):
        _destroy_emptying_on_refusal(ops.destroy, ops.empty, is_force=True)
    assert ops.calls == ["destroy"]
