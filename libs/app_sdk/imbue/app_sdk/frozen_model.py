from pydantic import BaseModel
from pydantic import ConfigDict


class FrozenModel(BaseModel):
    """Base of the library's immutable models, which reject fields they do not declare.

    The library defines its own base, rather than taking ``imbue-common``'s, so that an app's lock
    never has to follow ``imbue-common``'s releases through this package.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
