"""Tests for the pinned-asset manifest and the fetch-on-startup logic."""

import hashlib
from pathlib import Path

from imbue.mngr_foreman.assets import AssetSpec
from imbue.mngr_foreman.assets import AssetTier
from imbue.mngr_foreman.assets import MANIFEST
from imbue.mngr_foreman.assets import ensure_asset
from imbue.mngr_foreman.assets import ensure_assets

_REQUIRED_PATHS = {
    "marked.min.js",
    "xterm.min.js",
    "xterm.min.css",
    "xterm-addon-fit.min.js",
}


def _spec(path: str, body: bytes, tier: AssetTier) -> AssetSpec:
    return AssetSpec(path=path, url=f"https://example.test/{path}", sha256=hashlib.sha256(body).hexdigest(), tier=tier)


def _fetcher(bodies: dict[str, bytes], calls: list[str]):
    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        if url not in bodies:
            raise OSError(f"offline: {url}")
        return bodies[url]

    return fetch


# ---- manifest integrity ---------------------------------------------------


def test_manifest_paths_unique() -> None:
    paths = [spec.path for spec in MANIFEST]
    assert len(paths) == len(set(paths))


def test_manifest_hashes_are_sha256_hex() -> None:
    for spec in MANIFEST:
        assert len(spec.sha256) == 64
        assert all(c in "0123456789abcdef" for c in spec.sha256)


def test_manifest_required_tier_is_terminal_and_markdown_core() -> None:
    required = {spec.path for spec in MANIFEST if spec.tier is AssetTier.REQUIRED}
    assert required == _REQUIRED_PATHS


def test_manifest_paths_have_no_traversal() -> None:
    for spec in MANIFEST:
        assert not spec.path.startswith("/")
        assert ".." not in spec.path.split("/")


# ---- ensure_asset ---------------------------------------------------------


def test_ensure_asset_downloads_verifies_and_caches(tmp_path: Path) -> None:
    body = b"console.log('hi')"
    spec = _spec("marked.min.js", body, AssetTier.REQUIRED)
    calls: list[str] = []
    fetch = _fetcher({spec.url: body}, calls)

    assert ensure_asset(spec, tmp_path, fetch=fetch, timeout=1.0) is True
    assert (tmp_path / "marked.min.js").read_bytes() == body
    assert len(calls) == 1

    # Second call is served from the cache with no new download.
    assert ensure_asset(spec, tmp_path, fetch=fetch, timeout=1.0) is True
    assert len(calls) == 1


def test_ensure_asset_writes_nested_paths(tmp_path: Path) -> None:
    body = b"font-bytes"
    spec = _spec("katex/fonts/KaTeX_Main-Regular.woff2", body, AssetTier.OPTIONAL)
    assert ensure_asset(spec, tmp_path, fetch=_fetcher({spec.url: body}, []), timeout=1.0) is True
    assert (tmp_path / "katex" / "fonts" / "KaTeX_Main-Regular.woff2").read_bytes() == body


def test_ensure_asset_rejects_hash_mismatch(tmp_path: Path) -> None:
    spec = _spec("marked.min.js", b"expected", AssetTier.REQUIRED)
    fetch = _fetcher({spec.url: b"tampered-different-bytes"}, [])
    assert ensure_asset(spec, tmp_path, fetch=fetch, timeout=1.0) is False
    assert not (tmp_path / "marked.min.js").exists()


def test_ensure_asset_handles_fetch_failure(tmp_path: Path) -> None:
    spec = _spec("marked.min.js", b"body", AssetTier.REQUIRED)
    assert ensure_asset(spec, tmp_path, fetch=_fetcher({}, []), timeout=1.0) is False
    assert not (tmp_path / "marked.min.js").exists()


def test_ensure_asset_refetches_corrupt_cache(tmp_path: Path) -> None:
    body = b"good-bytes"
    spec = _spec("xterm.min.js", body, AssetTier.REQUIRED)
    (tmp_path / "xterm.min.js").write_bytes(b"corrupt")
    assert ensure_asset(spec, tmp_path, fetch=_fetcher({spec.url: body}, []), timeout=1.0) is True
    assert (tmp_path / "xterm.min.js").read_bytes() == body


# ---- ensure_assets aggregation -------------------------------------------


def test_ensure_assets_all_present(tmp_path: Path) -> None:
    req = _spec("xterm.min.js", b"xterm", AssetTier.REQUIRED)
    opt = _spec("mermaid.min.js", b"mermaid", AssetTier.OPTIONAL)
    bodies = {req.url: b"xterm", opt.url: b"mermaid"}
    result = ensure_assets(tmp_path, fetch=_fetcher(bodies, []), timeout=1.0, manifest=(req, opt))
    assert set(result.served) == {"xterm.min.js", "mermaid.min.js"}
    assert set(result.fetched) == {"xterm.min.js", "mermaid.min.js"}
    assert result.missing_required == ()
    assert result.missing_optional == ()


def test_ensure_assets_offline_buckets_by_tier(tmp_path: Path) -> None:
    req = _spec("xterm.min.js", b"xterm", AssetTier.REQUIRED)
    opt = _spec("mermaid.min.js", b"mermaid", AssetTier.OPTIONAL)
    result = ensure_assets(tmp_path, fetch=_fetcher({}, []), timeout=1.0, manifest=(req, opt))
    assert result.served == ()
    assert result.missing_required == ("xterm.min.js",)
    assert result.missing_optional == ("mermaid.min.js",)


def test_ensure_assets_cached_not_refetched(tmp_path: Path) -> None:
    opt = _spec("mermaid.min.js", b"mermaid", AssetTier.OPTIONAL)
    (tmp_path / "mermaid.min.js").write_bytes(b"mermaid")
    calls: list[str] = []
    result = ensure_assets(tmp_path, fetch=_fetcher({opt.url: b"mermaid"}, calls), timeout=1.0, manifest=(opt,))
    assert result.served == ("mermaid.min.js",)
    assert result.fetched == ()
    assert calls == []
