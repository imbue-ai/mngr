#!/usr/bin/env bash
#
# Check the open/closed state on GitHub of each issue referenced in TRIAGE.md.
#
# Usage:
#     ./scripts/check_triage_issues.sh
#     ./scripts/check_triage_issues.sh 1234 1235        # explicit numbers
#
# With no args, parses TRIAGE.md (in the repo root) for #NNNN references and
# checks each one. Output is "<issue>\t<STATE>" lines, suitable for piping.
#
# Dependencies: gh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TRIAGE_MD="${REPO_ROOT}/TRIAGE.md"

if [[ $# -gt 0 ]]; then
    issues=("$@")
else
    if [[ ! -f "${TRIAGE_MD}" ]]; then
        echo "TRIAGE.md not found at ${TRIAGE_MD}" >&2
        exit 1
    fi
    # Extract bare issue numbers from #NNNN refs (4+ digits to avoid #473.3 sub-numbering).
    mapfile -t issues < <(grep -oE '#[0-9]{3,}' "${TRIAGE_MD}" | tr -d '#' | sort -un)
fi

for n in "${issues[@]}"; do
    state=$(gh issue view "$n" --json state --jq .state 2>/dev/null || echo "UNKNOWN")
    printf "%s\t%s\n" "$n" "$state"
done
