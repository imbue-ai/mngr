from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.interfaces import SignatureVerifierInterface

# Verification is a local hash + signature check; it should be near-instant.
MINISIGN_VERIFY_TIMEOUT_SECONDS: Final[float] = 30.0


class MinisignSignatureVerifier(SignatureVerifierInterface):
    """Verifies detached minisign signatures via the ``minisign`` CLI."""

    minisign_binary: str = Field(default="minisign", frozen=True, description="Path/name of the minisign executable")
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Concurrency group used to run the minisign subprocess"
    )

    def verify_detached(self, *, signed_file: Path, signature_file: Path, public_key: str) -> None:
        # -P passes the public key inline so we never have to materialize a
        # pubkey file; -x points at the detached signature; -V verifies.
        command: list[str] = [
            self.minisign_binary,
            "-V",
            "-P",
            public_key,
            "-x",
            str(signature_file),
            "-m",
            str(signed_file),
        ]
        cg = self.concurrency_group.make_concurrency_group(name="minisign-verify")
        try:
            with cg:
                finished = cg.run_process_to_completion(
                    command,
                    timeout=MINISIGN_VERIFY_TIMEOUT_SECONDS,
                    is_checked_after=False,
                )
        except (OSError, ConcurrencyGroupError) as exc:
            raise LimaImageVerificationError(f"Failed to launch minisign verify: {exc}") from exc
        if finished.is_timed_out:
            raise LimaImageVerificationError("minisign verify timed out")
        if finished.returncode != 0:
            raise LimaImageVerificationError(
                f"Signature verification failed for {signed_file.name}: {finished.stderr.strip()}"
            )
        logger.debug("Verified minisign signature for {}", signed_file)
