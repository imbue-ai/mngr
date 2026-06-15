Added `mngr imbue_cloud admin server pricing`: an operator-only, read-only command that prints a per-slice pricing table for OVH bare-metal plans, to help decide what to buy before ordering.

- Each row is a server x RAM config x region. It reports the effective slice sizing (slots, vCPUs/slice, disk/slice) computed with the same `slices/bare_metal` math used to carve real slices, and the true monthly cost per slice (month-to-month price plus the one-time setup fee amortized over a year, divided by slot count). Rows are sorted cheapest-per-slice first and printed to stdout.

- Rows are split per region (vin = US-EAST-VA, hil = US-WEST-OR) because delivery time and stock differ by datacenter; each row shows the delivery-time and stock columns for its region (parsed from OVH availability). Knobs: `--region` (repeatable; default both US datacenters), `--memory-per-slice-gb` (default 8), `--cpu-overcommit` (default 2.0). Storage-upgrade options are listed at the end of each row as a marginal $/GB. A `CPU(c/t)` column shows the server's physical cores/threads so the (overcommitted) CPU/slice value is legible.

- A config is only excluded when NO available storage can host a slice at the chosen size; the base columns use the cheapest storage that IS sliceable, so RAM-dense servers that need a larger disk to fit a slice still appear (on that larger disk) instead of being dropped.

- The command only reads the OVH catalog and availability APIs; it never places an order. It needs `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` in the environment (from the activated env's ovh secret).
