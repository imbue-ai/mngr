Tests that drive a real external resource now inherit a budget sized for that class instead
of the repo-wide 10s `--timeout`. The shared conftest hooks carry a per-marker table of test
budgets, and any collected test carrying one of those markers gets it applied at collection
time. Today the table holds one entry: `tmux` tests get 60 seconds.

A test that declares its own `@pytest.mark.timeout` is left alone -- that decorator is how a
test says it differs from its class, and it still wins. The class budget is also declined
when the run's global `--timeout` is already at least as generous, so `pytest --timeout=300`
while debugging is not silently narrowed back down.

The 10s default is sized for the style guide's "each individual unit test runs quickly
(< 5 seconds)". Tests that create a real tmux session through the real CLI are integration
tests doing ~28 sequential subprocess spawns; measured across the 136 such tests that had no
timeout of their own, they cost a median of 1.78s and at most 3.60s on idle hardware. Against
10s that absorbs only a 2.8x slowdown, which CI's parallel contention regularly exceeded --
so the timeout was firing as a false positive about the harness rather than reporting anything
about the product.
