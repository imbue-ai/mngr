# mng-wait

Wait plugin for mng -- wait for agents/hosts to reach target states.

## Usage

```bash
# Wait for an agent to finish
mng wait my-agent DONE

# Wait for any terminal state
mng wait agent-abc123

# Wait with timeout
mng wait my-agent DONE --timeout 5m

# Wait for host to stop
mng wait host-xyz789 STOPPED

# Read target from stdin
echo agent-abc123 | mng wait

# Multiple states
mng wait my-agent --state WAITING --state DONE
```

## Exit Codes

- `0` - Target reached one of the requested states
- `1` - Error
- `2` - Timeout expired
