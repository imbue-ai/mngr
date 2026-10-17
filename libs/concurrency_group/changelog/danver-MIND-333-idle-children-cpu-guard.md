Tests only; the library behaves as before.

The guard that keeps waiting on idle background children from costing CPU turned CI red a few times a day (MIND-333). It compared a CPU measurement against 2% of a ~2.4s wait, which is about five ticks of the clock it reads: CI runs in a gVisor sandbox, which charges CPU in 10ms ticks to whichever thread is running when one fires, so a wait that really costs 2-4ms is charged anywhere from 0 to 0.12s. The budget sat inside that noise, and 20 runs of the guard in such a sandbox failed 1-2 times when idle and 6-7 times under CPU contention.

- A new unit test counts how often the read loop wakes while a child sits idle, rather than what the wakeups cost. The count is an exact integer, comes out the same whether the child idles for 0.53s or 2.37s, and was 6 or 9 across 140 runs on macOS and in a Modal sandbox, idle and loaded. Reintroducing the 10ms poll the loop replaced takes it to 159.

- The end-to-end CPU guard now has an absolute ceiling of 0.25 CPU-seconds instead of a 2% share of the wait. That is about twice the worst measurement healthy code has ever produced, and about half the cheapest measurement the polling implementation produced, so the guard keeps its breadth over any route that burns idle CPU while sitting outside the sampling noise. It no longer needs `@pytest.mark.flaky`.
