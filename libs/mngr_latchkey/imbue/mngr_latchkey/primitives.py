from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt


class MachinePolicyFingerprint(NonEmptyStr):
    """What a machine says identifies the policy it holds; opaque here, and only ever handed back to that machine."""

    ...


class PermissionsFormatVersion(NonNegativeInt):
    """The format one host's policy is written in; 0 when it was never stamped."""

    ...
