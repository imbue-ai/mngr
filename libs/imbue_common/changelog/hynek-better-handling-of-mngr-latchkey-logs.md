Added `RotatingLineWriter` and `rotate_file_if_too_large` to `imbue_common.logging` for capturing unstructured subprocess output into size-rotated, optionally timestamp-prefixed log files (used by the latchkey gateway/forward logs).

Fixed `make_jsonl_file_sink` so that rotated copies of a non-`events.jsonl` log file (e.g. a custom `--log-file` path) are pruned on rotation instead of accumulating forever; `cleanup_old_rotated_files` now takes a `base_name` argument.
