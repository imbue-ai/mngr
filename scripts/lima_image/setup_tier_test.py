import json
from collections.abc import Callable

import httpx
import pytest
from click import ClickException

from scripts.lima_image.setup_tier import CloudflareClient
from scripts.lima_image.setup_tier import CloudflareEnv
from scripts.lima_image.setup_tier import bucket_name
from scripts.lima_image.setup_tier import default_hostname
from scripts.lima_image.setup_tier import r2_s3_secret_access_key

_ENV = CloudflareEnv(api_token="tok", account_id="acct123", zone_id="zone123", domain="minds.example")


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> CloudflareClient:
    return CloudflareClient(_ENV, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_each_environment_gets_its_own_bucket_and_hostname() -> None:
    # Two developers publishing at once must not overwrite each other's image, nor production's.
    assert bucket_name("dev-weishi") != bucket_name("production")
    assert default_hostname("dev-weishi", "minds.example") != default_hostname("production", "minds.example")
    assert bucket_name("production") == "minds-lima-images-production"
    assert default_hostname("production", "minds.example") == "lima-images-production.minds.example"


def test_s3_secret_is_the_sha256_of_the_token_value() -> None:
    # R2 defines the S3 secret access key this way; publish.py cannot authenticate otherwise.
    assert r2_s3_secret_access_key("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_bucket_exists_is_false_only_for_a_missing_bucket() -> None:
    assert _client(lambda request: httpx.Response(404, json={"success": False})).bucket_exists("b") is False
    assert _client(lambda request: httpx.Response(200, json={"success": True})).bucket_exists("b") is True


def test_bucket_exists_raises_rather_than_calling_an_unreadable_bucket_absent() -> None:
    # A token that cannot read buckets must name that here, not report "no bucket" and
    # leave the operator staring at a confusing failure from the create call after it.
    with pytest.raises(ClickException):
        _client(lambda request: httpx.Response(403, json={"success": False})).bucket_exists("b")


def test_a_failed_call_raises_rather_than_reporting_success() -> None:
    # Cloudflare answers a rejected call with HTTP 200 and success=false, so a naive
    # status check would read a permission failure as a completed setup.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "errors": [{"message": "nope"}]})

    with pytest.raises(ClickException):
        _client(handler).create_bucket("b")


def test_minted_token_is_scoped_to_the_one_bucket() -> None:
    # An account-wide token would let any publisher overwrite every environment's image.
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "result": {"id": "tid", "value": "tval"}})

    token_id, token_value = _client(handler).create_bucket_scoped_r2_token("minds-lima-images-production", "n")
    assert (token_id, token_value) == ("tid", "tval")
    (payload,) = seen
    resources = payload["policies"][0]["resources"]
    assert list(resources) == ["com.cloudflare.edge.r2.bucket.acct123_default_minds-lima-images-production"], (
        "the token must name exactly one bucket, never the whole account"
    )


def test_custom_domain_is_attached_to_the_configured_zone() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "result": {}})

    _client(handler).attach_custom_domain("bucket", "lima-images.minds.example")
    (payload,) = seen
    assert payload["domain"] == "lima-images.minds.example"
    assert payload["zoneId"] == "zone123"
    assert payload["enabled"] is True
