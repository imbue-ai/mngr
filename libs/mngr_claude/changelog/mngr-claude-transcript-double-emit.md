Fixed claude agents recording parts of a transcript twice.

The raw-transcript streamer runs both as a poll daemon and as a single pass at each
turn end, and the two overlapped: each read the same per-session line offset and
appended the same range of session lines. The common transcript built from that
input then carried the affected turns twice -- duplicate `event_id`s, and an
inference whose text appeared twice inside one step. When the two appends
interleaved mid-line the raw transcript was left holding JSON that the converter
drops as malformed, losing those records altogether.

The streamer now emits and reconciles its offsets under the same lock the converter
takes, re-reading the offset from disk inside it rather than trusting a cached copy.
The converter keeps only the first record for a given source uuid, so an input that
replays a line still converts once.
