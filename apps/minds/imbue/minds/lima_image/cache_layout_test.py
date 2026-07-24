from pathlib import Path

from imbue.minds.lima_image.cache_layout import LimaImageCurrentPointer
from imbue.minds.lima_image.cache_layout import chunk_store_url
from imbue.minds.lima_image.cache_layout import index_url
from imbue.minds.lima_image.cache_layout import manifest_signature_url
from imbue.minds.lima_image.cache_layout import manifest_url
from imbue.minds.lima_image.primitives import ImageArch
from imbue.minds.lima_image.primitives import MindsImageVersion

_BASE = "https://cdn.example.com/lima"
_VERSION = MindsImageVersion("minds-v0.3.4")


def test_url_builders_compose_expected_paths() -> None:
    assert manifest_url(_BASE, _VERSION) == "https://cdn.example.com/lima/manifests/minds-v0.3.4/root.json"
    assert manifest_signature_url(_BASE, _VERSION) == manifest_url(_BASE, _VERSION) + ".minisig"
    assert chunk_store_url(_BASE) == "https://cdn.example.com/lima/store/"
    assert index_url(_BASE, "indexes/minds-v0.3.4/x86_64.caibx") == (
        "https://cdn.example.com/lima/indexes/minds-v0.3.4/x86_64.caibx"
    )


def test_url_builders_tolerate_trailing_slash_in_base() -> None:
    assert manifest_url(_BASE + "/", _VERSION) == manifest_url(_BASE, _VERSION)
    assert chunk_store_url(_BASE + "/") == chunk_store_url(_BASE)


def test_current_pointer_round_trips_through_json() -> None:
    pointer = LimaImageCurrentPointer(
        minds_version=_VERSION,
        arch=ImageArch.AARCH64,
        raw_path=Path("/data/lima-images/versions/minds-v0.3.4/AARCH64/image.raw"),
        index_path=Path("/data/lima-images/versions/minds-v0.3.4/AARCH64/image.caibx"),
    )
    restored = LimaImageCurrentPointer.model_validate_json(pointer.model_dump_json())
    assert restored == pointer
