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
# Export all member channels starting from 2024-01-01
slack-exporter

# Export specific channels only
slack-exporter --channels general random engineering

# Export with per-channel start dates
slack-exporter --channels "general:2024-01-01" "random:2024-06-01"

# Set a global start date
slack-exporter --since 2023-01-01

# Custom output directory
slack-exporter --output-dir my_slack_data

# Export only the 10 most recently active channels (based on historical data)
slack-exporter --recently-active-channels 10

# Include channels you're not a member of (default: only member channels)
slack-exporter --all

# Control how many recent relevant threads to check for reactions (default: 50)
slack-exporter --max-recent-threads-for-reactions 20

# Force re-fetch of cached data (channels, users, identity)
slack-exporter --refresh

# Configure cache TTL via environment variable (default: 600 seconds / 10 minutes)
SLACK_EXPORTER_CACHE_TTL_SECONDS=300 slack-exporter

# Verbose logging
slack-exporter -v
```

## How it works

1. Reads existing data from the output directory to understand what has already been exported
2. Fetches the authenticated user's identity (via `auth.test`) and saves if new or changed -- cached for `SLACK_EXPORTER_CACHE_TTL_SECONDS` (default 10 minutes)
3. Fetches the channel list from Slack (via `conversations.list`) and saves only new or changed channels -- cached for `SLACK_EXPORTER_CACHE_TTL_SECONDS`
4. Fetches unread markers (`last_read` position) per channel via `conversations.info` and saves when changed
5. Fetches the user list from Slack (via `users.list`) and saves only new users -- cached for `SLACK_EXPORTER_CACHE_TTL_SECONDS`
6. For each configured channel, fetches new messages (via `conversations.history`) starting from the most recent message already exported (or the configured oldest date on first run). If the configured oldest date is earlier than the oldest date already searched from, also backfills older messages down to that date
7. For messages with threads (reply_count > 0), uses the `latest_reply` field to skip threads with no new replies, then fetches replies (via `conversations.replies`) only for threads that have changed
8. Extracts reactions from fetched messages and saves when new or changed
9. Detects threads relevant to the authenticated user (threads where the user replied or was mentioned) and records them as `relevant_threads` events
10. After all channels are exported, checks reactions on the most recent relevant threads (sorted by latest reply, controlled by `--max-recent-threads-for-reactions`, default 50)

Use `--refresh` to bypass the cache and force re-fetching of all data.

## Output structure

Data is stored in a directory with created/updated streams per type:

```
slack_export/
  channel/created/events.jsonl               -- new channels (first seen)
  channel/updated/events.jsonl               -- all channel state changes (includes creates)
  message/created/events.jsonl               -- new messages
  message/updated/events.jsonl               -- all message state changes (includes creates)
  reaction/created/events.jsonl              -- new per-message reaction state (first seen)
  reaction/updated/events.jsonl              -- all reaction state changes (includes creates)
  relevant_thread/created/events.jsonl       -- threads user participated in (first seen)
  relevant_thread/updated/events.jsonl       -- all relevant thread changes (includes creates)
  relevant_thread_reply/created/events.jsonl -- replies in relevant threads (first seen)
  relevant_thread_reply/updated/events.jsonl -- all relevant thread reply changes (includes creates)
  reply/created/events.jsonl                 -- new thread replies
  reply/updated/events.jsonl                 -- all reply state changes (includes creates)
  self_identity/created/events.jsonl         -- authenticated user identity (first seen)
  self_identity/updated/events.jsonl         -- all identity state changes (includes creates)
  unread_marker/created/events.jsonl         -- new unread markers (first seen)
  unread_marker/updated/events.jsonl         -- all unread marker changes (includes creates)
  user/created/events.jsonl                  -- new users (first seen)
  user/updated/events.jsonl                  -- all user state changes (includes creates)
```

The `created` stream contains only first-seen items. The `updated` stream contains all state changes (including creates, since a create is logically an update from nothing). Subscribe to `created` for lower cardinality, or `updated` for the full change history.

Each line is a self-describing JSON event using the standard EventEnvelope format (with `timestamp`, `type`, `event_id`, `source` fields), plus domain-specific fields and the raw Slack API response.

Running the exporter multiple times is safe -- it only appends new or changed data to the appropriate stream.
