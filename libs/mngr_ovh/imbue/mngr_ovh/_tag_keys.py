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

# ``mngr-host-id`` value prefix of a recycle-candidate orphan: a VPS adopted from a
# timed-out order carries ``host-orphan-from-order-<order>-<uuid>`` there until the
# recycle path claims it for a real host, so it is not a host id and discovery must
# not treat it as one.
ORPHAN_HOST_ID_PLACEHOLDER_PREFIX: Final[str] = "host-orphan-from-order-"
