import json
from typing import Final

from imbue.imbue_common.pure import pure

# Detent reads this variable as the ``customMetadata`` of a permission check
# whenever latchkey supplies none of its own. Latchkey supplies ``{"account":
# ...}`` for every request it injects credentials into, so the value set here is
# only seen by checks on requests that carry their own credentials.
DETENT_CUSTOM_METADATA_ENV_VAR: Final[str] = "DETENT_CUSTOM_METADATA"

# Key of ``customMetadata`` holding the id of the computer the gateway runs on.
# A permissions file is shared between every computer a user connects to a
# machine from, so a rule meant for one of them has to gate on this.
DEVICE_ID_METADATA_KEY: Final[str] = "deviceId"


@pure
def build_device_metadata_env(device_id: str) -> dict[str, str]:
    """Build the environment that makes a latchkey gateway report ``device_id`` to its permission checks."""
    return {DETENT_CUSTOM_METADATA_ENV_VAR: json.dumps({DEVICE_ID_METADATA_KEY: device_id})}
