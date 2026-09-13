import shlex
from typing import Final
from uuid import uuid4

from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.slices.box_image_cache import BUILD_LOCK_TTL_SECONDS
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.box_image_cache import TransferKey
from imbue.mngr_imbue_cloud.slices.box_image_cache import box_image_tar_name
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

# SSH options for the box's loopback connection into a freshly-carved slice's
# VM-root sshd: the slice is operator-controlled during the bake and reached over
# the box's own loopback (the box-forwarded VM ssh port), so we accept its
# (unpinned) host key rather than fail-closed -- there is no persistent key to pin.
_SLICE_LOOPBACK_SSH_OPTS: Final[str] = (
    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30"
)
# A ~11 GiB docker save/load over the box loopback is bounded but not instant.
_TRANSFER_TIMEOUT_SECONDS: Final[float] = 1200.0
_SHORT_TIMEOUT_SECONDS: Final[float] = 60.0


class LimaBoxImageCache(BoxImageCacheInterface):
    """BoxImageCache backed by files + box-local docker save/load on a bare-metal box.

    Every operation runs on the box over SSH via the lima client's ``run_on_box``.
    The box has no Docker daemon; it only stores the ``docker save`` tar and pipes
    it to/from the slice's VM-root dockerd over the box's own loopback to the
    box-forwarded VM ssh port.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    slice_client: LimaSliceVpsClient = Field(frozen=True, description="Runs commands on the box over SSH")
    cache_dir: str = Field(frozen=True, description="Box dir holding the cached image tar(s), lock, and transfer keys")

    def _run(
        self, command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        return self.slice_client.run_on_box(command, timeout=timeout, label=label, is_streaming=is_streaming)

    def _tar_path(self, image_tag: str) -> str:
        return f"{self.cache_dir}/{box_image_tar_name(image_tag)}"

    def _lock_path(self, image_tag: str) -> str:
        return f"{self.cache_dir}/.lock-{box_image_tar_name(image_tag)}.d"

    def has_tar(self, image_tag: str) -> bool:
        rc, _out, _err = self._run(
            f"test -f {shlex.quote(self._tar_path(image_tag))}", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-has-tar"
        )
        return rc == 0

    def _read_build_lock_age_seconds(self, image_tag: str) -> int | None:
        """Age of the build-lock dir per the box's own clock, or None when unreadable.

        A lock that vanished between the caller's existence probe and this read
        yields a huge age (the ``|| echo 0`` epoch fallback), which every caller
        correctly treats as stale/absent.
        """
        lock = self._lock_path(image_tag)
        age_rc, age_out, _err = self._run(
            f"echo $(( $(date +%s) - $(stat -c %Y {shlex.quote(lock)} 2>/dev/null || echo 0) ))",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="cache-lock-age",
        )
        if age_rc != 0:
            return None
        try:
            return int(age_out.strip())
        except ValueError:
            return None

    def try_acquire_build_lock(self, image_tag: str) -> bool:
        # `mkdir -p` the cache dir first: on a box whose prep predates the
        # current cache dir name, a bare `mkdir <lock>` would fail on the
        # missing parent and read as "lock held" forever.
        lock = self._lock_path(image_tag)
        rc, _out, _err = self._run(
            f"mkdir -p {shlex.quote(self.cache_dir)} && mkdir {shlex.quote(lock)}",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="cache-lock",
        )
        if rc == 0:
            return True
        # The lock dir exists; reclaim it only if it is older than the build TTL (its
        # builder almost certainly died mid-seed), otherwise a seed is in flight.
        age_seconds = self._read_build_lock_age_seconds(image_tag)
        if age_seconds is None:
            return False
        if age_seconds <= BUILD_LOCK_TTL_SECONDS:
            return False
        self._run(f"rm -rf {shlex.quote(lock)}", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-lock-reclaim")
        retry_rc, _out, _err = self._run(
            f"mkdir {shlex.quote(lock)}", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-lock-retry"
        )
        return retry_rc == 0

    def is_build_locked(self, image_tag: str) -> bool:
        rc, _out, _err = self._run(
            f"test -d {shlex.quote(self._lock_path(image_tag))}",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="cache-lock-probe",
        )
        if rc != 0:
            return False
        # Mirror try_acquire_build_lock: an unreadable age counts as held (a seed
        # is presumably in flight), a stale age counts as not held (its builder
        # died; the next contender will reclaim it).
        age_seconds = self._read_build_lock_age_seconds(image_tag)
        if age_seconds is None:
            return True
        return age_seconds <= BUILD_LOCK_TTL_SECONDS

    def release_build_lock(self, image_tag: str) -> None:
        self._run(
            f"rm -rf {shlex.quote(self._lock_path(image_tag))}", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-unlock"
        )

    def wait_for_tar(self, image_tag: str, *, timeout_seconds: int) -> bool:
        tar = shlex.quote(self._tar_path(image_tag))
        lock = shlex.quote(self._lock_path(image_tag))
        # Also exit early (non-zero) when the build lock disappears without the tar:
        # the seeder died or its build failed, so the caller should re-contend the
        # lock right away instead of blocking out the full window. The seeder
        # publishes the tar (atomic mv) BEFORE releasing the lock, so a vanished
        # lock without a tar can only mean a dead/failed seed -- and the caller
        # re-checks has_tar before acting on a False return, closing the race where
        # the tar lands between our two probes.
        poll_script = f"until test -f {tar}; do test -d {lock} || exit 1; sleep 5; done"
        poll = f"timeout {int(timeout_seconds)} bash -c {shlex.quote(poll_script)}"
        rc, _out, _err = self._run(poll, timeout=float(timeout_seconds + 60), label="cache-wait-tar")
        return rc == 0

    def check_free_disk(self, required_bytes: int) -> None:
        rc, out, err = self._run(
            f"df -PB1 {shlex.quote(self.cache_dir)} | tail -1 | awk '{{print $4}}'",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="cache-df",
        )
        if rc != 0:
            raise BoxImageCacheError(f"could not check free disk on box cache dir {self.cache_dir}: {err.strip()}")
        try:
            available_bytes = int(out.strip())
        except ValueError as exc:
            raise BoxImageCacheError(f"unexpected df output for {self.cache_dir}: {out.strip()!r}") from exc
        if available_bytes < required_bytes:
            raise BoxImageCacheError(
                f"insufficient disk on box for the DEFAULT_WORKSPACE_TEMPLATE image tar: need {required_bytes} bytes, "
                f"have {available_bytes} free in {self.cache_dir}"
            )

    def create_transfer_key(self) -> TransferKey:
        key_path = f"{self.cache_dir}/.transfer-{uuid4().hex}"
        gen_rc, _out, gen_err = self._run(
            f"ssh-keygen -t ed25519 -N '' -q -f {shlex.quote(key_path)}",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="cache-keygen",
        )
        if gen_rc != 0:
            raise BoxImageCacheError(f"failed to generate transfer key on box: {gen_err.strip()}")
        pub_rc, pub_out, pub_err = self._run(
            f"cat {shlex.quote(key_path)}.pub", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-keycat"
        )
        if pub_rc != 0:
            self._run(
                f"rm -f {shlex.quote(key_path)} {shlex.quote(key_path)}.pub",
                timeout=_SHORT_TIMEOUT_SECONDS,
                label="cache-keyrm",
            )
            raise BoxImageCacheError(f"failed to read transfer public key on box: {pub_err.strip()}")
        return TransferKey(private_key_path_on_box=key_path, public_key=pub_out.strip())

    def destroy_transfer_key(self, transfer_key: TransferKey) -> None:
        quoted = shlex.quote(transfer_key.private_key_path_on_box)
        self._run(f"rm -f {quoted} {quoted}.pub", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-keyrm")

    def save_image_from_slice(self, image_tag: str, *, vm_ssh_port: int, transfer_key: TransferKey) -> None:
        tar_path = self._tar_path(image_tag)
        tmp_path = f"{tar_path}.tmp"
        key = shlex.quote(transfer_key.private_key_path_on_box)
        remote_save = (
            f"ssh -i {key} {_SLICE_LOOPBACK_SSH_OPTS} -p {int(vm_ssh_port)} root@127.0.0.1 "
            f"{shlex.quote('docker save ' + image_tag)}"
        )
        # Clean any stale .tmp from an interrupted prior save, stream the save to a
        # temp file, atomically rename it into place, then prune every other tag's
        # tar (the box keeps exactly the current tag).
        command = (
            f"rm -f {shlex.quote(tmp_path)}; "
            f"{remote_save} > {shlex.quote(tmp_path)} && mv {shlex.quote(tmp_path)} {shlex.quote(tar_path)} && "
            f"find {shlex.quote(self.cache_dir)} -maxdepth 1 -name '*.tar' "
            f"! -name {shlex.quote(box_image_tar_name(image_tag))} -delete"
        )
        rc, _out, err = self._run(command, timeout=_TRANSFER_TIMEOUT_SECONDS, label="cache-save", is_streaming=True)
        if rc != 0:
            self._run(f"rm -f {shlex.quote(tmp_path)}", timeout=_SHORT_TIMEOUT_SECONDS, label="cache-save-cleanup")
            raise BoxImageCacheError(f"failed to save image {image_tag} to box tar: {err.strip()}")

    def load_image_into_slice(self, image_tag: str, *, vm_ssh_port: int, transfer_key: TransferKey) -> None:
        tar_path = shlex.quote(self._tar_path(image_tag))
        key = shlex.quote(transfer_key.private_key_path_on_box)
        remote_load = f"ssh -i {key} {_SLICE_LOOPBACK_SSH_OPTS} -p {int(vm_ssh_port)} root@127.0.0.1 'docker load'"
        command = f"cat {tar_path} | {remote_load}"
        rc, _out, err = self._run(command, timeout=_TRANSFER_TIMEOUT_SECONDS, label="cache-load", is_streaming=True)
        if rc != 0:
            raise BoxImageCacheError(f"failed to load image {image_tag} from box tar into slice: {err.strip()}")
