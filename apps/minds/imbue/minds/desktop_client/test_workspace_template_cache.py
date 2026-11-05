import itertools
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.testing import make_test_workspace_template_cache
from imbue.minds.desktop_client.testing import materialize_cached_release
from imbue.minds.desktop_client.workspace_template_cache import WorkspaceTemplateCache
from imbue.minds.desktop_client.workspace_template_cache import WorkspaceTemplateCacheStatus
from imbue.minds.errors import GitCloneError
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import TemplateReleaseTag
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command

_RELEASE_TAG = TemplateReleaseTag("minds-v4.5.6")


def _git(repo: Path, *args: str) -> str:
    return run_git_command(repo, *args).stdout.strip()


@contextmanager
def _serving_http(
    handler_class: type[BaseHTTPRequestHandler], release_event: threading.Event
) -> Generator[str, None, None]:
    """Serve ``handler_class`` on a local port until the block exits, which first sets ``release_event``."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_address[1])
    finally:
        release_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _unanswered_git_remote(refused_request_count: int = 0) -> Generator[str, None, None]:
    """A git remote over HTTP that holds every request open, like one that stopped answering, until the block exits.

    Its first ``refused_request_count`` requests are answered 404 at once instead.
    """
    release_event = threading.Event()
    request_numbers = itertools.count()

    class _UnansweredHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if next(request_numbers) >= refused_request_count:
                release_event.wait(timeout=30.0)
            try:
                self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with _serving_http(_UnansweredHandler, release_event) as base_url:
        yield base_url + "/imbue/stalled-template.git"


@contextmanager
def _dumb_git_remote(
    bare_repo: Path, chunk_bytes: int, chunk_interval_seconds: float, refused_request_count: int = 0
) -> Generator[str, None, None]:
    """A git remote over dumb HTTP that serves ``bare_repo`` a chunk at a time, like a slow but working link.

    Its first ``refused_request_count`` requests are answered 404, which git does not retry, so each
    one fails the fetch that made it.
    """
    release_event = threading.Event()
    request_numbers = itertools.count()

    class _DumbHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = bare_repo / urlsplit(self.path).path.removeprefix("/template.git/")
            if next(request_numbers) < refused_request_count or not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for offset in range(0, len(body), chunk_bytes):
                if release_event.wait(timeout=chunk_interval_seconds):
                    return
                self.wfile.write(body[offset : offset + chunk_bytes])
                self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with _serving_http(_DumbHandler, release_event) as base_url:
        yield base_url + "/template.git"


def _make_release_served_over_dumb_http(tmp_path: Path, payload_bytes: int) -> tuple[Path, Path]:
    """An origin whose release carries ``payload_bytes`` of random data, and a bare copy of it ready for dumb HTTP."""
    origin = tmp_path / "origin"
    init_git_repo(origin)
    # Random bytes do not compress, so the release's pack is about this big on the wire.
    (origin / "payload.bin").write_bytes(os.urandom(payload_bytes))
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "release commit")
    _git(origin, "tag", "-a", str(_RELEASE_TAG), "-m", "release")
    bare = tmp_path / "template.git"
    run_git_command(tmp_path, "clone", "-q", "--bare", str(origin), str(bare))
    _git(bare, "repack", "-a", "-d", "-q")
    _git(bare, "update-server-info")
    return origin, bare


@pytest.mark.timeout(30)
def test_a_clone_step_that_outruns_its_timeout_fails_the_clone(tmp_path: Path) -> None:
    with _unanswered_git_remote() as url:
        started_at = time.monotonic()
        with pytest.raises(GitCloneError, match=r"git fetch failed:\ntimed out after 1\.0s"):
            clone_git_repo(GitUrl(url), tmp_path / "clone", branch=_RELEASE_TAG, timeout_seconds=1.0)

        # The remote holds the request for far longer, so only the step timeout ended the fetch.
        assert time.monotonic() - started_at < 10.0


@pytest.mark.timeout(30)
def test_the_missing_ref_probe_after_a_failed_fetch_gives_up_at_the_clone_timeout(tmp_path: Path) -> None:
    with _unanswered_git_remote(refused_request_count=1) as url:
        started_at = time.monotonic()
        with pytest.raises(GitCloneError, match=r"git fetch failed:"):
            clone_git_repo(GitUrl(url), tmp_path / "clone", branch=_RELEASE_TAG, timeout_seconds=1.0)

        # The fetch is refused at once and the remote holds the probe for far longer, so only the timeout ended it.
        assert time.monotonic() - started_at < 10.0


@pytest.mark.timeout(30)
def test_a_create_stops_waiting_on_a_stalled_prefetch_which_gives_up_on_its_own(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    clone_stall_timeout_seconds = 6
    with _unanswered_git_remote() as url:
        cache = WorkspaceTemplateCache(
            repo_url=GitUrl(url),
            release_tag=_RELEASE_TAG,
            cache_dir=tmp_path / "template-cache",
            concurrency_group=root_concurrency_group,
            clone_stall_timeout_seconds=clone_stall_timeout_seconds,
            prefetch_retry_waits_seconds=(),
        )
        cache.start_prefetch()
        clone_dir = tmp_path / "clone" / "repo"
        waits: list[str] = []

        started_at = time.monotonic()
        is_copied = materialize_cached_release(cache, clone_dir, waits, stall_timeout_seconds=0.5)
        # Well before the prefetch's own stall check ends it, so the create noticed the stall itself.
        assert time.monotonic() - started_at < 5.0

        assert not is_copied
        assert waits == ["waited"]
        assert not clone_dir.exists()
        # Only git's own stall check can end the download this soon; the per-step timeout is far longer.
        # It ends two stalls: the fetch's, then the missing-ref probe's against the same silent remote.
        wait_for(
            lambda: cache.status is WorkspaceTemplateCacheStatus.UNAVAILABLE,
            timeout=2 * clone_stall_timeout_seconds + 8.0,
        )
        assert not any((tmp_path / "template-cache").iterdir())


@pytest.mark.timeout(60)
def test_a_create_waits_out_a_slow_prefetch_that_is_still_receiving_data(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin, bare = _make_release_served_over_dumb_http(tmp_path, payload_bytes=400_000)
    stall_timeout_seconds = 1.5

    with _dumb_git_remote(bare, chunk_bytes=8192, chunk_interval_seconds=0.08) as url:
        cache = make_test_workspace_template_cache(url, _RELEASE_TAG, tmp_path, root_concurrency_group)
        cache.start_prefetch()
        copied = tmp_path / "copied" / "repo"
        waits: list[str] = []

        started_at = time.monotonic()
        is_copied = materialize_cached_release(cache, copied, waits, stall_timeout_seconds=stall_timeout_seconds)

    assert is_copied
    # The download took several stall timeouts, so a wait bounded by one would have given up.
    assert time.monotonic() - started_at > 2 * stall_timeout_seconds
    assert waits == ["waited"]
    assert _git(copied, "rev-parse", "HEAD") == _git(origin, "rev-parse", f"{_RELEASE_TAG}^{{commit}}")


@pytest.mark.timeout(30)
def test_a_failed_prefetch_is_retried_after_its_wait(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    origin, bare = _make_release_served_over_dumb_http(tmp_path, payload_bytes=1000)

    with _dumb_git_remote(bare, chunk_bytes=1 << 20, chunk_interval_seconds=0.0, refused_request_count=2) as url:
        # One refusal fails a prefetch that has no retries left.
        no_retry_cache = make_test_workspace_template_cache(url, _RELEASE_TAG, tmp_path, root_concurrency_group)
        assert no_retry_cache.prefetch_once() is WorkspaceTemplateCacheStatus.UNAVAILABLE

        cache = make_test_workspace_template_cache(
            url, _RELEASE_TAG, tmp_path, root_concurrency_group, prefetch_retry_waits_seconds=(0.2,)
        )
        cache.start_prefetch()
        wait_for(lambda: cache.status is WorkspaceTemplateCacheStatus.READY, timeout=20.0)

    copied = tmp_path / "copied" / "repo"
    waits: list[str] = []
    assert materialize_cached_release(cache, copied, waits)
    assert waits == []
    assert _git(copied, "rev-parse", "HEAD") == _git(origin, "rev-parse", f"{_RELEASE_TAG}^{{commit}}")
