# Transfer

Understanding this behavior corpus calls for the behaviors skill; consult it when reading this file.

This area covers moving one file's bytes between the local invocation and the addressed machine, in either direction.
`reading.feature` covers the machine-to-local direction and `writing.feature` the local-to-machine one.

Both directions treat a file as an indivisible unit, as the corpus-wide `whole-files-only` Rule requires: a read yields a file's entire content and a write supplies a file's entire new content.
Neither direction is a synchronization: nothing compares the two ends, nothing is skipped as already current, and nothing is merged.

## Where the local end of a transfer is

Each direction has a default local end and a named alternative, and the choice of which changes what the command reports.

Reading defaults to the local output stream, so a read can be piped onward without touching the local disk; naming a local file instead saves the bytes there.
Writing defaults to the local input stream, so a write can be fed from a pipe; naming a local file instead takes the content from it.

When a read goes to the local output stream, its content is what the command emits, and the report of the content and the content itself are the same bytes.
When a read is saved to a named file, the content is already on disk and the report describes the saved file rather than repeating its bytes.
This is why the two cases report different things, and it is the only respect in which the destination changes the report.

## Bytes are preserved exactly

A transfer is byte-exact in both directions: content of any kind survives a round trip unchanged, and nothing is re-encoded, line-ending-translated, or truncated on the way.
Where a report has to carry content in a machine-readable record, it carries an encoding of those exact bytes rather than a rendering of them as text.
