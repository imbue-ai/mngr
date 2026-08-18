import subprocess
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.cli.server import _bake_one_slice_with_retry
from imbue.mngr_imbue_cloud.cli.server import _box_ssh_host_key_options
from imbue.mngr_imbue_cloud.cli.server import _compose_prep_script
from imbue.mngr_imbue_cloud.cli.server import _destroy_one_pool_host
from imbue.mngr_imbue_cloud.cli.server import _format_capacity_table
from imbue.mngr_imbue_cloud.cli.server import _kill_bake_worker_processes
from imbue.mngr_imbue_cloud.cli.server import _resolve_vendored_mngr_source
from imbue.mngr_imbue_cloud.cli.server import _run_bake_attempts
from imbue.mngr_imbue_cloud.cli.server import assert_box_is_exclusive_to_tier
from imbue.mngr_imbue_cloud.cli.server import build_box_tier_audit_report
from imbue.mngr_imbue_cloud.cli.server import build_pool_host_destroy_report
from imbue.mngr_imbue_cloud.cli.server import build_registered_server
from imbue.mngr_imbue_cloud.cli.server import compute_server_slice_sizing
from imbue.mngr_imbue_cloud.cli.server import destroy_pool_hosts_in_parallel
from imbue.mngr_imbue_cloud.cli.server import run_outcome_workers_in_bounded_threads
from imbue.mngr_imbue_cloud.cli.server import server
from imbue.mngr_imbue_cloud.cli.server import slice_advertised_attributes
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BoxTierAudit
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyOutcome
from imbue.mngr_imbue_cloud.data_types import SliceBakeOutcome
from imbue.mngr_imbue_cloud.data_types import UnauditedBox
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import PoolHostDestroyOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.primitives import SliceBakeOutcomeStatus
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_lima_disk_name


def _server(
    slot_count: int,
    cpu_threads: int,
    *,
    memory_per_slice_gb: int = 8,
    cpu_overcommit_ratio: float = 1.5,
    disk_gb: int = 477,
) -> BareMetalServer:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.140.221",
        cpu_threads=cpu_threads,
        ram_gb=slot_count * memory_per_slice_gb,
        disk_gb=disk_gb,
        memory_per_slice_gb=memory_per_slice_gb,
        cpu_overcommit_ratio=cpu_overcommit_ratio,
        slot_count=slot_count,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
    )


def test_build_registered_server_derives_slot_count_from_memory_per_slice() -> None:
    built = build_registered_server(
        ovh_service_name="ns1.ovh.us",
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="1.2.3.4",
        ram_gb=64,
        cpu_cores=8,
        cpu_threads=16,
        disk_gb=477,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        raid_level="RAID1",
        lima_service_user="limahost",
        ovh_order_id="8144904",
        status=SERVER_STATUS_READY,
    )
    # 64GB box, 8GB slices: (64-8)*1024 // (8*1024 + 512) = 6 slots after host reserve.
    assert built.slot_count == 6
    assert built.disk_gb == 477
    assert built.ovh_service_name == "ns1.ovh.us"
    assert str(built.status) == "ready"


def test_compute_server_slice_sizing_uses_server_inputs_and_specs() -> None:
    sizing = compute_server_slice_sizing(_server(slot_count=8, cpu_threads=16))
    # 16 threads * 1.5 / 8 slots = 3 vCPU per slice.
    assert sizing["vcpus"] == 3
    assert sizing["advertised_memory_gb"] == 8
    # Guest gets the full advertised RAM (per-VM overhead is accounted in slot_count).
    assert sizing["memory_mib"] == 8 * 1024
    # Per-slice disk budget = (477 - max(20, ceil(477*0.10))=48 reserve) // 8, minus boot.
    assert sizing["disk_gib"] == (477 - 48) // 8 - SLICE_BOOT_DISK_GIB
    assert slice_advertised_attributes(sizing) == {"memory_gb": 8, "cpus": 3}


