"""Local replica + sync engine for per-account workspace records.

The connector holds one record per (account, host): plaintext metadata (name,
color, provider, location, lifecycle state) plus an opaque secrets blob
encrypted under the account's DEK (see ``dek_store``). This module owns the
minds side of that: a per-account on-disk replica (the offline cache and
dirty-queue), record assembly from discovery + the canonical restic env +
best-effort SSH key material, CAS push/pull through the ``mngr imbue_cloud
sync`` CLI, and the post-discovery reconcile (one-shot legacy-association
migration, dirty pushes, metadata refresh, definitively-absent tombstoning).

Association IS record existence: a workspace belongs to the account whose
replica holds an ACTIVE record for it; private workspaces have no record and
nothing about them ever leaves the machine. The settings-page associate /
disassociate operations push synchronously (they require connectivity);
everything else queues via dirty replica rows for the reconcile to push.
"""

import hashlib
import json
import shutil
import socket
import threading
import time
import tomllib
from base64 import b64decode
from base64 import b64encode
from collections.abc import Sequence
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.imbue_common.secret_wrapping import decrypt_secrets
from imbue.imbue_common.secret_wrapping import encrypt_secrets
from imbue.minds.bootstrap import imbue_cloud_provider_name_for_account
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import dek_store
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudSyncConflictCliError
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.primitives import AgentId

_RECORDS_DIRNAME = "workspace_records"
_LEGACY_ASSOCIATIONS_FILENAME = "workspace_associations.json"
# The pre-associations-file layout: full identity records keyed by user_id,
# each carrying a "workspace_ids" list. Only consulted when the newer
# associations file does not exist (mirroring the retired fallback reader).
_LEGACY_SESSIONS_FILENAME = "sessions.json"
_LEGACY_RETIRED_SUFFIX = ".pre-sync"
# Providers whose hosts any signed-in device can reach/modify; their records
# carry no hosting_device_id (concurrent writers are resolved by CAS).
_CLOUD_PROVIDER_PREFIX = "imbue_cloud_"

RECORD_STATE_ACTIVE = "active"
RECORD_STATE_DESTROYED = "destroyed"


def is_cloud_provider_kind(provider_kind: str) -> bool:
    """True for provider kinds whose hosts any signed-in device can reach (imbue_cloud rows)."""
    return provider_kind.startswith(_CLOUD_PROVIDER_PREFIX)


# File names inside an imbue_cloud provider instance's per-host state dir
# (``providers/imbue_cloud/<instance>/hosts/<host_id>/``). Mirrored from the
# plugin's layout by convention -- minds deliberately talks to the plugin only
# via the CLI, so these names are duplicated rather than imported.
_SSH_KEY_FILENAME = "ssh_key"
_SSH_PUBLIC_KEY_FILENAME = "ssh_key.pub"
_KNOWN_HOSTS_FILENAME = "known_hosts"
_LEASE_META_FILENAME = "lease.json"
# The orphan sweep never touches a per-host dir modified more recently than
# this: a lease in progress creates the keypair before it writes lease.json,
# and the sweep (on the scheduler thread) must not win that race.
_ORPHAN_SWEEP_GRACE_SECONDS = 3600.0


class WorkspaceSecretsPayload(FrozenModel):
    """Decrypted contents of a record's encrypted_secrets blob."""

    restic_env: str | None = Field(default=None, description="Canonical restic.env text (when backups configured)")
    ssh_private_key: str | None = Field(default=None, description="Private key that grants SSH access to the host")
    ssh_known_hosts: str | None = Field(default=None, description="known_hosts entries pinning the host's public key")


class BuiltRecordSecrets(FrozenModel):
    """An encrypted secrets blob paired with the digest of its plaintext."""

    encrypted: str = Field(description="Base64 AEAD blob under the account DEK")
    content_hash: str = Field(description="Digest of the plaintext payload (see secrets_payload_content_hash)")


def secrets_payload_content_hash(payload: WorkspaceSecretsPayload) -> str:
    """Stable digest of a secrets payload's plaintext.

    Encryption is randomized (the same plaintext yields a fresh ciphertext
    every time), so ciphertext comparison can never answer "did the material
    change?" -- this plaintext digest is what producers track to decide when a
    re-push is warranted.
    """
    return hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()


class ReplicaRecord(FrozenModel):
    """One workspace record as held in the local replica (wire fields + the dirty flag)."""

    host_id: str = Field(description="Host the workspace is on (PK with the account)")
    agent_id: str = Field(description="Logical workspace id")
    display_name: str = Field(default="", description="Workspace display name")
    color: str | None = Field(default=None, description="Workspace accent color")
    provider_kind: str = Field(default="", description="mngr provider instance name")
    hosting_device_id: str | None = Field(default=None, description="Hosting install (None for cloud rows)")
    device_label: str = Field(default="", description="Human-readable device name")
    state: str = Field(default=RECORD_STATE_ACTIVE, description="'active' or 'destroyed'")
    restored_from_host_id: str | None = Field(default=None, description="Lineage link for restorations")
    encrypted_secrets: str | None = Field(default=None, description="Base64 AEAD blob under the account DEK")
    revision: int = Field(default=0, description="Last server-acknowledged revision (0 = never pushed)")
    is_dirty: bool = Field(default=False, description="Local changes not yet pushed")
    secrets_content_hash: str | None = Field(
        default=None,
        description=(
            "Local-only: digest of the plaintext secrets this device last contributed "
            "(never synced; drives the producer's changed-material re-push)"
        ),
    )

    def to_wire(self, push_revision: int) -> dict[str, object]:
        """Render the record for a CLI push at the given target revision."""
        return {
            "host_id": self.host_id,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "color": self.color,
            "provider_kind": self.provider_kind,
            "hosting_device_id": self.hosting_device_id,
            "device_label": self.device_label,
            "state": self.state,
            "restored_from_host_id": self.restored_from_host_id,
            "encrypted_secrets": self.encrypted_secrets,
            "revision": push_revision,
        }


