Creating a workspace from the default template no longer waits on a clone from GitHub. The app now fetches its pinned default-workspace-template release once in the background at launch, into the tier's cache directory (`template-cache/`), and a create of that template at that release copies it locally instead of cloning it. The copy is the same repository the network clone produces, so the rest of the create is unchanged.

- Only `minds-vX.Y.Z` release tags (and prereleases such as `minds-vX.Y.Z-rc.1`) are cached. A create of a branch, a commit SHA, or any other repository clones from the network as before.

- A create that starts while the launch-time download is running waits for it, and the create log says so. If the download stops receiving data for a minute, or the cache is missing, damaged, or cannot be copied, the create clones from the network instead. The cache never fails a create.

- At launch the app re-fetches the cached release if its tag moved upstream or the clone is damaged, and deletes its clones of other releases.
