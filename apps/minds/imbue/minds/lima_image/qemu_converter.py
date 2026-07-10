import os
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.minds.errors import LimaImageToolError
from imbue.minds.lima_image.interfaces import ImageFormatConverterInterface

# Conversion is local disk IO over a multi-GB image; generous ceiling.
QEMU_CONVERT_TIMEOUT_SECONDS: Final[float] = 1800.0


def resolve_qemu_img_binary() -> str:
    """Resolve the qemu-img path.

    Prefers ``MINDS_QEMU_IMG_BINARY`` -- the bundled binary that ships in
    ``resources/qemu/bin/qemu-img``. Electron's backend.js sets it in the
    packaged app; tests get it from the session conftest. Falls back to
    ``"qemu-img"`` (PATH lookup) when unset, so a dev machine with a
    Homebrew qemu still works.
    """
    return os.environ.get("MINDS_QEMU_IMG_BINARY") or "qemu-img"


class QemuImageFormatConverter(ImageFormatConverterInterface):
    """Converts between raw and qcow2 via the ``qemu-img`` CLI."""

    qemu_img_binary: str = Field(
        default_factory=resolve_qemu_img_binary, frozen=True, description="Path/name of the qemu-img executable"
    )
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Concurrency group used to run the qemu-img subprocess"
    )

    def convert_raw_to_qcow2(self, *, raw_file: Path, qcow2_file: Path) -> None:
        self._convert(input_format="raw", output_format="qcow2", input_file=raw_file, output_file=qcow2_file)

    def convert_qcow2_to_raw(self, *, qcow2_file: Path, raw_file: Path) -> None:
        self._convert(input_format="qcow2", output_format="raw", input_file=qcow2_file, output_file=raw_file)

    def _convert(self, *, input_format: str, output_format: str, input_file: Path, output_file: Path) -> None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        command: list[str] = [
            self.qemu_img_binary,
            "convert",
            "-f",
            input_format,
            "-O",
            output_format,
            str(input_file),
            str(output_file),
        ]
        cg = self.concurrency_group.make_concurrency_group(name="qemu-img-convert")
        try:
            with cg:
                finished = cg.run_process_to_completion(
                    command,
                    timeout=QEMU_CONVERT_TIMEOUT_SECONDS,
                    is_checked_after=False,
                )
        except (OSError, ConcurrencyGroupError) as exc:
            raise LimaImageToolError(f"Failed to launch qemu-img convert: {exc}") from exc
        if finished.is_timed_out:
            raise LimaImageToolError("qemu-img convert timed out")
        if finished.returncode != 0:
            raise LimaImageToolError(f"qemu-img convert exited {finished.returncode}: {finished.stderr.strip()}")
        logger.debug("Converted {} ({}) -> {} ({})", input_file, input_format, output_file, output_format)
