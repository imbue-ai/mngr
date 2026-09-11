`mngr file list` and `mngr file put` accept a format template, like the other record-emitting mngr
commands, and render one line per record:

```
mngr file list my-agent --format '{name} {size}'
echo hello | mngr file put my-agent greeting.txt --format '{path} {size}'
```

For `list`, a template can address any field an entry carries -- `name`, `path`, `file_type`,
`size`, `modified`, `permissions` -- not just the columns currently displayed, so `--format` and
`--fields` do not interfere. For `put`, the fields are `path` and `size`, and `size` renders the
same way it does in a listing so that one field name means one thing across the command group.

`get` continues to reject a template: its outcome is the file's own bytes, and a template there
would replace the content asked for rather than describe it.

`mngr file get` now reports the two ordinary addressing mistakes as clean errors instead of
crashing with a Python traceback. Asking for a path that does not exist prints
`Error: No file at <path>. Use 'mngr file list' to see what is there.` Pointing `get` at a directory
prints `Error: <path> is a directory, not a file. Use 'mngr rsync' to transfer a directory, or
'mngr file list' to see what it holds.` Both exit 1, as before; previously each surfaced a raw
traceback from the underlying read. This now holds for a remote host as well as a local one -- see
the `mngr` entry for the SSH-side half of that fix.

`mngr file list` on a directory that does not exist is now an error rather than an empty listing.
It previously printed `(empty)` and exited 0, which was indistinguishable from a directory that
exists and holds nothing. It now prints `Error: No directory at <path>.` and exits 1. A genuinely
empty directory still prints `(empty)` and exits 0.

`mngr file get --output` now reports what it did instead of finishing silently. Saving to a local
file previously produced no output at all, even under `--format json`, so a scripted caller could
not confirm the transfer or learn the resolved remote path. It now prints
`Wrote <n> bytes to <local path>` in `human` format and emits a `file_read` event carrying `path`,
`output_path`, and `size` in `json`/`jsonl`. The event deliberately omits `content_base64`: the
bytes are already on disk at `output_path`, so repeating them would only inflate the event.
Reading to stdout without `--output` is unchanged and still carries `content_base64`.

Corrected three points in the project README, and documented the output-format, `get --output`, and
`list` behavior above:

- The "Target" section claimed that an identifier matching both an agent and a host raises a
  disambiguation error. No such check exists: agent-vs-host is decided from the text of the
  argument alone, with no lookup. The section now describes the actual rule.

- The README told users to write `--output-format`, which is not an option the commands accept.
  The supported spelling is `--format`.

- Added the missing note that a host whose name is a plain word is only reachable as `@NAME`; a
  bare name is read as an agent name and fails to resolve.
