Capped `azure-mgmt-network` below 31 and `azure-mgmt-resource` below 26 during the repo-wide dependency refresh. Both move to Azure's "hybrid models" API, which `client.py` is not written against: flat constructor keywords (`SecurityRule(protocol=..., access=...)`) become a nested `properties=` model, reads move behind `.properties`, and `ResourceManagementClient` moves from `azure.mgmt.resource` to `azure.mgmt.resource.resources` (the old import raises `ImportError`).

azure-mgmt-network 31 already declares only the nested overloads -- it keeps the flat form working at runtime -- and 32 drops the flat form outright, so 30.x is the last release this code actually targets.

Porting it is mechanical, since the compute half of `client.py` already uses the nested style, but it rewrites live provisioning calls that only a real Azure subscription exercises. That port is left to its own change (MIND-300).
