- The Lima VM now installs a pinned Docker Engine version from Docker's official
  apt repo (the same version the remote VPS providers use) instead of Debian's
  unpinned `docker.io` package, so workspace hosts run an identical, reproducible
  Docker regardless of provider.
