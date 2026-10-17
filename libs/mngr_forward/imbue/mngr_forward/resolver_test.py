from pathlib import Path

import pytest

from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.service_map_cache import PersistedServiceMap
from imbue.mngr_forward.service_map_cache import ServiceMapCache
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_HOST_ID_1
from imbue.mngr_forward.testing import TEST_INSTANCE_1
from imbue.mngr_forward.testing import TEST_INSTANCE_1_ON_HOST_2
from imbue.mngr_forward.testing import TEST_INSTANCE_2
from imbue.mngr_forward.testing import TEST_INSTANCE_2_ON_HOST_1


@pytest.fixture
def ssh_info() -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user="root",
        host="example.modal.run",
        port=22,
        key_path=Path("/tmp/key"),
    )


def test_resolve_returns_none_for_unknown_agent() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    assert resolver.resolve(TEST_INSTANCE_1) is None


def test_resolve_service_strategy_returns_none_when_url_unknown() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    assert resolver.resolve(TEST_INSTANCE_1) is None


def test_resolve_service_strategy_returns_url_when_known() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9100"}, {})
    target = resolver.resolve(TEST_INSTANCE_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9100"
    assert target.ssh_info is None


def test_resolve_service_strategy_includes_ssh_info(ssh_info: RemoteSSHInfo) -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9100"}, {})
    resolver.update_ssh_info(TEST_INSTANCE_1, ssh_info)
    target = resolver.resolve(TEST_INSTANCE_1)
    assert target is not None
    assert target.ssh_info == ssh_info


def test_resolve_port_strategy_returns_fixed_url(ssh_info: RemoteSSHInfo) -> None:
    resolver = ForwardResolver(strategy=ForwardPortStrategy(remote_port=PositiveInt(8080)))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_ssh_info(TEST_INSTANCE_1, ssh_info)
    target = resolver.resolve(TEST_INSTANCE_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8080"
    assert target.ssh_info == ssh_info


def test_resolve_named_service_returns_its_url() -> None:
    """A service origin resolves to that service's registered URL, not the shell's."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(
        TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9100", "terminal": "http://127.0.0.1:7681"}, {}
    )
    target = resolver.resolve(TEST_INSTANCE_1, "terminal")
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:7681"


def test_resolve_named_service_returns_none_when_unregistered() -> None:
    """An unknown-but-plausible service on a known agent is unroutable (loading page)."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9100"}, {})
    assert resolver.resolve(TEST_INSTANCE_1, "nonexistent") is None


def test_resolve_by_origin_label_maps_label_back_to_service() -> None:
    """A ``<label>.host-<hex>`` origin resolves via the label -> name map to the service's URL."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(
        TEST_INSTANCE_1,
        {"system_interface": "http://127.0.0.1:9100", "terminal": "http://127.0.0.1:7681"},
        {"system_interface-shell111": "system_interface", "terminal-term1111": "terminal"},
    )
    target = resolver.resolve_by_origin_label(TEST_INSTANCE_1, "terminal-term1111")
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:7681"


def test_resolve_by_origin_label_falls_back_to_treating_label_as_name() -> None:
    """A label with no mapping (label-less/legacy service) routes under its own name."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"terminal": "http://127.0.0.1:7681"}, {})
    target = resolver.resolve_by_origin_label(TEST_INSTANCE_1, "terminal")
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:7681"


def test_shell_origin_label_returns_the_shell_services_label() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(
        TEST_INSTANCE_1,
        {},
        {"system_interface-shell111": "system_interface", "terminal-term1111": "terminal"},
    )
    assert resolver.shell_origin_label(TEST_INSTANCE_1) == "system_interface-shell111"


def test_shell_origin_label_is_none_before_labels_known_and_in_port_mode() -> None:
    service_resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    service_resolver.add_known_agent(TEST_INSTANCE_1)
    assert service_resolver.shell_origin_label(TEST_INSTANCE_1) is None

    port_resolver = ForwardResolver(strategy=ForwardPortStrategy(remote_port=PositiveInt(8080)))
    port_resolver.add_known_agent(TEST_INSTANCE_1)
    port_resolver.update_services(TEST_INSTANCE_1, {}, {"system_interface-shell111": "system_interface"})
    assert port_resolver.shell_origin_label(TEST_INSTANCE_1) is None


