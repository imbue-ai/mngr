`mngr event --follow` no longer re-downloads a remote source's whole `events.jsonl` on every poll:

- Remote sources are polled with ranged reads: one SFTP stat plus a read of only the bytes past the saved offset. On a workspace whose `services/events.jsonl` had reached 1.16 GB, each poll used to take one to two minutes. The new `HostFileReadInterface.read_file_tail_from_offset` returns those bytes with the file size seen by the same call, which is how rotation is detected. Offsets now count raw bytes, so an undecodable byte can no longer push later reads into the middle of a line. Whole-file reads use the same primitive with a prefetched read.

- Every read of the discovery events log is byte-based. An undecodable byte used to make the discovery tail re-read the same bytes forever, and made `mngr stop`/`mngr connect` identifier resolution, agent/host name tab completion, and the `mngr forward --observe-via-file` attach fail. It now costs one skipped line with a warning.

- The agent-lifecycle follower that tails another process's `mngr observe` events file counts raw bytes too, so an undecodable byte no longer shifts its later reads into the middle of a line.

- A line in an events file that is valid JSON but not an event (no `timestamp`, or not a JSON object) is now logged and skipped. It used to stop `mngr event --follow` at that line for good, re-reading it on every poll and re-resolving the host, and it made a history read containing it exit at startup.
