from imbue.minds.desktop_client.host_keyed_ttl_cache import HostKeyedTtlCache

_HOST_A = "host-" + "a" * 32
_HOST_B = "host-" + "b" * 32


def test_host_keyed_ttl_cache_serves_hits_and_caches_negative_lookups() -> None:
    cache = HostKeyedTtlCache[str | None](ttl_seconds=60.0)

    assert cache.get(_HOST_A) is None
    cache.put(_HOST_A, "looked-up")
    cache.put(_HOST_B, None)

    hit = cache.get(_HOST_A)
    assert hit is not None
    assert hit.value == "looked-up"
    # A cached None answer is a hit too (value=None), distinct from a miss.
    negative_hit = cache.get(_HOST_B)
    assert negative_hit is not None
    assert negative_hit.value is None


def test_host_keyed_ttl_cache_expires_entries_after_the_ttl() -> None:
    cache = HostKeyedTtlCache[str | None](ttl_seconds=0.0)

    cache.put(_HOST_A, "looked-up")

    assert cache.get(_HOST_A) is None


def test_host_keyed_ttl_cache_invalidate_forces_the_next_lookup_to_miss() -> None:
    cache = HostKeyedTtlCache[str | None](ttl_seconds=60.0)
    cache.put(_HOST_A, "looked-up")

    cache.invalidate(_HOST_A)

    assert cache.get(_HOST_A) is None