def test_is_shell_target_matches_bare_origin_and_shell_labels() -> None:
    """The bare origin and any label mapping (directly or via the identity
    fallback) to the shell service name are shell targets; other labels are not."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(
        TEST_INSTANCE_1,
        {},
        {"system_interface-shell111": "system_interface", "terminal-term1111": "terminal"},
    )
    assert resolver.is_shell_target(TEST_INSTANCE_1, None)
    assert resolver.is_shell_target(TEST_INSTANCE_1, "system_interface-shell111")
    # Identity fallback: an unmapped label is treated as the name itself.
    assert resolver.is_shell_target(TEST_INSTANCE_1, "system_interface")
    assert not resolver.is_shell_target(TEST_INSTANCE_1, "terminal-term1111")
    assert not resolver.is_shell_target(TEST_INSTANCE_1, "terminal")


def test_is_shell_target_is_false_in_port_mode() -> None:
    """Port-forward mode has no shell service, so nothing is a shell target."""
    resolver = ForwardResolver(strategy=ForwardPortStrategy(remote_port=PositiveInt(8080)))
    resolver.add_known_agent(TEST_INSTANCE_1)
    assert not resolver.is_shell_target(TEST_INSTANCE_1, None)
    assert not resolver.is_shell_target(TEST_INSTANCE_1, "terminal")


def test_resolve_named_service_works_in_port_strategy_mode() -> None:
    """Manual port mode still resolves named services from the registered map;
    only the bare origin maps to the fixed port."""
    resolver = ForwardResolver(strategy=ForwardPortStrategy(remote_port=PositiveInt(8080)))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"terminal": "http://127.0.0.1:7681"}, {})
    named = resolver.resolve(TEST_INSTANCE_1, "terminal")
    assert named is not None
    assert str(named.url).rstrip("/") == "http://127.0.0.1:7681"
    bare = resolver.resolve(TEST_INSTANCE_1)
    assert bare is not None
    assert str(bare.url).rstrip("/") == "http://127.0.0.1:8080"


def test_resolve_agent_for_host_maps_host_coordinate() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    assert resolver.resolve_agent_for_host(str(TEST_HOST_ID_1)) == TEST_INSTANCE_1
    assert resolver.resolve_agent_for_host("host-" + "f" * 32) is None


def test_resolve_agent_for_host_is_deterministic_with_multiple_agents() -> None:
    """Two known agents on one host resolve to the lexicographically-smallest instance key."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.add_known_agent(TEST_INSTANCE_2_ON_HOST_1)
    expected = min(str(TEST_INSTANCE_1), str(TEST_INSTANCE_2_ON_HOST_1))
    assert str(resolver.resolve_agent_for_host(str(TEST_HOST_ID_1))) == expected


def test_resolve_agent_for_host_ignores_unknown_agents() -> None:
    """A host mapping for an agent that is no longer known must not resolve."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.remove_known_agent(TEST_INSTANCE_1)
    assert resolver.resolve_agent_for_host(str(TEST_HOST_ID_1)) is None


def test_same_agent_id_on_two_hosts_resolves_independently() -> None:
    """The duplicate-id (migration overlap) case: each instance keeps its own services."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.add_known_agent(TEST_INSTANCE_1_ON_HOST_2)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9100"}, {})
    resolver.update_services(TEST_INSTANCE_1_ON_HOST_2, {"system_interface": "http://127.0.0.1:9200"}, {})

    target_host_1 = resolver.resolve(TEST_INSTANCE_1)
    target_host_2 = resolver.resolve(TEST_INSTANCE_1_ON_HOST_2)
    assert target_host_1 is not None and str(target_host_1.url).rstrip("/") == "http://127.0.0.1:9100"
    assert target_host_2 is not None and str(target_host_2.url).rstrip("/") == "http://127.0.0.1:9200"

    # Destroying the instance on one host leaves the other fully routable.
    resolver.remove_known_agent(TEST_INSTANCE_1)
    assert resolver.resolve(TEST_INSTANCE_1) is None
    survivor = resolver.resolve(TEST_INSTANCE_1_ON_HOST_2)
    assert survivor is not None and str(survivor.url).rstrip("/") == "http://127.0.0.1:9200"


def test_update_known_agents_drops_state_for_removed() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:1"}, {})
    resolver.update_known_agents((TEST_INSTANCE_2,))
    assert resolver.resolve(TEST_INSTANCE_1) is None
    assert resolver.resolve_agent_for_host(str(TEST_HOST_ID_1)) is None
    assert resolver.list_known_agent_instances() == (TEST_INSTANCE_2,)


def test_remove_known_agent_drops_services() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:1"}, {})
    resolver.remove_known_agent(TEST_INSTANCE_1)
    assert resolver.resolve(TEST_INSTANCE_1) is None


