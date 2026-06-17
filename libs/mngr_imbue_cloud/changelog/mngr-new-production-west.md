`mngr imbue_cloud admin server order` now lets you order plans whose mandatory
option families (e.g. bandwidth, vrack) offer more than one choice. Previously the
cart build failed with "expected exactly one X option to auto-pick" on such plans
(e.g. the `24sys*` SYS line). Choose the offer per family explicitly with the new
repeatable `--option <planCode>` flag; single-offer families are still auto-selected.
Run `order` without it once and the error lists each ambiguous family's offers and
their monthly prices so you can re-run with the right `--option` values.

`mngr imbue_cloud admin pool create --backend slice` now requires `--server-id`
(the bare-metal box to bake the slices onto, from `admin server list`). It no
longer auto-selects the box with the most free slots -- baking always targets an
explicitly-chosen, ready server.
