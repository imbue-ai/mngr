import threading
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.minds.lima_image.data_types import LimaImagePrefetchState
from imbue.minds.lima_image.interfaces import LimaImageProgressSinkInterface


class FileLimaImageProgressSink(LimaImageProgressSinkInterface):
    """Persists ensure-image progress to a single JSON file under the env data root.

    The prefetch worker writes; the Lima create gate (a different thread, possibly
    a different request) reads. Writes are atomic (temp + rename) so a reader
    never observes a half-written file.
    """

    state_file: Path = Field(frozen=True, description="Path to the JSON progress/state file")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def write_state(self, state: LimaImagePrefetchState) -> None:
        with self._lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_file.with_suffix(".tmp")
            tmp_path.write_text(state.model_dump_json())
            tmp_path.rename(self.state_file)

    def read_state(self) -> LimaImagePrefetchState | None:
        with self._lock:
            if not self.state_file.exists():
                return None
            try:
                text = self.state_file.read_text()
            except OSError as exc:
                logger.warning("Failed to read lima image state file {}: {}", self.state_file, exc)
                return None
            try:
                return LimaImagePrefetchState.model_validate_json(text)
            except ValidationError as exc:
                logger.warning("Ignoring malformed lima image state file {}: {}", self.state_file, exc)
                return None