def test_update_known_agents_persists_a_bulk_drop_to_cache(tmp_path: Path) -> None:
    """A bulk drop must reach the cache, not just the in-memory map.

    ``update_known_agents`` is the one mutation point that drops several
    instances at once (a destruction sweep), and it persists a single snapshot
    for the batch rather than one per instance -- so a cache left out of that
    one call would seed the next run with agents that no longer exist.
    """
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.add_known_agent(TEST_INSTANCE_2)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9100"}, {})
    resolver.update_services(TEST_INSTANCE_2, {"system_interface": "http://127.0.0.1:9101"}, {})

    resolver.update_known_agents((TEST_INSTANCE_2,))

    assert cache.load() == PersistedServiceMap(
        services_by_instance={str(TEST_INSTANCE_2): {"system_interface": "http://127.0.0.1:9101"}},
        label_to_name_by_instance={},
    )


def test_update_known_agents_does_not_persist_when_it_dropped_no_services(tmp_path: Path) -> None:
    """A bulk update that drops no services entry must not touch the cache.

    ``update_known_agents`` runs on every full-discovery envelope, so persisting
    unconditionally would rewrite the whole cache file once per poll for a map
    that did not change. Asserted as "the file was never created", which a
    spurious persist cannot satisfy: ``persist`` writes through ``atomic_write``.
    """
    cache_path = tmp_path / "service_map.json"
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=ServiceMapCache(cache_path=cache_path),
    )

    # Known-agent metadata only, so neither call has a services entry to drop.
    resolver.update_known_agents((TEST_INSTANCE_1, TEST_INSTANCE_2))
    resolver.update_known_agents((TEST_INSTANCE_2,))

    assert not cache_path.exists()


def test_initial_discovery_flag() -> None:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    assert resolver.has_completed_initial_discovery() is False
    resolver.update_known_agents(())
    assert resolver.has_completed_initial_discovery() is True


# --- last-known service-map cache (fast first-load) -----------------------


