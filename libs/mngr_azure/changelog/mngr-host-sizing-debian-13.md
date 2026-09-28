The default marketplace image is now `Debian:debian-13:13-gen2` (trixie) instead of `Debian:debian-12:12-gen2`, matching the other mngr_vps clouds. Override with the `image_*` config fields as before.

- A VM create that Azure refuses with `SkuNotAvailable` now raises `AzureVmSizeUnavailableError`, which names the VM size and region and explains that this is a per-subscription restriction (new pay-as-you-go subscriptions are commonly barred from the B-series and older D-series sizes) with the ways out: another size such as `Standard_D2s_v6`, another region, or an access request. Previously the user saw Azure's raw "Capacity Restrictions" text, which reads like a passing shortage.

- The Azure release tests default to `Standard_D2s_v6` (override with `MNGR_AZURE_VM_SIZE`); the Dockerfile-build test uses the same knob instead of a hardcoded `Standard_D2s_v3`, which the CI subscription may not launch.
