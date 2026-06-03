Discovery no longer masks failures as "zero hosts". `_list_provider_vps_hostnames`
previously caught any IAM-listing error and returned an empty list, so a
transient OVH outage / expired credentials looked identical to a real empty
result -- which the discovery layer can't distinguish, and which defeats mngr's
"mark hosts UNKNOWN when a provider's discovery fails" safeguard. It now lets the
error propagate (the genuinely-unconfigured case is still the early-return), so
`mngr list --on-error continue` records the failure instead of silently dropping
live hosts.
