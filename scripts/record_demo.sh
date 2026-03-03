#!/usr/bin/env bash
#
# record_demo.sh - Record a terminal demo using asciinema and convert to GIF
#
# Usage:
#   ./scripts/record_demo.sh <demo_script> <output_name> [options]
#
# Arguments:
#   demo_script   Path to a bash script containing the commands to demo
#   output_name   Base name for output files (without extension)
#
# Options:
#   --cols N          Terminal width (default: 100)
#   --rows N          Terminal height (default: 30)
#   --theme THEME     agg theme for GIF (default: monokai)
#   --font-size N     Font size for GIF (default: 16)
#   --speed N         Playback speed multiplier for GIF (default: 1)
#   --idle-limit N    Max idle time in seconds (default: 2)
#   --last-frame N    Duration of last frame in seconds (default: 3)
#   --out-dir DIR     Output directory (default: .demos/)
#   --no-gif          Skip GIF conversion
#   --no-loop         Disable GIF looping
#
# Outputs:
#   <out_dir>/<output_name>.cast       asciinema recording
#   <out_dir>/<output_name>.gif        GIF (unless --no-gif)
#   <out_dir>/<output_name>.txt        Plain text dump of recording (for verification)
#
# Examples:
#   ./scripts/record_demo.sh demos/my_demo.sh my-feature-demo
#   ./scripts/record_demo.sh demos/quick.sh quick --cols 80 --rows 20 --speed 2

set -euo pipefail

if [ "${1:-}" = "--help" ]; then
    head -31 "$0" | tail -30
    exit 0
fi

if [ $# -lt 2 ]; then
    echo "Usage: $0 <demo_script> <output_name> [options]"
    echo "Run '$0 --help' for more information."
    exit 1
fi

DEMO_SCRIPT="$1"
OUTPUT_NAME="$2"
shift 2

# Defaults
COLS=100
ROWS=30
THEME="monokai"
FONT_SIZE=16
SPEED=1
IDLE_LIMIT=2
LAST_FRAME=3
OUT_DIR=".demos"
MAKE_GIF=true
LOOP_FLAG=""

# Parse options
while [ $# -gt 0 ]; do
    case "$1" in
        --cols)       COLS="$2"; shift 2 ;;
        --rows)       ROWS="$2"; shift 2 ;;
        --theme)      THEME="$2"; shift 2 ;;
        --font-size)  FONT_SIZE="$2"; shift 2 ;;
        --speed)      SPEED="$2"; shift 2 ;;
        --idle-limit) IDLE_LIMIT="$2"; shift 2 ;;
        --last-frame) LAST_FRAME="$2"; shift 2 ;;
        --out-dir)    OUT_DIR="$2"; shift 2 ;;
        --no-gif)     MAKE_GIF=false; shift ;;
        --no-loop)    LOOP_FLAG="--no-loop"; shift ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if [ ! -f "$DEMO_SCRIPT" ]; then
    echo "Error: Demo script not found: $DEMO_SCRIPT" >&2
    exit 1
fi

# Ensure demo script is executable
chmod +x "$DEMO_SCRIPT"

# Create output directory
mkdir -p "$OUT_DIR"

CAST_FILE="$OUT_DIR/$OUTPUT_NAME.cast"
GIF_FILE="$OUT_DIR/$OUTPUT_NAME.gif"
TXT_FILE="$OUT_DIR/$OUTPUT_NAME.txt"

echo "Recording demo: $DEMO_SCRIPT"
echo "  Terminal: ${COLS}x${ROWS}"
echo "  Output: $CAST_FILE"

# Record with asciinema
# Set COLUMNS/LINES to control terminal size in the recording
COLUMNS="$COLS" LINES="$ROWS" asciinema rec \
    --command "$DEMO_SCRIPT" \
    -q \
    --overwrite \
    -i "$IDLE_LIMIT" \
    "$CAST_FILE"

echo "Recording complete: $CAST_FILE"

# Extract plain text from the .cast file for verification
# The .cast format is JSONL: first line is header, subsequent lines are [time, type, data]
# We extract all "o" (output) events and concatenate the data
python3 -c "
import json
import sys

with open(sys.argv[1]) as f:
    # Skip header
    next(f)
    for line in f:
        event = json.loads(line)
        if event[1] == 'o':
            sys.stdout.write(event[2])
" "$CAST_FILE" > "$TXT_FILE"

echo "Text dump: $TXT_FILE"

# Convert to GIF
if [ "$MAKE_GIF" = true ]; then
    echo "Converting to GIF..."
    agg \
        --theme "$THEME" \
        --font-size "$FONT_SIZE" \
        --speed "$SPEED" \
        --idle-time-limit "$IDLE_LIMIT" \
        --last-frame-duration "$LAST_FRAME" \
        $LOOP_FLAG \
        "$CAST_FILE" \
        "$GIF_FILE"

    GIF_SIZE=$(du -h "$GIF_FILE" | cut -f1)
    echo "GIF created: $GIF_FILE ($GIF_SIZE)"
fi

echo ""
echo "--- Recording text content ---"
cat "$TXT_FILE"
echo ""
echo "--- End of recording ---"
