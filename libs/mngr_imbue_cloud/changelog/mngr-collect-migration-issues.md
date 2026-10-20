New `mngr imbue_cloud admin server backfill-autostart`: the fleet half of the
reboot-resilience rollout. Box by box (pool key + `limactl shell`), it applies
the merged volume-gated minds-autostart installer to every `mngr-slice-*` VM,
substituting each VM's own services-agent start-script path extracted from its
existing `/usr/local/sbin/minds-outer-autostart.sh` (older slices bake
`/mngr/code/scripts/...`; newer ones `/home/user/workspace/system/...`).

Idempotent and safe on running workspaces; a VM whose data volume is not
mounted is refused by the installer itself and reported as a per-VM failure,
and a VM whose existing autostart script cannot even be read (e.g. a stopped
VM) is likewise reported as a per-VM failure instead of being rendered with a
guessed start-script path.
One unreachable box never costs the rest of the fleet its sweep (unreadable
boxes are reported separately, like the occupancy audit). `--dry-run` lists
the per-VM plan; the command exits non-zero when any VM failed or any box was
unreachable. Wrapped by `minds server backfill-autostart`, which resolves the
pool DSN and Vault pool key from the activated env.