def test_format_capacity_table_shows_per_server_and_fleet_totals() -> None:
    capacities = [
        compute_capacity(_server(slot_count=8, cpu_threads=16), used_slots=3),
        compute_capacity(_server(slot_count=16, cpu_threads=32), used_slots=1),
    ]
    table = _format_capacity_table(capacities)
    assert "3/8" in table
    assert "1/16" in table
    # Fleet line: 24 total slots, 4 used, 20 free.
    assert "4/24 slots used, 20 free" in table


def test_box_ssh_host_key_options_pins_recorded_key() -> None:
    """With a recorded box host key, box SSH strictly pins it (no trust-on-first-use)."""
    with _box_ssh_host_key_options("203.0.113.7", "ssh-ed25519 AAAAtestboxkey") as opts:
        assert "StrictHostKeyChecking=yes" in opts
        assert any(o.startswith("UserKnownHostsFile=") for o in opts)
    # The accept-new TOFU fallback is gone entirely.
    assert "accept-new" not in " ".join(opts)


def test_box_ssh_host_key_options_fails_closed_without_a_key() -> None:
    """No recorded box host key -> refuse to SSH rather than trust-on-first-use."""
    with pytest.raises(BareMetalProvisioningError, match="strict host-key"):
        with _box_ssh_host_key_options("203.0.113.7", "") as _opts:
            pass


def test_compose_prep_script_without_an_extra_script_is_the_base_script() -> None:
    assert _compose_prep_script("echo base\n", None) == "echo base\n"


def test_compose_prep_script_runs_the_extra_script_after_the_standard_prep_steps(tmp_path: Path) -> None:
    # The extra script (e.g. the observability collector install) must run in the
    # same sudo bash session, strictly after the base prep, on its own line (so
    # its first command is never glued onto the base script's last line).
    extra_path = tmp_path / "install_collector.sh"
    extra_path.write_text("echo extra")
    composed = _compose_prep_script("echo base", extra_path)
    assert composed == "echo base\necho extra"


def test_server_group_help_lists_commands() -> None:
    result = CliRunner().invoke(server, ["--help"])
    assert result.exit_code == 0
    # The server group holds only the fleet-lifecycle verbs; slice baking moved to
    # ``admin pool create``.
    for command in ("prep", "list", "register", "set-status"):
        assert command in result.output
    assert "allocate-slice" not in result.output


