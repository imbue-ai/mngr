Fixed a race condition in `mngr_latchkey`'s per-directory encryption-key
resolution where a concurrent caller could read the on-disk key file
while another process was mid-write, observing an empty string. The key
file is now published atomically by writing to a sibling temp file,
`fsync`ing it, and `os.link`-ing it into the final path -- so the final
path only ever exists with complete contents.
