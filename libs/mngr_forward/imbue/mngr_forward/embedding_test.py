"""Unit tests for the embedding (frame-ancestors) policy builder."""

import pytest

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.mngr_forward.embedding import EmbedderOrigin
from imbue.mngr_forward.embedding import build_frame_ancestors_policy
from imbue.mngr_forward.primitives import ParsedForwardHost
from imbue.mngr_forward.primitives import parse_forward_host

_TEST_HOST = "host-" + "0123456789abcdef0123456789abcdef"


def _host_info(host_header: str) -> ParsedForwardHost:
    parsed = parse_forward_host(host_header)
    assert parsed is not None
    return parsed


def test_default_policy_denies_external_embedding() -> None:
    """Without embedder origins the policy is 'self' + the workspace family only."""
    policy = build_frame_ancestors_policy(
        host_info=_host_info(f"{_TEST_HOST}.localhost:8421"),
        listen_port=8421,
        use_http2=True,
        embedder_origins=(),
    )
    assert policy == (
        f"frame-ancestors 'self' https://{_TEST_HOST}.localhost:8421 https://*.{_TEST_HOST}.localhost:8421"
    )


def test_embedder_origins_are_appended() -> None:
    policy = build_frame_ancestors_policy(
        host_info=_host_info(f"svc.{_TEST_HOST}.localhost:8421"),
        listen_port=8421,
        use_http2=True,
        embedder_origins=(EmbedderOrigin("http://localhost:8420"), EmbedderOrigin("http://127.0.0.1:8420")),
    )
    assert policy.endswith("http://localhost:8420 http://127.0.0.1:8420")
    # The family sources are keyed by the workspace domain, not the full
    # service origin -- the shell (a sibling label) must be able to embed.
    assert f"https://*.{_TEST_HOST}.localhost:8421" in policy


def test_plain_http_policy_uses_http_scheme() -> None:
    policy = build_frame_ancestors_policy(
        host_info=_host_info(f"{_TEST_HOST}.localhost"),
        listen_port=8421,
        use_http2=False,
        embedder_origins=(),
    )
    assert f"http://{_TEST_HOST}.localhost:8421" in policy
    assert "https://" not in policy


def test_embedder_origin_rejects_paths_and_garbage() -> None:
    with pytest.raises(InvalidPrimitiveValueError):
        EmbedderOrigin("http://localhost:8420/path")
    with pytest.raises(InvalidPrimitiveValueError):
        EmbedderOrigin("localhost:8420")
    with pytest.raises(InvalidPrimitiveValueError):
        EmbedderOrigin("javascript://alert(1)")
    assert str(EmbedderOrigin("https://example.com")) == "https://example.com"
