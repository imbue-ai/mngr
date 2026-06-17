"""Guard the cross-package writer <-> reader seam.

pi's usage *writer* lives in mngr_pi_coding's lifecycle extension (pi loads one
explicit extension, owned by the harness), while the reader + gate live here.
The two packages agree only by hand-synced string literals across both a
language *and* a package boundary -- the gate filename and the source name. A
rename on either side silently disables usage tracking, so pin the invariant.
"""

from __future__ import annotations

from imbue.mngr_pi_coding.plugin import _LIFECYCLE_EXTENSION_NAME
from imbue.mngr_pi_coding.plugin import _load_resource
from imbue.mngr_pi_coding_usage.plugin import _PI_USAGE_SOURCE_NAME
from imbue.mngr_pi_coding_usage.plugin import _USAGE_GATE_FILENAME


def test_lifecycle_extension_uses_the_gate_and_source_this_package_owns() -> None:
    lifecycle_ts = _load_resource(_LIFECYCLE_EXTENSION_NAME)
    # The extension only emits usage when it sees this package's gate marker...
    assert f'"{_USAGE_GATE_FILENAME}"' in lifecycle_ts
    # ...and emits under the source the reader here claims (`<name>/usage`).
    assert f'"{_PI_USAGE_SOURCE_NAME}/usage"' in lifecycle_ts
