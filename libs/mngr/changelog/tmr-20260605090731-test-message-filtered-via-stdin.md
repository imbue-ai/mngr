Fixed the `test_message_filtered_via_stdin` e2e tutorial test. It now carries a
`@pytest.mark.timeout(90)` (plus a longer per-command timeout) so the two-stage
`mngr list | mngr msg` pipeline has enough headroom on slow filesystems, and the
superfluous `rsync`/`tmux`/`modal` resource marks were dropped because the
empty-filter pipeline is a no-op that never invokes those resources.

Also hardened the test to assert the filtered id list really is empty and that
no message is reported as delivered, and added a happy-path companion test
(`test_message_filtered_via_stdin_delivers_to_matching_agents`) that pipes a
non-empty id list (local-provider agents) into `mngr msg -` and verifies the
message is actually delivered to each matched agent.
