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

## Output structure

Data is stored in a directory with three subdirectories:

```
slack_export/
  channels/events.jsonl   -- channel metadata (only written when changed)
  messages/events.jsonl   -- individual messages (only new ones appended)
  users/events.jsonl      -- user info (only new users appended)
```

Each line is a JSON object containing the raw Slack API response plus metadata (timestamps, IDs).

Running the exporter multiple times is safe -- it only appends new or changed data.
