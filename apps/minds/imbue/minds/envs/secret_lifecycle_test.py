"""Unit tests for the timestamped Modal Secret naming + GC helpers."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.secret_lifecycle import DeployId
from imbue.minds.envs.secret_lifecycle import InvalidDeployIdError
from imbue.minds.envs.secret_lifecycle import gc_old_per_tier_secrets
from imbue.minds.envs.secret_lifecycle import make_deploy_id
from imbue.minds.envs.secret_lifecycle import parse_timestamped_secret_name
from imbue.minds.envs.secret_lifecycle import timestamped_secret_name


def test_deploy_id_round_trip() -> None:
    assert str(DeployId("20260517T143022Z")) == "20260517T143022Z"


def test_deploy_id_rejects_bad_format() -> None:
    with pytest.raises(InvalidDeployIdError):
        DeployId("not-a-deploy-id")
    with pytest.raises(InvalidDeployIdError):
        DeployId("2026-05-17T14:30:22Z")
    with pytest.raises(InvalidDeployIdError):
        DeployId("20260517T143022")


def test_make_deploy_id_uses_utc_compact_format() -> None:
    now = datetime(2026, 5, 17, 14, 30, 22, tzinfo=timezone.utc)
    assert make_deploy_id(now) == DeployId("20260517T143022Z")


def test_make_deploy_id_converts_non_utc_to_utc() -> None:
    """A timezone-aware non-UTC input is normalized to UTC before stamping."""
    tz = timezone(timedelta(hours=8))
    aware = datetime(2026, 5, 17, 22, 30, 22, tzinfo=tz)
    assert make_deploy_id(aware) == DeployId("20260517T143022Z")


def test_make_deploy_id_rejects_naive() -> None:
    with pytest.raises(InvalidDeployIdError):
        make_deploy_id(datetime(2026, 5, 17, 14, 30, 22))


def test_timestamped_secret_name_concatenates() -> None:
    assert timestamped_secret_name("litellm", "dev", DeployId("20260517T143022Z")) == "litellm-dev-20260517T143022Z"


def test_parse_timestamped_secret_name_round_trip() -> None:
    deploy_id = DeployId("20260517T143022Z")
    parsed = parse_timestamped_secret_name("litellm-dev-20260517T143022Z", tier="dev")
    assert parsed == ("litellm", deploy_id)


def test_parse_timestamped_secret_name_handles_service_with_hyphen() -> None:
    deploy_id = DeployId("20260517T143022Z")
    parsed = parse_timestamped_secret_name("litellm-connector-dev-20260517T143022Z", tier="dev")
    assert parsed == ("litellm-connector", deploy_id)


def test_parse_timestamped_secret_name_returns_none_for_wrong_tier() -> None:
    assert parse_timestamped_secret_name("cloudflare-staging-20260517T143022Z", tier="dev") is None


def test_parse_timestamped_secret_name_returns_none_for_unsuffixed_name() -> None:
    assert parse_timestamped_secret_name("cloudflare-dev", tier="dev") is None


def test_parse_timestamped_secret_name_returns_none_for_random_name() -> None:
    assert parse_timestamped_secret_name("MNGR_PLACEHOLDER", tier="dev") is None


def test_gc_keeps_last_n_per_service() -> None:
    """With 3 deploys' worth of cloudflare-dev-* secrets, GC keep_last=2 deletes 1."""
    cg = ConcurrencyGroup(name="gc-test")
    # Mix of three deploy ids' worth of dev secrets, a placeholder
    # (ignored by the parser), and a different tier's secret (ignored).
    secret_names = [
        "cloudflare-dev-20260517T143020Z",
        "cloudflare-dev-20260517T143021Z",
        "cloudflare-dev-20260517T143022Z",
        "litellm-dev-20260517T143020Z",
        "litellm-dev-20260517T143021Z",
        "litellm-dev-20260517T143022Z",
        "MNGR_PLACEHOLDER",
        "cloudflare-staging-20260517T143022Z",
    ]
    deleted: list[str] = []

    def fake_list(modal_env: str, parent_cg: ConcurrencyGroup) -> tuple[str, ...]:
        return tuple(secret_names)

    def fake_delete(secret_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
        deleted.append(secret_name)

    gc_old_per_tier_secrets(
        modal_env="dev-josh-1",
        tier="dev",
        list_modal_secrets_fn=fake_list,
        delete_modal_secret_fn=fake_delete,
        keep_last=2,
        parent_cg=cg,
    )
    # Per service, keep the 2 newest. Oldest (T143020Z) for each tier
    # service gets deleted.
    assert sorted(deleted) == [
        "cloudflare-dev-20260517T143020Z",
        "litellm-dev-20260517T143020Z",
    ]


def test_gc_with_keep_last_zero_deletes_all_matches() -> None:
    """``keep_last=0`` is the destroy-all path used by tier destroy."""
    cg = ConcurrencyGroup(name="gc-test-all")
    secret_names = [
        "cloudflare-dev-20260517T143020Z",
        "cloudflare-dev-20260517T143022Z",
        "MNGR_PLACEHOLDER",
        "cloudflare-staging-20260517T143022Z",
    ]
    deleted: list[str] = []

    def fake_list(modal_env: str, parent_cg: ConcurrencyGroup) -> tuple[str, ...]:
        return tuple(secret_names)

    def fake_delete(secret_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
        deleted.append(secret_name)

    gc_old_per_tier_secrets(
        modal_env="dev-josh-1",
        tier="dev",
        list_modal_secrets_fn=fake_list,
        delete_modal_secret_fn=fake_delete,
        keep_last=0,
        parent_cg=cg,
    )
    assert sorted(deleted) == [
        "cloudflare-dev-20260517T143020Z",
        "cloudflare-dev-20260517T143022Z",
    ]


def test_gc_continues_past_individual_delete_failures() -> None:
    """One failing delete is logged but doesn't abort the rest."""
    cg = ConcurrencyGroup(name="gc-test-partial")
    secret_names = [
        "cloudflare-dev-20260517T143020Z",
        "cloudflare-dev-20260517T143021Z",
        "cloudflare-dev-20260517T143022Z",
    ]
    deleted: list[str] = []

    def fake_list(modal_env: str, parent_cg: ConcurrencyGroup) -> tuple[str, ...]:
        return tuple(secret_names)

    def fake_delete(secret_name: str, modal_env: str, parent_cg: ConcurrencyGroup) -> None:
        if "T143020Z" in secret_name:
            raise ModalDeployError("simulated failure")
        deleted.append(secret_name)

    gc_old_per_tier_secrets(
        modal_env="dev-josh-1",
        tier="dev",
        list_modal_secrets_fn=fake_list,
        delete_modal_secret_fn=fake_delete,
        keep_last=0,
        parent_cg=cg,
    )
    # T143020Z attempt failed, but T143021Z and T143022Z still got deleted.
    assert sorted(deleted) == [
        "cloudflare-dev-20260517T143021Z",
        "cloudflare-dev-20260517T143022Z",
    ]
