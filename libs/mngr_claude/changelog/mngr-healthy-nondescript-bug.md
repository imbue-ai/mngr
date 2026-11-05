New release tests pin two Claude Code internals that mngr hardcodes, by driving a real claude. They, the transcript record contract and the adopt-session test are marked `harness_internals(harness="claude")`, so the release test workflow also runs them against the latest Claude Code release, and a change to an internal they pin fails the release.

- `test_claude_stop_hook_block_keeps_agent_running_through_its_continuation`: when a project Stop hook blocks a turn end, the agent stays RUNNING through claude's continuation and reports WAITING after it. It also checks the `Stop hook feedback:` record and the `stop_hook_summary` after it, which `wait_for_stop_hook.sh` reads.

- `test_claude_mid_turn_message_lands_once_in_the_common_transcript`: a message sent during a tool call lands exactly once in the common transcript. It also checks the `absorbed_mid_turn` queue removal and `queued_command` attachment `common_transcript_convert.py` reads.

- `test_claude_transcript_record_contract` now checks queue operations as (operation, reason) pairs, so a new removal reason fails it.
