import pluggy

from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import get_provider_config_class
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.config.provider_config_registry import reset_provider_config_registry
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance

# Cache for registered backends
_backend_registry: dict[ProviderBackendName, type[ProviderBackendInterface]] = {}
# Use a mutable container to track state without 'global' keyword
_registry_state: dict[str, bool] = {"backends_loaded": False}
# Plugin manager reference, set by main.py after creating the manager. Used by
# the lazy ``_ensure_backends_loaded`` path so consumers like
# ``api/providers.py`` don't have to thread ``pm`` through every call.
_pm_ref: dict[str, pluggy.PluginManager | None] = {"pm": None}


def set_plugin_manager(pm: pluggy.PluginManager) -> None:
    """Register the plugin manager with this registry so backends can be loaded lazily.

    Called from ``main.py`` after the plugin manager singleton is constructed.
    Tests that explicitly call ``load_local_backend_only(pm)`` do not need this.
    """
    _pm_ref["pm"] = pm


def load_all_registries(pm: pluggy.PluginManager) -> None:
    """Load all registries from plugins.

    Historically this was called once during application startup. After the
    lazy-load refactor it is only called when a consumer actually needs a
    backend (via ``get_backend`` / ``list_backends`` / ``get_config_class``)
    or when help-text generation needs the full set of provider args.

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


def _ensure_backends_loaded() -> None:
    """Lazy-load all backends if they haven't been loaded yet.

    Used by the public lookup functions so consumers don't need to remember
    to load backends explicitly. Requires ``set_plugin_manager`` to have been
    called first; raises if the plugin manager isn't registered.
    """
    if _registry_state["backends_loaded"]:
        return
    pm = _pm_ref["pm"]
    if pm is None:
        raise RuntimeError(
            "Provider registry accessed before plugin manager was registered. "
            "Call set_plugin_manager(pm) first, or use load_local_backend_only(pm) for tests."
        )
    load_backends_from_plugins(pm)


def _load_backends(pm: pluggy.PluginManager, *, include_modal: bool, include_docker: bool) -> None:
    """Load provider backends from the specified modules.

    The pm parameter is the pluggy plugin manager. If include_modal is True,
    the Modal backend is included (requires Modal credentials). If include_docker
    is True, the Docker backend is included (requires a Docker daemon).
    """
    if _registry_state["backends_loaded"]:
        return

    # Backend module imports are deferred to here so that simply importing
    # ``imbue.mngr.providers.registry`` (e.g. for ``set_plugin_manager`` or
    # ``get_all_provider_args_help_sections``) does not pull pyinfra / docker
    # / paramiko (~80ms wall) at startup.
    import imbue.mngr.providers.local.backend as local_backend_module  # noqa: PLC0415
    import imbue.mngr.providers.ssh.backend as ssh_backend_module  # noqa: PLC0415

    pm.register(local_backend_module, name="local")
    pm.register(ssh_backend_module, name="ssh")
    if include_docker:
        import imbue.mngr.providers.docker.backend as docker_backend_module  # noqa: PLC0415

        pm.register(docker_backend_module, name="docker")
    # Note: modal backend is loaded via the mngr_modal plugin entry point

    registrations = pm.hook.register_provider_backend()

    for registration in registrations:
        if registration is not None:
            backend_class, config_class = registration
            backend_name = backend_class.get_name()
            if not include_modal and str(backend_name) == "modal":
                continue
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
    _ensure_backends_loaded()
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
    _ensure_backends_loaded()
    return get_provider_config_class(str(name))


def list_backends() -> list[str]:
    """List all registered backend names."""
    _ensure_backends_loaded()
    return sorted(str(k) for k in _backend_registry.keys())


def build_provider_instance(
    instance_name: ProviderInstanceName,
    backend_name: ProviderBackendName,
    config: ProviderInstanceConfig,
    mngr_ctx: MngrContext,
) -> BaseProviderInstance:
    """Build a provider instance using the registered backend."""
    backend_class = get_backend(backend_name)
    obj = backend_class.build_provider_instance(
        name=instance_name,
        config=config,
        mngr_ctx=mngr_ctx,
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
    _ensure_backends_loaded()
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
