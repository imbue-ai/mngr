Variable machine sizing (specs/slice-fleet, phase 1). A machine's size is now measured in units (1 unit = 1GiB of guest RAM, with vCPUs and fair-share bandwidth scaling proportionally); its data disk is a second, grow-only factor sized at carve (3.5GiB per unit).

New `mngr imbue_cloud machines show` and `mngr imbue_cloud machines resize <machine> --units N [--disk-gb M]` commands: a resize is record-then-restart -- nothing changes until the machine's next stop/start applies it (in place when its box has room, otherwise via a restore).

The gen-2 renderers move to the sizing model: a 512-ordinal ceiling, a units-based env schema (`MNGR_SLICE_UNITS`/`MNGR_SLICE_TOTAL_UNITS`/`MNGR_SLICE_DATA_DISK_GIB` replacing `MNGR_SLICE_SLOT_COUNT`), a two-budget reserve guard (memory units and disk, with distinct `NO_UNITS`/`NO_DISK` refusal markers), single ordinal-template cidata payloads substituted on the box (replacing per-ordinal case tables), a units-proportional HTB bandwidth guarantee, and two in-guest every-boot oneshots that grow the data filesystem and keep the workspace container's memory cap in step with the VM's RAM.

Wire models gain the additive `memory_units`/`target_memory_units`/`disk_gb`/`target_disk_gb` fields on lease and workspace responses, plus the machine-sizing quota/usage fields on the account models.

This branch also carries the earlier slice-fleet gen-2 phase 1-4 work; see the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR.

Dev-canary fixes landed in this same PR: the gen-2 slice client sends its PATH as a standalone `export` so compound remote commands (the disk listing's `for` loop) stay valid bash, and a freshly-carved gen-2 guest's first-boot cloud-init is waited out before any outer provisioning touches the VM (racing it collided on the dpkg lock).
