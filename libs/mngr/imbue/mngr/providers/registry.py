from collections.abc import Callable

import pluggy

import imbue.mngr.providers.docker.backend as docker_backend_module
import imbue.mngr.providers.local.backend as local_backend_module
import imbue.mngr.providers.ssh.backend as ssh_backend_module
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import get_provider_config_class
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.config.provider_config_registry import reset_provider_config_registry
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.errors import UnknownBackendError
from imbue.mngr.interfaces.provider_backend import LazyProviderBackend
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance

# Cache for registered (or already-materialized) backend classes.
_backend_registry: dict[ProviderBackendName, type[ProviderBackendInterface]] = {}
# Backends registered lazily: name -> loader that imports and returns the class. The
# loader is called (and its result cached into _backend_registry) only when get_backend
# first needs the class, so a plain `mngr config`/`list`/`--help` never imports it.
_backend_loader_registry: dict[ProviderBackendName, Callable[[], type[ProviderBackendInterface]]] = {}
# Build/start CLI help text for lazily-registered backends, so create's --help can
# render provider args without importing the (heavy) backend class.
_backend_help_registry: dict[ProviderBackendName, tuple[str, str]] = {}
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
    _backend_loader_registry.clear()
    _backend_help_registry.clear()
    reset_provider_config_registry()
    _registry_state["backends_loaded"] = False


# Provider backends that require credentials at registration time (e.g.
# Modal SDK auth, Vultr API key, AWS access keys) or at first
# ``discover_hosts`` (e.g. an imbue_cloud session). Tests use
# ``load_local_backend_only`` to skip these. Lima is intentionally
# excluded: its backend defers limactl checks to first use, so
# registering it is safe even without limactl installed.
_REMOTE_BACKEND_NAMES: frozenset[str] = frozenset({"aws", "azure", "gcp", "imbue_cloud", "modal", "ovh", "vultr"})


def _load_backends(pm: pluggy.PluginManager, *, include_docker: bool, include_remote: bool) -> None:
    """Load provider backends from the specified modules.

    The pm parameter is the pluggy plugin manager. If include_docker is True,
    the Docker backend is included (requires a Docker daemon). If include_remote
    is True, plugin-provided backends that require external services
    (Modal, Lima, Vultr, AWS, ...) are included.
    """
    if _registry_state["backends_loaded"]:
        return

    pm.register(local_backend_module, name="local")
    pm.register(ssh_backend_module, name="ssh")
    if include_docker:
        pm.register(docker_backend_module, name="docker")
    # Note: remote backends (modal, lima, vultr, aws, ...) are loaded via plugin entry points

    registrations = pm.hook.register_provider_backend()

    for registration in registrations:
        if registration is None:
            continue
        if isinstance(registration, LazyProviderBackend):
            # Lazy: register only the lightweight metadata now; defer importing the
            # backend class (and its cloud SDK) until get_backend first needs it.
            backend_name = registration.name
            if not include_remote and str(backend_name) in _REMOTE_BACKEND_NAMES:
                continue
            _backend_loader_registry[backend_name] = registration.load
            _backend_help_registry[backend_name] = (registration.build_args_help, registration.start_args_help)
            register_provider_config(str(backend_name), registration.config_class)
        else:
            backend_class, config_class = registration
            backend_name = backend_class.get_name()
            if not include_remote and str(backend_name) in _REMOTE_BACKEND_NAMES:
                continue
            _backend_registry[backend_name] = backend_class
            register_provider_config(str(backend_name), config_class)

    _registry_state["backends_loaded"] = True


def load_local_backend_only(pm: pluggy.PluginManager) -> None:
    """Load only the local and SSH provider backends.

    This is used by tests to avoid depending on external services.
    Unlike load_backends_from_plugins, this only registers the local and SSH backends
    (not Docker or any remote backends which require external daemons/credentials).
    """
    _load_backends(pm, include_docker=False, include_remote=False)


def load_backends_from_plugins(pm: pluggy.PluginManager) -> None:
    """Load all provider backends from plugins."""
    _load_backends(pm, include_docker=True, include_remote=True)


def _all_backend_names() -> set[ProviderBackendName]:
    """Every registered backend name -- eagerly registered plus lazily registered."""
    return set(_backend_registry) | set(_backend_loader_registry)


