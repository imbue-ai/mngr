### Migrate to the new `imbue.mngr.api.rsync` interface

`mngr_mapreduce`'s reducer-launch path now calls `rsync_to_remote` (from
`imbue.mngr.api.rsync`) instead of the removed `push_files` wrapper.
``extra_args`` replaces the dropped ``is_dry_run``/``is_delete`` parameters,
and the source path is passed with an explicit trailing ``/`` since mngr no
longer mangles slashes on the caller's behalf. Behavior is unchanged.
