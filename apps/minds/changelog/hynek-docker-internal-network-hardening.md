# Remote workspaces' VPS gateway port is fenced to the docker bridge

The `mngr_latchkey` provisioning pass a remote workspace's VPS goes through now
loads an nftables policy that drops traffic to the VPS-resident gateway's port
(and the owner-exec daemon's) unless it arrives on the docker bridge or the
VPS's own loopback, and drops any other new connection the workspace opens into
its VPS over the bridge; the permissions doc says so, and the latchkey e2e
release test asserts the policy is loaded on the VPS.
