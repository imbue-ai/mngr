## Internal: disable the new `gcp` provider in Vultr release-test settings

- The Vultr release tests write a `settings.toml` that disables every other remote provider so the create-host preflight does not trip resolving their credentials. With the new `gcp` provider now registered as a remote backend, it is added to that disable-set (matching the existing modal/aws/ovh/imbue_cloud entries). No behavioral change for Vultr.
