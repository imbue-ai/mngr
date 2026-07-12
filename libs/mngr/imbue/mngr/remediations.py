"""Canonical rendering of user-facing configuration remediation hints.

Error and warning messages routinely need to tell users how to change their
configuration. Historically each call site hand-wrote its own ``mngr config
set`` suggestion (or, worse, told users to open ``settings.toml`` by hand),
which drifted over time in flag order, scope, and whether a runnable command
was offered at all.

Every such hint should be produced here instead. Routing all sites through
these helpers keeps the spelling consistent and makes that drift structurally
impossible: there is exactly one place that decides the flag order and the
recommended scope.
"""

from typing import Literal

# The scope names accepted by ``mngr config set/unset --scope`` (CLI spelling).
ConfigScopeName = Literal["user", "project", "local"]


def format_config_set(key: str, value: str, *, scope: ConfigScopeName | None = None) -> str:
    """Return a runnable ``mngr config set`` command setting ``key`` to ``value``.

    When ``scope`` is given it is rendered as ``--scope <scope>`` immediately
    after ``set`` -- the one canonical flag position. When ``None`` no
    ``--scope`` flag is emitted and ``mngr config set`` writes to its default
    (project) scope.
    """
    scope_flag = f"--scope {scope} " if scope is not None else ""
    return f"mngr config set {scope_flag}{key} {value}"


def format_config_unset(key: str, *, scope: ConfigScopeName | None = None) -> str:
    """Return a runnable ``mngr config unset`` command clearing ``key``.

    ``scope`` follows the same rules as :func:`format_config_set`.
    """
    scope_flag = f"--scope {scope} " if scope is not None else ""
    return f"mngr config unset {scope_flag}{key}"


def format_disable_provider(provider_name: str) -> str:
    """Return a runnable command that disables the named provider instance.

    Always recommends ``--scope local``. Config precedence is
    user < project < local (local wins), so a write to the local scope takes
    effect regardless of which layer currently enables the provider. A hint
    recommending a lower scope (e.g. ``user``) would be silently overridden --
    and therefore ineffective -- whenever the provider is enabled at the
    project or local layer.
    """
    return format_config_set(f"providers.{provider_name}.is_enabled", "false", scope="local")
