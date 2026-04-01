#!/usr/bin/env python3
"""Filter and format Claude Code session transcript JSONL files.

Reads a JSONL transcript file and outputs a filtered, human-readable view
with line numbers. The line numbers correspond to the original JSONL file,
so you can use `sed -n '<N>p' <file>` to get the raw JSON for any line.

Default output shows user and assistant messages only, with text content
extracted. Use flags to include other message types.

Usage:
    filter_transcript.py [options] <file.jsonl>
    cat <file.jsonl> | filter_transcript.py [options]

Examples:
    # Default: user + assistant messages with line numbers
    filter_transcript.py session.jsonl

    # Include tool results
    filter_transcript.py --tool-results session.jsonl

    # Include everything
    filter_transcript.py --all session.jsonl

    # Raw JSON output instead of formatted text
    filter_transcript.py --json session.jsonl

    # Size estimate (bytes of filtered output)
    filter_transcript.py --size session.jsonl
"""

import argparse
import json
import sys


def extract_text(content):
    """Extract readable text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = item.get("text", "")
                if text.strip():
                    parts.append(text)
            elif item.get("type") == "tool_result":
                # Show tool result content if it's text
                result_content = item.get("content", "")
                if isinstance(result_content, str) and result_content.strip():
                    parts.append(f"[tool_result] {result_content[:200]}")
            elif item.get("type") == "tool_use":
                name = item.get("name", "?")
                inp = item.get("input", {})
                if isinstance(inp, dict):
                    # Show command for Bash, file_path for Read/Write, pattern for Grep
                    detail = inp.get("command", inp.get("file_path", inp.get("pattern", "")))
                    if detail:
                        parts.append(f"[{name}] {detail[:200]}")
                    else:
                        parts.append(f"[{name}]")
                else:
                    parts.append(f"[{name}]")
        return "\n".join(parts)
    return ""


def get_message_type(obj):
    """Determine the message type from a JSONL object."""
    # Top-level type field
    msg_type = obj.get("type", "")

    # Check nested message type
    message = obj.get("message", {})
    if isinstance(message, dict):
        role = message.get("role", "")
        if role in ("user", "assistant"):
            return role

    # Check for content with tool_result (user turn with tool results)
    if msg_type == "user":
        return "user"

    return msg_type


def get_content(obj):
    """Extract the content field from a JSONL object."""
    # Try message.content first (standard format)
    message = obj.get("message", {})
    if isinstance(message, dict):
        content = message.get("content")
        if content is not None:
            return content

    # Fall back to top-level content
    return obj.get("content", "")


def should_include(msg_type, args):
    """Decide whether to include a message based on its type and flags."""
    if args.all:
        return True

    if msg_type in ("user", "assistant"):
        return True
    if msg_type == "tool_use" and args.tool_use:
        return True
    if msg_type == "tool_result" and args.tool_results:
        return True
    if msg_type == "thinking" and args.thinking:
        return True
    if msg_type == "system" and args.system:
        return True
    if msg_type == "progress" and args.progress:
        return True

    return False


def format_line(line_num, msg_type, text, use_json, show_line_numbers=True):
    """Format a single output line."""
    if use_json:
        record = {"type": msg_type, "text": text}
        if show_line_numbers:
            record["line"] = line_num
        return json.dumps(record)

    # Indent continuation lines
    if show_line_numbers:
        prefix = f"L{line_num}\t[{msg_type}]\t"
        indent = "\t\t\t"
    else:
        prefix = f"[{msg_type}]\t"
        indent = "\t\t"
    lines = text.split("\n")
    if len(lines) == 1:
        return f"{prefix}{lines[0]}"
    else:
        formatted = [f"{prefix}{lines[0]}"]
        for continuation in lines[1:]:
            formatted.append(f"{indent}{continuation}")
        return "\n".join(formatted)


def _compute_filtered_size(path, args):
    """Compute the filtered output size in bytes for a single file."""
    total = 0
    with open(path) as f:
        for line_num, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            msg_type = get_message_type(obj)
            if not should_include(msg_type, args):
                continue
            content = get_content(obj)
            text = extract_text(content)
            if not text.strip():
                continue
            output = format_line(line_num, msg_type, text, args.json, show_line_numbers=not args.no_line_numbers)
            total += len(output.encode("utf-8")) + 1
    return total


def main():
    parser = argparse.ArgumentParser(description="Filter and format Claude Code session transcript JSONL files.")
    parser.add_argument("file", nargs="?", help="JSONL file to filter (reads stdin if omitted)")
    parser.add_argument("--tool-use", action="store_true", help="Include tool_use messages")
    parser.add_argument("--tool-results", action="store_true", help="Include tool_result messages")
    parser.add_argument("--thinking", action="store_true", help="Include thinking messages")
    parser.add_argument("--system", action="store_true", help="Include system messages")
    parser.add_argument("--progress", action="store_true", help="Include progress messages")
    parser.add_argument("--all", action="store_true", help="Include all message types")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of formatted text")
    parser.add_argument("--no-line-numbers", action="store_true", help="Omit line numbers")
    parser.add_argument("--size", action="store_true", help="Only output total byte count of filtered output")
    parser.add_argument(
        "--total-size",
        action="store_true",
        help="Read file paths (one per line, optionally tab-prefixed) from stdin and print total filtered size",
    )
    args = parser.parse_args()

    if args.total_size:
        total = 0
        for line in sys.stdin:
            # Support both plain paths and "source\tpath" format from export_transcript_paths.sh
            parts = line.strip().split("\t")
            path = parts[-1]
            if not path:
                continue
            try:
                total += _compute_filtered_size(path, args)
            except (FileNotFoundError, PermissionError):
                pass
        print(total)
        return

    if args.file:
        try:
            infile = open(args.file)
        except FileNotFoundError:
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
    else:
        infile = sys.stdin

    total_size = 0
    try:
        for line_num, raw_line in enumerate(infile, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            msg_type = get_message_type(obj)
            if not should_include(msg_type, args):
                continue

            content = get_content(obj)
            text = extract_text(content)
            if not text.strip():
                continue

            output = format_line(line_num, msg_type, text, args.json, show_line_numbers=not args.no_line_numbers)

            if args.size:
                total_size += len(output.encode("utf-8")) + 1  # +1 for newline
            else:
                print(output)

    finally:
        if args.file and infile is not sys.stdin:
            infile.close()

    if args.size:
        print(total_size)


if __name__ == "__main__":
    main()
