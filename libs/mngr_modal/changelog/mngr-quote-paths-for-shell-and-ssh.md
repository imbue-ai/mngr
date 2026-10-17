No behaviour change: `build_ssh_connect_command` moved from `imbue.mngr.primitives` to `imbue.mngr.utils.ssh`, and the import here follows it.

The function renders an ssh command rather than defining shared vocabulary, so it sits with the ssh option quoting it uses instead of alongside the enums and id types in `primitives`.
