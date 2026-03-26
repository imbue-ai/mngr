#!/usr/bin/env bash
set -euo pipefail
#
# config_utils.sh
#
# Shared config-reading utilities. Source this file, then call read_json_config.
#
# Usage:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config_utils.sh"
#   val=$(read_json_config "path/to/config.json" "key_name" "default_value")
#   val=$(read_json_config "path/to/config.json" "nested.key" "default_value")
#
# For each config file, a .local.json sibling is checked first. For example,
# if the config path is "foo/bar.json", the function first checks "foo/bar.local.json".
# Local configs are gitignored and take precedence over checked-in configs.

# Read a single key from a JSON config file with local-override support.
# Args: <config_path> <key> <default>
read_json_config() {
    local config_path="$1"
    local key="$2"
    local default="$3"
    local val

    # Derive .local.json path: foo/bar.json -> foo/bar.local.json
    local local_path="${config_path%.json}.local.json"

    # Build a jq path expression from the key. Dotted keys like "ci.is_enabled"
    # become the jq path .ci.is_enabled; simple keys like "enabled" become .enabled.
    local jq_path
    jq_path=$(echo "$key" | sed 's/\././g; s/^/./')

    # Local overrides take precedence
    if [ -f "$local_path" ]; then
        val=$(jq -r "if $jq_path == null then empty else $jq_path end" "$local_path" 2>/dev/null)
        if [ -n "$val" ]; then
            echo "$val"
            return
        fi
    fi
    if [ -f "$config_path" ]; then
        val=$(jq -r "if $jq_path == null then empty else $jq_path end" "$config_path" 2>/dev/null)
        if [ -n "$val" ]; then
            echo "$val"
            return
        fi
    fi
    echo "$default"
}