def replica_record_from_wire(wire: dict[str, object]) -> ReplicaRecord:
    """Build a clean (non-dirty) replica row from a server wire record."""
    return ReplicaRecord(
        host_id=str(wire.get("host_id", "")),
        agent_id=str(wire.get("agent_id", "")),
        display_name=str(wire.get("display_name", "")),
        color=str(wire["color"]) if wire.get("color") is not None else None,
        provider_kind=str(wire.get("provider_kind", "")),
        hosting_device_id=(str(wire["hosting_device_id"]) if wire.get("hosting_device_id") is not None else None),
        device_label=str(wire.get("device_label", "")),
        state=str(wire.get("state", RECORD_STATE_ACTIVE)),
        restored_from_host_id=(
            str(wire["restored_from_host_id"]) if wire.get("restored_from_host_id") is not None else None
        ),
        encrypted_secrets=(str(wire["encrypted_secrets"]) if wire.get("encrypted_secrets") is not None else None),
        revision=int(str(wire.get("revision", 0))),
        is_dirty=False,
    )


def read_device_id(mngr_host_dir: Path) -> str:
    """Return this install's device id (the minds env's mngr host_id file), or '' when absent."""
    path = mngr_host_dir / "host_id"
    if not path.is_file():
        return ""
    try:
        return path.read_text().strip()
    except OSError as e:
        logger.warning("Could not read the mngr host_id file at {}: {}", path, e)
        return ""


def read_device_label() -> str:
    """Return a human-readable label for this device (the hostname)."""
    return socket.gethostname()


def _resolve_mngr_profile_dir(mngr_host_dir: Path) -> Path | None:
    """Resolve ``<host_dir>/profiles/<active-profile>``, or None when mngr is uninitialized.

    Mirrors the plugin's ``get_active_profile_dir`` without importing the
    plugin (minds deliberately talks to it only via the CLI).
    """
    config_path = mngr_host_dir / "config.toml"
    if not config_path.is_file():
        return None
    try:
        root_config = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read the mngr root config at {}: {}", config_path, e)
        return None
    profile_id = root_config.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return None
    return mngr_host_dir / "profiles" / profile_id


def _read_optional_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text()
    except OSError as e:
        logger.warning("Could not read SSH material at {}: {}", path, e)
        return None


def collect_ssh_key_material(mngr_host_dir: Path, provider_kind: str, host_id: str) -> tuple[str | None, str | None]:
    """Best-effort collection of the (private key, known_hosts) that grant access to a host.

    Looks for a per-host keypair under any provider instance's state dir
    (``providers/*/*/hosts/<host_id>/ssh_key``, the imbue_cloud layout), then
    falls back to the lima provider-wide root key for lima hosts. Providers
    without a recognizable key layout sync without SSH material -- the record
    still carries the backup env, which is the DR-critical part.
    """
    profile_dir = _resolve_mngr_profile_dir(mngr_host_dir)
    if profile_dir is None:
        return None, None
    providers_dir = profile_dir / "providers"
    if not providers_dir.is_dir():
        return None, None
    for key_path in sorted(providers_dir.glob(f"*/*/hosts/{host_id}/ssh_key")):
        known_hosts = _read_optional_text(key_path.parent / "known_hosts")
        private_key = _read_optional_text(key_path)
        if private_key is not None:
            return private_key, known_hosts
    if provider_kind.startswith("lima"):
        private_key = _read_optional_text(providers_dir / "lima" / "lima" / "keys" / "root_ssh_key")
        known_hosts = _read_optional_text(providers_dir / "lima" / "lima" / "keys" / "hosts")
        if private_key is not None:
            return private_key, known_hosts
    return None, None


def derive_openssh_public_key_line(private_key_text: str) -> str | None:
    """Derive the OpenSSH public-key line from a private key, or None when unparseable.

    The materializer must write ``ssh_key.pub`` alongside the private key:
    mngr's ``load_or_create_ssh_keypair`` regenerates the whole pair when
    either half is missing, which would silently clobber a materialized key.

    Both container formats found in mngr profiles are handled: traditional /
    PKCS#8 PEM (mngr's client keypairs are PEM RSA keys, ``-----BEGIN RSA
    PRIVATE KEY-----``) and the OpenSSH format (``-----BEGIN OPENSSH PRIVATE
    KEY-----``) -- ``cryptography`` needs a different loader for each.
    """
    key_bytes = private_key_text.encode("utf-8")
    last_error: Exception | None = None
    for load_private_key in (serialization.load_pem_private_key, serialization.load_ssh_private_key):
        try:
            private_key = load_private_key(key_bytes, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as e:
            last_error = e
            continue
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH
        )
        return public_bytes.decode("utf-8")
    logger.warning("Could not parse a synced SSH private key: {}", last_error)
    return None


def merge_known_hosts_text(existing_text: str | None, synced_text: str | None) -> str | None:
    """Merge synced known_hosts lines into the existing text, add-if-absent per line.

    Synced entries are only ever appended -- the connector-fed pins that
    discovery records stay authoritative (a synced copy can be stale after a
    legitimate host rebuild). Returns the merged text, or None when there is
    nothing new to write.
    """
    if not synced_text:
        return None
    existing_lines: list[str] = [line for line in (existing_text or "").splitlines() if line.strip()]
    known_lines: set[str] = set(existing_lines)
    added_lines: list[str] = []
    for line in synced_text.splitlines():
        if line.strip() and line not in known_lines:
            known_lines.add(line)
            added_lines.append(line)
    if not added_lines:
        return None
    return "\n".join(existing_lines + added_lines) + "\n"


