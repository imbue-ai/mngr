#!/usr/bin/env bash
# Symlink each top-level skill into a role's skills directory.
#
# Usage: link_skills.sh <role>
#
# For each skill in the top-level skills/ directory, creates a relative
# symlink at <role>/skills/<skill-name> pointing to ../../skills/<skill-name>.
# If the skill already exists in the role's skills directory (as a real
# directory, file, or existing symlink), a warning is emitted and the
# skill is skipped.
#
# This script is idempotent and can be re-run at any time, for example
# after new top-level skills are added.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <role>" >&2
    exit 1
fi

ROLE="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$SCRIPT_DIR/skills"
ROLE_SKILLS_DIR="$SCRIPT_DIR/$ROLE/skills"

if [ ! -d "$SKILLS_DIR" ]; then
    echo "No top-level skills directory found at $SKILLS_DIR, nothing to link." >&2
    exit 0
fi

mkdir -p "$ROLE_SKILLS_DIR"

for skill_dir in "$SKILLS_DIR"/*/; do
    # Guard against unexpanded glob (no skill directories)
    [ -d "$skill_dir" ] || continue

    skill_name="$(basename "$skill_dir")"
    target="$ROLE_SKILLS_DIR/$skill_name"

    if [ -e "$target" ] || [ -L "$target" ]; then
        echo "WARNING: Skill '$skill_name' already exists in $ROLE/skills/, skipping symlink" >&2
        continue
    fi

    ln -s "../../skills/$skill_name" "$target"
    echo "Linked $ROLE/skills/$skill_name -> ../../skills/$skill_name"
done
