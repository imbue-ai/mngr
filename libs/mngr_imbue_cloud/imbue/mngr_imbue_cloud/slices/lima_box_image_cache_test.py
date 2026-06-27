from collections.abc import Callable

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.slices.box_image_cache import TransferKey
from imbue.mngr_imbue_cloud.slices.lima_box_image_cache import LimaBoxImageCache
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

_CACHE_DIR = "/home/limahost/.cache/mngr-slice-fct"
_TAG = "fct:minds-v0.3.2"
_KEY = TransferKey(private_key_path_on_box=f"{_CACHE_DIR}/.transfer-abc", public_key="ssh-ed25519 AAA")


class _ScriptedBoxClient(LimaSliceVpsClient):
    """LimaSliceVpsClient whose box SSH is replaced by a scripted, recording responder."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    responder: Callable[[str], tuple[int | None, str, str]] = Field(description="Maps a remote command to its result")
    recorded: list[str] = Field(default_factory=list)

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        self.recorded.append(remote_command)
        return self.responder(remote_command)


def _cache(responder: Callable[[str], tuple[int | None, str, str]]) -> LimaBoxImageCache:
    client = _ScriptedBoxClient(
        box_address="box.example",
        box_ssh_user="limahost",
        private_key_path="/tmp/id",
        responder=responder,
        recorded=[],
    )
    return LimaBoxImageCache(slice_client=client, cache_dir=_CACHE_DIR)


def test_has_tar_reflects_test_f_exit_code() -> None:
    assert _cache(lambda cmd: (0, "", "")).has_tar(_TAG) is True
    assert _cache(lambda cmd: (1, "", "")).has_tar(_TAG) is False


def test_try_acquire_build_lock_succeeds_when_mkdir_succeeds() -> None:
    assert _cache(lambda cmd: (0, "", "")).try_acquire_build_lock(_TAG) is True


def test_try_acquire_build_lock_fails_when_lock_is_fresh() -> None:
    def responder(command: str) -> tuple[int | None, str, str]:
        if command.startswith("mkdir"):
            return 1, "", "File exists"
        if "stat -c %Y" in command:
            return 0, "100\n", ""  # 100s old, well under the TTL
        return 0, "", ""

    assert _cache(responder).try_acquire_build_lock(_TAG) is False


def test_try_acquire_build_lock_reclaims_a_stale_lock() -> None:
    state = {"mkdir_calls": 0}

    def responder(command: str) -> tuple[int | None, str, str]:
        if command.startswith("mkdir"):
            state["mkdir_calls"] += 1
            # First mkdir loses (lock present); the post-reclaim retry wins.
            return (1, "", "File exists") if state["mkdir_calls"] == 1 else (0, "", "")
        if "stat -c %Y" in command:
            return 0, "99999\n", ""  # far older than the TTL -> reclaimable
        return 0, "", ""

    cache = _cache(responder)
    assert cache.try_acquire_build_lock(_TAG) is True
    assert state["mkdir_calls"] == 2


def test_save_image_renders_atomic_save_and_prune() -> None:
    cache = _cache(lambda cmd: (0, "", ""))
    cache.save_image_from_slice(_TAG, vm_ssh_port=2200, transfer_key=_KEY)
    save_cmd = next(c for c in cache.slice_client.recorded if "docker save" in c)  # type: ignore[attr-defined]
    assert "docker save fct:minds-v0.3.2" in save_cmd
    assert "-p 2200 root@127.0.0.1" in save_cmd
    assert ".tar.tmp" in save_cmd and "mv" in save_cmd
    assert "-delete" in save_cmd  # prunes other tags


def test_save_image_raises_and_cleans_tmp_on_failure() -> None:
    def responder(command: str) -> tuple[int | None, str, str]:
        return (1, "", "boom") if "docker save" in command else (0, "", "")

    cache = _cache(responder)
    with pytest.raises(BoxImageCacheError):
        cache.save_image_from_slice(_TAG, vm_ssh_port=2200, transfer_key=_KEY)
    assert any(c.startswith("rm -f") and ".tar.tmp" in c for c in cache.slice_client.recorded)  # type: ignore[attr-defined]


def test_load_image_renders_cat_into_docker_load() -> None:
    cache = _cache(lambda cmd: (0, "", ""))
    cache.load_image_into_slice(_TAG, vm_ssh_port=2244, transfer_key=_KEY)
    load_cmd = next(c for c in cache.slice_client.recorded if "docker load" in c)  # type: ignore[attr-defined]
    assert load_cmd.startswith("cat ")
    assert "docker load" in load_cmd and "-p 2244 root@127.0.0.1" in load_cmd


def test_load_image_raises_on_failure() -> None:
    cache = _cache(lambda cmd: (1, "", "no such file"))
    with pytest.raises(BoxImageCacheError):
        cache.load_image_into_slice(_TAG, vm_ssh_port=2244, transfer_key=_KEY)


def test_check_free_disk_raises_when_below_required() -> None:
    cache = _cache(lambda cmd: (0, "5\n", ""))
    with pytest.raises(BoxImageCacheError):
        cache.check_free_disk(required_bytes=10)
    # Plenty free -> no raise.
    _cache(lambda cmd: (0, "10000000000\n", "")).check_free_disk(required_bytes=10)


def test_create_transfer_key_returns_generated_public_key() -> None:
    def responder(command: str) -> tuple[int | None, str, str]:
        if command.startswith("cat "):
            return 0, "ssh-ed25519 GENERATED\n", ""
        return 0, "", ""

    transfer_key = _cache(responder).create_transfer_key()
    assert transfer_key.public_key == "ssh-ed25519 GENERATED"
    assert transfer_key.private_key_path_on_box.startswith(f"{_CACHE_DIR}/.transfer-")
