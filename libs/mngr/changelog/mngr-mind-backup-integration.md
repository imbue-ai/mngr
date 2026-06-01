Install `restic` in the mngr Docker image (`libs/mngr/imbue/mngr/resources/Dockerfile`).

This is the offload test image; the minds app now requires `restic` on the
machine running it (it initializes each workspace's backup repository
itself), and its tests exercise a real local restic repository, so restic
must be present in the test environment.
