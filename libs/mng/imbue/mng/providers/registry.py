# NOTE: These top-level imports cause Modal to be loaded even when not needed,
# adding ~0.1s to every command. Profiling of `mng list --provider local` shows:
#   - Total CLI time: ~0.9s
#   - With Modal disabled entirely (--disable-plugin modal): ~0.76s
#   - Python-level work (imports + list_agents): ~0.58s
#
# The Modal import happens here unconditionally, even when --provider filters to
# local-only. To fix: move these imports inside load_backends_from_plugins() and
# load_local_backend_only(), or only import backends that are actually enabled.
#
# Another candidate for lazy loading: celpy (~45ms) in api/list.py. It's only
# needed when CEL filters are used (--include/--exclude), but is currently
# imported at the top level via imbue.mng.utils.cel_utils.
import pluggy

import imbue.mng.providers.docker.backend as docker_backend_module
import imbue.mng.providers.local.backend as local_backend_module
import imbue.mng.providers.modal.backend as modal_backend_module
import imbue.mng.providers.ssh.backend as ssh_backend_module
from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.config.provider_config_registry import get_provider_config_class
from imbue.mng.config.provider_config_registry import register_provider_config
from imbue.mng.config.provider_config_registry import reset_provider_config_registry
from imbue.mng.errors import ConfigStructureError
from imbue.mng.errors import UnknownBackendError
from imbue.mng.interfaces.provider_backend import ProviderBackendInterface
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.base_provider import BaseProviderInstance

# Cache for registered backends
_backend_registry: dict[ProviderBackendName, type[ProviderBackendInterface]] = {}
# Use a mutable container to track state without 'global' keyword
_registry_state: dict[str, bool] = {"backends_loaded": False}


def load_all_registries(pm: pluggy.PluginManager) -> None:
    """Load all registries from plugins.

    This is the main entry point for loading all pluggy-based registries.
    Call this once during application startup, before using any registry lookups.

    Note: agent registries are loaded separately via
    agents.agent_registry.load_agents_from_plugins(), called from main.py.
    """
    load_backends_from_plugins(pm)


def reset_backend_registry() -> None:
    """Reset the backend registry to its initial state.

    This is primarily used for test isolation to ensure a clean state between tests.
    """
    _backend_registry.clear()
    reset_provider_config_registry()
    _registry_state["backends_loaded"] = False


def _load_backends(pm: pluggy.PluginManager, *, include_modal: bool, include_docker: bool) -> None:
    """Load provider backends from the specified modules.

    The pm parameter is the pluggy plugin manager. If include_modal is True,
    the Modal backend is included (requires Modal credentials). If include_docker
    is True, the Docker backend is included (requires a Docker daemon).
    """
    if _registry_state["backends_loaded"]:
        return

    pm.register(local_backend_module, name="local")
    pm.register(ssh_backend_module, name="ssh")
    if include_docker:
        pm.register(docker_backend_module, name="docker")
    if include_modal:
        pm.register(modal_backend_module, name="modal")

    registrations = pm.hook.register_provider_backend()

    for registration in registrations:
        if registration is not None:
            backend_class, config_class = registration
            backend_name = backend_class.get_name()
            _backend_registry[backend_name] = backend_class
            register_provider_config(str(backend_name), config_class)

    _registry_state["backends_loaded"] = True


def load_local_backend_only(pm: pluggy.PluginManager) -> None:
    """Load only the local and SSH provider backends.

    This is used by tests to avoid depending on external services.
    Unlike load_backends_from_plugins, this only registers the local and SSH backends
    (not Modal or Docker which require external daemons/credentials).
    """
    _load_backends(pm, include_modal=False, include_docker=False)


def load_backends_from_plugins(pm: pluggy.PluginManager) -> None:
    """Load all provider backends from plugins."""
    _load_backends(pm, include_modal=True, include_docker=True)


def get_backend(name: str | ProviderBackendName) -> type[ProviderBackendInterface]:
    """Get a provider backend class by name.

    Backends are loaded from plugins via the plugin manager.
    """
    key = ProviderBackendName(name) if isinstance(name, str) else name
    if key not in _backend_registry:
        available = sorted(str(k) for k in _backend_registry.keys())
        raise UnknownBackendError(
            f"Unknown provider backend: {key}. Registered backends: {', '.join(available) or '(none)'}"
        )
    return _backend_registry[key]


def get_config_class(name: str | ProviderBackendName) -> type[ProviderInstanceConfig]:
    """Get the config class for a provider backend.

    Delegates to the config-layer registry. This function exists for callers
    above the config layer (api, cli) that historically imported from here.
    """
    return get_provider_config_class(str(name))


def list_backends() -> list[str]:
    """List all registered backend names."""
    return sorted(str(k) for k in _backend_registry.keys())


def build_provider_instance(
    instance_name: ProviderInstanceName,
    backend_name: ProviderBackendName,
    config: ProviderInstanceConfig,
    mng_ctx: MngContext,
) -> BaseProviderInstance:
    """Build a provider instance using the registered backend."""
    backend_class = get_backend(backend_name)
    obj = backend_class.build_provider_instance(
        name=instance_name,
        config=config,
        mng_ctx=mng_ctx,
    )
    if not isinstance(obj, BaseProviderInstance):
        raise ConfigStructureError(
            f"Backend {backend_name} returned {type(obj).__name__}, expected BaseProviderInstance subclass"
        )
    return obj


@pure
def _indent_text(text: str, indent: str) -> str:
    """Indent each line of text with the given prefix."""
    return "\n".join(indent + line if line.strip() else "" for line in text.split("\n"))


def get_all_provider_args_help_sections() -> tuple[tuple[str, str], ...]:
    """Generate help sections for build/start args from all registered backends.

    Returns a tuple of (title, content) pairs suitable for use as additional
    sections in CommandHelpMetadata.
    """
    lines: list[str] = []
    for backend_name in sorted(_backend_registry.keys()):
        backend_class = _backend_registry[backend_name]
        build_help = backend_class.get_build_args_help().strip()
        start_help = backend_class.get_start_args_help().strip()
        lines.append(f"Provider: {backend_name}")
        lines.append(_indent_text(build_help, "  "))
        if start_help != build_help:
            lines.append(_indent_text(start_help, "  "))
        lines.append("")
    return (("Provider Build/Start Arguments", "\n".join(lines)),)
