"""What this install holds per workspace: this computer's own decisions about each workspace.

One atomically-written JSON file per workspace under
``<data_dir>/workspace_install_settings/``, holding a
:class:`WorkspaceInstallSettings` record, read into memory on first use and
served from memory after, behind a lock, with change callbacks, in the shape
of :mod:`folder_sync_store`: a file per workspace keeps one workspace's write
from racing another's and makes forgetting a destroyed workspace a file
deletion (``specs/workspace-microphone-permission/spec.md``, section 5.2).

It holds the client permissions, the decisions this computer made about
whether a workspace's pages may use its microphone. They never sync, never
enter the workspace, and never reach another computer (spec D1): this store
is the only place they exist.

An empty record is deleted rather than written, since an empty file and an
absent one mean the same thing and only one of them accumulates. A record a
newer build wrote (its ``settings_format`` ahead of this one) is read and
resolved from but never written, per the settings model's rule
(``specs/user-profile-state/spec.md``, section 4.3).
"""

import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.account_settings_store import SETTINGS_RECORD_TOO_NEW_MESSAGE
from imbue.minds.desktop_client.settings.schema import ClientPermissionDecision
from imbue.minds.desktop_client.settings.schema import ClientPermissionName
from imbue.minds.desktop_client.settings.schema import SUPPORTED_SETTINGS_FORMAT
from imbue.minds.desktop_client.settings.schema import WorkspaceInstallSettings
from imbue.minds.desktop_client.settings.schema import is_settings_format_supported
from imbue.minds.errors import WorkspaceInstallSettingsStoreError
from imbue.minds.errors import WorkspaceInstallSettingsTooNewError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.file_utils import atomic_write

OnWorkspaceInstallSettingsChangedCallback = Callable[[], None]


def _is_record_empty(record: WorkspaceInstallSettings) -> bool:
    """Whether the record says nothing: no decisions, and no key a newer build wrote that must be kept."""
    return not record.client_permissions and not record.model_extra


class WorkspaceInstallSettingsStore(MutableModel):
    """On-disk record of what this install holds for each workspace, read through an in-memory copy."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    records_dir: Path = Field(frozen=True, description="Directory holding one <agent_id>.json per workspace")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until the directory has been read once; loaded lazily so a store can be built before its dir exists.
    _records_by_agent: dict[str, WorkspaceInstallSettings] | None = PrivateAttr(default=None)
    _on_change_callbacks: list[OnWorkspaceInstallSettingsChangedCallback] = PrivateAttr(default_factory=list)

    def add_on_change_callback(self, callback: OnWorkspaceInstallSettingsChangedCallback) -> None:
        """Register a callback fired after any workspace's record is written or forgotten."""
        with self._lock:
            self._on_change_callbacks.append(callback)

    def _fire_on_change(self) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("A workspace-install-settings change callback failed: {}", e)

    def _record_path(self, agent_id: str) -> Path:
        return self.records_dir / f"{agent_id}.json"

    def _records_locked(self) -> dict[str, WorkspaceInstallSettings]:
        """The in-memory records, read from the directory on first use. Must hold ``self._lock``."""
        if self._records_by_agent is None:
            self._records_by_agent = self._read_all_from_disk()
        return self._records_by_agent

    def _read_all_from_disk(self) -> dict[str, WorkspaceInstallSettings]:
        """Every record on disk, skipping unreadable files with a warning."""
        records: dict[str, WorkspaceInstallSettings] = {}
        if not self.records_dir.is_dir():
            return records
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                agent_id = AgentId(path.stem)
            except ValueError:
                logger.warning("Ignoring workspace-install-settings file with a non-agent-id name: {}", path)
                continue
            record = self._read_from_disk(str(agent_id))
            if record is not None:
                records[str(agent_id)] = record
        return records

    def _read_from_disk(self, agent_id: str) -> WorkspaceInstallSettings | None:
        path = self._record_path(agent_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not read workspace-install-settings record {}: {}", path, e)
            return None
        try:
            return WorkspaceInstallSettings.model_validate_json(raw)
        except ValueError as e:
            logger.warning("Workspace-install-settings record {} is not valid; ignoring it: {}", path, e)
            return None

    def _write_locked(self, agent_id: str, record: WorkspaceInstallSettings) -> None:
        """Write one workspace's record to disk and memory, deleting it when empty. Must hold ``self._lock``."""
        path = self._record_path(agent_id)
        if _is_record_empty(record):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                raise WorkspaceInstallSettingsStoreError(
                    f"Could not delete workspace-install-settings record {path}: {e}"
                ) from e
            self._records_locked().pop(agent_id, None)
            return
        try:
            atomic_write(path, record.model_dump_json(indent=2))
        except OSError as e:
            raise WorkspaceInstallSettingsStoreError(
                f"Could not write workspace-install-settings record {path}: {e}"
            ) from e
        self._records_locked()[agent_id] = record

    def read(self, agent_id: str) -> WorkspaceInstallSettings:
        """What this install holds for one workspace; an empty record when it holds nothing."""
        with self._lock:
            return self._records_locked().get(agent_id, WorkspaceInstallSettings())

    def set_client_permission(
        self, agent_id: str, name: ClientPermissionName, decision: ClientPermissionDecision | None
    ) -> WorkspaceInstallSettings:
        """Record this computer's decision about one permission for one workspace; None deletes the entry.

        Returns the record as written. Raises
        :class:`WorkspaceInstallSettingsTooNewError` for a record a newer build
        wrote, which this build must not rewrite, and
        :class:`WorkspaceInstallSettingsStoreError` when the file cannot be
        written, which the caller reports rather than swallowing: a decision
        shown but not stored would come back as undecided on the next launch.
        """
        with self._lock:
            existing = self._records_locked().get(agent_id, WorkspaceInstallSettings())
            if not is_settings_format_supported(existing.settings_format):
                raise WorkspaceInstallSettingsTooNewError(SETTINGS_RECORD_TOO_NEW_MESSAGE)
            kept = {
                held_name: held for held_name, held in existing.client_permissions.items() if held_name != name.value
            }
            if decision is not None:
                kept[name.value] = decision
            updated = existing.model_copy_update(
                to_update(existing.field_ref().client_permissions, kept),
                to_update(existing.field_ref().settings_format, SUPPORTED_SETTINGS_FORMAT),
            )
            if updated == existing:
                return existing
            self._write_locked(agent_id, updated)
        self._fire_on_change()
        return updated

    def forget_workspace(self, agent_id: str) -> None:
        """Drop everything recorded for one workspace, for a workspace that is gone."""
        with self._lock:
            if self._records_locked().get(agent_id) is None:
                return
            self._write_locked(agent_id, WorkspaceInstallSettings())
        self._fire_on_change()
