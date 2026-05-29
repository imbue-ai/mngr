Fixed the tutorial's jq filtering example for `mngr list --format json`. The
JSON output is an object (`{"agents": [...], "errors": [...]}`), so the example
now iterates `.agents[]` instead of `.[]`, which previously failed with
`jq: error: Cannot index array with string "labels"`.
