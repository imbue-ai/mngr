#!/usr/bin/env bash
# spare-capacity.sh -- exit 0 if there's spare capacity (as defined above), else
# non-zero (including when there's no usage data to judge from).
set -euo pipefail

mngr usage --format json | jq -e '
  .sources[]
  | select(.source == "claude")
  | (.five_hour.used_percentage // 100)  as $u5
  | (.seven_day.elapsed_percentage // 0) as $elw
  | (.seven_day.used_percentage // 100)  as $uw
  | $u5 < 80 and $uw < $elw * (1 - 0.30 * (100 - $elw) / 100)
' >/dev/null
