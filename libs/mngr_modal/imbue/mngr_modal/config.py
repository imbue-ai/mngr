import os
import tomllib
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import UserId
from imbue.mngr_modal.errors import ModalProfileNotFoundError


class ModalMode(UpperCaseStrEnum):
    """How the modal provider backend talks to Modal.

    ``DIRECT`` uses the Modal SDK against the user's account.
    ``PROXIED`` routes Modal traffic through the imbue_cloud gateway.
    """

    DIRECT = auto()
    PROXIED = auto()


class ModalProviderConfig(ProviderInstanceConfig):
    """Configuration for the modal provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("modal"),
        description="Provider backend (always 'modal' for this type)",
    )
    mode: ModalMode = Field(
        default=ModalMode.DIRECT,
        description=(
            "How to reach Modal. ``DIRECT`` uses the Modal SDK against the "
            "user's Modal account. ``PROXIED`` routes Modal traffic through the imbue_cloud gateway."
        ),
    )
    token_id: str | None = Field(
        default=None,
        description=(
            "Modal token id this provider instance authenticates with (DIRECT mode). Set together with "
            "token_secret so the instance carries its own Modal account, independent of MODAL_TOKEN_ID / "
            "MODAL_TOKEN_SECRET in the environment and of ~/.modal.toml. When unset, the Modal SDK's own "
            "credential resolution applies. Override via MNGR__PROVIDERS__<NAME>__TOKEN_ID."
        ),
    )
    token_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Modal token secret paired with token_id (DIRECT mode); after the config layers merge, both must be "
            "set or both unset (the two may come from different layers, e.g. the secret from the environment). "
            "Override via MNGR__PROVIDERS__<NAME>__TOKEN_SECRET."
        ),
    )
    modal_profile: str | None = Field(
        default=None,
        description=(
            "Name of a profile section in this machine's ~/.modal.toml (written by `modal token new --profile "
            "<name>`) whose token this instance authenticates with (DIRECT mode), so the block carries no "
            "secret. Mutually exclusive with token_id / token_secret. The profile is read when the provider is "
            "built, so a profile added later is picked up by the next check; a missing profile reports the "
            "provider as awaiting its credential source."
        ),
    )
    user_id: UserId | None = Field(
        default=None,
        description=(
            "Override the profile user_id for this provider instance. When set, this "
            "user_id is used instead of the profile's user_id for namespacing Modal "
            "resources (environments, apps, volumes). This allows sharing Modal resources "
            "across different mngr profiles or installations."
        ),
    )
    environment: str = Field(
        default="main",
        description="Modal environment name",
    )
    app_name: str | None = Field(
        default=None,
        description="Modal app name (defaults to 'mngr-{user_id}-{name}')",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data on the sandbox (defaults to /mngr)",
    )
    default_sandbox_timeout: int = Field(
        default=900,
        description="Default sandbox timeout in seconds",
    )
    shutdown_buffer_seconds: int = Field(
        default=90,
        description=(
            "Buffer time added to the host shutdown timeout. This ensures the activity watcher can trigger a clean shutdown before a hard kill. The max_host_age in data.json is set to the original timeout (without buffer), so the host shuts down gracefully before the infrastructure-level timeout expires."
        ),
    )
    default_idle_timeout: int = Field(
        default=800,
        description="Default host idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Default idle mode for hosts",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: tuple(ActivitySource),
        description="Default activity sources that count toward keeping host active",
    )
    default_cpu: float = Field(
        default=1.0,
        description="Default CPU cores",
    )
    default_memory: float = Field(
        default=1.0,
        description="Default memory in GB",
    )
    default_gpu: str | None = Field(
        default=None,
        description="Default GPU type (e.g., 'h100', 'a10g'). None means no GPU.",
    )
    default_image: str | None = Field(
        default=None,
        description="Default base image (e.g., 'python:3.12-slim'). None uses debian_slim.",
    )
    default_region: str | None = Field(
        default=None,
        description="Default region (e.g., 'us-east'). None lets Modal choose.",
    )
    is_persistent: bool = Field(
        default=True,
        description=(
            "Configuration marker for Modal app persistence. When True (default), indicates "
            "the app is intended for production use. When False (set in tests), indicates "
            "the app is for testing and should be cleaned up. This field enables tests to "
            "signal their intent for easier identification and cleanup of test resources."
        ),
    )
    is_snapshotted_after_create: bool = Field(
        default=True,
        description=(
            "Whether to create an initial snapshot immediately after host creation. "
            "When True (default), an 'initial' snapshot is created, allowing the host "
            "to be restarted even if it's hard-killed. When False, the host can only "
            "be restarted if it was stopped gracefully (which creates a snapshot)."
        ),
    )
    ssh_connect_timeout: float = Field(
        default=60.0,
        description="Timeout in seconds for waiting for sshd to be ready on the sandbox",
    )
    is_host_volume_created: bool = Field(
        default=True,
        description=(
            "Whether to create and mount a persistent Modal Volume for the host directory. "
            "When True (default), a volume is created and the host directory is symlinked to it, "
            "so data (including logs) persists across sandbox restarts and is accessible when the "
            "host is offline. When False, no host volume is created; the host directory is a regular "
            "directory on the sandbox filesystem. Logs and other host data will only be available "
            "while the host is online."
        ),
    )
    is_vm_runtime_enabled: bool = Field(
        default=False,
        description=(
            "Whether to run sandboxes on Modal's VM runtime (https://modal.com/docs/guide/vm-sandboxes) "
            "instead of the default gVisor runtime. When True, sandboxes are created with "
            "``experimental_options={'vm_runtime': True}``, which provides stronger isolation and "
            "broader syscall compatibility (e.g. Docker-in-sandbox state surviving a filesystem snapshot). "
            "When False (default), sandboxes use Modal's default gVisor runtime."
        ),
    )


# Modal's own location for its profiles file, and the variable the SDK honors to move it.
_MODAL_CONFIG_PATH_ENV_VAR: Final[str] = "MODAL_CONFIG_PATH"
_DEFAULT_MODAL_CONFIG_PATH: Final[Path] = Path("~/.modal.toml")


class ModalProfileToken(FrozenModel):
    """The token pair one ``~/.modal.toml`` profile section holds."""

    token_id: str = Field(description="The profile's token id")
    token_secret: SecretStr = Field(description="The profile's token secret")


def modal_config_path() -> Path:
    """Where this machine keeps its Modal profiles: ``MODAL_CONFIG_PATH`` when set, else ``~/.modal.toml``."""
    raw = os.environ.get(_MODAL_CONFIG_PATH_ENV_VAR)
    return Path(raw).expanduser() if raw else _DEFAULT_MODAL_CONFIG_PATH.expanduser()


def read_modal_profiles(config_path: Path) -> dict[str, ModalProfileToken]:
    """Every section of a Modal profiles file that holds a token pair, by name, in file order.

    Read here rather than through the Modal SDK, which loads the file once at
    import: a long-lived ``mngr observe`` must see a profile created after it
    started, on the check that follows ``modal token new --profile <name>``.
    ``FileNotFoundError`` and ``OSError`` / ``tomllib.TOMLDecodeError`` propagate,
    since callers differ on what they mean (a listing shows nothing; a block
    naming a profile reports it missing).
    """
    raw = tomllib.loads(config_path.read_text())
    profiles: dict[str, ModalProfileToken] = {}
    for name, section in raw.items():
        token = _token_of_profile_section(section)
        if token is not None:
            profiles[name] = token
    return profiles


def _token_of_profile_section(section: Any) -> ModalProfileToken | None:
    """The token pair a profile section holds, or None when it is not a section with both keys set."""
    if not isinstance(section, dict):
        return None
    token_id = section.get("token_id")
    token_secret = section.get("token_secret")
    if not isinstance(token_id, str) or not token_id or not isinstance(token_secret, str) or not token_secret:
        return None
    return ModalProfileToken(token_id=token_id, token_secret=SecretStr(token_secret))


def read_modal_profile_token(profile_name: str, config_path: Path) -> ModalProfileToken:
    """The token pair of one named profile in a Modal profiles file.

    Raises ``ModalProfileNotFoundError`` when the file is absent or unreadable
    or when no section of it holds a token pair under that name, since none of
    those leaves a usable profile.
    """
    remediation = f"run `modal token new --profile {profile_name}`"
    try:
        profiles = read_modal_profiles(config_path)
    except FileNotFoundError as e:
        raise ModalProfileNotFoundError(
            f"Modal profile {profile_name!r} is not configured on this machine ({config_path} does not exist).",
            short_remediation=remediation,
        ) from e
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ModalProfileNotFoundError(
            f"Modal profile {profile_name!r} cannot be read from {config_path}: {e}",
            short_remediation=remediation,
        ) from e
    token = profiles.get(profile_name)
    if token is None:
        raise ModalProfileNotFoundError(
            f"Modal profile {profile_name!r} is not configured on this machine (no section of {config_path} "
            "holds a token_id / token_secret pair under that name).",
            short_remediation=remediation,
        )
    return token
