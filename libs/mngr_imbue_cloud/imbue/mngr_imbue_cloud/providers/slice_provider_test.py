from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.mock_box_image_cache_test import MockBoxImageCache

_TAG = "fct:minds-v0.3.2"


class _OrchestrationProvider(SliceVpsDockerProvider):
    """Slice provider whose box cache + seed/load steps are recorded, to test the decision branching.

    Overrides the three seams ``_ensure_cached_image_present`` drives -- the cache
    factory and the seed/load actions -- so the branch logic can be exercised
    without a real box, slice dockerd, or image build.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    test_cache: MockBoxImageCache = Field(description="Injected in-memory cache")
    seeded_tags: list[str] = Field(default_factory=list)
    loaded_tags: list[str] = Field(default_factory=list)

    def _make_box_image_cache(self) -> BoxImageCacheInterface:
        return self.test_cache

    def _seed_box_image(
        self,
        *,
        cache: BoxImageCacheInterface,
        outer: OuterHostInterface,
        host_id: HostId,
        vm_ssh_port: int,
        image_tag: str,
        build_args: Sequence[str] | None,
    ) -> None:
        self.seeded_tags.append(image_tag)

    def _load_cached_image(
        self, *, cache: BoxImageCacheInterface, outer: OuterHostInterface, vm_ssh_port: int, image_tag: str
    ) -> None:
        self.loaded_tags.append(image_tag)


def _provider(cache: MockBoxImageCache) -> _OrchestrationProvider:
    # model_construct skips the heavy VpsProvider field wiring; the overridden seams
    # the test exercises touch only the injected cache + the record lists.
    return _OrchestrationProvider.model_construct(test_cache=cache, seeded_tags=[], loaded_tags=[])


def _ensure(provider: _OrchestrationProvider) -> None:
    provider._ensure_cached_image_present(
        outer=_FAKE_OUTER,
        host_id=HostId.generate(),
        vm_ssh_port=2200,
        image_tag=_TAG,
        build_args=("--file=Dockerfile", "."),
    )


# Never used by the overridden seed/load seams; passed only to satisfy the signature.
_FAKE_OUTER: Any = object()


def test_loads_when_tar_already_present() -> None:
    provider = _provider(MockBoxImageCache(tars_present={_TAG}))
    _ensure(provider)
    assert provider.loaded_tags == [_TAG]
    assert provider.seeded_tags == []


def test_seeds_when_no_tar_and_lock_is_acquired() -> None:
    provider = _provider(MockBoxImageCache())
    _ensure(provider)
    assert provider.seeded_tags == [_TAG]
    assert provider.loaded_tags == []


def test_waits_then_loads_when_another_slice_is_seeding() -> None:
    # No tar yet and the lock is held by an in-flight builder, so we must take the
    # try_acquire (fails) -> wait_for_tar (tar appears) -> load path rather than the
    # has_tar() fast path.
    cache = MockBoxImageCache(locks_held={_TAG}, is_tar_published_on_wait=True)
    provider = _provider(cache)
    _ensure(provider)
    assert provider.loaded_tags == [_TAG]
    assert provider.seeded_tags == []


def test_raises_when_lock_held_and_tar_never_appears() -> None:
    # Lock held by a builder, but the tar never materializes within the wait budget.
    cache = MockBoxImageCache(locks_held={_TAG})
    provider = _provider(cache)
    with pytest.raises(BoxImageCacheError):
        _ensure(provider)
    assert provider.seeded_tags == []
    assert provider.loaded_tags == []
