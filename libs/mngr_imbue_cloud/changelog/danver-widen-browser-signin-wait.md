`mngr imbue_cloud auth login` now keeps its localhost callback listener open for ~10 minutes (was ~5), so a slow browser sign-in leg no longer times out and strands an already-created account.
