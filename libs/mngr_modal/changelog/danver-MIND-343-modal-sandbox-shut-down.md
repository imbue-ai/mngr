Fixed: creating or restarting a Modal host no longer fails outright when Modal ends the sandbox while mngr is still bringing it up.

mngr already probed a freshly created sandbox with a shell no-op and, if it did not run, discarded the sandbox and created another. But it recognized only one of the two ways Modal answers for a sandbox it has lost: while the container is dead but still addressable, the probe is accepted and comes back SIGKILLed, which mngr handled. Once Modal's control plane had dropped the task, the probe call was refused instead -- `Modal Sandbox with container ID ... not found. This means this Sandbox has already shut down`, or `Modal Sandbox is shutting down` -- and that answer escaped the probe rather than being reported by it, so it skipped the replacement and surfaced as `Failed to create Modal host: ...`.

Both of Modal's answers now mean the same thing to the probe, so both reach the replacement that was already there. Nothing waits on a sandbox that is gone: each attempt creates a fresh one.

Added: `test_modal_refuses_a_command_for_a_sandbox_it_has_ended`, an acceptance test that holds Modal to the wording mngr reads as "this sandbox is gone". Only the wording tells that refusal from a resource that genuinely never existed, so if Modal rewords it this test fails and names the pattern to update.

Fixed: a host bring-up command that Modal refuses to run -- because it has ended the sandbox since mngr created it -- is now reported as the sandbox being gone rather than as that command failing. Bring-up already attributed a SIGKILLed command to the sandbox rather than the command; a refused one never got far enough to be attributed at all.
