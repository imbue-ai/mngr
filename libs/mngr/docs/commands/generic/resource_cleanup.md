## Resource Cleanup [future]

- `--keep-nothing`: Do not preserve any resources when destroying agents (default behavior)
- `--keep-containers`: Preserve Docker/Modal containers/sandboxes/local folders when destroying agents
- `--keep-snapshots`: Preserve snapshots when destroying agents
- `--keep-images`: Preserve Docker/Modal images when destroying agents
- `--keep-volumes`: Preserve Docker/Modal volumes when destroying agents
- `--keep-logs`: Preserve log files when destroying agents
- `--keep-cache`: Preserve build cache when destroying agents
- `--keep-clones`: Preserve git clones when destroying agents
