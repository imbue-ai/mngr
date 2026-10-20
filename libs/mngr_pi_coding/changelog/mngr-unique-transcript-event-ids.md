# Globally unique pi common-transcript and usage event ids

- Common-transcript and usage event ids are now derived by hashing the message's own timestamp and the first 1024 characters of its content, instead of a per-agent line counter. Counter ids (`pi-0`, `evt-pi-usage-0`) repeated identically for every pi agent on every host, which collides under fleet-wide dedup by event id (e.g. in analytics).
