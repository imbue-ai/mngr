from collections.abc import Callable

import httpx
import pytest

from scripts.lima_image.publish import CloudflareApiObjectStore

_ACCOUNT = "acct123"
_BUCKET = "minds-lima-images-test"
_KEY = "store/0001/abc.cacnk"


def _store(handler: Callable[[httpx.Request], httpx.Response]) -> CloudflareApiObjectStore:
    """A store whose HTTP calls are served by ``handler`` instead of the network."""
    return CloudflareApiObjectStore(
        account_id=_ACCOUNT,
        api_token="tok",
        bucket=_BUCKET,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_exists_uses_get_because_the_object_api_rejects_head() -> None:
    # Cloudflare's R2 object API answers HEAD with 405, so an exists() built on HEAD
    # raises on the very first chunk and no upload can ever succeed.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(404)

    assert _store(handler).exists(_KEY) is False
    assert seen == ["GET"], f"exists() must probe with GET, not {seen}"


def test_exists_is_true_for_a_present_object() -> None:
    assert _store(lambda request: httpx.Response(200, content=b"chunk")).exists(_KEY) is True


def test_exists_raises_on_auth_failure_rather_than_reporting_absent() -> None:
    # Treating a 403 as "absent" would silently skip a chunk that is then never
    # uploaded, producing a published image that cannot be reassembled.
    with pytest.raises(httpx.HTTPStatusError):
        _store(lambda request: httpx.Response(403)).exists(_KEY)


def test_get_optional_returns_none_only_for_a_missing_object() -> None:
    assert _store(lambda request: httpx.Response(404)).get_optional(_KEY) is None
    assert _store(lambda request: httpx.Response(200, content=b"body")).get_optional(_KEY) == b"body"


def test_get_optional_raises_on_auth_failure() -> None:
    # A 403 read of the root manifest must not look like "no manifest published yet":
    # the publisher would overwrite the manifest and drop the other arch's entry.
    with pytest.raises(httpx.HTTPStatusError):
        _store(lambda request: httpx.Response(403)).get_optional(_KEY)


def test_put_targets_the_object_api_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _store(handler).put(_KEY, b"data", "application/octet-stream")
    (request,) = seen
    assert request.method == "PUT"
    assert str(request.url).endswith(f"/accounts/{_ACCOUNT}/r2/buckets/{_BUCKET}/objects/{_KEY}")
    assert request.headers["content-type"] == "application/octet-stream"
