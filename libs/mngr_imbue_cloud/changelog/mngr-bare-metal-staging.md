Added `mngr imbue_cloud admin server pricing`: an operator-only, read-only command that prints a per-slice pricing table for OVH bare-metal plans, to help decide what to buy before ordering.

- Each row is a server x RAM config. It reports the effective slice sizing (slots, vCPUs/slice, disk/slice) computed with the same `slices/bare_metal` math used to carve real slices, and the true monthly cost per slice (month-to-month price plus the one-time setup fee amortized over a year, divided by slot count). Rows are sorted cheapest-per-slice first.

- Includes delivery-time and stock columns (parsed from OVH datacenter availability), a `--region` filter (vin = US-EAST-VA, hil = US-WEST-OR; default both), `--memory-per-slice-gb` (default 8) and `--cpu-overcommit` (default 2.0) knobs, and storage-upgrade options listed at the end of each row as a marginal $/GB. Configs whose per-slice disk can't host a slice are excluded.

- The command only reads the OVH catalog and availability APIs; it never places an order. It needs `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` in the environment (from the activated env's ovh secret).
