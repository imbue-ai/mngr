import threading
from typing import Any

from imbue.imbue_common.sentry.s3_uploader import DEFAULT_REGION
from imbue.imbue_common.sentry.s3_uploader import S3_UPLOAD_THREAD_COUNT
from imbue.imbue_common.sentry.s3_uploader import _S3Uploader

# must stay under the suite's per-test timeout so a stuck thread fails here, with a useful message
_THREAD_WAIT_TIMEOUT_SECONDS = 5.0


def test_each_upload_thread_gets_its_own_client_and_reuses_it() -> None:
    """Concurrent uploads must never share one S3 client (and therefore one SSLContext).

    Each thread must also reuse its own client, so the isolation does not become a client per upload.
    """
    uploader = _S3Uploader(bucket="unused-test-bucket", region=DEFAULT_REGION)
    # every thread reaches client creation at the same moment, so concurrent creation is exercised
    start_barrier = threading.Barrier(S3_UPLOAD_THREAD_COUNT)
    clients_by_thread: dict[int, tuple[Any, Any]] = {}
    results_lock = threading.Lock()

    def collect_clients() -> None:
        start_barrier.wait(timeout=_THREAD_WAIT_TIMEOUT_SECONDS)
        first_client = uploader._client_for_current_thread()
        second_client = uploader._client_for_current_thread()
        with results_lock:
            clients_by_thread[threading.get_ident()] = (first_client, second_client)

    threads = [threading.Thread(target=collect_clients) for _ in range(S3_UPLOAD_THREAD_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_THREAD_WAIT_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "an upload thread never finished acquiring its client"

    assert len(clients_by_thread) == S3_UPLOAD_THREAD_COUNT

    for first_client, second_client in clients_by_thread.values():
        assert first_client is second_client

    distinct_client_ids = {id(first_client) for first_client, _ in clients_by_thread.values()}
    assert len(distinct_client_ids) == S3_UPLOAD_THREAD_COUNT
