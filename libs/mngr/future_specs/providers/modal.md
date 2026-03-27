# Modal Provider Spec

## Agent Self-Management

Modal sandboxes are isolated environments that don't have direct access to Modal's control plane APIs. To allow agents running inside Modal sandboxes to pause or stop themselves, mngr must deploy a Modal function that agents can call.

This function acts as a bridge between the sandboxed agent and Modal's control plane, allowing agents to request their own sandbox to be paused or stopped

This approach avoids injecting Modal credentials directly into the sandbox, maintaining security isolation.

## Snapshots

Modal provides native snapshot support. Snapshots are fully incremental since the last snapshot.

To minimize the risk of work loss if a sandbox crashes, Modal sandshots should be taken fairly frequently while the agent is working. The frequency should be configurable but default to a reasonable interval (e.g., every 15-30 minutes of active work).

Note: Automatic periodic snapshots [future] are not yet implemented (currently only on-demand snapshots and snapshot-on-idle are implemented).
