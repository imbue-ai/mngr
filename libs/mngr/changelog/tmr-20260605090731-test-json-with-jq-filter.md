Fixed the tutorial example that combines `mngr list --format json` with `jq`. The
filter now reads `.agents[] | select(.state == "RUNNING") | .name` to match the
actual JSON shape (`mngr list --format json` emits an object with an `agents`
array), instead of the previous `.[]` which errored with "Cannot index array with
string". Tightened the corresponding e2e test accordingly.
