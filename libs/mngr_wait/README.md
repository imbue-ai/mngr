# mngr-wait

Wait plugin for mngr -- wait for agents/hosts to reach target states.

## Usage

```bash
# Wait for an agent to finish
mngr wait my-agent DONE

# Wait for any terminal state
mngr wait agent-abc123

# Wait with timeout
mngr wait my-agent DONE --timeout 5m

# Wait for host to stop
mngr wait host-xyz789 STOPPED

# Read target from stdin
echo agent-abc123 | mngr wait

# Multiple states
mngr wait my-agent --state WAITING --state DONE
```

## Exit Codes

- `0` - Target reached one of the requested states
- `1` - Error
- `2` - Timeout expired
