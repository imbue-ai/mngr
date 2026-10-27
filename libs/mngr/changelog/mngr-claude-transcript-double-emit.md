The agent release harness now reports the records behind its two duplicate-detection
failures instead of only a count.

The event-id uniqueness check names the repeated ids, and the exactly-once delivery
check names each duplicated delivery's event_id and timestamp -- which separates a
transcript that recorded one line twice (one id, twice) from a token that really was
sent twice (two ids). Both failures previously had to be diagnosed by reading the
transcript pipeline.
