- Pin a `digest:` field on the default Debian 12 lima image in the generated
  lima.yaml. With the pinned hash, lima trusts the cached qcow2 as long as
  its sha256 matches and skips the HEAD-revalidation round trip to
  cloud.debian.org on every VM start. Without it, every boot needs that
  upstream host reachable -- a TLS-handshake hiccup leaves lima logging
  "Using cache" but then fataling with `open <instance>/basedisk: no such
  file or directory` instead of completing from the cache. Only the
  aarch64 default image carries a hash so far; amd64 keeps lima's
  pre-digest behavior until a runner exercises it.
