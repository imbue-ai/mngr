from imbue.imbue_common.primitives import NonEmptyStr


class ServiceName(NonEmptyStr):
    """Name of a service registered under ``runtime/applications.toml`` (e.g. 'web', 'terminal')."""

    ...
