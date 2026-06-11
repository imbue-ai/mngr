## AWS discovery-skip warning: cleaner message

- The read-path "skipping discovery" warning emitted when an AWS provider can't resolve credentials or an AMI now interpolates the bare cause (`ProviderEmptyError.reason`) instead of `str(e)` of the wrapped error, so the rendered line no longer double-prints the provider name or the "has no state yet" framing. Purely cosmetic; the actionable guidance (env vars / profile / instance role / `default_ami_id`) is unchanged. (Landed here on the `mngr/gcp` branch alongside the identical GCP fix to keep the two providers symmetric.)
