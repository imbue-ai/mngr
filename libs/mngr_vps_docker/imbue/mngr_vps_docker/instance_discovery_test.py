"""Unit tests for the shared discovery flow on ``VpsDockerProvider``.

The methods exercised here -- ``_read_records_from_vps``,
``_discover_host_records_with_agents``, and ``_find_host_record`` -- are
provider-agnostic: every concrete subclass (AWS, Vultr, OVH) inherits
them. The tests target real regressions, not coverage padding:

- if the per-VPS cache-fallback in ``_read_records_from_vps`` breaks,
  hosts on a transiently-unreachable VPS silently vanish from
  ``mngr list`` instead of surfacing as offline;
- if ``_discover_host_records_with_agents`` stops aggregating
  per-VPS agent data by ``host_id``, a host that has agents on the
  same VPS shows fewer agents than it actually has;
- if ``_find_host_record`` stops short-circuiting on a cache hit,
  every name/id lookup pays for a full discovery sweep (every CLI
  invocation gets much slower);
- if it stops short-circuiting when credentials are missing, users
  without credentials get a confusing provider error instead of
  "host not found".

To keep the SSH layer out of the tests we override the two well-defined
extension hooks (``_list_provider_vps_hostnames``, ``_credentials_configured``)
plus the SSH boundary (``_make_outer_for_vps_ip``, ``_read_records_from_vps``);
the actual fan-out / aggregation / caching code under test runs unmodified.
"""

from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import cast

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps_docker.config import VpsDockerProviderConfig
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.host_store import VpsHostConfig
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.primitives import VpsInstanceId
from imbue.mngr_vps_docker.primitives import VpsInstanceStatus
from imbue.mngr_vps_docker.vps_client import VpsClientInterface


