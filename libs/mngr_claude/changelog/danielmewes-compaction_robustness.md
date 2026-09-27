Record agent compaction even if sending the `/compact` command raises an exception. This prevents `mngr autocompact` from repeatedly attempting compaction when compaction requests fail.
