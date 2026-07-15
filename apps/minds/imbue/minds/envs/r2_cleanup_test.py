from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from imbue.minds.envs.r2_cleanup import R2Bucket
from imbue.minds.envs.r2_cleanup import R2CleanupError
from imbue.minds.envs.r2_cleanup import bucket_owner_prefix_for_user
from imbue.minds.envs.r2_cleanup import find_sweepable_buckets

_NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
# A developer's user id, as SuperTokens reports it, and the bucket prefix the
# connector derives from it.
_DEV_USER_ID = "4caec486-a38b-46f0-9d3e-1b2c3d4e5f60"
_DEV_PREFIX = "4caec486a38b46"


def _bucket(name: str, *, age_hours: float = 48.0) -> R2Bucket:
    return R2Bucket(name=name, created_at=_NOW - timedelta(hours=age_hours))


def test_owner_prefix_matches_the_connector_derivation() -> None:
    # The connector strips hyphens and takes the first 16 characters.
    assert bucket_owner_prefix_for_user(_DEV_USER_ID) == "4caec486a38b46f0"


def test_a_live_users_buckets_are_never_swept() -> None:
    live_prefix = bucket_owner_prefix_for_user(_DEV_USER_ID)
    buckets = [_bucket(f"{live_prefix}--host-abc"), _bucket("deadbeefdeadbeef--host-def")]

    sweepable = find_sweepable_buckets(buckets, frozenset({live_prefix}), now=_NOW)

    assert [bucket.name for bucket in sweepable] == ["deadbeefdeadbeef--host-def"]


def test_infrastructure_buckets_are_never_swept() -> None:
    buckets = [_bucket("minds-lima-images-dev"), _bucket("minds-lima-images-dev-weishi")]

    assert find_sweepable_buckets(buckets, frozenset({_DEV_PREFIX}), now=_NOW) == ()


def test_young_buckets_are_left_for_the_run_that_may_still_own_them() -> None:
    # An in-flight run's account exists, but the app-list read can race the
    # bucket's creation; the age floor makes that race harmless.
    buckets = [_bucket("deadbeefdeadbeef--host-abc", age_hours=0.5)]

    assert find_sweepable_buckets(buckets, frozenset({_DEV_PREFIX}), now=_NOW) == ()


def test_an_empty_live_owner_set_aborts_rather_than_sweeping_everything() -> None:
    # A SuperTokens outage that reported no users must never be read as
    # "nobody owns these buckets" -- that would delete every developer's.
    with pytest.raises(R2CleanupError):
        find_sweepable_buckets([_bucket("deadbeefdeadbeef--host-abc")], frozenset(), now=_NOW)


def test_buckets_without_an_owner_prefix_are_left_alone() -> None:
    assert find_sweepable_buckets([_bucket("some-standalone-bucket")], frozenset({_DEV_PREFIX}), now=_NOW) == ()
