# ty 0.0.39 type fixes

- `fixture_uses_resources`'s `TypeVar` is now bound to a small `_NamedCallable` protocol (a callable that also carries `__name__`), so reading `func.__name__` in the misconfiguration error type-checks under `ty` 0.0.39. The decorator is only ever applied to fixture functions, which satisfy the protocol.
- The pytest hookwrapper generators (`_pytest_fixture_setup`, `_pytest_runtest_makereport`, and their plugin-class wrappers) now annotate their generator send type as `pluggy.Result[...]`, so `outcome.get_result()` / `outcome.excinfo` resolve instead of being treated as attributes of `None`.

No user-facing behavior change.
