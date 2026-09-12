Integration branch for the imbue_cloud slice-fleet generation 2 program (see the other projects' `mngr-gen2-combined.md` entries). The latchkey-side change it carries:

Gen-2 small follow-ups: the VM-resident owner-exec daemon pin (`OWNER_EXEC_VERSION` in `owner_exec_vm.py`) moves from v0.2.1 to v0.2.2, which never authorizes an `authorized_keys` line carrying options (`command=`, `restrict`, `from=`, `cert-authority`); v0.2.1 stripped the options and granted such keys unrestricted exec. The default-workspace-template's in-container pin moves in lockstep.
