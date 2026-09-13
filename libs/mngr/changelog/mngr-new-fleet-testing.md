Variable machine sizing (specs/slice-fleet, phase 1): the generated command reference (`docs/commands/secondary/imbue_cloud.md`) now documents the new `mngr imbue_cloud machines show` and `mngr imbue_cloud machines resize` subcommands (units + grow-only disk, record-then-restart). The commands themselves live in the `mngr_imbue_cloud` plugin; see that project's entry in this same PR.

This branch also carries the earlier slice-fleet gen-2 phase 1-4 work; see the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR.
