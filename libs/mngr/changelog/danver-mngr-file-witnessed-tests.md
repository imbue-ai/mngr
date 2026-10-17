Corrected two things a volume-backed offline host did while witnessing the `mngr file` behavior
corpus (see the `mngr_file` changelog entry).

- `OfflineHostWithVolume.list_directory` asks the volume whether a path exists before listing it.
  A volume served by a storage service, such as a Modal volume, raises an error of its own kind for
  a missing path where a filesystem volume returns nothing, so listing a missing directory on a
  stopped host escaped as an uncaught error instead of an empty listing the caller can refuse
  cleanly.

- A path outside the host directory is now refused in the user's terms -- naming the host directory
  and saying that only files under it can be reached while the host is not running -- rather than
  naming the internal `OfflineHostWithVolume` class and its volume.
