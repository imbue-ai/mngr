`mngr list` now reports the real size of every `mngr_vps` cloud host instead of the hardcoded 1 CPU / 1 GB placeholder:

- `create` asks the cloud for the created instance's shape (a new `VpsClientInterface.get_instance_shape`: vCPUs, RAM, and the root disk) right after the instance exists and records it on the host record (`VpsHostConfig.shape`), so `get_host_resources` answers from the record alone, for stopped hosts too. A describe that fails is logged and the host lists with an unknown size rather than failing the create.

- A host record written before shapes were recorded answers from a small per-provider table of the plans in use at the time (the provider default plus the sizes the Imbue Studio create form offers); a plan outside it lists the size as unknown (`VpsHostSizeUnknownError`, which the listing turns into an absent `host.resource`) rather than a made-up number.

- The three cloud providers with a configurable root disk share one knob, `root_disk_size_gb` on `OfflineCapableVpsProviderConfig` (default 30 GB). The former per-cloud names (`root_volume_size_gb` on AWS, `boot_disk_size_gb` on GCP, `os_disk_size_gb` on Azure) still work as deprecated aliases that fold into the unified field; a settings block that sets both to different values is rejected with `VpsConfigError`.

- The shared cloud release profile asserts that the size `mngr list` reports for the created host matches what the cloud itself says the shape is.
