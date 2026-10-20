Deflake the doc-link URL resolution tests (MIND-188), which periodically turned
per-PR CI red. Three tests fetched live github.com URLs to verify the
version-pinning ref policy in `doc_links`; they were on the per-PR merge gate
(`@pytest.mark.acceptance`), so a slow or briefly-unreachable github.com --
orthogonal to the ref policy -- failed the gate. The failures came two ways:
curl's `--max-time 30` outran the 10s pytest-timeout, producing an uncatchable
`Failed: Timeout (>10.0s)` SIGALRM, and transient transport errors raised
`CalledProcessError`.

A live third-party dependency does not belong on the merge gate: it will fail
when the third party is down, and a networked check cannot be made to pass
honestly (skipping on failure proves nothing). The deterministic half of the ref
policy -- the URL *shape* a code change can actually break, e.g. a wrong tag
format -- is already pinned on every PR with no network by the existing
`doc_links_test.py` unit test, so nothing is lost by taking the live check off the
gate.

The three live-resolution tests (repo public, tag pushed, doc present) move to the
release lane (`@pytest.mark.release`), where they run in the release workflow and
the daily TMR schedule. They fail closed: if github.com is unreachable the test
fails rather than skips, and a human decides whether to re-run or merge past. A
per-test timeout override clears curl's 30s budget so a slow github.com yields a
clean curl error instead of the uncatchable pytest-timeout SIGALRM.