def test_seeded_entry_not_served_until_agent_is_known() -> None:
    """A seeded service URL is not routable until discovery confirms the agent.

    This is the safety property that makes seeding a stale cache acceptable:
    ``resolve`` gates on this run's known-agent set, so a cache entry for an
    agent this run does not discover is never served.
    """
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.seed_services(
        PersistedServiceMap(
            services_by_instance={str(TEST_INSTANCE_1): {"system_interface": "http://127.0.0.1:8000"}},
            label_to_name_by_instance={},
        )
    )
    assert resolver.resolve(TEST_INSTANCE_1) is None
    resolver.add_known_agent(TEST_INSTANCE_1)
    target = resolver.resolve(TEST_INSTANCE_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8000"


def test_seed_services_drops_legacy_bare_agent_id_keys() -> None:
    """A cache entry keyed by a bare agent id (the pre-instance-key format) is dropped at seed time.

    Such a key could never match a discovery-supplied instance, so seeding it
    would only pin dead data; valid instance keys in the same map still seed.
    """
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.seed_services(
        PersistedServiceMap(
            services_by_instance={
                str(TEST_AGENT_ID_1): {"system_interface": "http://127.0.0.1:8000"},
                str(TEST_INSTANCE_2): {"system_interface": "http://127.0.0.1:8100"},
            },
            label_to_name_by_instance={},
        )
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.add_known_agent(TEST_INSTANCE_2)
    # TEST_INSTANCE_1 is an instance of TEST_AGENT_ID_1, but the bare-id entry
    # was dropped rather than attached to any of that agent's instances.
    assert resolver.resolve(TEST_INSTANCE_1) is None
    seeded = resolver.resolve(TEST_INSTANCE_2)
    assert seeded is not None
    assert str(seeded.url).rstrip("/") == "http://127.0.0.1:8100"


def test_live_update_overwrites_seeded_service_entry() -> None:
    """The live event stream's full-replace corrects a stale seed."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.seed_services(
        PersistedServiceMap(
            services_by_instance={str(TEST_INSTANCE_1): {"system_interface": "http://127.0.0.1:8000"}},
            label_to_name_by_instance={},
        )
    )
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:9999"}, {})
    target = resolver.resolve(TEST_INSTANCE_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9999"


def test_update_services_persists_to_cache(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:8000"}, {})
    assert cache.load() == PersistedServiceMap(
        services_by_instance={str(TEST_INSTANCE_1): {"system_interface": "http://127.0.0.1:8000"}},
        label_to_name_by_instance={},
    )


def test_remove_known_agent_drops_cache_entry(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:8000"}, {})
    resolver.remove_known_agent(TEST_INSTANCE_1)
    assert cache.load() == PersistedServiceMap(services_by_instance={}, label_to_name_by_instance={})


def test_persisted_map_seeds_a_fresh_resolver(tmp_path: Path) -> None:
    """End-to-end: one run persists its service map; a fresh run seeds from it and resolves."""
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    first_run = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    first_run.add_known_agent(TEST_INSTANCE_1)
    first_run.update_services(TEST_INSTANCE_1, {"system_interface": "http://127.0.0.1:8000"}, {})

    fresh_run = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    fresh_run.seed_services(cache.load())
    # Still gated on discovery: not served until this run marks the agent known.
    assert fresh_run.resolve(TEST_INSTANCE_1) is None
    fresh_run.add_known_agent(TEST_INSTANCE_1)
    target = fresh_run.resolve(TEST_INSTANCE_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:8000"


def test_seeded_labels_route_app_origins_before_the_event_stream_delivers() -> None:
    """A seeded label map resolves an app's ``<name>-<rand>`` origin immediately.

    Regression for the permanent "Loading workspace" wedge: the shell resolves
    from a seeded cache (bare origin needs no label), but an app origin routes
    by its unguessable label -- so a seed without labels left every app on the
    503 loader until the slow (and sometimes wedged) per-agent event stream
    replayed. With labels seeded, the app origin must resolve with no
    update_services call at all.
    """
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    resolver.seed_services(
        PersistedServiceMap(
            services_by_instance={
                str(TEST_INSTANCE_1): {
                    "system_interface": "http://127.0.0.1:8000",
                    "myapp": "http://127.0.0.1:8092",
                }
            },
            label_to_name_by_instance={str(TEST_INSTANCE_1): {"myapp-x7k9q2w1": "myapp"}},
        )
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    app_target = resolver.resolve_by_origin_label(TEST_INSTANCE_1, "myapp-x7k9q2w1")
    assert app_target is not None
    assert str(app_target.url).rstrip("/") == "http://127.0.0.1:8092"
    # The shell's label was not part of this seed, so the bare-origin redirect stays off.
    assert resolver.shell_origin_label(TEST_INSTANCE_1) is None
    shell_target = resolver.resolve(TEST_INSTANCE_1)
    assert shell_target is not None


def test_update_services_persists_labels_to_cache(tmp_path: Path) -> None:
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.update_services(
        TEST_INSTANCE_1,
        {"myapp": "http://127.0.0.1:8092"},
        {"myapp-x7k9q2w1": "myapp"},
    )
    assert cache.load() == PersistedServiceMap(
        services_by_instance={str(TEST_INSTANCE_1): {"myapp": "http://127.0.0.1:8092"}},
        label_to_name_by_instance={str(TEST_INSTANCE_1): {"myapp-x7k9q2w1": "myapp"}},
    )


def test_persisted_labels_seed_a_fresh_resolver_end_to_end(tmp_path: Path) -> None:
    """One run persists services + labels; a fresh run seeds and routes an app origin."""
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    first_run = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    first_run.add_known_agent(TEST_INSTANCE_1)
    first_run.update_services(
        TEST_INSTANCE_1,
        {"system_interface": "http://127.0.0.1:8000", "myapp": "http://127.0.0.1:8092"},
        {"system_interface-shell111": "system_interface", "myapp-x7k9q2w1": "myapp"},
    )

    fresh_run = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    fresh_run.seed_services(cache.load())
    fresh_run.add_known_agent(TEST_INSTANCE_1)
    app_target = fresh_run.resolve_by_origin_label(TEST_INSTANCE_1, "myapp-x7k9q2w1")
    assert app_target is not None
    assert str(app_target.url).rstrip("/") == "http://127.0.0.1:8092"
    assert fresh_run.shell_origin_label(TEST_INSTANCE_1) == "system_interface-shell111"


def test_remove_known_agent_prunes_labels_from_cache(tmp_path: Path) -> None:
    """A destroyed agent's labels must leave the cache along with its services."""
    cache = ServiceMapCache(cache_path=tmp_path / "service_map.json")
    resolver = ForwardResolver(
        strategy=ForwardServiceStrategy(service_name="system_interface"),
        service_map_cache=cache,
    )
    resolver.add_known_agent(TEST_INSTANCE_1)
    resolver.add_known_agent(TEST_INSTANCE_2)
    resolver.update_services(TEST_INSTANCE_1, {"myapp": "http://127.0.0.1:8092"}, {"myapp-x7k9q2w1": "myapp"})
    resolver.update_services(TEST_INSTANCE_2, {"web": "http://127.0.0.1:8080"}, {"web-a1b2c3d4": "web"})
    resolver.remove_known_agent(TEST_INSTANCE_1)
    assert cache.load() == PersistedServiceMap(
        services_by_instance={str(TEST_INSTANCE_2): {"web": "http://127.0.0.1:8080"}},
        label_to_name_by_instance={str(TEST_INSTANCE_2): {"web-a1b2c3d4": "web"}},
    )
