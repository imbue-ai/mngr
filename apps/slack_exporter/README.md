# slack-exporter

Export Slack channel messages, channel metadata, and user info to JSONL files using [latchkey](https://github.com/nichochar/latchkey) for authentication.

## Prerequisites

- [latchkey](https://github.com/nichochar/latchkey) installed and configured with Slack credentials:
  ```bash
  npm install -g latchkey
  latchkey auth browser slack
  ```

## Usage

```bash
# Export #general (default) starting from 2024-01-01
slack-exporter

# Export specific channels
slack-exporter --channels general random engineering

# Export with per-channel start dates
slack-exporter --channels "general:2024-01-01" "random:2024-06-01"

# Set a global start date
slack-exporter --since 2023-01-01

# Custom output directory
slack-exporter --output-dir my_slack_data

# Verbose logging
slack-exporter -v
```

## How it works

1. Reads existing data from the output directory to understand what has already been exported
2. Fetches the channel list from Slack (via `conversations.list`) and saves only new or changed channels
3. Fetches the user list from Slack (via `users.list`) and saves only new users
4. For each configured channel, fetches new messages (via `conversations.history`) starting from either the configured oldest date or the most recent message already in the file
5. For messages with threads (reply_count > 0), fetches replies (via `conversations.replies`) and saves only new ones

## Output structure

Data is stored in a directory with created/updated streams per type:

```
slack_export/
  channels/created/events.jsonl   -- new channels (first seen)
  channels/updated/events.jsonl   -- all channel state changes (includes creates)
  messages/created/events.jsonl   -- new messages
  messages/updated/events.jsonl   -- all message state changes (includes creates)
  replies/created/events.jsonl    -- new thread replies
  replies/updated/events.jsonl    -- all reply state changes (includes creates)
  users/created/events.jsonl      -- new users (first seen)
  users/updated/events.jsonl      -- all user state changes (includes creates)
```

The `created` stream contains only first-seen items. The `updated` stream contains all state changes (including creates, since a create is logically an update from nothing). Subscribe to `created` for lower cardinality, or `updated` for the full change history.

Each line is a self-describing JSON event using the standard EventEnvelope format (with `timestamp`, `type`, `event_id`, `source` fields), plus domain-specific fields and the raw Slack API response.

Running the exporter multiple times is safe -- it only appends new or changed data to the appropriate stream.
