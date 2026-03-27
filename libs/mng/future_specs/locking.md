## Locking

- Commands require exclusive access via locking [future] if and only if they are modifying the *state* of a host or agent
- Lock files count as activity for idle shutdown detection [future]
- Commands that affect multiple agents/hosts must specify the behavior when all matches cannot all be locked (continue-and-warn, fail immediately, or retry-until-locked [future])

In particular, this means that the following commands require locking:

- create
- start
- stop
- destroy
- cleanup
- clone
- migrate
- provision
- limit
- rename

While operations like push and pull are clearly modifying their targets, locking is not required because they are not modifying the *state* directory of the host or agent (just the working directory).
For such commands, see the [multi-target](../generic/multi_target.md) options for behavior when some agents cannot be processed.

## Deployment Locking and Idle Detection Coordination [future]

It is ideal to avoid concurrent access from multiple instances of mng to a host/agent while deployment commands are running. This prevents race conditions and state corruption during critical operations.

### Mechanism

The deployment locking mechanism [future] works as follows:

1. **Start of deployment**: When a deployment command begins (create, provision, etc.), write the current datetime to a special file in a special folder (e.g., `$MNG_HOST_DIR/deploy_lock/timestamp`)

2. **Idle detection**: When the idle detection script determines the host should shut down:
   - Attempt to delete the special folder using `rmdir` (which only succeeds if the folder is empty)
   - If the folder cannot be deleted (because it contains the deployment lock file), the idle script should not shut down the host
   - This prevents idle shutdown from interrupting an active deployment

3. **End of deployment**: When the deployment completes successfully, remove the lock file
   - This allows idle detection to function normally again

4. **Concurrent deployment attempts**: If another mng instance tries to deploy while a deployment is in progress:
   - The attempt to write to the lock folder will fail (folder doesn't exist or is being used)
   - This failure indicates that the host is either being deployed to or has gone away
   - The operation should fail with an appropriate error message

### Crash Recovery [future]

If a deployment crashes mid-operation, the lock file will be left in place. To handle this:

- The idle shutdown script should have a longer timeout (e.g., 2-4 hours) as a backup
- After this timeout, the host shuts down anyway, even if the lock file exists
- This ensures that a crashed deployment doesn't leave hosts running indefinitely
- The timeout should be long enough to cover legitimate long-running deployments
- Users should be warned if a host shuts down while a lock file exists (indicates a crashed deployment)

### Benefits

This mechanism provides:
- Prevention of concurrent deployments to the same host
- Coordination between deployment operations and idle detection
- Automatic recovery from crashed deployments (via timeout)
- Clear indication when a host is unavailable due to ongoing deployment