def test_order_command_exposes_dry_run_flag() -> None:
    # `order --dry-run` is the no-charge price/spec preview the deployment playbook
    # relies on; guard that the flag stays on the CLI surface with its no-charge contract.
    result = CliRunner().invoke(server, ["order", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "No charge" in result.output or "no charge" in result.output


def test_kill_bake_worker_processes_terminates_a_child() -> None:
    # On a top-level kill the bake's in-flight `mngr create` workers must be reaped
    # so they don't keep carving VMs; this is the helper that does it. Spawn a child
    # and confirm it is killed (the helper kills all children of this process).
    child = subprocess.Popen(["sleep", "39517"])
    try:
        assert child.poll() is None
        _kill_bake_worker_processes(grace_seconds=5.0)
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_from_tag_bake_keeps_the_tags_vendored_mngr() -> None:
    """A --from-tag bake (no explicit --mngr-source) must NOT vendor the local checkout.

    Regression test: --from-tag means byte-for-byte tag content, including the mngr
    vendored at the tag. Returning the local repo_root here would silently bake the
    operator's working-tree mngr over the tag's, producing a same-version content
    skew (the bug that broke chat-agent creation on a minds-vX slice).
    """
    resolved = _resolve_vendored_mngr_source(mngr_source=None, repo_root=Path("/monorepo"), is_from_tag=True)
    assert resolved is None


def test_workspace_dir_bake_vendors_the_local_checkout() -> None:
    """A --workspace-dir (dev) bake with no explicit --mngr-source vendors repo_root."""
    resolved = _resolve_vendored_mngr_source(mngr_source=None, repo_root=Path("/monorepo"), is_from_tag=False)
    assert resolved == Path("/monorepo")


def test_explicit_mngr_source_always_wins() -> None:
    """An explicit --mngr-source overrides the vendored mngr for either bake source."""
    for is_from_tag in (True, False):
        resolved = _resolve_vendored_mngr_source(
            mngr_source="/some/other/mngr", repo_root=Path("/monorepo"), is_from_tag=is_from_tag
        )
        assert resolved == Path("/some/other/mngr")


def test_destroy_report_counts_already_gone_as_destroyed() -> None:
    """Re-running the same id list after a partial failure must converge to success.

    Ids whose rows are already gone report 'already_gone' and count as destroyed;
    only genuine teardown failures make the report (and thus the command) fail.
    """
    report = build_pool_host_destroy_report(
        [
            PoolHostDestroyOutcome(pool_host_id="a", status=PoolHostDestroyOutcomeStatus.DESTROYED),
            PoolHostDestroyOutcome(pool_host_id="b", status=PoolHostDestroyOutcomeStatus.ALREADY_GONE),
            PoolHostDestroyOutcome(pool_host_id="c", status=PoolHostDestroyOutcomeStatus.SKIPPED_LEASED),
            PoolHostDestroyOutcome(
                pool_host_id="d", status=PoolHostDestroyOutcomeStatus.FAILED, detail="box unreachable"
            ),
        ]
    )
    assert report.requested == 4
    assert report.destroyed == 2
    assert report.skipped == 1
    assert report.failed == 1
    assert [host.pool_host_id for host in report.hosts] == ["a", "b", "c", "d"]
    # The wire form the CLI emits keeps the documented lowercase statuses and omits
    # absent details.
    dumped = report.model_dump(mode="json", exclude_none=True)
    assert dumped["hosts"][0] == {"pool_host_id": "a", "status": "destroyed"}
    assert dumped["hosts"][3]["status"] == "failed"


def test_destroy_report_for_no_hosts_is_all_zero() -> None:
    report = build_pool_host_destroy_report([])
    assert report.model_dump(mode="json", exclude_none=True) == {
        "requested": 0,
        "destroyed": 0,
        "skipped": 0,
        "failed": 0,
        "hosts": [],
    }


def test_destroy_pool_hosts_in_parallel_rejects_nonpositive_concurrency() -> None:
    with pytest.raises(click.UsageError):
        destroy_pool_hosts_in_parallel(
            pool_host_ids=["row-1"],
            database_url="postgres://example",
            eligible_statuses=("available",),
            is_row_drop_only=False,
            max_concurrency=0,
        )


def test_destroy_worker_turns_db_errors_into_failed_outcomes() -> None:
    """A DB failure in one worker must become a per-host 'failed' outcome, not a raise.

    ObservableThread.join() re-raises worker exceptions, so an uncaught psycopg2 error
    in one thread would abort the whole batch mid-join, skip the outcome report, and
    tear down the shared temp key dir under the sibling threads.
    """
    outcome = _destroy_one_pool_host(
        pool_host_id="11111111-1111-1111-1111-111111111111",
        # Port 1 on localhost refuses connections immediately -- a fast, deterministic
        # psycopg2.OperationalError without any real DB.
        database_url="postgresql://user@127.0.0.1:1/db",
        private_key_path=None,
        eligible_statuses=("available",),
        is_row_drop_only=True,
    )
    assert outcome.status == PoolHostDestroyOutcomeStatus.FAILED
    assert outcome.pool_host_id == "11111111-1111-1111-1111-111111111111"


def test_bounded_fan_out_caps_concurrency_and_collects_all_outcomes() -> None:
    """The shared fan-out runs at most max_concurrency workers at once and returns every outcome.

    The Barrier(2) forces each admitted pair to overlap (so the cap is actually
    exercised, not just scheduled around), and the counter asserts the semaphore
    never admits more than the cap.
    """
    barrier = threading.Barrier(2)
    concurrency_lock = threading.Lock()
    concurrency_state = {"current": 0, "max": 0}

    def worker(item: int) -> dict[str, Any]:
        with concurrency_lock:
            concurrency_state["current"] += 1
            concurrency_state["max"] = max(concurrency_state["max"], concurrency_state["current"])
        barrier.wait(timeout=30)
        with concurrency_lock:
            concurrency_state["current"] -= 1
        return {"item": item, "status": "done"}

    outcomes = run_outcome_workers_in_bounded_threads(
        worker=worker,
        worker_kwargs_list=[dict(item=idx) for idx in range(4)],
        max_concurrency=2,
        thread_name_prefix="fanout-test",
        progress_noun="Fan-out test",
        describe_outcome=lambda outcome: str(outcome["item"]),
        interruption_exception_types=(),
        on_join_interrupted=None,
    )
    assert sorted(outcome["item"] for outcome in outcomes) == [0, 1, 2, 3]
    assert concurrency_state["max"] == 2


def _bake_outcome(status: SliceBakeOutcomeStatus, host_name: str) -> SliceBakeOutcome:
    # error is documented "failed only" on SliceBakeOutcome, so stamp it only there.
    error = "boom" if status == SliceBakeOutcomeStatus.FAILED else None
    return SliceBakeOutcome(host_name=host_name, server_id="server-1", status=status, error=error)


def test_run_bake_attempts_returns_the_first_success_without_retrying() -> None:
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        return _bake_outcome(SliceBakeOutcomeStatus.SUCCEEDED, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=3, termination_event=threading.Event())
    assert outcome.status == SliceBakeOutcomeStatus.SUCCEEDED
    assert call_count["count"] == 1


def test_run_bake_attempts_retries_a_transient_failure_with_a_fresh_slice() -> None:
    # A failed bake destroys its VM and writes no row, so the retry is a clean fresh
    # slice: two transient failures followed by a success must yield the success.
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        status = SliceBakeOutcomeStatus.SUCCEEDED if call_count["count"] == 3 else SliceBakeOutcomeStatus.FAILED
        return _bake_outcome(status, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=3, termination_event=threading.Event())
    assert outcome.status == SliceBakeOutcomeStatus.SUCCEEDED
    assert outcome.host_name == "slice-3"
    assert call_count["count"] == 3


def test_run_bake_attempts_returns_the_last_failure_after_exhausting_attempts() -> None:
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        return _bake_outcome(SliceBakeOutcomeStatus.FAILED, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=2, termination_event=threading.Event())
    assert outcome.status == SliceBakeOutcomeStatus.FAILED
    assert outcome.host_name == "slice-2"
    assert call_count["count"] == 2


def test_run_bake_attempts_does_not_retry_after_the_bake_is_terminated() -> None:
    # A termination signal's kill sweep makes every in-flight attempt fail; retrying
    # those would spawn replacement bakes (new VMs) after the operator killed the
    # bake, so a set termination event must return the failure without retrying.
    termination_event = threading.Event()
    termination_event.set()
    call_count = {"count": 0}

    def bake_once() -> SliceBakeOutcome:
        call_count["count"] += 1
        return _bake_outcome(SliceBakeOutcomeStatus.FAILED, f"slice-{call_count['count']}")

    outcome = _run_bake_attempts(bake_once, attempt_count=3, termination_event=termination_event)
    assert outcome.status == SliceBakeOutcomeStatus.FAILED
    assert call_count["count"] == 1


def test_bake_worker_does_not_start_a_first_attempt_after_termination() -> None:
    # A worker still queued on the concurrency semaphore when the bake is terminated
    # must not start its first `mngr create` (a brand-new VM carve after the kill
    # sweep); it reports the slice as failed instead. The worker kwargs deliberately
    # omit everything _bake_one_slice requires, so any accidental bake attempt
    # fails loudly.
    termination_event = threading.Event()
    termination_event.set()
    outcome = _bake_one_slice_with_retry(termination_event=termination_event, server=_server(4, 16))
    assert outcome.status == SliceBakeOutcomeStatus.FAILED
    assert outcome.host_name == "slice-never-started"
    assert outcome.error is not None and "terminated before" in outcome.error


def test_assert_box_is_exclusive_to_tier_accepts_a_single_key_and_same_tier_slices() -> None:
    mine = slice_lima_disk_name(HostId.generate(), "staging")
    assert_box_is_exclusive_to_tier(
        server=_server(slot_count=6, cpu_threads=16),
        env_name="staging",
        box_disk_names={mine},
        authorized_key_count=1,
    )


def test_assert_box_is_exclusive_to_tier_rejects_an_extra_authorized_key() -> None:
    # prep writes authorized_keys with a single-key overwrite, so a second key can
    # only have been added out of band -- it hands another tier SSH into this box.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name="staging",
            box_disk_names=set(),
            authorized_key_count=2,
        )
    assert "authorizes 2 SSH keys" in str(exc_info.value)
    assert "added out of band" in str(exc_info.value)
    # Re-prepping overwrites authorized_keys, so it must not be advised before the
    # operator has checked whether the other key's owner has slices running here.
    assert "Do NOT re-prep before checking" in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_rejects_an_empty_authorized_keys_as_never_prepped() -> None:
    # An empty authorized_keys is the opposite failure from an extra key: nothing was
    # added out of band, prep simply never ran, and re-prepping is the safe remedy.
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name="staging",
            box_disk_names=set(),
            authorized_key_count=0,
        )
    assert "authorizes 0 SSH keys" in str(exc_info.value)
    assert "never prepped" in str(exc_info.value)
    assert "added out of band" not in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_rejects_a_foreign_tier_slice() -> None:
    theirs = slice_lima_disk_name(HostId.generate(), "dev-xiaq")
    with pytest.raises(click.UsageError) as exc_info:
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name="staging",
            box_disk_names={theirs},
            authorized_key_count=1,
        )
    assert theirs in str(exc_info.value)
    assert "another tier" in str(exc_info.value)