class _NoopVpsClient(VpsClientInterface):
    """Concrete VpsClientInterface that fails fast if any method is called.

    The discovery code under test never reaches the VPS client; if a future
    change starts to, the test fails loudly rather than silently passing.
    """

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        raise AssertionError("VpsClient.create_instance must not be called from discovery tests")

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        raise AssertionError("VpsClient.destroy_instance must not be called from discovery tests")

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        raise AssertionError("VpsClient.get_instance_status must not be called from discovery tests")

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        raise AssertionError("VpsClient.get_instance_ip must not be called from discovery tests")

    def wait_for_instance_active(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> str:
        raise AssertionError("VpsClient.wait_for_instance_active must not be called from discovery tests")

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise AssertionError("VpsClient.upload_ssh_key must not be called from discovery tests")

    def delete_ssh_key(self, key_id: str) -> None:
        raise AssertionError("VpsClient.delete_ssh_key must not be called from discovery tests")


class _DiscoveryTestProvider(VpsDockerProvider):
    """Concrete VpsDockerProvider used only to exercise base-class discovery.

    Subclasses configure the discovery hooks via plain instance attributes,
    set after construction. The two extension hooks plus the SSH boundary
    are overridden here; everything else (fan-out, aggregation, caching,
    name/id lookup) is the real base-class implementation.
    """

    hostnames: list[str] = Field(default_factory=list)
    credentials_present: bool = True
    per_vps_records: dict[str, tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]] = Field(
        default_factory=dict
    )
    per_vps_outer_errors: dict[str, Exception] = Field(default_factory=dict)
    state_container_ready: dict[str, bool] = Field(default_factory=dict)
    # For the discover_hosts_and_agents container-inspect probe: whether the
    # container on each vps_ip is running (drives the State.Running inspect).
    container_running_by_ip: dict[str, bool] = Field(default_factory=dict)
    # vps_ips whose outer is unreachable at container-inspect time, even though a
    # record exists for them (records came from a cache, the VPS is now down).
    inspect_unreachable_ips: set[str] = Field(default_factory=set)
    _list_hostnames_calls: int = PrivateAttr(default=0)

    def _list_provider_vps_hostnames(self) -> list[str]:
        self._list_hostnames_calls += 1
        return list(self.hostnames)

    def _credentials_configured(self) -> bool:
        return self.credentials_present

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        # Discovery tests never exercise the create path that calls this; the
        # body is just enough to satisfy the abstract-method contract.
        return ParsedVpsBuildOptions(region="", plan="", docker_build_args=tuple(build_args or ()))

    def _read_records_from_vps(
        self,
        vps_ip: str,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        # When a test wants to drive the *real* _read_records_from_vps logic
        # (cache-fallback or state-container-not-ready paths), it sets the
        # vps_ip in per_vps_outer_errors or state_container_ready, and we
        # route through the superclass method (which will in turn use our
        # overridden _make_outer_for_vps_ip below).
        # Otherwise short-circuit with the canned per-VPS payload.
        if vps_ip in self.per_vps_outer_errors or vps_ip in self.state_container_ready:
            return super()._read_records_from_vps(vps_ip)
        return self.per_vps_records.get(vps_ip, ([], {}))

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        # Used by tests that opt into the real _read_records_from_vps body:
        # per_vps_outer_errors[ip] -> raise that exception (cache-fallback test);
        # state_container_ready[ip]=False -> yield a dummy outer that reports no
        # mngr container (state-container-not-ready test).
        # Tests that don't opt in never reach here -- _read_records_from_vps
        # short-circuits with canned payloads above.
        # For discover_hosts_and_agents' container-inspect probe:
        # inspect_unreachable_ips -> raise (VPS down at inspect time);
        # container_running_by_ip -> yield a stub answering the State.Running probe.
        if vps_ip in self.inspect_unreachable_ips:
            raise MngrError(f"VPS {vps_ip} unreachable at container inspect")
        if vps_ip in self.container_running_by_ip:
            yield cast(OuterHostInterface, _InspectOuter(self.container_running_by_ip[vps_ip]))
            return
        exc = self.per_vps_outer_errors.get(vps_ip)
        if exc is not None:
            raise exc
        if vps_ip in self.state_container_ready:
            if self.state_container_ready[vps_ip]:
                raise AssertionError(f"state_container_ready=True for {vps_ip!r} not supported by this stub")
            # _DummyOuter answers the single docker-ps probe issued by
            # _read_host_id_label_from_vps with an empty result; cast to satisfy
            # the OuterHostInterface yield type, matching the sibling vps_docker
            # tests (e.g. _outer_helpers_test, instance_test).
            yield cast(OuterHostInterface, _DummyOuter())
            return
        raise AssertionError(f"unexpected _make_outer_for_vps_ip call for {vps_ip!r}")


class _DummyOuter:
    """Minimal outer for the state-container-not-ready discovery path.

    main's ``_read_records_from_vps`` detects "no mngr container yet" via
    ``_read_host_id_label_from_vps``, which runs a single ``docker ps``-based
    command; empty stdout means no container. This stub answers that one
    probe with an empty, successful result so discovery returns empty rather
    than raising. Any other access is a regression and raises.
    """

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return CommandResult(stdout="", stderr="", success=True)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_DummyOuter.{name} must not be accessed in discovery tests")


class _InspectOuter:
    """Outer stub that answers only the ``docker inspect .State.Running`` probe.

    ``discover_hosts_and_agents`` calls ``docker_inspect_running`` (a single
    ``docker inspect --format '{{.State.Running}}'`` command) to decide whether a
    host's container is up. This stub returns the configured running state and
    raises on any other access, so an unexpected SSH command surfaces as a test
    failure rather than a silent pass.
    """

    def __init__(self, running: bool) -> None:
        self._running = running

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        if ".State.Running" in command:
            return CommandResult(stdout="true" if self._running else "false", stderr="", success=True)
        raise AssertionError(f"_InspectOuter received unexpected command: {command!r}")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_InspectOuter.{name} must not be accessed in discovery tests")


def _make_certified_data(host_id: HostId, host_name: str) -> CertifiedHostData:
    now = datetime.now(timezone.utc)
    return CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        idle_timeout_seconds=800,
        activity_sources=(),
        image="debian:bookworm-slim",
        user_tags={},
        created_at=now,
        updated_at=now,
    )


def _make_record(host_id: HostId, host_name: str, vps_ip: str) -> VpsDockerHostRecord:
    return VpsDockerHostRecord(
        certified_host_data=_make_certified_data(host_id, host_name),
        vps_ip=vps_ip,
    )


