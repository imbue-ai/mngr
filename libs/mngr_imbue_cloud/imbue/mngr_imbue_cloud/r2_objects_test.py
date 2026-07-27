import hashlib
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError

from imbue.mngr_imbue_cloud.r2_objects import R2ObjectDeletionError
from imbue.mngr_imbue_cloud.r2_objects import derive_s3_secret_from_token_value
from imbue.mngr_imbue_cloud.r2_objects import empty_bucket_via_s3


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, Bucket: str) -> list[dict[str, Any]]:
        return self._pages


class _FakeS3Client:
    """Stub S3 client capturing delete_objects batches."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.deleted_batches: list[list[dict[str, str]]] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "list_objects_v2"
        return _FakePaginator(self._pages)

    def delete_objects(self, Bucket: str, Delete: dict[str, list[dict[str, str]]]) -> None:
        self.deleted_batches.append(Delete["Objects"])


def test_empty_bucket_via_s3_deletes_in_batches() -> None:
    pages = [
        {"Contents": [{"Key": f"a/{i}"} for i in range(1500)]},
        {"Contents": [{"Key": "b/last"}]},
    ]
    client = _FakeS3Client(pages)

    deleted_count = empty_bucket_via_s3(client, "bucket-x")

    assert deleted_count == 1501
    # The 1500-object page splits into a full 1000 batch plus the remainder.
    assert [len(batch) for batch in client.deleted_batches] == [1000, 500, 1]


def test_empty_bucket_via_s3_handles_empty_bucket() -> None:
    client = _FakeS3Client([{}])
    assert empty_bucket_via_s3(client, "bucket-x") == 0
    assert client.deleted_batches == []


def test_empty_bucket_via_s3_wraps_client_errors() -> None:
    class _FailingClient(_FakeS3Client):
        def get_paginator(self, name: str) -> _FakePaginator:
            raise BotoCoreError()

    with pytest.raises(R2ObjectDeletionError):
        empty_bucket_via_s3(_FailingClient([]), "bucket-x")


def test_derive_s3_secret_is_sha256_hex() -> None:
    assert derive_s3_secret_from_token_value("tok") == hashlib.sha256(b"tok").hexdigest()
