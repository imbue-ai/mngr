`mngr forward` loads a remote workspace's pages faster: its SSH connections to remote hosts now ask for zlib compression, so the uncompressed scripts and stylesheets a workspace serves cross the link at about a third of their size. Connections to a loopback host (a local VM or container behind a forwarded port) stay uncompressed, and a server that does not offer compression is connected to without it.

Measured against a real Imbue Cloud workspace over a lossy link, replaying the chat frame's request chain on a fresh connection took about 2.8 s instead of 3.9-4.0 s.
