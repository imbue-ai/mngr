import os

from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import AgentLabelOptions


def resolve_env_vars(
    pass_env_var_names: tuple[str, ...],
    explicit_env_var_strings: tuple[str, ...],
) -> tuple[EnvVar, ...]:
    """Resolve and merge environment variables.

    Resolves pass_env_var_names from os.environ and merges with explicit_env_var_strings.
    Explicit env vars take precedence over pass-through values.
    """
    # Start with pass-through env vars from current shell
    merged: dict[str, str] = {}
    for var_name in pass_env_var_names:
        if var_name in os.environ:
            merged[var_name] = os.environ[var_name]

    # Explicit env vars override pass-through values
    for env_str in explicit_env_var_strings:
        env_var = EnvVar.from_string(env_str)
        merged[env_var.key] = env_var.value

    return tuple(EnvVar(key=k, value=v) for k, v in merged.items())


def resolve_labels(label_strings: tuple[str, ...]) -> AgentLabelOptions:
    """Parse KEY=VALUE label strings into AgentLabelOptions."""
    labels_dict: dict[str, str] = {}
    for label_string in label_strings:
        if "=" not in label_string:
            raise UserInputError(f"Label must be in KEY=VALUE format, got: {label_string}")
        key, value = label_string.split("=", 1)
        labels_dict[key.strip()] = value.strip()
    return AgentLabelOptions(labels=labels_dict)
