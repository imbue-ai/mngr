Add the blueprint plan for the smolvm provider effort (`blueprint/smolvm-provider/`), covering the libkrunfw btrfs kernel, smolvm data-disk/archive-import/poweroff features, the new `libs/mngr_smolvm` provider, and the FCT/minds integration.

Register the new `imbue.mngr_smolvm` package in the root pyproject coverage configuration (a `--cov` target plus coverage omits for its KVM-requiring modules, mirroring the mngr_lima entries).
