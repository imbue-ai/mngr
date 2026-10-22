Fix SSH access grants failing with "its authorized_keys kept changing while the
key was added" on every grant after the first. The write guard compared a
digest of the body read through `mngr exec --format json` (trailing newline
stripped) with a digest of the file on disk (trailing newline kept), so it
never matched once the file ended in a newline, which every grant's own write
leaves. Both sides now hash the body with trailing newlines stripped.
