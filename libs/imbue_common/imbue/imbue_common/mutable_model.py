from typing import Any
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict

from imbue.imbue_common.model_update import FieldProxy
from imbue.imbue_common.model_update import to_update_dict


class MutableModel(BaseModel):
    """Base class for mutable pydantic models that allow attribute mutation after construction."""

    model_config = ConfigDict(
        frozen=False,
        extra="forbid",
        arbitrary_types_allowed=False,
    )

    def field_ref(self) -> Self:
        """Return a proxy for type-safe field references with to_update()."""
        return FieldProxy()  # ty: ignore[invalid-return-type]

    def model_copy_update(self, *updates: tuple[str, Any]) -> Self:
        """Create an updated copy using type-safe to_update() pairs."""
        return self.model_copy(update=to_update_dict(*updates))
