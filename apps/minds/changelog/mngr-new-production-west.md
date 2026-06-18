`minds pool create --backend slice` now requires `--server-id` (the bare-metal
box to bake the slices onto, from `mngr imbue_cloud admin server list`), forwarded
to the underlying `mngr imbue_cloud admin pool create`. Slice baking now targets
an explicitly-chosen, ready server rather than auto-selecting one.
