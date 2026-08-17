Add a positive regression guard to the modal `mngr create` acceptance test: it now asserts that recursive provisioning actually staged the deployer's mngr config files to the remote host (a non-empty "Uploaded N mngr config files" upload), rather than merely that the CLI exited 0. This catches the class of silent provisioning skips behind GH-392, where deploy-file collection failed, was downgraded to a warning, and the acceptance suite passed while never provisioning anything.

(GitHub issue #392)
