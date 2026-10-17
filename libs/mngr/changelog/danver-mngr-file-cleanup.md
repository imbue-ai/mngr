Reading a directory over SSH now fails the same way it does locally. A local read of a directory
raises `IsADirectoryError`, but the SFTP path surfaced the server's opaque failure instead, so any
caller distinguishing "that path is a directory" from a genuine read error got the right answer on
a local host and the wrong one on a remote host. `OuterHost.read_file` now classifies that case by
asking the server what the path is, and raises `IsADirectoryError` for a directory on both.

The classification costs nothing on the success path: the extra question is asked only after a read
has already failed, and a failure to answer it leaves the original error to stand.
