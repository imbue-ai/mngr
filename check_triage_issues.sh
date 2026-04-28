#!/usr/bin/env bash
# Check state of each triaged issue. Outputs: ISSUE STATE
set -euo pipefail

ISSUES=(
  473 475 476 491 522 567 751
  1002 1034 1035 1036 1037 1038 1039 1040 1041 1043 1045 1046 1048 1049
  1051 1060 1073 1087 1088 1089 1090 1091 1092 1095 1096 1098 1099
  1101 1102 1104 1106 1108 1154 1158 1237 1256 1280 1332 1360 1408 1411
)

for n in "${ISSUES[@]}"; do
  state=$(gh issue view "$n" --json state --jq .state 2>/dev/null || echo "UNKNOWN")
  printf "%s\t%s\n" "$n" "$state"
done