def test_assert_box_is_exclusive_to_tier_allows_sibling_dev_envs_on_one_box() -> None:
    assert_box_is_exclusive_to_tier(
        server=_server(slot_count=6, cpu_threads=16),
        env_name="dev-josh",
        box_disk_names={
            slice_lima_disk_name(HostId.generate(), "dev-josh"),
            slice_lima_disk_name(HostId.generate(), "dev-alice"),
        },
        authorized_key_count=1,
    )


def test_assert_box_is_exclusive_to_tier_still_checks_keys_for_an_unstamped_bake() -> None:
    # env_name None means a legacy un-stamped bake: the slice tier is unknowable so
    # that half is skipped, but the key check does not depend on our tier.
    assert_box_is_exclusive_to_tier(
        server=_server(slot_count=6, cpu_threads=16),
        env_name=None,
        box_disk_names={slice_lima_disk_name(HostId.generate(), "dev-xiaq")},
        authorized_key_count=1,
    )
    with pytest.raises(click.UsageError):
        assert_box_is_exclusive_to_tier(
            server=_server(slot_count=6, cpu_threads=16),
            env_name=None,
            box_disk_names=set(),
            authorized_key_count=3,
        )


def test_build_box_tier_audit_report_counts_each_verdict_separately() -> None:
    # An unaudited box is NOT a clean one: it must never be folded into `exclusive`.
    clean = BoxTierAudit(
        server_id="a",
        public_address="203.0.113.1",
        slot_count=6,
        box_used_slots=1,
        authorized_key_count=1,
        foreign_tier_slices=(),
        degraded_md_arrays=(),
        raw_swap_devices=(),
    )
    contaminated = BoxTierAudit(
        server_id="b",
        public_address="203.0.113.2",
        slot_count=6,
        box_used_slots=2,
        authorized_key_count=2,
        foreign_tier_slices=(),
        degraded_md_arrays=(),
        raw_swap_devices=(),
    )
    report = build_box_tier_audit_report(
        env_name="staging",
        audits=[clean, contaminated],
        unaudited=[UnauditedBox(server_id="c", public_address=None, reason="the row has no public_address")],
    )
    assert (report.exclusive, report.contaminated, report.unaudited) == (1, 1, 1)
    assert report.env_name == "staging"
    assert report.is_foreign_tier_checked


def test_build_box_tier_audit_report_marks_the_foreign_tier_half_as_unchecked_without_an_env() -> None:
    # Without an env there is no tier to compare against, so an empty foreign-slice
    # list must not be readable as a clean bill of health.
    report = build_box_tier_audit_report(env_name=None, audits=[], unaudited=[])
    assert not report.is_foreign_tier_checked
