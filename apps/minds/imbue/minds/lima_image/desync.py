import os
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.minds.errors import LimaImageDownloadError
from imbue.minds.lima_image.interfaces import ImageChunkStoreInterface
from imbue.minds.lima_image.interfaces import ProcessOutputCallback

# Generous ceiling for a full multi-GB assembly over the network. desync itself
# retries transient chunk fetches (-e) within this window.
DESYNC_EXTRACT_TIMEOUT_SECONDS: Final[float] = 3600.0

# Per-chunk network retry count handed to desync via -e. desync applies its own
# (default) linear backoff between attempts; we only override the retry count.
DESYNC_ERROR_RETRY_COUNT: Final[int] = 5


def _get_desync_binary() -> str:
    """Resolve the desync path.

    Prefers ``MINDS_DESYNC_BINARY`` -- the bundled binary that ships in
    ``resources/desync/desync``. Electron's backend.js sets it in both dev and
    packaged mode whenever that binary is staged; tests get it from the session
    conftest. Falls back to ``"desync"`` (PATH lookup) when unset.
    """
    return os.environ.get("MINDS_DESYNC_BINARY") or "desync"


class DesyncImageChunkStore(ImageChunkStoreInterface):
    """Assembles raw images via the ``desync`` CLI from an HTTP(S) chunk store."""

    desync_binary: str = Field(
        default_factory=_get_desync_binary, frozen=True, description="Path/name of the desync executable"
    )
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Concurrency group used to run the desync subprocess"
    )

    def extract_image(
        self,
        *,
        index_file: Path,
        chunk_store_url: str,
        output_file: Path,
        local_cache_dir: Path,
        seed_index_file: Path | None,
        seed_blob_file: Path | None,
        on_output: ProcessOutputCallback | None,
    ) -> None:
        local_cache_dir.mkdir(parents=True, exist_ok=True)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        # -k keeps the partial output on error so a re-run resumes instead of
        # re-fetching completed chunks; -c is a local chunk cache that survives
        # across runs; -e bounds transient network retries (desync applies its
        # own default linear backoff between attempts).
        command: list[str] = [
            self.desync_binary,
            "extract",
            "-k",
            "-s",
            chunk_store_url,
            "-c",
            str(local_cache_dir),
            "-e",
            str(DESYNC_ERROR_RETRY_COUNT),
        ]
        if seed_index_file is not None and seed_blob_file is not None:
            # desync expects <index>:<blob> when the blob name doesn't match the
            # index basename, which is our case (we name them by version).
            command.extend(["--seed", f"{seed_index_file}:{seed_blob_file}"])
        command.extend([str(index_file), str(output_file)])

        cg = self.concurrency_group.make_concurrency_group(name="desync-extract")
        try:
            with cg:
                finished = cg.run_process_to_completion(
                    command,
                    timeout=DESYNC_EXTRACT_TIMEOUT_SECONDS,
                    is_checked_after=False,
                    on_output=on_output,
                )
        except (OSError, ConcurrencyGroupError) as exc:
            raise LimaImageDownloadError(f"Failed to launch desync extract: {exc}") from exc
        if finished.is_timed_out:
            raise LimaImageDownloadError(f"desync extract timed out after {int(DESYNC_EXTRACT_TIMEOUT_SECONDS)}s")
        if finished.returncode != 0:
            raise LimaImageDownloadError(f"desync extract exited {finished.returncode}: {finished.stderr.strip()}")
        logger.debug("Assembled raw image at {} from chunk store {}", output_file, chunk_store_url)