def _make_record_with_config(host_id: HostId, host_name: str, vps_ip: str) -> VpsDockerHostRecord:
    """A record with a populated config, so discover_hosts_and_agents runs the container-inspect probe.

    discover_hosts_and_agents only inspects the container when both ``vps_ip``
    and ``config`` are set; the plain ``_make_record`` leaves config None.
    """
    return VpsDockerHostRecord(
        certified_host_data=_make_certified_data(host_id, host_name),
        vps_ip=vps_ip,
        config=VpsHostConfig(
            vps_instance_id=VpsInstanceId(f"vps-{host_id}"),
            region="test-region",
            plan="test-plan",
            container_name=f"mngr-{host_name}",
            volume_name=f"mngr-vol-{host_name}",
        ),
    )


@pytest.fixture()
def provider(temp_mngr_ctx: MngrContext) -> _DiscoveryTestProvider:
    """A real _DiscoveryTestProvider rooted in a real MngrContext.

    Hooks default to "no VPSes, credentials present"; individual tests
    mutate the attributes to set up the scenario they want.
    """
    return _DiscoveryTestProvider(
        name=ProviderInstanceName("test-vps-docker"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsDockerProviderConfig(backend=ProviderBackendName("test-vps-docker")),
        vps_client=_NoopVpsClient(),
    )


# =========================================================================
# _read_records_from_vps -- cache fallback when SSH to a VPS fails
# =========================================================================


def test_read_records_from_vps_falls_back_to_cache_on_host_connection_error(
    provider: _DiscoveryTestProvider,
) -> None:
    """A VPS becomes temporarily unreachable; its hosts must remain in the listing."""
    host_a = HostId.generate()
    host_b = HostId.generate()
    cached_on_unreachable = _make_record(host_a, "host-A", vps_ip="10.0.0.1")
    cached_on_other_vps = _make_record(host_b, "host-B", vps_ip="10.0.0.2")
    provider._host_record_cache[host_a] = cached_on_unreachable
    provider._host_record_cache[host_b] = cached_on_other_vps
    provider.per_vps_outer_errors["10.0.0.1"] = HostConnectionError("connection refused")

    records, agent_data = provider._read_records_from_vps("10.0.0.1")

    assert records == [cached_on_unreachable]
    assert agent_data == {}


def test_read_records_from_vps_returns_empty_when_no_cache_and_ssh_fails(
    provider: _DiscoveryTestProvider,
) -> None:
    """If a VPS is unreachable and we have no cache, return empty -- not raise."""
    provider.per_vps_outer_errors["10.0.0.3"] = HostConnectionError("no route to host")

    records, agent_data = provider._read_records_from_vps("10.0.0.3")

    assert records == []
    assert agent_data == {}


def test_read_records_from_vps_falls_back_on_mngr_error(provider: _DiscoveryTestProvider) -> None:
    """``MngrError`` from the SSH path must trigger the same cache-fallback as a connection error."""
    host_c = HostId.generate()
    cached = _make_record(host_c, "host-C", vps_ip="10.0.0.4")
    provider._host_record_cache[host_c] = cached
    provider.per_vps_outer_errors["10.0.0.4"] = MngrError("docker inspect failed")

    records, _agent_data = provider._read_records_from_vps("10.0.0.4")

    assert records == [cached]


def test_read_records_from_vps_returns_empty_when_state_container_not_ready(
    provider: _DiscoveryTestProvider,
) -> None:
    """Concurrent ``mngr create`` may list a VPS before its state container is up; that's normal -- return empty.

    Distinct from the SSH-failure path: the outer connection succeeds, the
    container is just absent. A regression here that raises instead of
    returning empty would break listings during cold-start windows.
    """
    provider.state_container_ready["10.0.0.6"] = False

    records, agent_data = provider._read_records_from_vps("10.0.0.6")

    assert records == []
    assert agent_data == {}


# =========================================================================
# _discover_host_records_with_agents -- fan-out + aggregation
# =========================================================================


def test_discover_host_records_returns_empty_without_calling_ssh_when_no_vpses(
    provider: _DiscoveryTestProvider,
) -> None:
    """No VPSes from the provider listing -> no SSH attempts, empty result.

    Any unexpected call into ``_make_outer_for_vps_ip`` would raise from the
    override's final ``AssertionError``, so an SSH attempt here would surface
    as a test failure rather than a silent empty result.
    """
    records, agent_data = provider._discover_host_records_with_agents()

    assert records == []
    assert agent_data == {}
    # Confirm the no-vpses path was actually exercised (i.e. the listing
    # hook ran and reported zero hostnames).
    assert provider._list_hostnames_calls == 1


def test_discover_host_records_aggregates_records_across_multiple_vpses(
    provider: _DiscoveryTestProvider,
) -> None:
    """Records from every VPS must appear in the aggregated result."""
    host_a, host_b, host_c = HostId.generate(), HostId.generate(), HostId.generate()
    record_a = _make_record(host_a, "host-A", vps_ip="10.0.0.1")
    record_b = _make_record(host_b, "host-B", vps_ip="10.0.0.2")
    record_c = _make_record(host_c, "host-C", vps_ip="10.0.0.2")
    provider.hostnames = ["10.0.0.1", "10.0.0.2"]
    provider.per_vps_records = {
        "10.0.0.1": ([record_a], {}),
        "10.0.0.2": ([record_b, record_c], {}),
    }

    records, _agent_data = provider._discover_host_records_with_agents()

    assert {r.certified_host_data.host_id for r in records} == {str(host_a), str(host_b), str(host_c)}


def test_discover_host_records_merges_agent_data_by_host_id(
    provider: _DiscoveryTestProvider,
) -> None:
    """Agent data for the same host_id seen across VPSes must be concatenated, not overwritten."""
    host_id = HostId.generate()
    agents_on_first_vps = [{"agent_id": "a-1"}]
    agents_on_second_vps = [{"agent_id": "a-2"}, {"agent_id": "a-3"}]
    provider.hostnames = ["10.0.0.1", "10.0.0.2"]
    provider.per_vps_records = {
        "10.0.0.1": ([], {host_id: agents_on_first_vps}),
        "10.0.0.2": ([], {host_id: agents_on_second_vps}),
    }

    _records, agent_data = provider._discover_host_records_with_agents()

    assert sorted(a["agent_id"] for a in agent_data[host_id]) == ["a-1", "a-2", "a-3"]


# =========================================================================
# _find_host_record -- cache-first, credential short-circuit, cache population
# =========================================================================


def test_find_host_record_returns_cached_by_id_without_triggering_discovery(
    provider: _DiscoveryTestProvider,
) -> None:
    """Cache hit by HostId must NOT enumerate VPSes -- a regression here makes every lookup slow."""
    host_a = HostId.generate()
    cached = _make_record(host_a, "host-A", vps_ip="10.0.0.1")
    provider._host_record_cache[host_a] = cached

    found = provider._find_host_record(host_a)

    assert found is cached
    assert provider._list_hostnames_calls == 0


def test_find_host_record_returns_cached_by_name_without_triggering_discovery(
    provider: _DiscoveryTestProvider,
) -> None:
    """Cache hit by HostName has the same short-circuit guarantee."""
    host_a = HostId.generate()
    cached = _make_record(host_a, "host-A", vps_ip="10.0.0.1")
    provider._host_record_cache[host_a] = cached

    found = provider._find_host_record(HostName("host-A"))

    assert found is cached
    assert provider._list_hostnames_calls == 0


def test_find_host_record_returns_none_when_credentials_missing(
    provider: _DiscoveryTestProvider,
) -> None:
    """Missing credentials -> None (do not raise, do not call the listing API)."""
    provider.credentials_present = False

    assert provider._find_host_record(HostName("nonexistent")) is None
    assert provider._list_hostnames_calls == 0


def test_find_host_record_triggers_discovery_on_cache_miss_and_populates_cache(
    provider: _DiscoveryTestProvider,
) -> None:
    """Cache miss with credentials -> discovery runs, result returned, cache populated for next call."""
    host_fresh = HostId.generate()
    record = _make_record(host_fresh, "host-fresh", vps_ip="10.0.0.5")
    provider.hostnames = ["10.0.0.5"]
    provider.per_vps_records = {"10.0.0.5": ([record], {})}
    assert provider._host_record_cache == {}

    found = provider._find_host_record(HostName("host-fresh"))

    assert found is not None
    assert found.certified_host_data.host_id == str(host_fresh)
    # The cache must now contain it so the next lookup is free.
    assert host_fresh in provider._host_record_cache

    # Second call must be a cache hit: removing the listing source would break
    # the lookup if discovery were re-run.
    provider.hostnames = []
    provider.per_vps_records = {}
    assert provider._find_host_record(host_fresh) is record


def test_find_host_record_returns_none_when_discovery_finds_no_match(
    provider: _DiscoveryTestProvider,
) -> None:
    """Discovery runs, sees real records, none match -> None (not an error)."""
    host_other = HostId.generate()
    other_record = _make_record(host_other, "other-host", vps_ip="10.0.0.7")
    provider.hostnames = ["10.0.0.7"]
    provider.per_vps_records = {"10.0.0.7": ([other_record], {})}

    assert provider._find_host_record(HostName("does-not-exist")) is None
    # Discovery still warms the cache with what it did find, so a subsequent
    # lookup for the real host short-circuits.
    assert host_other in provider._host_record_cache


# =========================================================================
# discover_hosts_and_agents -- a cleanly-stopped container is STOPPED + visible,
# not CRASHED + hidden (the idle-watcher / `mngr stop` reconnection bug)
# =========================================================================


def test_discover_reports_stopped_and_keeps_visible_when_vps_reachable_but_container_down(
    provider: _DiscoveryTestProvider,
) -> None:
    """A reachable VPS whose container is stopped is STOPPED and visible to conn/start.

    This is the regression that made an idle-stopped (or `mngr stop`-ed) agent
    show CRASHED and vanish from ``mngr conn`` (which passes
    include_destroyed=False): the host was filtered out entirely. A reachable
    VPS with a stopped container is a clean stop, not a crash.
    """
    host_id = HostId.generate()
    record = _make_record_with_config(host_id, "host-stopped", vps_ip="10.0.0.10")
    provider.hostnames = ["10.0.0.10"]
    provider.per_vps_records = {"10.0.0.10": ([record], {})}
    provider.container_running_by_ip = {"10.0.0.10": False}

    result = provider.discover_hosts_and_agents(cg=provider.mngr_ctx.concurrency_group, include_destroyed=False)

    hosts = list(result.keys())
    assert len(hosts) == 1, "a reachable, cleanly-stopped host must remain visible to conn/start"
    assert hosts[0].host_id == host_id
    assert hosts[0].host_state == HostState.STOPPED


def test_discover_hides_unreachable_vps_host_when_not_including_destroyed(
    provider: _DiscoveryTestProvider,
) -> None:
    """An *unreachable* VPS host (the genuine down/crash case) stays hidden from conn.

    Distinct from the stopped-but-reachable case above: here the VPS itself is
    unreachable, so we cannot confirm a clean stop. With include_destroyed=False
    (the conn/start path) it is filtered out, preserving the prior behavior for
    genuinely-down hosts.
    """
    host_id = HostId.generate()
    record = _make_record_with_config(host_id, "host-down", vps_ip="10.0.0.11")
    provider.hostnames = ["10.0.0.11"]
    provider.per_vps_records = {"10.0.0.11": ([record], {})}
    provider.inspect_unreachable_ips = {"10.0.0.11"}

    result = provider.discover_hosts_and_agents(cg=provider.mngr_ctx.concurrency_group, include_destroyed=False)

    assert result == {}


def test_discover_reports_unreachable_vps_host_as_crashed_when_including_destroyed(
    provider: _DiscoveryTestProvider,
) -> None:
    """The same unreachable host surfaces as CRASHED in the full listing (include_destroyed=True).

    `mngr list` uses include_destroyed=True, so a down VPS with no recorded
    stop_reason and no snapshots derives to CRASHED -- the genuine failure
    signal, unchanged by the stopped-but-reachable fix.
    """
    host_id = HostId.generate()
    record = _make_record_with_config(host_id, "host-down", vps_ip="10.0.0.12")
    provider.hostnames = ["10.0.0.12"]
    provider.per_vps_records = {"10.0.0.12": ([record], {})}
    provider.inspect_unreachable_ips = {"10.0.0.12"}

    result = provider.discover_hosts_and_agents(cg=provider.mngr_ctx.concurrency_group, include_destroyed=True)

    hosts = list(result.keys())
    assert len(hosts) == 1
    assert hosts[0].host_id == host_id
    assert hosts[0].host_state == HostState.CRASHED
