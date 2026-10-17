"""IAM tag-key string constants shared between ``client.py`` and ``iam_tags.py``.

Lives in its own module so both ``client.py`` (which uses the
recycling-lock key in ``destroy_instance``'s mid-recycle short-circuit)
and ``iam_tags.py`` (which uses all three for tag attach/list/filter
helpers) can import the same canonical strings without an import cycle
-- ``iam_tags.py`` depends on ``OvhVpsClient`` from ``client.py``, so
``client.py`` can't import from ``iam_tags.py`` directly.
"""

from typing import Final

MNGR_PROVIDER_TAG_KEY: Final[str] = "mngr-provider"
MNGR_HOST_ID_TAG_KEY: Final[str] = "mngr-host-id"
MNGR_RECYCLING_LOCK_TAG_KEY: Final[str] = "mngr-recycling-by"
