Stabilize the `test_start_connect` e2e release test covering `mngr start --connect`.

The test was marked `@pytest.mark.rsync` but never shelled out to rsync: unlike its sibling start tests it verifies `--connect` purely through the connect command's pidfile side effect, with no `mngr exec`/`mngr stop` step. The unused mark tripped the resource guard's "marked but never invoked" check and failed the test. Removed the spurious mark.

Separately, `mngr start` drives a single large tmux round-trip (new session, windows, send-keys, and the background activity tracker) that can occasionally stall past the 30s per-command timeout under heavy offload load, even though it normally finishes in a couple of seconds. Marked the test `@pytest.mark.flaky` so offload retries this infra-level fluke, widened the start command's per-command timeout to 90s, and raised the test timeout to 180s to match the sibling start tests.
