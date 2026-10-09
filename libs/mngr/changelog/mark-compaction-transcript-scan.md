Added a bounded, cached transcript reader for compaction eligibility checks.

- `scan_jsonl_file_backward` and `scan_jsonl_tail_backward` (`imbue.mngr.agents.jsonl_backward_scan`) feed a JSONL file's complete lines to a `JsonlRecordVisitor` newest-first. They read a 64 KiB window from the end through `read_file_tail_from_offset` and double it only until the visitor has what it needs, so memory stays proportional to the bytes read rather than the file size. A final line without a trailing newline is treated as still being written and is read once it is complete. Lines are split only at `\n`, so a record containing U+0085, U+2028 or U+2029 is read; the harnesses' old whole-file parsers also split at those characters and skipped such a record. `read_jsonl_file_size` returns a file's size without reading its content.

- `CompactionTranscriptCache` and its shared instance `COMPACTION_TRANSCRIPT_CACHE` (`imbue.mngr.agents.compaction_transcript`) remember each transcript's newest assistant-turn timestamp and context token count, keyed by host and path, with the byte offset scanned. An unchanged transcript is answered without re-reading any of its complete lines, a grown one is read only from where the last check stopped, and a truncated or replaced one is rescanned. `evict_agents_other_than` drops the entries of agents no longer being checked, and the cache is capped at 2048 entries.

- Harnesses implementing `HasCompactionMixin` subclass `CompactionTranscriptScanner` with their own record matching. A scanner whose match for an older record depends on newer records overrides `carries_state_into_older_records`, which makes an incremental update rescan the file.

- New `JsonlFileShrankDuringScanError`, raised when a file shrinks between two reads of one scan.