def _write_private_text_file(path: Path, content: str) -> None:
    """Write a secret file atomically (tmp + rename) with owner-only permissions."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content)
    tmp_path.chmod(0o600)
    tmp_path.rename(path)


def _materialize_ssh_files(host_dir: Path, payload: WorkspaceSecretsPayload, public_key_line: str) -> bool:
    """Write the synced private key (+ derived .pub) and merge known_hosts into ``host_dir``.

    Idempotent compare-and-write: unchanged material touches nothing, so the
    caller can use the return value to decide whether discovery needs a
    bounce. Raises OSError on filesystem failures.
    """
    assert payload.ssh_private_key is not None, "caller guarantees SSH material is present"
    host_dir.mkdir(parents=True, exist_ok=True)
    is_changed = False

    # The private key (and its derived public half -- mngr regenerates the
    # whole pair when either file is missing, so both must exist together).
    key_path = host_dir / _SSH_KEY_FILENAME
    public_key_path = host_dir / _SSH_PUBLIC_KEY_FILENAME
    if _read_optional_text(key_path) != payload.ssh_private_key:
        _write_private_text_file(key_path, payload.ssh_private_key)
        _write_private_text_file(public_key_path, public_key_line)
        is_changed = True
    elif _read_optional_text(public_key_path) != public_key_line:
        _write_private_text_file(public_key_path, public_key_line)
        is_changed = True
    else:
        # Both halves already match the synced material.
        pass

    # known_hosts entries merge add-if-absent (connector-fed pins stay).
    known_hosts_path = host_dir / _KNOWN_HOSTS_FILENAME
    merged_known_hosts = merge_known_hosts_text(_read_optional_text(known_hosts_path), payload.ssh_known_hosts)
    if merged_known_hosts is not None:
        _write_private_text_file(known_hosts_path, merged_known_hosts)
        is_changed = True
    return is_changed


class WorkspaceRecordStore(MutableModel):
    """Owns the per-account replica files and every record push/pull.

    Thread-safe via one internal lock (the SSE list-build path, mutation
    handlers, and the reconcile all touch the replica). CLI calls happen
    outside the lock so a slow connector round-trip never blocks readers.
    """

    paths: WorkspacePaths = Field(frozen=True, description="Minds data dir (replica + keys live under it)")
    mngr_host_dir: Path | None = Field(
        default=None,
        frozen=True,
        description=(
            "The minds env's mngr host dir (SSH key material + device id live under it). "
            "None falls back to paths.mngr_host_dir (the default-env convention)."
        ),
    )
    cli: ImbueCloudCli | None = Field(default=None, frozen=True, description="Transport; None disables pushes/pulls")
    device_id: str = Field(frozen=True, description="This install's mngr host_id (record provenance)")
    device_label: str = Field(frozen=True, description="This device's human-readable name")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records_by_user_id: dict[str, dict[str, ReplicaRecord]] = PrivateAttr(default_factory=dict)
    _is_loaded: bool = PrivateAttr(default=False)
    # Accounts whose server-side key bundle presence was confirmed this
    # process run (see _ensure_bundle_uploaded). In-memory on purpose: one
    # redundant GET per account per app launch is cheap, and no on-disk
    # marker can go stale.
    _bundle_confirmed_user_ids: set[str] = PrivateAttr(default_factory=set)
    # Last SSH-materialization failure per workspace agent id, surfaced as a
    # tile chip. In-memory on purpose: it is recomputed by every pass, and a
    # restart naturally retries.
    _ssh_material_error_by_agent_id: dict[str, str] = PrivateAttr(default_factory=dict)

    # -- Replica persistence -------------------------------------------------

    def _effective_mngr_host_dir(self) -> Path:
        return self.mngr_host_dir if self.mngr_host_dir is not None else self.paths.mngr_host_dir

    def _records_dir(self) -> Path:
        return self.paths.data_dir / _RECORDS_DIRNAME

    def _replica_path(self, user_id: str) -> Path:
        return self._records_dir() / f"{user_id}.json"

    def _load_unlocked(self) -> None:
        if self._is_loaded:
            return
        self._records_by_user_id = {}
        records_dir = self._records_dir()
        if records_dir.is_dir():
            for path in sorted(records_dir.glob("*.json")):
                user_id = path.stem
                try:
                    raw = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning("Ignoring unreadable workspace-record replica {}: {}", path, e)
                    continue
                entries = raw.get("records", []) if isinstance(raw, dict) else []
                by_host: dict[str, ReplicaRecord] = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        record = ReplicaRecord.model_validate(entry)
                    except ValueError as e:
                        logger.warning("Skipping malformed replica record in {}: {}", path, e)
                        continue
                    by_host[record.host_id] = record
                self._records_by_user_id[user_id] = by_host
        self._is_loaded = True

    def _save_unlocked(self, user_id: str) -> None:
        records_dir = self._records_dir()
        records_dir.mkdir(parents=True, exist_ok=True)
        path = self._replica_path(user_id)
        by_host = self._records_by_user_id.get(user_id, {})
        payload = {"records": [record.model_dump(mode="json") for record in by_host.values()]}
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.chmod(0o600)
        tmp_path.rename(path)

    def _set_record_unlocked(self, user_id: str, record: ReplicaRecord) -> None:
        self._load_unlocked()
        self._records_by_user_id.setdefault(user_id, {})[record.host_id] = record
        self._save_unlocked(user_id)

    def _drop_record_unlocked(self, user_id: str, host_id: str) -> None:
        self._load_unlocked()
        by_host = self._records_by_user_id.get(user_id, {})
        if host_id in by_host:
            del by_host[host_id]
            self._save_unlocked(user_id)

    # -- Read API -------------------------------------------------------------

    def list_records(self, user_id: str) -> list[ReplicaRecord]:
        with self._lock:
            self._load_unlocked()
            return list(self._records_by_user_id.get(user_id, {}).values())

    def list_all_records(self) -> dict[str, list[ReplicaRecord]]:
        """All replica records keyed by user_id (every account ever synced on this device)."""
        with self._lock:
            self._load_unlocked()
            return {user_id: list(by_host.values()) for user_id, by_host in self._records_by_user_id.items()}

    def associations_view(self) -> dict[str, list[str]]:
        """``user_id -> [agent_id, ...]`` for ACTIVE records (the association source of truth)."""
        result: dict[str, list[str]] = {}
        for user_id, records in self.list_all_records().items():
            active = [record.agent_id for record in records if record.state == RECORD_STATE_ACTIVE]
            if active:
                result[user_id] = active
        return result

    def find_active_record(self, agent_id: str) -> tuple[str, ReplicaRecord] | None:
        """Return ``(user_id, record)`` for the ACTIVE record of ``agent_id``, or None."""
        for user_id, records in self.list_all_records().items():
            for record in records:
                if record.agent_id == str(agent_id) and record.state == RECORD_STATE_ACTIVE:
                    return user_id, record
        return None

    # -- Secrets --------------------------------------------------------------

    def _collect_secrets_payload(
        self, user_id: str, agent_id: str, provider_kind: str, host_id: str
    ) -> WorkspaceSecretsPayload | None:
        """Assemble the workspace's plaintext secrets from their local sources, or None when empty."""
        restic_env = read_canonical_env(self.paths, AgentId(agent_id))
        ssh_private_key, ssh_known_hosts = collect_ssh_key_material(
            self._effective_mngr_host_dir(), provider_kind, host_id
        )
        if restic_env is None and ssh_private_key is None:
            return None
        return WorkspaceSecretsPayload(
            restic_env=restic_env, ssh_private_key=ssh_private_key, ssh_known_hosts=ssh_known_hosts
        )

    def build_encrypted_secrets(
        self, user_id: str, agent_id: str, provider_kind: str, host_id: str
    ) -> BuiltRecordSecrets | None:
        """Assemble and encrypt the workspace's secret payload under the account's DEK.

        Returns None when the account is locked on this device (no DEK) or
        there is nothing to sync (no backup env and no SSH material). The
        returned bundle pairs the ciphertext with its plaintext digest so
        callers can stamp the record's local ``secrets_content_hash``.
        """
        dek = dek_store.load_dek(self.paths, user_id)
        if dek is None:
            return None
        payload = self._collect_secrets_payload(user_id, agent_id, provider_kind, host_id)
        if payload is None:
            return None
        blob = encrypt_secrets(dek, payload.model_dump_json().encode("utf-8"))
        return BuiltRecordSecrets(
            encrypted=b64encode(blob).decode("ascii"),
            content_hash=secrets_payload_content_hash(payload),
        )

    def decrypt_record_secrets(self, user_id: str, record: ReplicaRecord) -> WorkspaceSecretsPayload | None:
        """Decrypt a record's secrets with the account's DEK; None when locked/absent/corrupt."""
        if record.encrypted_secrets is None:
            return None
        dek = dek_store.load_dek(self.paths, user_id)
        if dek is None:
            return None
        try:
            plaintext = decrypt_secrets(dek, b64decode(record.encrypted_secrets))
        except (SecretWrappingError, ValueError) as e:
            logger.warning("Could not decrypt the secrets for workspace {}: {}", record.agent_id, e)
            return None
        try:
            return WorkspaceSecretsPayload.model_validate_json(plaintext)
        except ValueError as e:
            logger.warning("Malformed decrypted secrets payload for workspace {}: {}", record.agent_id, e)
            return None

    # -- Record building ------------------------------------------------------

    def build_record_from_resolver(
        self,
        user_id: str,
        agent_id: str,
        resolver: BackendResolverInterface,
        state: str = RECORD_STATE_ACTIVE,
    ) -> ReplicaRecord | None:
        """Assemble a fresh record (metadata + secrets) for a locally-discovered workspace."""
        info = resolver.get_agent_display_info(AgentId(agent_id))
        if info is None:
            return None
        provider_kind = info.provider_name or ""
        display_name = resolver.get_workspace_name(AgentId(agent_id)) or info.agent_name
        color = resolver.get_workspace_color(AgentId(agent_id))
        is_cloud_row = provider_kind.startswith(_CLOUD_PROVIDER_PREFIX)
        built_secrets = self.build_encrypted_secrets(user_id, agent_id, provider_kind, info.host_id)
        return ReplicaRecord(
            host_id=info.host_id,
            agent_id=str(agent_id),
            display_name=display_name,
            color=color,
            provider_kind=provider_kind,
            hosting_device_id=None if is_cloud_row else self.device_id,
            device_label=self.device_label,
            state=state,
            encrypted_secrets=built_secrets.encrypted if built_secrets is not None else None,
            revision=0,
            is_dirty=True,
            secrets_content_hash=built_secrets.content_hash if built_secrets is not None else None,
        )

    # -- Push / pull ----------------------------------------------------------

    def _push_record(self, user_id: str, account_email: str, record: ReplicaRecord) -> ReplicaRecord:
        """CAS-push one record; on a revision conflict, rebase once onto the stored revision.

        Local content wins on rebase: local rows have a single writer (this
        device), and cloud rows are only pushed from synchronous user actions
        where last-actor-wins is the intended semantics.
        """
        if self.cli is None:
            raise WorkspaceSyncError("workspace sync is not configured (no imbue_cloud CLI)")
        # The metadata-only tier: while the account has no (non-empty) master
        # password, its secrets never leave this machine -- the wire copy is
        # stripped. (The next pull mirrors the secretless server row into the
        # replica; this device reads its own secrets from the canonical env
        # files, never from the replica.) Only an UNLOCKED device can make
        # that call: a locked device has no bundle mirror even when a password
        # is set elsewhere, and stripping there would scrub secrets another
        # device synced -- it passes the replica's opaque blob through as-is.
        is_unlocked = dek_store.is_account_unlocked(self.paths, user_id)
        is_password_set = dek_store.is_master_password_set_for_account(self.paths, user_id)
        is_metadata_only = is_unlocked and not is_password_set
        wire = record.to_wire(record.revision + 1)
        if is_metadata_only:
            wire["encrypted_secrets"] = None
        try:
            stored = self.cli.sync_record_push(account_email, wire)
        except ImbueCloudSyncConflictCliError as conflict:
            if conflict.stored_record is None:
                raise
            server_revision = int(str(conflict.stored_record.get("revision", 0)))
            rebased = record.to_wire(server_revision + 1)
            if is_metadata_only:
                rebased["encrypted_secrets"] = None
            stored = self.cli.sync_record_push(account_email, rebased)
        acked = record.model_copy_update(
            to_update(record.field_ref().revision, int(str(stored.get("revision", record.revision + 1)))),
            to_update(record.field_ref().is_dirty, False),
        )
        with self._lock:
            self._set_record_unlocked(user_id, acked)
        return acked

    def upsert_local_record(self, user_id: str, account_email: str, record: ReplicaRecord) -> None:
        """Store a (dirty) record locally and best-effort push it now.

        A failed push leaves the row dirty for the next reconcile -- used by
        the queueing mutation sites (create, env writes, renames). The
        synchronous sites (associate/disassociate) call the ``*_or_raise``
        variants instead.
        """
        dirty = record.model_copy_update(to_update(record.field_ref().is_dirty, True))
        with self._lock:
            self._set_record_unlocked(user_id, dirty)
        if self.cli is None:
            return
        try:
            self._push_record(user_id, account_email, dirty)
        except (ImbueCloudCliError, WorkspaceSyncError) as e:
            logger.warning("Queued workspace record for {} (push failed: {})", record.agent_id, e)

    def associate_workspace_or_raise(
        self, user_id: str, account_email: str, agent_id: str, resolver: BackendResolverInterface
    ) -> None:
        """Create + push the record binding ``agent_id`` to the account (requires connectivity).

        Raises ``WorkspaceSyncError`` when the workspace isn't locally known or
        the push fails -- the caller surfaces this to the settings action.
        """
        record = self.build_record_from_resolver(user_id, agent_id, resolver)
        if record is None:
            raise WorkspaceSyncError(f"workspace {agent_id} is not in local discovery; cannot associate it")
        existing = self.find_active_record(agent_id)
        if existing is not None and existing[0] != user_id:
            raise WorkspaceSyncError(
                "workspace is associated with another account; disassociate it first, then associate"
            )
        with self._lock:
            self._load_unlocked()
            previous = self._records_by_user_id.get(user_id, {}).get(record.host_id)
        if previous is not None:
            record = record.model_copy_update(
                to_update(record.field_ref().revision, previous.revision),
            )
        try:
            self._push_record(user_id, account_email, record)
        except ImbueCloudCliError as e:
            raise WorkspaceSyncError(f"could not push the association to the connector: {e}") from e

    def disassociate_workspace_or_raise(self, user_id: str, account_email: str, agent_id: str) -> None:
        """Remove the record binding ``agent_id`` to the account (requires connectivity)."""
        found = self.find_active_record(agent_id)
        if found is None or found[0] != user_id:
            return
        _, record = found
        if self.cli is None:
            raise WorkspaceSyncError("workspace sync is not configured (no imbue_cloud CLI)")
        try:
            self.cli.sync_record_delete(account_email, record.host_id)
        except ImbueCloudCliError as e:
            raise WorkspaceSyncError(f"could not remove the record from the connector: {e}") from e
        with self._lock:
            self._drop_record_unlocked(user_id, record.host_id)

    def tombstone_record(self, user_id: str, account_email: str, agent_id: str) -> None:
        """Mark ``agent_id``'s record DESTROYED (kept, with secrets, for backup access)."""
        found = self.find_active_record(agent_id)
        if found is None or found[0] != user_id:
            return
        _, record = found
        tombstoned = record.model_copy_update(
            to_update(record.field_ref().state, RECORD_STATE_DESTROYED),
            to_update(record.field_ref().is_dirty, True),
        )
        with self._lock:
            self._set_record_unlocked(user_id, tombstoned)
        if self.cli is None:
            return
        try:
            self._push_record(user_id, account_email, tombstoned)
        except (ImbueCloudCliError, WorkspaceSyncError) as e:
            logger.warning("Queued tombstone for {} (push failed: {})", agent_id, e)

    def remove_record_or_raise(self, user_id: str, account_email: str, host_id: str) -> None:
        """Remove a record outright by host id (the manual remove-from-list escape hatch)."""
        if self.cli is None:
            raise WorkspaceSyncError("workspace sync is not configured (no imbue_cloud CLI)")
        try:
            self.cli.sync_record_delete(account_email, host_id)
        except ImbueCloudCliError as e:
            raise WorkspaceSyncError(f"could not remove the record from the connector: {e}") from e
        with self._lock:
            self._drop_record_unlocked(user_id, host_id)

    def pull(self, user_id: str, account_email: str) -> bool:
        """Merge the server's records into the replica (local dirty rows win until pushed).

        Returns True when the server was reached and its records merged; False
        when sync is unconfigured or the connector was unreachable, so callers
        can distinguish "the account has no records" from "the records could
        not be fetched".
        """
        if self.cli is None:
            return False
        try:
            wire_records = self.cli.sync_records_pull(account_email)
        except ImbueCloudCliError as e:
            logger.warning("Could not pull workspace records for {}: {}", account_email, e)
            return False
        with self._lock:
            self._load_unlocked()
            by_host = self._records_by_user_id.setdefault(user_id, {})
            server_host_ids = set()
            for wire in wire_records:
                record = replica_record_from_wire(wire)
                server_host_ids.add(record.host_id)
                local = by_host.get(record.host_id)
                if local is not None and local.is_dirty:
                    continue
                # secrets_content_hash is local-only state ("the plaintext this
                # device last contributed"), so it must survive server rows
                # replacing local ones -- otherwise every pull would reset the
                # producer's change tracking.
                if local is not None and local.secrets_content_hash is not None:
                    record = record.model_copy_update(
                        to_update(record.field_ref().secrets_content_hash, local.secrets_content_hash),
                    )
                by_host[record.host_id] = record
            # A row the server no longer has (deleted elsewhere) drops out of
            # the replica unless it has unpushed local changes.
            for host_id in list(by_host.keys()):
                if host_id not in server_host_ids and not by_host[host_id].is_dirty:
                    del by_host[host_id]
            self._save_unlocked(user_id)
        return True

    def push_dirty(self, user_id: str, account_email: str) -> None:
        for record in self.list_records(user_id):
            if not record.is_dirty:
                continue
            try:
                self._push_record(user_id, account_email, record)
            except (ImbueCloudCliError, WorkspaceSyncError) as e:
                logger.warning("Deferred dirty record push for {}: {}", record.agent_id, e)

    def push_all_secrets(self, user_id: str, account_email: str, resolver: BackendResolverInterface) -> None:
        """Build + push secrets for pending locally-discovered ACTIVE records (password just set).

        Pending means the record carries no synced secrets yet -- the rows the
        metadata-only tier stripped on the wire. Rows that already carry
        secrets stay untouched: a password change is rewrap-only (the DEK is
        unchanged), so existing blobs remain valid, and rebuilding a row this
        device does not host (only in discovery on other devices) would
        overwrite the hosting device's full material with whatever partial
        material -- e.g. a materialized backup env without the SSH key --
        exists here.
        """
        known_ids = {str(aid) for aid in resolver.list_known_workspace_ids()}
        for record in self.list_records(user_id):
            if record.state != RECORD_STATE_ACTIVE or record.agent_id not in known_ids:
                continue
            if record.encrypted_secrets is not None:
                continue
            built_secrets = self.build_encrypted_secrets(
                user_id, record.agent_id, record.provider_kind, record.host_id
            )
            if built_secrets is None:
                continue
            updated = record.model_copy_update(
                to_update(record.field_ref().encrypted_secrets, built_secrets.encrypted),
                to_update(record.field_ref().is_dirty, True),
                to_update(record.field_ref().secrets_content_hash, built_secrets.content_hash),
            )
            self.upsert_local_record(user_id, account_email, updated)

    def materialize_env_from_record(self, agent_id: str) -> bool:
        """Write ``backup_envs/<agent_id>.env`` from the record's synced secrets, if possible.

        Covers backup status/export for workspaces this device never
        provisioned (hosted on another device, or destroyed elsewhere).
        Returns True when an env file now exists (either it already did, or
        it was just materialized); False when there is no record, no synced
        secrets, or the account is locked on this device.
        """
        if read_canonical_env(self.paths, AgentId(agent_id)) is not None:
            return True
        found = self._find_record_any_state(agent_id)
        if found is None:
            return False
        user_id, record = found
        payload = self.decrypt_record_secrets(user_id, record)
        if payload is None or payload.restic_env is None:
            return False
        write_canonical_env(self.paths, AgentId(agent_id), payload.restic_env)
        logger.info("Materialized the backup env for {} from its synced workspace record", agent_id)
        return True

    # -- SSH material consumption ----------------------------------------------

    def imbue_cloud_host_state_dir(self, account_email: str, host_id: str) -> Path | None:
        """The account's imbue_cloud per-host state dir, or None while mngr is uninitialized.

        The instance name is derived locally from the account email (never
        trusted from the wire), so materialized files always land where this
        install's provider will look.
        """
        profile_dir = _resolve_mngr_profile_dir(self._effective_mngr_host_dir())
        if profile_dir is None:
            return None
        instance_name = imbue_cloud_provider_name_for_account(account_email)
        return profile_dir / "providers" / "imbue_cloud" / instance_name / "hosts" / host_id

    def imbue_cloud_host_ssh_key_path(self, account_email: str, host_id: str) -> Path | None:
        """The materialized (or lease-time) private key path for one cloud host, or None."""
        host_dir = self.imbue_cloud_host_state_dir(account_email, host_id)
        if host_dir is None:
            return None
        return host_dir / _SSH_KEY_FILENAME

    def ssh_material_errors(self) -> dict[str, str]:
        """Last materialization failure per workspace agent id (for the tile chips)."""
        with self._lock:
            return dict(self._ssh_material_error_by_agent_id)

    def _set_ssh_material_error(self, agent_id: str, error: str) -> None:
        with self._lock:
            self._ssh_material_error_by_agent_id[agent_id] = error

    def _clear_ssh_material_error(self, agent_id: str) -> None:
        with self._lock:
            self._ssh_material_error_by_agent_id.pop(agent_id, None)

    def materialize_account_synced_secrets(self, user_id: str, account_email: str) -> bool:
        """Materialize the account's synced secrets into their local consumers.

        For every ACTIVE record: eagerly materialize the backup env, and (for
        cloud rows) write the synced SSH key material into the imbue_cloud
        provider's per-host state dir so discovery can reach hosts leased on
        another install. Also sweeps orphaned per-host key dirs. No-op while
        the account is locked. Returns True when SSH material was created or
        changed, so the caller can bounce discovery.
        """
        if not dek_store.is_account_unlocked(self.paths, user_id):
            return False
        is_ssh_material_changed = False
        for record in self.list_records(user_id):
            if record.state != RECORD_STATE_ACTIVE:
                continue
            if record.encrypted_secrets is not None:
                self.materialize_env_from_record(record.agent_id)
            if record.provider_kind.startswith(_CLOUD_PROVIDER_PREFIX):
                is_ssh_material_changed = (
                    self._materialize_record_ssh_material(user_id, account_email, record) or is_ssh_material_changed
                )
        self._sweep_orphaned_ssh_key_dirs(user_id, account_email)
        return is_ssh_material_changed

    def _materialize_record_ssh_material(self, user_id: str, account_email: str, record: ReplicaRecord) -> bool:
        """Write one cloud record's synced SSH material; returns True when files changed."""
        host_dir = self.imbue_cloud_host_state_dir(account_email, record.host_id)
        if host_dir is None:
            return False
        if (host_dir / _LEASE_META_FILENAME).is_file():
            # This install leased the host itself: its own keypair (and the
            # connector-fed known_hosts pins) are authoritative.
            self._clear_ssh_material_error(record.agent_id)
            return False
        if record.encrypted_secrets is None:
            self._clear_ssh_material_error(record.agent_id)
            return False
        payload = self.decrypt_record_secrets(user_id, record)
        if payload is None:
            self._set_ssh_material_error(record.agent_id, "Could not decrypt the synced secrets for this workspace.")
            return False
        if payload.ssh_private_key is None:
            self._clear_ssh_material_error(record.agent_id)
            return False
        public_key_line = derive_openssh_public_key_line(payload.ssh_private_key)
        if public_key_line is None:
            self._set_ssh_material_error(record.agent_id, "The synced SSH key for this workspace could not be parsed.")
            return False
        try:
            is_changed = _materialize_ssh_files(host_dir, payload, public_key_line)
        except OSError as e:
            logger.warning("Could not materialize SSH material for workspace {}: {}", record.agent_id, e)
            self._set_ssh_material_error(record.agent_id, f"Could not write the SSH key material: {e}")
            return False
        if is_changed:
            logger.info("Materialized synced SSH material for workspace {} at {}", record.agent_id, host_dir)
        self._clear_ssh_material_error(record.agent_id)
        return is_changed

    def _sweep_orphaned_ssh_key_dirs(self, user_id: str, account_email: str) -> None:
        """Delete per-host key dirs with no lease.json and no ACTIVE record.

        Covers workspaces destroyed elsewhere (tombstoned rows), records
        removed via the UI, and records deleted while this install was closed.
        A recent-mtime grace protects an in-flight lease whose keypair exists
        but whose lease.json has not landed yet.
        """
        example_dir = self.imbue_cloud_host_state_dir(account_email, "placeholder")
        if example_dir is None:
            return
        hosts_root = example_dir.parent
        if not hosts_root.is_dir():
            return
        active_cloud_host_ids = {
            record.host_id
            for record in self.list_records(user_id)
            if record.state == RECORD_STATE_ACTIVE and record.provider_kind.startswith(_CLOUD_PROVIDER_PREFIX)
        }
        for host_dir in hosts_root.iterdir():
            if not host_dir.is_dir():
                continue
            if host_dir.name in active_cloud_host_ids:
                continue
            if (host_dir / _LEASE_META_FILENAME).is_file():
                continue
            try:
                is_recently_touched = (time.time() - host_dir.stat().st_mtime) < _ORPHAN_SWEEP_GRACE_SECONDS
            except OSError:
                continue
            if is_recently_touched:
                continue
            logger.info("Sweeping orphaned imbue_cloud host key dir {} (no lease, no active record)", host_dir)
            shutil.rmtree(host_dir, ignore_errors=True)

    def _find_record_any_state(self, agent_id: str) -> tuple[str, ReplicaRecord] | None:
        """Like :meth:`find_active_record` but tombstoned records count too (backup access)."""
        fallback: tuple[str, ReplicaRecord] | None = None
        for user_id, records in self.list_all_records().items():
            for record in records:
                if record.agent_id != str(agent_id):
                    continue
                if record.state == RECORD_STATE_ACTIVE:
                    return user_id, record
                fallback = (user_id, record)
        return fallback

    def locked_account_user_ids(self, signed_in_user_ids: Sequence[str]) -> list[str]:
        """Signed-in accounts whose secrets exist server-side but whose DEK is absent here.

        These are the accounts the unlock banner prompts for: a bundle exists
        (a non-empty master password was set somewhere) but this device has
        no DEK file yet.
        """
        locked: list[str] = []
        for user_id in signed_in_user_ids:
            if dek_store.is_account_unlocked(self.paths, user_id):
                continue
            if dek_store.read_bundle_mirror(self.paths, user_id) is not None:
                locked.append(user_id)
                continue
            has_secretful_record = any(record.encrypted_secrets is not None for record in self.list_records(user_id))
            if has_secretful_record:
                locked.append(user_id)
        return locked

    def unlock_account(self, user_id: str, account_email: str, password: SecretStr) -> bool:
        """New-device unlock: fetch the bundle, unwrap with ``password``, install the DEK.

        Returns False (without raising) when the password is wrong or no
        bundle exists anywhere; True when the account is now unlocked.
        """
        bundle = dek_store.read_bundle_mirror(self.paths, user_id)
        if bundle is None and self.cli is not None:
            try:
                bundle = self.cli.sync_bundle_pull(account_email)
            except ImbueCloudCliError as e:
                logger.warning("Could not fetch the key bundle for {}: {}", account_email, e)
                return False
        if bundle is None:
            return False
        try:
            dek_store.unlock_account_with_bundle(self.paths, user_id, bundle, password)
        except SecretWrappingError:
            return False
        return True

    # -- Legacy conversion + reconcile ----------------------------------------

    def _legacy_associations_path(self) -> Path:
        return self.paths.data_dir / _LEGACY_ASSOCIATIONS_FILENAME

    def _legacy_sessions_path(self) -> Path:
        return self.paths.data_dir / _LEGACY_SESSIONS_FILENAME

    def read_legacy_associations(self) -> dict[str, list[str]]:
        """Read the legacy association state (user_id -> [agent_id, ...]).

        Prefers ``workspace_associations.json``; when that never existed,
        falls back to the older ``sessions.json`` layout (full identity
        records), extracting each entry's ``workspace_ids`` -- the same
        precedence the pre-sync reader used, so associations written only in
        the sessions.json era still convert.
        """
        path = self._legacy_associations_path()
        if path.is_file():
            try:
                raw = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Could not read the legacy associations file {}: {}", path, e)
                return {}
            if not isinstance(raw, dict):
                return {}
            result: dict[str, list[str]] = {}
            for user_id, value in raw.items():
                if isinstance(value, list):
                    result[user_id] = [str(item) for item in value if isinstance(item, str)]
            return result
        sessions_path = self._legacy_sessions_path()
        if not sessions_path.is_file():
            return {}
        try:
            raw_sessions = json.loads(sessions_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read the legacy sessions file {}: {}", sessions_path, e)
            return {}
        if not isinstance(raw_sessions, dict):
            return {}
        result_sessions: dict[str, list[str]] = {}
        for user_id, data in raw_sessions.items():
            if not isinstance(data, dict):
                continue
            workspace_ids = data.get("workspace_ids", [])
            if isinstance(workspace_ids, list):
                result_sessions[user_id] = [str(item) for item in workspace_ids if isinstance(item, str)]
        return result_sessions

    def _retire_legacy_associations(self) -> None:
        # Both generations must retire together: a lingering sessions.json
        # would read as unconverted legacy state on every later pass (and
        # re-create records the user has since disassociated).
        for path in (self._legacy_associations_path(), self._legacy_sessions_path()):
            if not path.is_file():
                continue
            try:
                path.rename(path.with_name(path.name + _LEGACY_RETIRED_SUFFIX))
            except OSError as e:
                logger.warning("Could not retire the legacy associations file {}: {}", path, e)

    def reconcile(
        self,
        accounts: dict[str, str],
        resolver: BackendResolverInterface,
    ) -> dict[str, bool]:
        """The post-discovery sync pass for every signed-in account.

        ``accounts`` maps user_id -> account email. Steps per account: pull,
        migrate legacy associations into records (one-shot), refresh metadata
        for locally-hosted rows whose name/color changed, push dirty rows, and
        tombstone rows whose host is definitively absent from local discovery.

        Returns per-account pull success (user_id -> True when the connector
        was reached), so the scheduler's initial-sync tracking can distinguish
        "the account has no records" from "the records could not be fetched".
        """
        with log_span("Reconciling workspace records"):
            legacy = self.read_legacy_associations()
            is_legacy_fully_converted = True
            is_pull_ok_by_user_id: dict[str, bool] = {}
            for user_id, account_email in accounts.items():
                self._ensure_bundle_uploaded(user_id, account_email)
                is_pull_ok_by_user_id[user_id] = self.pull(user_id, account_email)
                for agent_id in legacy.get(user_id, []):
                    if self.find_active_record(agent_id) is None:
                        record = self.build_record_from_resolver(user_id, agent_id, resolver)
                        if record is not None:
                            self.upsert_local_record(user_id, account_email, record)
                        elif self._is_definitively_absent_from_discovery(agent_id, resolver):
                            logger.info(
                                "Legacy association for {} names a workspace that no longer exists; dropping it",
                                agent_id,
                            )
                        else:
                            is_legacy_fully_converted = False
                            logger.warning(
                                "Legacy association for {} could not convert this pass (workspace not in "
                                "discovery yet); keeping the legacy file for a retry",
                                agent_id,
                            )
                self._refresh_local_metadata(user_id, account_email, resolver)
                self._tombstone_definitively_absent(user_id, account_email, resolver)
                self.push_dirty(user_id, account_email)
            signed_out_user_ids = set(legacy) - set(accounts)
            if signed_out_user_ids:
                is_legacy_fully_converted = False
                logger.info(
                    "Keeping the legacy associations file: it has entries for {} account(s) not signed in here",
                    len(signed_out_user_ids),
                )
            if legacy and accounts and is_legacy_fully_converted:
                self._retire_legacy_associations()
            return is_pull_ok_by_user_id

    def _ensure_bundle_uploaded(self, user_id: str, account_email: str) -> None:
        """Upload this device's key-bundle mirror when the server has none.

        The settings password-change flow pushes bundles directly, but the
        legacy one-shot conversion (and any crash between wrapping and
        pushing) can leave a device holding a wrapped key the connector never
        saw -- then no other device can ever unlock the synced secrets. Heal
        that here: when a mirror exists and the server has NO bundle, push
        the mirror. A server bundle that already exists always wins (it may
        be newer, e.g. a password changed on another device while this one
        was offline), so this can never clobber a rewrap.
        """
        if user_id in self._bundle_confirmed_user_ids or self.cli is None:
            return
        mirror = dek_store.read_bundle_mirror(self.paths, user_id)
        if mirror is None:
            return
        try:
            if self.cli.sync_bundle_pull(account_email) is None:
                self.cli.sync_bundle_push(account_email, mirror)
                logger.info("Uploaded the missing key bundle for account {}", user_id[:8])
        except ImbueCloudCliError as e:
            logger.warning("Could not verify/upload the key bundle for {}: {}", user_id[:8], e)
            return
        self._bundle_confirmed_user_ids.add(user_id)

    def _is_definitively_absent_from_discovery(self, agent_id: str, resolver: BackendResolverInterface) -> bool:
        """Whether a legacy-association workspace is provably gone from this device.

        Mirrors the tombstone pass's caution: only a complete discovery
        snapshot with no errored providers can prove absence -- a failed poll
        proves nothing, so its associations must survive for a later pass.
        """
        if not resolver.has_completed_initial_discovery():
            return False
        if resolver.get_provider_errors():
            return False
        return str(agent_id) not in {str(aid) for aid in resolver.list_known_workspace_ids()}

    def _refresh_local_metadata(self, user_id: str, account_email: str, resolver: BackendResolverInterface) -> None:
        """Fold local metadata/secret changes into locally-discovered rows (queued push).

        Any ACTIVE row whose workspace is in local discovery is refreshable
        from here -- that covers this device's own rows and imbue_cloud leased
        rows (which every signed-in device discovers). Rows hosted on other
        devices never appear in local discovery, so they are never touched.
        Freshly-created rows seeded with minimal metadata (empty provider,
        no secrets yet) are enriched by this pass once discovery catches up.
        """
        known_ids = {str(aid) for aid in resolver.list_known_workspace_ids()}
        # Secrets updates are only meaningful while a master password is set:
        # without one, pushes strip the secrets from the wire and pulls mirror
        # the secretless server row back, so re-adding them here would
        # dirty-push a new revision every pass without ever converging.
        is_secrets_sync_enabled = dek_store.is_master_password_set_for_account(self.paths, user_id)
        for record in self.list_records(user_id):
            if record.state != RECORD_STATE_ACTIVE or record.agent_id not in known_ids:
                continue
            rebuilt = self.build_record_from_resolver(user_id, record.agent_id, resolver, state=record.state)
            if rebuilt is None:
                continue
            # A secrets update is warranted when this device is (re-)adding
            # material the record lacks, OR when this device contributed the
            # record's current secrets (its hash is known here) and the local
            # material has since changed. A device that never contributed
            # (hash unknown) must not replace another device's secrets with
            # its own -- possibly partial -- view of the material.
            is_secrets_changed = (
                is_secrets_sync_enabled
                and rebuilt.encrypted_secrets is not None
                and (
                    record.encrypted_secrets is None
                    or (
                        record.secrets_content_hash is not None
                        and rebuilt.secrets_content_hash != record.secrets_content_hash
                    )
                )
            )
            is_changed = (
                rebuilt.display_name != record.display_name
                or rebuilt.color != record.color
                or rebuilt.provider_kind != record.provider_kind
                or rebuilt.hosting_device_id != record.hosting_device_id
                or is_secrets_changed
            )
            if not is_changed:
                continue
            merged = record.model_copy_update(
                to_update(record.field_ref().display_name, rebuilt.display_name),
                to_update(record.field_ref().color, rebuilt.color),
                to_update(record.field_ref().provider_kind, rebuilt.provider_kind),
                to_update(record.field_ref().hosting_device_id, rebuilt.hosting_device_id),
                to_update(
                    record.field_ref().encrypted_secrets,
                    rebuilt.encrypted_secrets if is_secrets_changed else record.encrypted_secrets,
                ),
                to_update(
                    record.field_ref().secrets_content_hash,
                    rebuilt.secrets_content_hash if is_secrets_changed else record.secrets_content_hash,
                ),
                to_update(record.field_ref().is_dirty, True),
            )
            with self._lock:
                self._set_record_unlocked(user_id, merged)

    def _tombstone_definitively_absent(
        self, user_id: str, account_email: str, resolver: BackendResolverInterface
    ) -> None:
        """Tombstone this device's ACTIVE rows whose host is definitively gone locally.

        Definitively absent means: discovery completed, the record's provider
        did not error this poll, and the workspace is not among the known ids
        (which still include DESTROYED-but-lingering hosts, so the provider's
        grace window is honored). Cloud rows are skipped -- their lifecycle is
        driven by lease state, and any device may see them. Rows with an empty
        provider_kind are skipped too: those are create-path seeds discovery
        has never enriched, so "absent from discovery" says nothing about the
        host (the create just finished and the next poll hasn't seen it yet).
        """
        if not resolver.has_completed_initial_discovery():
            return
        if not self.device_id:
            # Without a real device id (missing/unreadable mngr host_id file)
            # this install cannot attribute hosted rows to itself: another
            # id-less install's rows would match an empty id and get destroyed.
            logger.warning("Skipping absent-host tombstoning: this install has no device id")
            return
        known_ids = {str(aid) for aid in resolver.list_known_workspace_ids()}
        errored_providers = {str(name) for name in resolver.get_provider_errors()}
        for record in self.list_records(user_id):
            if record.state != RECORD_STATE_ACTIVE or record.hosting_device_id != self.device_id:
                continue
            if not record.provider_kind:
                continue
            if record.agent_id in known_ids or record.provider_kind in errored_providers:
                continue
            logger.info(
                "Tombstoning workspace record {} ({}): host no longer exists on this device",
                record.agent_id,
                record.display_name,
            )
            tombstoned = record.model_copy_update(
                to_update(record.field_ref().state, RECORD_STATE_DESTROYED),
                to_update(record.field_ref().is_dirty, True),
            )
            with self._lock:
                self._set_record_unlocked(user_id, tombstoned)
