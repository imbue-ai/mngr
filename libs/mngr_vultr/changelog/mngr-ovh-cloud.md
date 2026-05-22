`mngr_vultr` now only contributes the tag-listing; the shared parallel-SSH discovery has been lifted into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method.