def get_backend(name: str | ProviderBackendName) -> type[ProviderBackendInterface]:
    """Get a provider backend class by name.

    Backends are loaded from plugins via the plugin manager. A backend registered
    lazily (``LazyProviderBackend``) is imported on first access here and cached.
    """
    key = ProviderBackendName(name) if isinstance(name, str) else name
    if key in _backend_registry:
        return _backend_registry[key]
    loader = _backend_loader_registry.get(key)
    if loader is not None:
        backend_class = loader()
        _backend_registry[key] = backend_class
        return backend_class
    available = sorted(str(k) for k in _all_backend_names())
    raise UnknownBackendError(str(key), available)


def get_config_class(name: str | ProviderBackendName) -> type[ProviderInstanceConfig]:
    """Get the config class for a provider backend.

    Delegates to the config-layer registry. This function exists for callers
    above the config layer (api, cli) that historically imported from here.
    """
    return get_provider_config_class(str(name))


def list_backends() -> list[str]:
    """List all registered backend names (eager and lazy)."""
    return sorted(str(k) for k in _all_backend_names())


def resolve_backend_and_config(
    provider_name: ProviderInstanceName,
    mngr_ctx: MngrContext,
) -> tuple[type[ProviderBackendInterface], ProviderInstanceConfig]:
    """Resolve the backend class and config for a provider-instance name.

    Two cases:
    1. The name matches a configured provider instance in ``mngr_ctx.config.providers``:
       return the declared backend and the user's config.
    2. Otherwise, treat the name as a bare backend name and instantiate the
       backend's default config (supports e.g. ``--provider local`` /
       ``--provider docker`` without a ``[providers.<name>]`` block).

    Used by both ``get_provider_instance`` and the ``mngr create``
    bootstrap path so the "configured-instance vs. bare-backend-name"
    fallback logic lives in exactly one place.
    """
    if provider_name in mngr_ctx.config.providers:
        provider_config = mngr_ctx.config.providers[provider_name]
        backend_name = provider_config.backend
    else:
        backend_name = ProviderBackendName(str(provider_name))
        config_class = get_provider_config_class(str(backend_name))
        provider_config = config_class(backend=backend_name)
    return get_backend(backend_name), provider_config


def build_provider_instance(
    instance_name: ProviderInstanceName,
    backend_name: ProviderBackendName,
    config: ProviderInstanceConfig,
    mngr_ctx: MngrContext,
) -> BaseProviderInstance:
    """Build a provider instance using the registered backend.

    ``mngr create`` callers should invoke ``backend.bootstrap_for_host_creation``
    before calling this (see ``api/create.py``); ``build_provider_instance``
    itself is always treated as read-only-or-existing-host and must not
    bootstrap backend-side state. Backends with one-time resources (the Modal
    per-user environment, the Docker singleton state container) raise
    ``ProviderEmptyError`` here when those resources are missing, so read paths
    skip the provider rather than creating them on first use.
    """
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


def _get_backend_args_help(backend_name: ProviderBackendName) -> tuple[str, str]:
    """Return ``(build_args_help, start_args_help)`` for a backend.

    A lazily-registered backend's help text was captured at registration time, so
    it is served from the metadata registry without importing the backend class.
    """
    help_texts = _backend_help_registry.get(backend_name)
    if help_texts is not None:
        return help_texts
    backend_class = _backend_registry[backend_name]
    return backend_class.get_build_args_help(), backend_class.get_start_args_help()


def get_all_provider_args_help_sections() -> tuple[tuple[str, str], ...]:
    """Generate help sections for build/start args from all registered backends.

    Returns a tuple of (title, content) pairs suitable for use as additional
    sections in CommandHelpMetadata. Lazily-registered backends contribute their
    help text from the registration metadata, so this does not import them (and
    therefore does not pull their cloud SDKs into a plain ``mngr --help``).
    """
    lines: list[str] = []
    for backend_name in sorted(_all_backend_names()):
        build_help, start_help = _get_backend_args_help(backend_name)
        build_help = build_help.strip()
        start_help = start_help.strip()
        lines.append(f"Provider: {backend_name}")
        lines.append(_indent_text(build_help, "  "))
        if start_help != build_help:
            lines.append(_indent_text(start_help, "  "))
        lines.append("")
    return (("Provider Build/Start Arguments", "\n".join(lines)),)
