# Style guide deltas for minds

All rules from the [normal style guide](../../style_guide.md) apply to minds, with the following exceptions and additions:

1. async code is permitted sparingly (since we use FastAPI), but wherever possible, we should still prefer to use synchronous routes. In practice this means that things like web socket related code and routes may end up being async, but most of the rest should stay sync.
