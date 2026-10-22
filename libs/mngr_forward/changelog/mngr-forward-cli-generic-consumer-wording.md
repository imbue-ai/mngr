Three comments in `mngr forward`'s CLI describe the process that relays its exit report and
stops it as a generic consumer rather than naming the higher-level app. The comment on the
TLS/HTTP-2 setup failure log no longer claims the consumer's listen wait would time out, which
stopped being true once that wait returned on the forward's exit. Comments only; no behaviour
changed.
